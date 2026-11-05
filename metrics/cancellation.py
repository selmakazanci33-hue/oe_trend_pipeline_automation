"""Cancellation/termination discovery helpers (developer validation only).

People-level aggregates and daily policy series are independent identities.
Nothing here invents widget ids, measure titles, fingerprints, or payloads.
Nothing is stored in snapshot history or shown in the executive report.
"""

from __future__ import annotations

from typing import Any, Mapping

from config.settings import VERIFIED_DATASOURCE
from metrics.catalog import (
    GRAIN_DAYS,
    KIND_DAILY_POLICY_SERIES,
    KIND_PEOPLE_AGGREGATE,
    CancellationCandidate,
)
from metrics.runtime_validation import (
    BINDING_MEASURE_TITLE,
    RV_NOT_AVAILABLE,
    RV_SERIES_EXTRACTED,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    BindingError,
    MeasureBinding,
    RuntimeValue,
    extract_runtime_value,
)
from sisense.discovery import SHAPE_TIME_SERIES, normalize_label, summarize_response, widget_structure
from sisense.models import InvalidJaqlPayloadError
from validation.validators import parse_json_body

DAILY_GRAIN = GRAIN_DAYS


def _measure_titles(payload: Mapping[str, Any]) -> list[str]:
    """Exact measure titles; dimensions are skipped (not treated as measures)."""
    metadata = payload.get("metadata")
    if not isinstance(metadata, list):
        raise BindingError("Request definition metadata is not a list")
    titles = []
    for index, item in enumerate(metadata):
        if not isinstance(item, Mapping) or item.get("panel") == "scope":
            continue
        jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
        if jaql.get("type") == "measure" or "formula" in jaql:
            title = jaql.get("title")
            if not isinstance(title, str) or not title:
                raise BindingError(f"metadata[{index}] measure has no title")
            titles.append(title)
    return titles


def bind_exact_measure(payload: Mapping[str, Any], label: str) -> MeasureBinding:
    """Bind ``label`` to exactly one measure title. No positional or widget-title fallback."""
    try:
        titles = _measure_titles(payload)
    except BindingError:
        raise
    except InvalidJaqlPayloadError as exc:
        raise BindingError(str(exc)) from None
    key = normalize_label(label)
    hits = [i for i, title in enumerate(titles) if normalize_label(title) == key]
    if len(hits) > 1:
        raise BindingError(f"Ambiguous: {len(hits)} measures carry the label")
    if not hits:
        raise BindingError("No measure in the request definition is bound to the label")
    column = hits[0]
    return MeasureBinding(titles[column], column, BINDING_MEASURE_TITLE, False)


def _date_column(structure) -> Any:
    dates = [c for c in structure.dimensions if c.is_date]
    if len(dates) != 1:
        return None
    return dates[0]


def people_level_row(candidate: CancellationCandidate, payload: Mapping[str, Any], body_text: str,
                     common: dict[str, Any]) -> RuntimeValue:
    """Aggregate count: measures-only query, exact measure title, nonnegative integer."""
    from metrics.catalog import VALUE_COUNT
    structure = widget_structure(payload)
    if structure is not None and any(c.is_date for c in structure.dimensions):
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="People-level cancellation is not a daily series", **common)
    try:
        titles = tuple(_measure_titles(payload))
    except BindingError:
        titles = ()
    common = {**common, "measure_titles": titles}
    result, value, binding, reason = extract_runtime_value(
        payload, body_text, candidate.discovery_label, VALUE_COUNT)
    if binding is not None and binding.basis != BINDING_MEASURE_TITLE:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Cancellation metrics require exact measure-title binding", **common)
    if result == RV_VALUE_EXTRACTED and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0):
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            binding=binding.basis if binding else None,
                            bound_measure_title=binding.title if binding else None,
                            reason="Count is not a nonnegative whole-number", **common)
    return RuntimeValue(
        candidate.metric_id, candidate.discovery_label, result, value,
        binding=binding.basis if binding else None, reason=reason,
        bound_measure_title=binding.title if binding else None, **common,
    )


def daily_series_row(candidate: CancellationCandidate, payload: Mapping[str, Any], body_text: str,
                     common: dict[str, Any]) -> RuntimeValue:
    """SOURCE daily series: exact measure, one date dimension, grain, and date semantics."""
    structure = widget_structure(payload)
    if structure is None:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Request definition is not a widget query", **common)
    date = _date_column(structure)
    if date is None:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Daily series requires exactly one date dimension", **common)
    if candidate.source_date_column and (
            date.table != candidate.source_table or date.column != candidate.source_date_column):
        return RuntimeValue(
            candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
            reason="Daily series date dimension is not the observed source column",
            date_dimension_title=date.title, date_table=date.table, date_column=date.column,
            time_grain=date.level, **common,
        )
    try:
        binding = bind_exact_measure(payload, candidate.discovery_label)
    except BindingError as exc:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason=str(exc),
                            date_dimension_title=date.title, time_grain=date.level, **common)
    grain = date.level
    summary = summarize_response(common.get("http_status"), body_text, structure, [candidate.discovery_label])
    try:
        titles = tuple(_measure_titles(payload))
    except BindingError:
        titles = ()
    common = {
        **common,
        "shape": summary.shape,
        "date_dimension_title": date.title,
        "date_table": date.table,
        "date_column": date.column,
        "time_grain": grain,
        "bound_measure_title": binding.title,
        "binding": binding.basis,
        "measure_titles": titles,
    }
    if common.get("http_status") != 200:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Daily series requires HTTP 200", **common)
    if structure.datasource_title != VERIFIED_DATASOURCE["title"]:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Datasource is not the verified MAIN datasource", **common)
    if grain != DAILY_GRAIN:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Daily series requires verified time grain 'days'", **common)
    if summary.shape == "EMPTY":
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_NOT_AVAILABLE,
                            reason="Dashboard response contains no result", **common)
    if summary.shape != SHAPE_TIME_SERIES:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason=f"Daily series response shape is {summary.shape}", **common)
    if not summary.first_period or not summary.last_period or not summary.time_points:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Date semantics/date range not established", **common)
    try:
        body = parse_json_body(body_text)
    except Exception as exc:
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason=str(exc), **common)
    if body.get("error"):
        return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                            reason="Response reports an error", **common)
    semantics = f"{summary.first_period} → {summary.last_period} ({summary.time_points} source dates)"
    return RuntimeValue(
        candidate.metric_id, candidate.discovery_label, RV_SERIES_EXTRACTED,
        reason=None, date_semantics=semantics, series_point_count=summary.time_points, **common,
    )


def extract_cancellation(candidate: CancellationCandidate, payload: Mapping[str, Any], body_text: str,
                         common: dict[str, Any]) -> RuntimeValue:
    if candidate.kind == KIND_PEOPLE_AGGREGATE:
        return people_level_row(candidate, payload, body_text, common)
    if candidate.kind == KIND_DAILY_POLICY_SERIES:
        return daily_series_row(candidate, payload, body_text, common)
    return RuntimeValue(candidate.metric_id, candidate.discovery_label, RV_VALIDATION_FAILED,
                        reason="Unknown cancellation kind", **common)


def cancellation_eligibility(row: RuntimeValue, candidate: CancellationCandidate,
                             control_passed: bool) -> str | None:
    """None when the observation is structurally eligible for a future human promotion review."""
    if not control_passed:
        return "Currently Enrolled control did not pass"
    if candidate.kind == KIND_PEOPLE_AGGREGATE:
        if row.status != RV_VALUE_EXTRACTED:
            return f"Extraction status {row.status}"
        if not isinstance(row.value, int) or isinstance(row.value, bool) or row.value < 0:
            return "Value is not a nonnegative whole-number count"
    elif candidate.kind == KIND_DAILY_POLICY_SERIES:
        if row.status != RV_SERIES_EXTRACTED:
            return f"Extraction status {row.status}"
        if not row.date_dimension_title:
            return "Daily series requires a verified date dimension"
        if candidate.source_date_column and (
                row.date_table != candidate.source_table or row.date_column != candidate.source_date_column):
            return "Daily series date dimension is not the observed source column"
        if row.time_grain != DAILY_GRAIN:
            return "Daily series requires verified time grain 'days'"
        if not row.date_semantics:
            return "Daily series requires verified date semantics"
    else:
        return "Unknown cancellation kind"
    if row.http_status != 200:
        return f"HTTP {row.http_status}"
    if not row.widget_id:
        return "Widget identity not observed"
    if not row.datasource:
        return "Datasource not observed"
    if row.datasource != VERIFIED_DATASOURCE["title"]:
        return "Datasource is not the verified MAIN datasource"
    if not row.definition_fingerprint:
        return "No definition fingerprint"
    if not row.measure_titles:
        return "Exact measure titles not observed"
    if not row.bound_measure_title or row.measure_titles.count(row.bound_measure_title) != 1:
        return "Value not bound to exactly one exact measure title"
    if row.binding != BINDING_MEASURE_TITLE:
        return "Binding is not an exact measure title"
    return None
