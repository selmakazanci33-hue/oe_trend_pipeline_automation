"""Browser authentication and session lifecycle for Sisense."""

from __future__ import annotations

import getpass
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, Request
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config.settings import MissingVerifiedInformation, Settings
from sisense.browser import launch_browser
from sisense.diagnostics import BrowserJaqlObserver
from sisense.models import AuthenticationError
from sisense.retry import RetryPolicy, run_with_retry

logger = logging.getLogger(__name__)

PageState = Literal["dashboard", "login"]


def prompt_password() -> str:
    return getpass.getpass("Sisense password: ")


@dataclass
class AuthenticatedSession:
    browser: Browser
    context: BrowserContext
    page: Page
    reused_saved_state: bool

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self.browser.close()


class SisenseAuthenticator:
    def __init__(
        self,
        playwright: Playwright,
        settings: Settings,
        password_prompt: Callable[[], str] = prompt_password,
    ):
        self._playwright = playwright
        self._settings = settings
        self._password_prompt = password_prompt
        self._navigation_retry = RetryPolicy.from_settings(settings.retry)
        observe = settings.debug_session or settings.diagnostics_only
        self.jaql_observer = BrowserJaqlObserver() if observe else None

    def open_session(self) -> AuthenticatedSession:
        """Return a browser context that is verified to be on the target dashboard."""
        browser = launch_browser(self._playwright, self._settings.browser_channel, self._settings.headless)
        page: Page | None = None
        try:
            state_path = self._settings.storage_state_path
            if state_path.is_file():
                logger.info("Saved session found; verifying")
                context = self._new_context(browser, storage_state=state_path)
                page = context.new_page()
                if self._open_dashboard(page) == "dashboard" and self._is_authenticated(page):
                    logger.info("Sisense session verified (saved state)")
                    return AuthenticatedSession(browser, context, page, reused_saved_state=True)
                logger.info("Saved session is expired or invalid; discarding it")
                context.close()
                page = None
                state_path.unlink(missing_ok=True)

            context = self._new_context(browser)
            page = context.new_page()
            if self._open_dashboard(page) == "login":
                self._login(page)
            if not self._is_authenticated(page):
                raise AuthenticationError("Dashboard did not load as an authenticated user after login")

            logger.info("Authentication successful")
            logger.info("Sisense session verified")
            self._save_state(context)
            return AuthenticatedSession(browser, context, page, reused_saved_state=False)
        except Exception:
            if page is not None:
                self._capture_failure(page)
            browser.close()
            raise

    def _new_context(self, browser: Browser, storage_state: Path | None = None) -> BrowserContext:
        if storage_state is not None:
            context = browser.new_context(storage_state=str(storage_state))
        else:
            context = browser.new_context()
        context.set_default_timeout(self._settings.timeouts.default)
        context.set_default_navigation_timeout(self._settings.timeouts.slow)
        if self.jaql_observer is not None:
            self.jaql_observer.attach(context)
        return context

    # Locators -----------------------------------------------------------

    def _dashboard_title(self, page: Page) -> Locator:
        return page.get_by_text(self._settings.dashboard_title, exact=True).first

    def _password_field(self, page: Page) -> Locator:
        return page.locator(self._settings.login_selectors.password).first

    # Navigation / state detection ----------------------------------------

    def _open_dashboard(self, page: Page) -> PageState:
        """Navigate to the dashboard and wait until either it or a login form renders."""

        def attempt(n: int) -> PageState:
            timeout = self._settings.timeouts.for_attempt(n)
            page.goto(self._settings.dashboard_url, wait_until="domcontentloaded", timeout=timeout)
            dashboard = self._dashboard_title(page)
            password = self._password_field(page)
            dashboard.or_(password).first.wait_for(state="visible", timeout=timeout)
            return "dashboard" if dashboard.is_visible() else "login"

        state = run_with_retry(attempt, policy=self._navigation_retry, description="Dashboard navigation")
        logger.info("Dashboard navigation landed on: %s", state)
        return state

    def _is_authenticated(self, page: Page) -> bool:
        """Authenticated means: on the Sisense host, dashboard title visible, no login form."""
        if urlsplit(page.url).netloc != self._settings.host:
            logger.warning("Page is not on the Sisense host after navigation")
            return False
        if not self._dashboard_title(page).is_visible():
            return False
        if self._password_field(page).is_visible():
            return False
        if self._settings.dashboard_id not in page.url:
            logger.warning("Dashboard ID %s not present in the current URL", self._settings.dashboard_id)
        logger.info("Dashboard: %s", self._settings.dashboard_title)
        return True

    # Login ----------------------------------------------------------------

    def _login(self, page: Page) -> None:
        selectors = self._settings.login_selectors
        if not selectors.email:
            raise MissingVerifiedInformation([
                "SISENSE_LOGIN_EMAIL_SELECTOR (.env): a verified CSS selector for the email/username "
                "field on the Sisense login page (DevTools -> right-click field -> Inspect)",
            ])

        timeouts = self._settings.timeouts
        email_field = page.locator(selectors.email).first
        password_field = self._password_field(page)
        try:
            email_field.wait_for(state="visible", timeout=timeouts.short)
        except PlaywrightTimeoutError as exc:
            raise AuthenticationError(
                "Login form detected but the configured email field is not visible; "
                "check SISENSE_LOGIN_EMAIL_SELECTOR"
            ) from exc

        logger.info("Login form detected; credentials required")
        password = self._password_prompt()
        if not password:
            raise AuthenticationError("Empty password entered")

        try:
            email_field.fill(self._settings.email, timeout=timeouts.short)
            password_field.fill(password, timeout=timeouts.short)
        finally:
            del password

        # Submission is confirmed passively: the login page itself must issue a
        # POST or a navigation. No request is sent by this code.
        def is_submission(request: Request) -> bool:
            return request.method == "POST" or request.is_navigation_request()

        try:
            with page.expect_event("request", predicate=is_submission, timeout=timeouts.short):
                if selectors.submit:
                    page.locator(selectors.submit).first.click(timeout=timeouts.short)
                else:
                    password_field.press("Enter", timeout=timeouts.short)
        except PlaywrightTimeoutError as exc:
            if selectors.submit:
                raise AuthenticationError(
                    "Clicking SISENSE_LOGIN_SUBMIT_SELECTOR did not submit the login form"
                ) from exc
            raise MissingVerifiedInformation([
                "SISENSE_LOGIN_SUBMIT_SELECTOR (.env): pressing Enter in the password field did not "
                "submit the login form; a verified selector for the login button is required",
            ]) from exc
        logger.info("Credentials submitted; waiting for dashboard (SSO/MFA may be completed in the browser)")

        try:
            self._dashboard_title(page).wait_for(state="visible", timeout=timeouts.very_slow)
        except PlaywrightTimeoutError as exc:
            raise AuthenticationError(
                f"Dashboard did not appear within {timeouts.very_slow / 1000:.0f} sec after login"
            ) from exc

    # Persistence / artifacts ----------------------------------------------

    def _save_state(self, context: BrowserContext) -> None:
        path = self._settings.storage_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        context.storage_state(path=str(path))
        os.chmod(path, 0o600)
        logger.info("Session state saved to %s", path.relative_to(path.parents[2]))

    def _capture_failure(self, page: Page) -> None:
        artifacts = self._settings.artifacts_dir
        try:
            artifacts.mkdir(parents=True, exist_ok=True)
            password_field = self._password_field(page)
            if password_field.is_visible():
                password_field.fill("", timeout=self._settings.timeouts.short)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = artifacts / f"auth_failure_{stamp}.png"
            page.screenshot(path=str(path), timeout=self._settings.timeouts.short)
            logger.error("Failure screenshot saved: %s", path)
        except PlaywrightError as exc:
            logger.warning("Could not capture failure screenshot: %s", type(exc).__name__)
