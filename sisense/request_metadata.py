"""Request metadata taken from the dashboard's own successful JAQL request.

The header names below were verified on an HTTP 200 JAQL POST issued by the
authenticated dashboard. Their values are never defined here: at runtime they
are read from that observed browser request in the same BrowserContext, kept
in memory only, and never logged or persisted.
"""

from __future__ import annotations

from typing import Mapping
from urllib.parse import unquote

from playwright.sync_api import BrowserContext

from config.settings import VERIFIED_JAQL_URL
from sisense.diagnostics import BrowserJaqlObserver
from sisense.models import RequestMetadataUnavailable

FORWARDED_HEADER_NAMES = (
    "x-xsrf-token",
    "x-sisense-origin",
    "x-tenant-id",
    "internal",
    "origin",
    "referer",
)
REQUIRED_HEADER_NAME = "x-xsrf-token"
PAIRED_COOKIE_NAME = "XSRF-TOKEN"


class ForwardedHeaders:
    """Allow-listed header values held in memory; repr/str expose names only."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]):
        unexpected = set(values) - set(FORWARDED_HEADER_NAMES)
        if unexpected:
            raise ValueError(f"Headers outside the verified allow-list: {sorted(unexpected)}")
        self._values = dict(values)

    def names(self) -> list[str]:
        return sorted(self._values)

    def as_headers(self) -> dict[str, str]:
        return dict(self._values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def __repr__(self) -> str:
        return f"ForwardedHeaders(names={self.names()})"

    __str__ = __repr__


def headers_from_observed_request(observer: BrowserJaqlObserver) -> ForwardedHeaders:
    """Return allow-listed headers from the first observed HTTP 200 dashboard JAQL POST."""
    for request in observer.finished_requests():
        try:
            response = request.response()
            if response is None or response.status != 200:
                continue
            headers = {name.lower(): value for name, value in request.all_headers().items()}
        except Exception:
            continue
        if REQUIRED_HEADER_NAME not in headers:
            continue
        return ForwardedHeaders({name: headers[name] for name in FORWARDED_HEADER_NAMES if name in headers})
    raise RequestMetadataUnavailable(
        "No successful (HTTP 200) dashboard JAQL request with the verified CSRF header was observed "
        "in the authenticated BrowserContext; refusing to send the application JAQL request"
    )


def csrf_header_matches_context_cookie(
    forwarded: ForwardedHeaders, context: BrowserContext, url: str = VERIFIED_JAQL_URL
) -> bool:
    """True when the forwarded CSRF header equals the context's current paired cookie."""
    header_value = forwarded.get(REQUIRED_HEADER_NAME)
    cookie_values = [c.get("value") for c in context.cookies([url]) if c.get("name") == PAIRED_COOKIE_NAME]
    if header_value is None or len(cookie_values) != 1 or cookie_values[0] is None:
        return False
    return unquote(header_value) == unquote(cookie_values[0])
