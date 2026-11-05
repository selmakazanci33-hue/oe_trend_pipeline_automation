import secrets
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import main as app
from config.settings import VERIFIED_JAQL_URL, Timeouts
from sisense.client import SisenseJaqlClient
from sisense.diagnostics import BrowserJaqlObserver
from sisense.models import RequestMetadataUnavailable
from sisense.request_metadata import (
    FORWARDED_HEADER_NAMES,
    ForwardedHeaders,
    csrf_header_matches_context_cookie,
    headers_from_observed_request,
)
from sisense.retry import RetryPolicy
from tests.test_login import make_settings
from tests.test_session_context import OK_BODY, FakeContext, FakePlaywright, FakeResponse, ObservedRequest

BROWSER_ONLY_HEADERS = {
    "sec-ch-ua": '"Edge";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "content-length": "6244",
    ":authority": "gahixreports.getinsured.com",
    ":method": "POST",
    "connection": "keep-alive",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US",
    "user-agent": "Mozilla/5.0",
    "content-type": "application/json;charset=UTF-8",
    "accept": "application/json, text/plain, */*",
}


class RuntimeSecrets:
    """Values generated per test run so nothing can match a hardcoded constant."""

    def __init__(self):
        self.xsrf = secrets.token_urlsafe(24) + "/+="
        self.session = secrets.token_hex(16)
        self.tenant = str(uuid.uuid4())
        self.sisense_origin = f"origin-{secrets.token_hex(4)}"
        self.internal = f"internal-{secrets.token_hex(4)}"

    def all(self):
        return [self.xsrf, quote(self.xsrf, safe=""), self.session, self.tenant, self.sisense_origin, self.internal]

    def observed_headers(self):
        return {
            **BROWSER_ONLY_HEADERS,
            "cookie": f"server.session={self.session}; XSRF-TOKEN={quote(self.xsrf, safe='')}",
            "x-xsrf-token": self.xsrf,
            "x-sisense-origin": self.sisense_origin,
            "x-tenant-id": self.tenant,
            "internal": self.internal,
            "origin": "https://gahixreports.getinsured.com",
            "referer": "https://gahixreports.getinsured.com/app/main/dashboards/6a98048ccef60720fe7f9b30",
        }

    def cookies(self):
        return [
            {"name": "server.session", "value": self.session, "domain": "gahixreports.getinsured.com",
             "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": 1},
            {"name": "XSRF-TOKEN", "value": quote(self.xsrf, safe=""), "domain": "gahixreports.getinsured.com",
             "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": 1},
        ]


def observer_with(*requests):
    observer = BrowserJaqlObserver()
    for request in requests:
        observer._on_request(request)
        observer._on_request_finished(request)
    return observer


class HeaderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.s = RuntimeSecrets()

    def test_values_come_from_observed_request(self):
        forwarded = headers_from_observed_request(observer_with(ObservedRequest(self.s.observed_headers())))
        headers = forwarded.as_headers()
        self.assertEqual(set(headers), set(FORWARDED_HEADER_NAMES))
        self.assertEqual(headers["x-xsrf-token"], self.s.xsrf)
        self.assertEqual(headers["x-tenant-id"], self.s.tenant)
        self.assertEqual(headers["x-sisense-origin"], self.s.sisense_origin)
        self.assertEqual(headers["internal"], self.s.internal)

    def test_browser_only_headers_not_copied(self):
        headers = headers_from_observed_request(observer_with(ObservedRequest(self.s.observed_headers()))).as_headers()
        for name in [*BROWSER_ONLY_HEADERS, "cookie"]:
            with self.subTest(name=name):
                self.assertNotIn(name, headers)

    def test_only_present_headers_forwarded(self):
        observed = self.s.observed_headers()
        del observed["internal"]
        headers = headers_from_observed_request(observer_with(ObservedRequest(observed))).as_headers()
        self.assertNotIn("internal", headers)

    def test_non_200_and_tokenless_requests_skipped(self):
        tokenless = self.s.observed_headers()
        del tokenless["x-xsrf-token"]
        observer = observer_with(
            ObservedRequest(self.s.observed_headers(), status=401),
            ObservedRequest(tokenless),
        )
        with self.assertRaises(RequestMetadataUnavailable):
            headers_from_observed_request(observer)

    def test_no_observed_request_fails_closed(self):
        with self.assertRaises(RequestMetadataUnavailable):
            headers_from_observed_request(BrowserJaqlObserver())

    def test_repr_never_exposes_values(self):
        forwarded = headers_from_observed_request(observer_with(ObservedRequest(self.s.observed_headers())))
        for text in (repr(forwarded), str(forwarded), f"{forwarded}"):
            for secret in self.s.all():
                self.assertNotIn(secret, text)

    def test_non_allowlisted_header_rejected(self):
        with self.assertRaises(ValueError):
            ForwardedHeaders({"authorization": "x"})


class CsrfPairingTests(unittest.TestCase):
    def setUp(self):
        self.s = RuntimeSecrets()
        self.forwarded = headers_from_observed_request(observer_with(ObservedRequest(self.s.observed_headers())))

    def context(self, cookies):
        context = FakeContext([])
        context.cookies_list = cookies
        return context

    def test_matches_url_encoded_cookie(self):
        self.assertTrue(csrf_header_matches_context_cookie(self.forwarded, self.context(self.s.cookies())))

    def test_mismatch_missing_or_duplicate_cookie(self):
        other = RuntimeSecrets().cookies()
        for cookies in (other, self.s.cookies()[:1], self.s.cookies() + self.s.cookies()[1:]):
            with self.subTest(cookies=[c["name"] for c in cookies]):
                self.assertFalse(csrf_header_matches_context_cookie(self.forwarded, self.context(cookies)))


class ObserverContextBindingTests(unittest.TestCase):
    def test_requests_from_earlier_context_ignored(self):
        class Ctx:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers.setdefault(event, []).append(handler)

        old, new = Ctx(), Ctx()
        observer = BrowserJaqlObserver()
        observer.attach(old)
        observer.attach(new)
        request = ObservedRequest({"x-xsrf-token": "v"})
        for handler in old.handlers["requestfinished"]:
            handler(request)
        self.assertEqual(observer.finished_count, 0)
        for handler in new.handlers["requestfinished"]:
            handler(request)
        self.assertEqual(observer.finished_count, 1)


class DashboardContext(FakeContext):
    """Fake context whose dashboard emits one observed JAQL POST when waited on."""

    def __init__(self, responses, observed, cookies):
        super().__init__(responses)
        self._observed = observed
        self.cookies_list = cookies
        self._handlers = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def wait_for_event(self, event, predicate=None, timeout=None):
        """The dashboard's requests complete once; afterwards the dashboard is quiet."""
        if getattr(self, "_fired", False) or not self._observed:
            raise PlaywrightTimeoutError("Timeout")
        self._fired = True
        for request in self._observed:
            for name in ("request", "requestfinished"):
                for handler in self._handlers.get(name, []):
                    handler(request)
        return self._observed[0]


class MainNormalModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RuntimeSecrets()

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, responses, observed=None, cookies=None, **overrides):
        observed = [ObservedRequest(self.s.observed_headers())] if observed is None else observed
        cookies = self.s.cookies() if cookies is None else cookies
        pw = FakePlaywright(responses)
        pw.chromium.browser.new_context = lambda **kw: pw.chromium.browser.contexts.append(
            DashboardContext(responses, observed, cookies)) or pw.chromium.browser.contexts[-1]
        base = make_settings()
        settings = type(base)(**{
            **base.__dict__,
            "storage_state_path": Path(self.tmp.name) / "auth" / "sisense_state.json",
            "artifacts_dir": Path(self.tmp.name) / "artifacts",
            **overrides,
        })

        @contextmanager
        def fake_sync_playwright():
            yield pw

        with mock.patch.object(app, "load_settings", return_value=settings), \
             mock.patch.object(app, "sync_playwright", fake_sync_playwright), \
             mock.patch("sisense.auth.getpass.getpass", return_value="pw"), \
             mock.patch("sisense.retry.time.sleep", side_effect=AssertionError("no retry sleep expected")), \
             mock.patch("builtins.print"), \
             self.assertLogs(level="DEBUG") as logs:
            exit_code = app.main()
        return exit_code, pw.chromium.browser.contexts, "\n".join(logs.output)

    @staticmethod
    def posts(contexts):
        return [call for context in contexts for call in context.request.calls]

    def assert_no_secrets(self, text):
        for secret in self.s.all():
            self.assertNotIn(secret, text)

    def assert_no_secrets_persisted(self):
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                self.assert_no_secrets(path.read_text(encoding="utf-8", errors="ignore"))

    def test_one_post_with_runtime_metadata(self):
        exit_code, contexts, logs = self.run_main([FakeResponse(200, OK_BODY)])
        self.assertEqual(exit_code, app.EXIT_OK)
        calls = self.posts(contexts)
        self.assertEqual(len(calls), 1)
        headers = calls[0]["headers"]
        self.assertEqual(calls[0]["url"], VERIFIED_JAQL_URL)
        self.assertEqual(headers["x-xsrf-token"], self.s.xsrf)
        self.assertEqual(headers["x-tenant-id"], self.s.tenant)
        self.assertEqual(headers["x-sisense-origin"], self.s.sisense_origin)
        self.assertEqual(headers["internal"], self.s.internal)
        self.assertEqual(
            set(headers),
            {"Content-Type", "Accept", *FORWARDED_HEADER_NAMES},
        )
        for name in [*BROWSER_ONLY_HEADERS, "cookie"]:
            self.assertNotIn(name, headers)
        self.assertIn("x-xsrf-token", logs)
        self.assert_no_secrets(logs)
        self.assert_no_secrets_persisted()

    def test_401_and_403_stop_after_one_post(self):
        for status in (401, 403):
            with self.subTest(status=status):
                exit_code, contexts, logs = self.run_main(
                    [FakeResponse(status, '{"error": "denied"}'), FakeResponse(200, OK_BODY)]
                )
                self.assertEqual(exit_code, app.EXIT_FAILURE)
                self.assertEqual(len(self.posts(contexts)), 1)
                self.assert_no_secrets(logs)

    def test_csrf_cookie_mismatch_sends_nothing(self):
        exit_code, contexts, logs = self.run_main(
            [FakeResponse(200, OK_BODY)], cookies=RuntimeSecrets().cookies()
        )
        self.assertEqual(exit_code, app.EXIT_FAILURE)
        self.assertEqual(self.posts(contexts), [])
        self.assertIn("RequestMetadataUnavailable", logs)
        self.assert_no_secrets(logs)

    def test_no_successful_dashboard_request_sends_nothing(self):
        exit_code, contexts, _ = self.run_main(
            [FakeResponse(200, OK_BODY)], observed=[ObservedRequest(self.s.observed_headers(), status=500)]
        )
        self.assertEqual(exit_code, app.EXIT_FAILURE)
        self.assertEqual(self.posts(contexts), [])

    def test_diagnostics_only_sends_nothing(self):
        exit_code, contexts, logs = self.run_main([FakeResponse(200, OK_BODY)], diagnostics_only=True)
        self.assertEqual(exit_code, app.EXIT_OK)
        self.assertEqual(self.posts(contexts), [])
        self.assert_no_secrets(logs)


class ClientMetadataTests(unittest.TestCase):
    def test_client_rejects_plain_dict_metadata(self):
        with self.assertRaises(TypeError):
            SisenseJaqlClient(
                FakeContext([]), timeouts=Timeouts(), retry_policy=RetryPolicy(max_attempts=1),
                request_metadata={"x-xsrf-token": "x"},
            )


if __name__ == "__main__":
    unittest.main()
