"""Runtime configuration for oe_trend_pipeline.

Verified constants (base URL, JAQL endpoint, datasource, dashboard) live here
as module-level values. Everything environment-specific is loaded from `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VERIFIED_BASE_URL = "https://gahixreports.getinsured.com"
VERIFIED_JAQL_PATH = "/api/datasources/MAIN/jaql"
VERIFIED_JAQL_URL = VERIFIED_BASE_URL + VERIFIED_JAQL_PATH

VERIFIED_DATASOURCE = {
    "title": "MAIN",
    "database": "aMAIN",
    "id": "localhost_aMAIN",
    "fullname": "localhost/MAIN",
    "live": False,
}
SOURCE_LABEL = "Sisense MAIN"

VERIFIED_DASHBOARD_ID = "6a98048ccef60720fe7f9b30"
VERIFIED_DASHBOARD_TITLE = "PY 2027 - OE Executive Dashboard"

# Point-in-time manual observation. Used for logging comparisons only, never as
# an expected value.
MANUAL_BASELINE_CURRENTLY_ENROLLED = 791826

VERIFIED_PASSWORD_SELECTOR = "#password"


class ConfigurationError(Exception):
    """Configuration is present but invalid."""


class MissingVerifiedInformation(ConfigurationError):
    """Required verified information has not been supplied."""

    def __init__(self, items: list[str]):
        self.items = list(items)
        super().__init__(
            "Missing verified information:\n" + "\n".join(f"  - {item}" for item in self.items)
        )


@dataclass(frozen=True)
class Timeouts:
    """Timeout tiers in milliseconds."""

    short: int = 10_000
    default: int = 30_000
    slow: int = 60_000
    very_slow: int = 120_000

    def for_attempt(self, attempt: int) -> int:
        """Escalate the timeout tier on each retry attempt."""
        tiers = (self.default, self.slow, self.very_slow)
        return tiers[min(max(attempt, 1), len(tiers)) - 1]


@dataclass(frozen=True)
class RetrySettings:
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (2.0, 5.0, 10.0)
    jitter_ratio: float = 0.2


@dataclass(frozen=True)
class LoginSelectors:
    email: str | None
    password: str
    submit: str | None


@dataclass(frozen=True)
class Settings:
    base_url: str
    email: str
    dashboard_id: str
    dashboard_title: str
    dashboard_url: str
    headless: bool
    timeouts: Timeouts
    retry: RetrySettings
    login_selectors: LoginSelectors
    storage_state_path: Path
    artifacts_dir: Path
    manual_baseline: int = MANUAL_BASELINE_CURRENTLY_ENROLLED

    @property
    def jaql_url(self) -> str:
        return VERIFIED_JAQL_URL

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        value = int(raw.replace("_", ""))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must not be negative, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true/false, got {raw!r}")


def _env_backoff(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        values = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be comma-separated seconds, got {raw!r}") from exc
    if not values or any(v < 0 for v in values):
        raise ConfigurationError(f"{name} must contain non-negative values, got {raw!r}")
    return values


def _normalize_base_url(raw: str) -> str:
    base_url = raw.rstrip("/")
    if base_url != VERIFIED_BASE_URL:
        raise ConfigurationError(
            f"SISENSE_BASE_URL must be the verified base URL {VERIFIED_BASE_URL!r}, got {raw!r}"
        )
    return base_url


def _validate_dashboard_url(dashboard_url: str, base_url: str) -> str:
    parts = urlsplit(dashboard_url)
    expected = urlsplit(base_url)
    if parts.scheme != "https" or parts.netloc != expected.netloc:
        raise ConfigurationError(
            f"SISENSE_DASHBOARD_URL must be an https URL on {expected.netloc}, got {dashboard_url!r}"
        )
    return dashboard_url


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    missing: list[str] = []

    base_url = _normalize_base_url(_env_str("SISENSE_BASE_URL", VERIFIED_BASE_URL))

    email = _env_str("SISENSE_EMAIL")
    if not email:
        missing.append("SISENSE_EMAIL (.env): the account email used on the Sisense login page")

    dashboard_url = _env_str("SISENSE_DASHBOARD_URL")
    if not dashboard_url:
        missing.append(
            "SISENSE_DASHBOARD_URL (.env): the exact browser address-bar URL of "
            f"'{VERIFIED_DASHBOARD_TITLE}' (copy it from the browser, do not construct it)"
        )

    if missing:
        raise MissingVerifiedInformation(missing)

    timeouts = Timeouts(
        short=_env_int("SHORT_TIMEOUT", 10_000),
        default=_env_int("DEFAULT_TIMEOUT", 30_000),
        slow=_env_int("SLOW_TIMEOUT", 60_000),
        very_slow=_env_int("VERY_SLOW_TIMEOUT", 120_000),
    )

    retry = RetrySettings(
        max_attempts=_env_int("JAQL_MAX_ATTEMPTS", 3),
        backoff_seconds=_env_backoff("JAQL_BACKOFF_SECONDS", (2.0, 5.0, 10.0)),
        jitter_ratio=_env_float("JAQL_BACKOFF_JITTER", 0.2),
    )

    login_selectors = LoginSelectors(
        email=_env_str("SISENSE_LOGIN_EMAIL_SELECTOR"),
        password=_env_str("SISENSE_LOGIN_PASSWORD_SELECTOR", VERIFIED_PASSWORD_SELECTOR),
        submit=_env_str("SISENSE_LOGIN_SUBMIT_SELECTOR"),
    )

    return Settings(
        base_url=base_url,
        email=email,
        dashboard_id=_env_str("SISENSE_DASHBOARD_ID", VERIFIED_DASHBOARD_ID),
        dashboard_title=_env_str("SISENSE_DASHBOARD_TITLE", VERIFIED_DASHBOARD_TITLE),
        dashboard_url=_validate_dashboard_url(dashboard_url, base_url),
        headless=_env_bool("SISENSE_HEADLESS", False),
        timeouts=timeouts,
        retry=retry,
        login_selectors=login_selectors,
        storage_state_path=PROJECT_ROOT / "runtime" / "auth" / "sisense_state.json",
        artifacts_dir=PROJECT_ROOT / "runtime" / "artifacts",
    )
