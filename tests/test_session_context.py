"""The JAQL POST must travel through the exact BrowserContext that authenticated."""

import json
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import main as app
from config.settings import VERIFIED_JAQL_URL, Timeouts
from sisense.auth import SisenseAuthenticator
from sisense.client import SisenseJaqlClient
from sisense.diagnostics import BrowserJaqlObserver, log_cookie_diagnostics
from sisense.models import AuthenticationError
from sisense.payloads.currently_enrolled import build_currently_enrolled_query
from sisense.retry import RetryPolicy
from tests.test_login import DASHBOARD_URL, make_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COOKIE_SECRET = "COOKIE-VALUE-MUST-NOT-APPEAR"
OK_BODY = json.dumps({"values": [{"data": 812345, "text": "812345"}, {"data": 0.97, "text": "0.97"}]})


class FakeResponse:
    def __init__(self, status, text, headers=None):
        self.status = status
        self._text = text
        self.headers = headers or {"content-type": "application/json"}

    def text(self):
        return self._text

    def dispose(self):
        pass


class FakeAPIRequest:
    def __init__(self, context, responses):
        self.context = context
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "context_closed": self.context.closed, **kwargs})
        return self.responses.pop(0)


class FakeLocator:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    @property
    def first(self):
        return self

    def or_(self, other):
        return FakeLocator(self.page, "dashboard-or-login")

    def wait_for(self, state="visible", timeout=None):
        if self.name == "dashboard" and not self.page.logged_in:
            raise PlaywrightTimeoutError("Timeout")

    def is_visible(self):
        if self.name == "dashboard":
            return self.page.logged_in
        if self.name == "#password":
            return not self.page.logged_in
        return True

    def fill(self, value, timeout=None):
        pass

    def press(self, key, timeout=None):
        self.page.logged_in = True

    def click(self, timeout=None):
        self.page.logged_in = True


class FakePage:
    def __init__(self, context):
        self.context = context
        self.logged_in = False
        self.url = DASHBOARD_URL
        self._locators = {}

    def goto(self, url, **kwargs):
        self.url = url

    def screenshot(self, path, **kwargs):
        pass

    def locator(self, selector):
        return self._locators.setdefault(selector, FakeLocator(self, selector))

    def get_by_text(self, text, exact=False):
        return self._locators.setdefault("dashboard", FakeLocator(self, "dashboard"))

    @contextmanager
    def expect_event(self, event, predicate=None, timeout=None):
        yield None
        if not self.logged_in:
            raise PlaywrightTimeoutError("Timeout")


class FakeContext:
    def __init__(self, responses):
        self.closed = False
        self.pages = []
        self.request = FakeAPIRequest(self, responses)
        self.cookies_list = [
            {"name": "sid", "value": COOKIE_SECRET, "domain": "gahixreports.getinsured.com", "path": "/",
             "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": -1},
        ]

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def set_default_timeout(self, timeout):
        pass

    def set_default_navigation_timeout(self, timeout):
        pass

    def storage_state(self, path):
        Path(path).write_text("{}")

    def cookies(self, urls=None):
        return list(self.cookies_list)

    def add_cookies(self, cookies):
        raise AssertionError("cookies must not be reconstructed manually")

    def set_extra_http_headers(self, headers):
        raise AssertionError("auth headers must not be injected")

    def on(self, event, handler):
        pass

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, responses):
        self.responses = responses
        self.contexts = []

    def new_context(self, **kwargs):
        context = FakeContext(self.responses)
        self.contexts.append(context)
        return context

    def close(self):
        pass


class FakeChromium:
    def __init__(self, responses):
        self.browser = FakeBrowser(responses)

    def launch(self, **kwargs):
        return self.browser


class ForbiddenRequestFactory:
    def new_context(self, *args, **kwargs):
        raise AssertionError("an independent APIRequestContext must not be created")


class FakePlaywright:
    def __init__(self, responses):
        self.chromium = FakeChromium(responses)
        self.request = ForbiddenRequestFactory()


class SameContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = make_settings()
        self.settings = type(base)(**{
            **base.__dict__,
            "storage_state_path": Path(self.tmp.name) / "auth" / "sisense_state.json",
            "artifacts_dir": Path(self.tmp.name) / "artifacts",
        })
        self.query = build_currently_enrolled_query()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, responses):
        pw = FakePlaywright(responses)
        session = SisenseAuthenticator(pw, self.settings, password_prompt=lambda: "pw").open_session()
        return pw, session

    def test_jaql_posts_through_authenticated_context(self):
        pw, session = self.login([FakeResponse(200, OK_BODY)])
        contexts = pw.chromium.browser.contexts
        self.assertEqual(len(contexts), 1)
        self.assertIs(session.context, contexts[0])
        self.assertIs(session.page.context, session.context)
        self.assertTrue(session.page.logged_in, "login happened in this context's page")

        value = app.retrieve_value(session, self.settings, self.query)

        self.assertEqual(value, 812345)
        calls = session.context.request.calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], VERIFIED_JAQL_URL)
        self.assertEqual(calls[0]["data"], self.query.raw_body)
        self.assertFalse(calls[0]["context_closed"], "context must be open when the JAQL POST is sent")
        self.assertEqual(len(pw.chromium.browser.contexts), 1, "no replacement context after login")

    def test_saved_state_session_uses_its_own_context(self):
        self.settings.storage_state_path.parent.mkdir(parents=True)
        self.settings.storage_state_path.write_text("{}")
        pw = FakePlaywright([FakeResponse(200, OK_BODY)])
        original_new_context = pw.chromium.browser.new_context

        def logged_in_context(**kwargs):
            context = original_new_context(**kwargs)
            original_new_page = context.new_page

            def new_page():
                page = original_new_page()
                page.logged_in = True
                return page

            context.new_page = new_page
            return context

        pw.chromium.browser.new_context = logged_in_context
        session = SisenseAuthenticator(pw, self.settings, password_prompt=lambda: "pw").open_session()
        self.assertTrue(session.reused_saved_state)
        app.retrieve_value(session, self.settings, self.query)
        self.assertEqual(len(session.context.request.calls), 1)

    def test_headers_contain_no_auth_material(self):
        _, session = self.login([FakeResponse(200, OK_BODY)])
        app.retrieve_value(session, self.settings, self.query)
        headers = session.context.request.calls[0]["headers"]
        self.assertEqual(set(headers), {"Content-Type", "Accept"})

    def test_401_fails_immediately_without_retry(self):
        sleeps = []
        _, session = self.login([FakeResponse(401, '{"error": "Unauthorized"}'), FakeResponse(200, OK_BODY)])
        client = SisenseJaqlClient(
            session.context,
            timeouts=Timeouts(),
            retry_policy=RetryPolicy(max_attempts=3),
            sleep=sleeps.append,
        )
        with self.assertRaises(AuthenticationError) as ctx:
            client.execute(self.query)
        self.assertEqual(len(session.context.request.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertIn("Unauthorized", str(ctx.exception))


class DiagnosticsTests(unittest.TestCase):
    def test_cookie_diagnostics_never_log_values(self):
        context = FakeContext([])
        with self.assertLogs("sisense.diagnostics", level="INFO") as logs:
            log_cookie_diagnostics(context)
        output = "\n".join(logs.output)
        self.assertIn("sid", output)
        self.assertIn("gahixreports.getinsured.com", output)
        self.assertNotIn(COOKIE_SECRET, output)

    def test_observer_records_header_names_only(self):
        class Req:
            method = "POST"
            url = VERIFIED_JAQL_URL

            def all_headers(self):
                return {"Content-Type": "application/json", "Cookie": COOKIE_SECRET}

        observer = BrowserJaqlObserver()
        observer._on_request(Req())
        with self.assertLogs("sisense.diagnostics", level="INFO") as logs:
            observer.log_summary()
        output = "\n".join(logs.output)
        self.assertIn("cookie", output)
        self.assertNotIn(COOKIE_SECRET, output)


class SourceGuardTests(unittest.TestCase):
    FORBIDDEN = [
        r"\.request\.new_context",
        r"request\.new_context",
        r"add_cookies",
        r"set_extra_http_headers",
        r"['\"]Cookie['\"]\s*:",
        r"['\"]Authorization['\"]",
        r"Bearer",
        r"X-XSRF",
        r"XSRF-TOKEN",
        r"X-CSRF",
    ]

    def test_application_code_has_no_isolated_context_or_auth_material(self):
        sources = [p for p in PROJECT_ROOT.rglob("*.py") if ".venv" not in p.parts and "tests" not in p.parts]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for pattern in self.FORBIDDEN:
                with self.subTest(path=path.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
