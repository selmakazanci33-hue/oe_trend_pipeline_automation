import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from playwright.sync_api import Error as PlaywrightError

from config.settings import ConfigurationError, load_settings
from sisense.auth import SisenseAuthenticator
from sisense.browser import CONFIGURED_BROWSER_NOT_AVAILABLE, launch_browser
from sisense.models import BrowserNotAvailableError
from tests import NetworkBlockedError
from tests.test_login import DASHBOARD_URL, make_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StopAfterLaunch(Exception):
    pass


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def new_context(self, **kwargs):
        raise StopAfterLaunch()

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, error=None):
        self.error = error
        self.launch_calls = []
        self.browser = FakeBrowser()

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.browser


class FakePlaywright:
    def __init__(self, error=None):
        self.chromium = FakeChromium(error)


MISSING_EDGE = PlaywrightError(
    'Chromium distribution \'msedge\' is not found at C:\\Program Files (x86)\\Microsoft\\Edge\\msedge.exe\n'
    "Run \"npx playwright install msedge\""
)


class LaunchBrowserTests(unittest.TestCase):
    def test_msedge_channel_passed(self):
        pw = FakePlaywright()
        launch_browser(pw, "msedge", headless=False)
        self.assertEqual(pw.chromium.launch_calls, [{"channel": "msedge", "headless": False}])

    def test_chrome_channel_passed(self):
        pw = FakePlaywright()
        launch_browser(pw, "chrome", headless=False)
        self.assertEqual(pw.chromium.launch_calls, [{"channel": "chrome", "headless": False}])

    def test_missing_browser_reports_marker(self):
        pw = FakePlaywright(error=MISSING_EDGE)
        with self.assertRaises(BrowserNotAvailableError) as ctx:
            launch_browser(pw, "msedge", headless=False)
        self.assertIn(CONFIGURED_BROWSER_NOT_AVAILABLE, str(ctx.exception))
        self.assertEqual(len(pw.chromium.launch_calls), 1, "must not fall back to another channel")

    def test_unsupported_channel_never_launches(self):
        pw = FakePlaywright()
        with self.assertRaises(BrowserNotAvailableError):
            launch_browser(pw, "chromium", headless=False)
        self.assertEqual(pw.chromium.launch_calls, [])

    def test_no_install_or_subprocess_invoked(self):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")) as popen, \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")) as run, \
             mock.patch.object(os, "system", side_effect=AssertionError("os.system")) as system:
            launch_browser(FakePlaywright(), "msedge", headless=False)
            with self.assertRaises(BrowserNotAvailableError):
                launch_browser(FakePlaywright(error=MISSING_EDGE), "msedge", headless=False)
        popen.assert_not_called()
        run.assert_not_called()
        system.assert_not_called()

    def test_application_code_has_no_install_command(self):
        sources = [p for p in PROJECT_ROOT.rglob("*.py") if ".venv" not in p.parts and "tests" not in p.parts]
        self.assertTrue(sources)
        for path in sources:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("playwright install", text)
                self.assertNotIn("subprocess", text)
                self.assertNotIn("executable_path", text)


class AuthenticatorLaunchTests(unittest.TestCase):
    def test_open_session_launches_configured_channel_headed(self):
        for channel in ("msedge", "chrome"):
            with self.subTest(channel=channel):
                pw = FakePlaywright()
                settings = make_settings()
                settings = type(settings)(**{**settings.__dict__, "headless": False, "browser_channel": channel})
                with self.assertRaises(StopAfterLaunch):
                    SisenseAuthenticator(pw, settings).open_session()
                self.assertEqual(pw.chromium.launch_calls, [{"channel": channel, "headless": False}])
                self.assertTrue(pw.chromium.browser.closed)

    def test_open_session_missing_browser_fails_before_any_page(self):
        pw = FakePlaywright(error=MISSING_EDGE)
        prompted = []
        auth = SisenseAuthenticator(pw, make_settings(), password_prompt=lambda: prompted.append(1) or "x")
        with self.assertRaises(BrowserNotAvailableError) as ctx:
            auth.open_session()
        self.assertIn(CONFIGURED_BROWSER_NOT_AVAILABLE, str(ctx.exception))
        self.assertEqual(prompted, [], "password must not be requested when the browser cannot start")


class BrowserChannelSettingsTests(unittest.TestCase):
    def load(self, extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f"SISENSE_EMAIL=user@example.com\nSISENSE_DASHBOARD_URL={DASHBOARD_URL}\n{extra}")
            with mock.patch.dict(os.environ, {}, clear=True):
                return load_settings(env_file)

    def test_default_is_msedge(self):
        self.assertEqual(self.load().browser_channel, "msedge")

    def test_chrome_configurable(self):
        self.assertEqual(self.load("PLAYWRIGHT_BROWSER_CHANNEL=chrome\n").browser_channel, "chrome")

    def test_unsupported_channel_rejected(self):
        for value in ("chromium", "firefox", "edge"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                self.load(f"PLAYWRIGHT_BROWSER_CHANNEL={value}\n")


class NetworkGuardTests(unittest.TestCase):
    def test_outbound_connections_blocked(self):
        with self.assertRaises(NetworkBlockedError):
            socket.create_connection(("gahixreports.getinsured.com", 443), timeout=1)
        sock = socket.socket()
        try:
            with self.assertRaises(NetworkBlockedError):
                sock.connect(("127.0.0.1", 9))
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
