import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config.settings import (
    LoginSelectors,
    MissingVerifiedInformation,
    RetrySettings,
    Settings,
    Timeouts,
    load_settings,
)
from sisense.auth import SisenseAuthenticator
from sisense.models import AuthenticationError

DASHBOARD_URL = "https://gahixreports.getinsured.com/app/main/dashboards/6a98048ccef60720fe7f9b30"
SECRET = "s3cr3t-Pa55word!"


class FakeLocator:
    def __init__(self, page, name):
        self.page = page
        self.name = name
        self.value = None

    @property
    def first(self):
        return self

    def wait_for(self, state="visible", timeout=None):
        if self.name == "dashboard" and not self.page.dashboard_appears:
            raise PlaywrightTimeoutError("Timeout")

    def fill(self, value, timeout=None):
        self.value = value

    def press(self, key, timeout=None):
        self.page.actions.append(("press", self.name, key))
        self.page.submitted = self.page.enter_submits

    def click(self, timeout=None):
        self.page.actions.append(("click", self.name))
        self.page.submitted = True


class FakePage:
    def __init__(self, enter_submits=True, dashboard_appears=True):
        self.enter_submits = enter_submits
        self.dashboard_appears = dashboard_appears
        self.submitted = False
        self.actions = []
        self.locators = {}

    def locator(self, selector):
        return self.locators.setdefault(selector, FakeLocator(self, selector))

    def get_by_text(self, text, exact=False):
        return self.locators.setdefault("dashboard", FakeLocator(self, "dashboard"))

    @contextmanager
    def expect_event(self, event, predicate=None, timeout=None):
        yield None
        if not self.submitted:
            raise PlaywrightTimeoutError("Timeout waiting for event 'request'")


def make_settings(submit=None):
    return Settings(
        base_url="https://gahixreports.getinsured.com",
        email="user@example.com",
        dashboard_id="6a98048ccef60720fe7f9b30",
        dashboard_title="PY 2027 - OE Executive Dashboard",
        dashboard_url=DASHBOARD_URL,
        headless=True,
        timeouts=Timeouts(),
        retry=RetrySettings(),
        login_selectors=LoginSelectors(email="#username", password="#password", submit=submit),
        storage_state_path=Path("/nonexistent/sisense_state.json"),
        artifacts_dir=Path("/nonexistent/artifacts"),
    )


class LoginSubmitTests(unittest.TestCase):
    def login(self, page, submit=None):
        auth = SisenseAuthenticator(None, make_settings(submit), password_prompt=lambda: SECRET)
        auth._login(page)

    def test_enter_submits_and_fills_verified_fields(self):
        page = FakePage(enter_submits=True)
        self.login(page)
        self.assertEqual(page.locators["#username"].value, "user@example.com")
        self.assertEqual(page.locators["#password"].value, SECRET)
        self.assertEqual(page.actions, [("press", "#password", "Enter")])

    def test_enter_not_submitting_stops_for_submit_selector(self):
        with self.assertRaises(MissingVerifiedInformation) as ctx:
            self.login(FakePage(enter_submits=False))
        self.assertIn("SISENSE_LOGIN_SUBMIT_SELECTOR", str(ctx.exception))

    def test_configured_submit_selector_is_clicked(self):
        page = FakePage(enter_submits=False)
        self.login(page, submit="#verified-submit")
        self.assertEqual(page.actions, [("click", "#verified-submit")])

    def test_dashboard_not_appearing_is_authentication_error(self):
        with self.assertRaises(AuthenticationError):
            self.login(FakePage(dashboard_appears=False))

    def test_password_never_logged(self):
        with self.assertLogs("sisense.auth", level="DEBUG") as logs:
            self.login(FakePage())
        self.assertFalse(any(SECRET in line for line in logs.output))


class LoginSettingsTests(unittest.TestCase):
    def load(self, contents):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(contents)
            with mock.patch.dict(os.environ, {}, clear=True):
                return load_settings(env_file)

    def test_verified_selectors_and_dashboard_url(self):
        settings = self.load(
            "SISENSE_EMAIL=user@example.com\n"
            f"SISENSE_DASHBOARD_URL={DASHBOARD_URL}\n"
            "SISENSE_LOGIN_EMAIL_SELECTOR=#username\n"
        )
        self.assertEqual(settings.dashboard_url, DASHBOARD_URL)
        self.assertEqual(settings.login_selectors.email, "#username")
        self.assertEqual(settings.login_selectors.password, "#password")
        self.assertIsNone(settings.login_selectors.submit)

    def test_missing_email_is_reported(self):
        with self.assertRaises(MissingVerifiedInformation) as ctx:
            self.load(f"SISENSE_EMAIL=\nSISENSE_DASHBOARD_URL={DASHBOARD_URL}\n")
        self.assertIn("SISENSE_EMAIL", str(ctx.exception))

    def test_project_env_has_no_password(self):
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if not env_path.exists():
            self.skipTest(".env not present")
        keys = [line.split("=", 1)[0].strip().upper() for line in env_path.read_text().splitlines() if "=" in line]
        self.assertFalse(any("PASSWORD" in key and "SELECTOR" not in key for key in keys))


if __name__ == "__main__":
    unittest.main()
