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

The table ``source_series_points`` lives in ``history.source_series_store``.
The production pipeline does not write cancellation points until a human
promotion. Daily points are never inserted into ``metric_snapshots``.
"""

from __future__ import annotations

from typing import Any, Mapping

from metrics.date_labels import valid_date_label
from metrics.catalog import CANCELLATION_CANDIDATES, KIND_DAILY_POLICY_SERIES, KIND_PEOPLE_AGGREGATE
from reporting.series_semantics import CURRENTLY_ENROLLED_SNAPSHOT_TREND, SeriesIdentity, identity_complete
from sisense.discovery import WidgetStructure, normalize_label
from validation.validators import parse_json_body

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


def extract_source_points(structure: WidgetStructure, body_text: str,
                          measure_title: str) -> list[tuple[str | None, int]]:
    """Source-date points for one exact measure title. Position is not a binding.

    The date is None when no cell representation is a provable calendar date;
    the point is then an ordered observation only.
    """
    date_indexes = [index for index, column in enumerate(structure.columns) if column.is_date]
    if len(date_indexes) != 1:
        raise ValueError("Daily series requires exactly one date dimension")
    key = normalize_label(measure_title)
    measure_indexes = [
        index for index, column in enumerate(structure.columns)
        if column.kind == "measure" and normalize_label(column.title) == key
    ]
    if len(measure_indexes) != 1:
        raise ValueError("Exact measure title is required")
    body = parse_json_body(body_text)
    values = body.get("values")
    if not isinstance(values, list):
        raise ValueError("Response has no values")
    points: list[tuple[str, int]] = []
    for row in values:
        if not isinstance(row, list) or len(row) != len(structure.columns):
            raise ValueError("Row does not match request columns")
        source_date = _source_date(row[date_indexes[0]])
        raw = row[measure_indexes[0]].get("data") if isinstance(row[measure_indexes[0]], Mapping) else None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("Series value must be a nonnegative integer")
        points.append((source_date, raw))
    return points


def _source_date(cell: Any) -> str | None:
    """Raw ``data`` first (ISO from the datasource), then display ``text``; only a valid calendar date is kept."""
    if not isinstance(cell, Mapping):
        raise ValueError("Source date is missing")
    data, text = cell.get("data"), cell.get("text")
    if not (isinstance(data, str) and data) and not (isinstance(text, str) and text):
        raise ValueError("Source date is missing")
    return valid_date_label(data) or valid_date_label(text)


def source_series_figure(points: list[tuple[str, int]]) -> dict[str, Any] | None:
    """Bar chart whose x-axis is the source date, never the pipeline retrieval time."""
    if not points:
        return None
    return {
        "data": [{
            "type": "bar",
            "x": [source_date for source_date, _value in points],
            "y": [value for _source_date, value in points],
        }],
        "layout": {
            "xaxis": {"title": {"text": "Source date"}, "type": "category"},
            "yaxis": {"title": {"text": "Record count"}},
        },
    }
