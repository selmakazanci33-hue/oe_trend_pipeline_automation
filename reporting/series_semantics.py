"""Business-series identity for reporting.

Two charts or time series may be labelled, compared, reconciled, or presented
as equivalent only when all four fields are verified and equal:

1. metric identity
2. business filters
3. time grain
4. date range / date semantics

A missing field fails closed (not equivalent). This module does not retrieve
values or invent source definitions.
"""

from __future__ import annotations

from dataclasses import dataclass

_IDENTITY_FIELDS = ("metric_id", "filters", "time_grain", "date_semantics")


@dataclass(frozen=True)
class SeriesIdentity:
    label: str
    metric_id: str | None = None
    filters: str | None = None
    time_grain: str | None = None
    date_semantics: str | None = None


def series_equivalent(left: SeriesIdentity, right: SeriesIdentity) -> bool:
    """True only when every identity field is present on both sides and equal."""
    for name in _IDENTITY_FIELDS:
        a, b = getattr(left, name), getattr(right, name)
        if a is None or b is None or a != b:
            return False
    return True


# Production snapshot trend: stored validated Currently Enrolled observations.
CURRENTLY_ENROLLED_SNAPSHOT_TREND = SeriesIdentity(
    label="Currently Enrolled — Snapshot Trend",
    metric_id="currently_enrolled_people",
    filters="verified Currently Enrolled People widget definition",
    time_grain="snapshot slot",
    date_semantics="retrieved_at of stored validated observations",
)

# Roadmap-only daily series. Identity fields stay unset until independently verified.
CANCELLED_POLICIES_DAILY = SeriesIdentity(label="Cancelled Policies — Daily")
TERMINATED_POLICIES_DAILY = SeriesIdentity(label="Terminated Policies — Daily")
