"""Opt-in local session diagnostics (SISENSE_DEBUG_SESSION=true).

Logs only names and attributes: cookie names/domains/paths/flags and HTTP
header names. Values of cookies, headers, tokens, and passwords are never
written to log output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


def cookie_names_from_header(cookie_header: str | None) -> list[str]:
    """Names from a Cookie request header; values are discarded."""
    if not cookie_header:
        return []
    return sorted({part.split("=", 1)[0].strip() for part in cookie_header.split(";") if part.strip()})


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


@dataclass(frozen=True)
class ObservedJaqlRequest:
    header_names: frozenset[str]
    cookie_names: tuple[str, ...]
    status: int | None


class BrowserJaqlObserver:
    """Passively records the dashboard's own JAQL POSTs.

    Event handlers only store Request references: in the sync API they run
    inside Playwright's dispatcher, where blocking calls such as
    ``Request.all_headers()`` cannot complete. Headers are read later, from
    the main flow, via ``inspect()``.
    """

    MAX_INSPECTED = 10

    def __init__(self, url: str = VERIFIED_JAQL_URL):
        self._url = url
        self._context: BrowserContext | None = None
        self.request_count = 0
        self._finished: list[Request] = []

    @property
    def finished_count(self) -> int:
        return len(self._finished)

    def finished_requests(self) -> list[Request]:
        return list(self._finished)

    def attach(self, context: BrowserContext) -> None:
        """Observe ``context`` only; anything recorded from an earlier context is discarded."""
        self._context = context
        self.request_count = 0
        self._finished = []

        def on_request(request: Request) -> None:
            if context is self._context:
                self._on_request(request)

        def on_request_finished(request: Request) -> None:
            if context is self._context:
                self._on_request_finished(request)

        context.on("request", on_request)
        context.on("requestfinished", on_request_finished)

    def _matches(self, request: Request) -> bool:
        return request.method == "POST" and request.url == self._url

    def _on_request(self, request: Request) -> None:
        if self._matches(request):
            self.request_count += 1

    def _on_request_finished(self, request: Request) -> None:
        if self._matches(request) and len(self._finished) < self.MAX_INSPECTED:
            self._finished.append(request)

    def inspect(self) -> list[ObservedJaqlRequest]:
        observed: list[ObservedJaqlRequest] = []
        for request in self._finished:
            try:
                headers = request.all_headers()
            except Exception as exc:
                logger.info("[debug] Could not read observed request headers: %s", type(exc).__name__)
                continue
            status = None
            try:
                response = request.response()
                status = response.status if response is not None else None
            except Exception as exc:
                logger.info("[debug] Could not read observed response status: %s", type(exc).__name__)
            observed.append(ObservedJaqlRequest(
                header_names=frozenset(name.lower() for name in headers),
                cookie_names=tuple(cookie_names_from_header(headers.get("cookie"))),
                status=status,
            ))
        return observed

    def log_summary(self) -> None:
        logger.info(
            "[debug] Dashboard sent %d JAQL POST(s); %d finished and kept for inspection",
            self.request_count, self.finished_count,
        )
        observed = self.inspect()
        if not observed:
            logger.info("[debug] No finished dashboard JAQL POSTs could be inspected")
            return

        statuses = sorted({str(o.status) for o in observed})
        logger.info("[debug] Inspected %d dashboard JAQL POST(s); statuses: %s", len(observed), statuses)

        successful = [o for o in observed if o.status == 200]
        if successful:
            common = frozenset.intersection(*(o.header_names for o in successful))
            logger.info("[debug] Header names on every HTTP 200 dashboard JAQL POST: %s", sorted(common))
            cookies = sorted(set.intersection(*(set(o.cookie_names) for o in successful)))
            logger.info("[debug] Cookie names sent on every HTTP 200 dashboard JAQL POST: %s", cookies)
        all_names = frozenset().union(*(o.header_names for o in observed))
        logger.info("[debug] Header names seen on any dashboard JAQL POST: %s", sorted(all_names))
