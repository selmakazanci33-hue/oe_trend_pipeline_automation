"""Launches an already-installed browser (Edge/Chrome) through Playwright.

Never downloads or installs a browser. If the configured channel is not
installed, launching fails with CONFIGURED_BROWSER_NOT_AVAILABLE.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Browser, Playwright
from playwright.sync_api import Error as PlaywrightError

from config.settings import SUPPORTED_BROWSER_CHANNELS
from sisense.models import BrowserNotAvailableError

CONFIGURED_BROWSER_NOT_AVAILABLE = "CONFIGURED_BROWSER_NOT_AVAILABLE"

logger = logging.getLogger(__name__)


def launch_browser(playwright: Playwright, channel: str, headless: bool) -> Browser:
    if channel not in SUPPORTED_BROWSER_CHANNELS:
        raise BrowserNotAvailableError(
            f"{CONFIGURED_BROWSER_NOT_AVAILABLE}: unsupported channel {channel!r}; "
            f"expected one of {sorted(SUPPORTED_BROWSER_CHANNELS)}"
        )
    logger.info("Launching installed browser channel: %s (headless=%s)", channel, headless)
    try:
        return playwright.chromium.launch(channel=channel, headless=headless)
    except PlaywrightError as exc:
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise BrowserNotAvailableError(
            f"{CONFIGURED_BROWSER_NOT_AVAILABLE}: could not launch installed browser channel "
            f"{channel!r} ({reason[:200]}). Install that browser or set PLAYWRIGHT_BROWSER_CHANNEL "
            f"to another installed channel."
        ) from exc
