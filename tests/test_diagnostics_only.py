import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import main as app
from config.settings import load_settings
from sisense.auth import SisenseAuthenticator
from sisense.client import SisenseJaqlClient
from tests.test_login import DASHBOARD_URL, make_settings
from tests.test_session_context import FakePlaywright


class ForbiddenAPIRequest:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(args)
        raise AssertionError("no JAQL request may be sent in diagnostics-only mode")


class MainModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **overrides):
        base = make_settings()
        return type(base)(**{
            **base.__dict__,
            "storage_state_path": Path(self.tmp.name) / "auth" / "sisense_state.json",
            "artifacts_dir": Path(self.tmp.name) / "artifacts",
            **overrides,
        })

    def run_main(self, settings, pw):
        @contextmanager
        def fake_sync_playwright():
            yield pw

        with mock.patch.object(app, "load_settings", return_value=settings), \
             mock.patch.object(app, "sync_playwright", fake_sync_playwright), \
             mock.patch("sisense.auth.getpass.getpass", return_value="pw"):
            return app.main()

    def test_diagnostics_only_reaches_diagnostics_and_never_retrieves(self):
        pw = FakePlaywright([])
        original_new_context = pw.chromium.browser.new_context

        def new_context(**kwargs):
            context = original_new_context(**kwargs)
            context.request = ForbiddenAPIRequest()
            return context

        pw.chromium.browser.new_context = new_context
        settings = self.settings(diagnostics_only=True)

        with mock.patch.object(app, "retrieve_value") as retrieve, \
             mock.patch.object(app, "SisenseJaqlClient") as client_cls, \
             mock.patch.object(SisenseJaqlClient, "execute") as execute, \
             mock.patch.object(app, "extract_count_measure_value") as extract, \
             mock.patch.object(app, "run_session_diagnostics", wraps=app.run_session_diagnostics) as diagnostics, \
             self.assertLogs("oe_trend_pipeline", level="INFO") as logs:
            exit_code = self.run_main(settings, pw)

        self.assertEqual(exit_code, app.EXIT_OK)
        diagnostics.assert_called_once()
        session_arg, observer_arg, _ = diagnostics.call_args.args
        self.assertIsNotNone(observer_arg, "passive observer must be attached in diagnostics-only mode")
        self.assertTrue(session_arg.page.logged_in, "dashboard was reached through the normal login flow")
        retrieve.assert_not_called()
        client_cls.assert_not_called()
        execute.assert_not_called()
        extract.assert_not_called()
        contexts = pw.chromium.browser.contexts
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].request.calls, [])
        self.assertTrue(contexts[0].closed, "session is closed after diagnostics")
        self.assertIn("requestfinished", contexts[0].handlers)
        self.assertTrue(any("Diagnostics-only mode" in line for line in logs.output))

    def test_normal_mode_still_retrieves(self):
        pw = FakePlaywright([])
        settings = self.settings()
        with mock.patch.object(app, "retrieve_value", return_value=812345) as retrieve, \
             mock.patch.object(app, "run_session_diagnostics") as diagnostics, \
             mock.patch("builtins.print"):
            exit_code = self.run_main(settings, pw)
        self.assertEqual(exit_code, app.EXIT_OK)
        retrieve.assert_called_once()
        diagnostics.assert_not_called()
        self.assertIs(retrieve.call_args.args[0].context, pw.chromium.browser.contexts[0])


class DiagnosticsOnlySettingsTests(unittest.TestCase):
    def load(self, extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f"SISENSE_EMAIL=user@example.com\nSISENSE_DASHBOARD_URL={DASHBOARD_URL}\n{extra}")
            with mock.patch.dict(os.environ, {}, clear=True):
                return load_settings(env_file)

    def test_default_off(self):
        self.assertFalse(self.load().diagnostics_only)

    def test_enabled(self):
        self.assertTrue(self.load("SISENSE_DIAGNOSTICS_ONLY=true\n").diagnostics_only)

    def test_observer_attached_without_debug_flag(self):
        base = make_settings()
        settings = type(base)(**{**base.__dict__, "diagnostics_only": True, "debug_session": False})
        self.assertIsNotNone(SisenseAuthenticator(None, settings).jaql_observer)

    def test_no_observer_in_plain_normal_mode(self):
        self.assertIsNone(SisenseAuthenticator(None, make_settings()).jaql_observer)


if __name__ == "__main__":
    unittest.main()
