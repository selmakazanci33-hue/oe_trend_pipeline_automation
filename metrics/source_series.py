"""Persistence design for verified Sisense SOURCE time series.

``metric_snapshots`` stores one pipeline observation of a production KPI at
retrieval time (snapshot slot). It is the history for aggregate SOURCE values
such as Currently Enrolled People and, after promotion, people-level
cancellation counts.

A Sisense daily policy series is a different object: its x-axis is the
source date dimension, its grain and date semantics come from the request
definition, and its points are not pipeline retrieval times. Storing those
points as ``metric_snapshots`` would silently present snapshot history as a
business daily series.

Before any daily cancellation chart is production-rendered, a dedicated
store is required, for example:

    source_series_points(
        metric_id, source_date, time_grain, date_semantics,
        value, definition_fingerprint, observed_at_utc
    )

This module does not create that table. Daily series remain PENDING until a
verified definition exists and this store is implemented.
"""

from __future__ import annotations

from metrics.catalog import CANCELLATION_CANDIDATES, KIND_DAILY_POLICY_SERIES, KIND_PEOPLE_AGGREGATE
from reporting.series_semantics import CURRENTLY_ENROLLED_SNAPSHOT_TREND, SeriesIdentity, identity_complete

PROPOSED_SOURCE_SERIES_STORE = "source_series_points"

DAILY_SOURCE_METRIC_IDS = frozenset(
    c.metric_id for c in CANCELLATION_CANDIDATES.values() if c.kind == KIND_DAILY_POLICY_SERIES
)
PEOPLE_AGGREGATE_METRIC_IDS = frozenset(
    c.metric_id for c in CANCELLATION_CANDIDATES.values() if c.kind == KIND_PEOPLE_AGGREGATE
)


def may_persist_in_metric_snapshots(metric_id: str) -> bool:
    """Daily SOURCE series must not be written as pipeline snapshot slots."""
    return metric_id not in DAILY_SOURCE_METRIC_IDS


def production_daily_chart_allowed(identity: SeriesIdentity) -> bool:
    """True only when the source identity is complete and is not snapshot history."""
    if not identity_complete(identity):
        return False
    if identity.date_semantics == CURRENTLY_ENROLLED_SNAPSHOT_TREND.date_semantics:
        return False
    if identity.time_grain == CURRENTLY_ENROLLED_SNAPSHOT_TREND.time_grain:
        return False
    return True
