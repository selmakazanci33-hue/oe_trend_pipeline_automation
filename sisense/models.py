from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


class SisenseError(Exception):
    """Base class for all Sisense pipeline errors."""


class AuthenticationError(SisenseError):
    """Login failed, session expired, or the request was redirected to login (HTTP 401/3xx)."""


class AuthorizationError(SisenseError):
    """Authenticated but not permitted (HTTP 403)."""


class InvalidJaqlPayloadError(SisenseError):
    """Payload failed local validation or was rejected by Sisense (HTTP 400)."""


class UnverifiedEndpointError(SisenseError):
    """An endpoint other than the verified JAQL endpoint was supplied."""


class TransientSisenseError(SisenseError):
    """A failure that is reasonable to retry (HTTP 502/503/504)."""


class UnexpectedResponseError(SisenseError):
    """An HTTP status that is neither success nor a known retryable/terminal case."""


class ResponseValidationError(SisenseError):
    """The JAQL response did not have the expected structure or value."""


class BrowserNotAvailableError(SisenseError):
    """The configured installed browser channel could not be launched."""


class VerifiedJaqlPayloadRequired(SisenseError):
    """The verified JAQL payload for a metric has not been supplied."""


@dataclass(frozen=True)
class JaqlQuery:
    """A verified JAQL request body. Only payload builders should create these."""

    metric: str
    plan_year: int
    widget_title: str
    body: Mapping[str, Any]
    primary_measure_title: str | None = None
    # Exact verified request text; when set it is sent byte-for-byte instead of re-serializing body.
    raw_body: str | None = None


@dataclass(frozen=True)
class JaqlResponse:
    status: int
    body: dict[str, Any]
    duration_seconds: float
    attempts: int


@dataclass(frozen=True)
class MetricResult:
    metric: str
    plan_year: int
    value: int | float
    retrieved_at: str
    source: str
    dashboard: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
