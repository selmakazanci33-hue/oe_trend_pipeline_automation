"""Bounded retry with backoff and jitter for transient failures only."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config.settings import RetrySettings
from sisense.models import TransientSisenseError

T = TypeVar("T")

TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})

_TRANSIENT_NETWORK_MARKERS = (
    "ECONNRESET",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_NETWORK_CHANGED",
    "ETIMEDOUT",
    "socket hang up",
)

logger = logging.getLogger(__name__)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientSisenseError):
        return True
    if isinstance(exc, PlaywrightTimeoutError):
        return True
    if isinstance(exc, PlaywrightError):
        message = str(exc)
        return any(marker in message for marker in _TRANSIENT_NETWORK_MARKERS)
    return False


def _short_reason(exc: BaseException) -> str:
    # Playwright messages append a multi-line call log; keep only the headline.
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first_line[:200]}"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (2.0, 5.0, 10.0)
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not self.backoff_seconds:
            raise ValueError("backoff_seconds must not be empty")
        if not 0 <= self.jitter_ratio < 1:
            raise ValueError("jitter_ratio must be in [0, 1)")

    @classmethod
    def from_settings(cls, settings: RetrySettings) -> "RetryPolicy":
        return cls(
            max_attempts=settings.max_attempts,
            backoff_seconds=settings.backoff_seconds,
            jitter_ratio=settings.jitter_ratio,
        )

    def delay_for(self, failed_attempt: int, rng: random.Random | None = None) -> float:
        base = self.backoff_seconds[min(failed_attempt, len(self.backoff_seconds)) - 1]
        spread = base * self.jitter_ratio
        return max(0.0, base + (rng or random).uniform(-spread, spread))


def run_with_retry(
    operation: Callable[[int], T],
    *,
    policy: RetryPolicy,
    description: str,
    classify: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``operation(attempt)`` until it succeeds or a non-transient error occurs.

    The attempt number is passed in so the operation can escalate its timeout tier.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation(attempt)
        except Exception as exc:
            if not classify(exc):
                raise
            if attempt == policy.max_attempts:
                logger.error(
                    "%s failed on final attempt %d/%d: %s",
                    description, attempt, policy.max_attempts, _short_reason(exc),
                )
                raise
            delay = policy.delay_for(attempt, rng)
            logger.warning(
                "%s attempt %d/%d failed (transient): %s; retrying in %.1f sec",
                description, attempt, policy.max_attempts, _short_reason(exc), delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")
