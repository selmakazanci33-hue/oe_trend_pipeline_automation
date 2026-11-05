"""Opt-in local session diagnostics (SISENSE_DEBUG_SESSION=true).

Logs only names and attributes: cookie names/domains/paths/flags and HTTP
header names. Values of cookies, headers, tokens, and passwords are never read
into log output.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Request

from config.settings import VERIFIED_JAQL_URL

logger = logging.getLogger(__name__)


def _cookie_attributes(cookie: dict[str, Any]) -> str:
    expires = cookie.get("expires", -1)
    return (
        f"{cookie.get('name')} domain={cookie.get('domain')} path={cookie.get('path')} "
        f"secure={cookie.get('secure')} httpOnly={cookie.get('httpOnly')} "
        f"sameSite={cookie.get('sameSite')} session={expires in (-1, None)}"
    )


def log_cookie_diagnostics(context: BrowserContext, url: str = VERIFIED_JAQL_URL) -> None:
    host = urlsplit(url).netloc
    all_cookies = context.cookies()
    applicable = context.cookies([url])
    logger.info("[debug] Context cookies: %d total", len(all_cookies))
    logger.info("[debug] Cookie domains: %s", sorted({str(c.get("domain")) for c in all_cookies}))
    logger.info(
        "[debug] Cookies applicable to %s%s: %d",
        host, urlsplit(url).path, len(applicable),
    )
    for cookie in applicable:
        logger.info("[debug]   %s", _cookie_attributes(cookie))
    not_applicable = [c for c in all_cookies if c not in applicable]
    for cookie in not_applicable:
        logger.info("[debug]   not sent to JAQL: %s", _cookie_attributes(cookie))


class BrowserJaqlObserver:
    """Passively records header names of JAQL POSTs the dashboard itself sends."""

    def __init__(self, url: str = VERIFIED_JAQL_URL):
        self._url = url
        self.request_count = 0
        self.header_names: set[str] = set()

    def attach(self, context: BrowserContext) -> None:
        context.on("request", self._on_request)

    def _on_request(self, request: Request) -> None:
        if request.method != "POST" or request.url != self._url:
            return
        self.request_count += 1
        try:
            names = request.all_headers().keys()
        except Exception:
            names = request.headers.keys()
        self.header_names.update(name.lower() for name in names)

    def log_summary(self) -> None:
        if not self.request_count:
            logger.info("[debug] No dashboard JAQL POSTs were observed in this context")
            return
        logger.info(
            "[debug] Dashboard sent %d JAQL POST(s); header names: %s",
            self.request_count, sorted(self.header_names),
        )
