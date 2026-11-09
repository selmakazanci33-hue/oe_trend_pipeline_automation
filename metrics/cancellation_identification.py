"""Developer-only cancellation source identification from natural JAQL structure.

Nominates widgets for human review. Does not bind production metrics, does not
invent titles, and does not treat a structural candidate as VERIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from metrics.catalog import CANCELLATION_CANDIDATES, GRAIN_DAYS, KIND_DAILY_POLICY_SERIES, KIND_PEOPLE_AGGREGATE
from sisense.discovery import (
    SHAPE_MULTI_MEASURE,
    SHAPE_SINGLE,
    SHAPE_TIME_SERIES,
    DiscoveryResult,
    WidgetDiscovery,
    normalize_label,
)

ROLE_PEOPLE = "PEOPLE_CANCELLATION_CANDIDATE"
ROLE_DAILY = "DAILY_CANCELLATION_CANDIDATE"
ROLE_UNRESOLVED = "UNRESOLVED"

CONF_STRUCTURAL = "STRUCTURAL_CANDIDATE"
CONF_EXACT = "EXACT_IDENTITY_CONFIRMED"
CONF_UNRESOLVED = "UNRESOLVED"

_PEOPLE_HINTS = ("cancel",)
_DAILY_HINTS = ("cancel", "terminat")
_PEOPLE_SHAPES = {SHAPE_MULTI_MEASURE, SHAPE_SINGLE, None}


@dataclass(frozen=True)
class CancellationSourceCandidate:
    widget_id: str
    widget_title: str | None
    datasource: str | None
    measure_titles: tuple[str | None, ...]
    dimension_titles: tuple[str | None, ...]
    date_grain: str | None
    response_shape: str | None
    fingerprint: str | None
    semantic_role: str
    confidence: str
    reason: str
    column_titles: tuple[str, ...] = ()
    context_fields: tuple[str, ...] = ()


def _haystack(widget: WidgetDiscovery) -> str:
    parts = [widget.structure.widget_title]
    for column in widget.structure.columns:
        parts.extend((column.title, column.column_title))
    for role in widget.structure.metadata_roles:
        parts.append(role.column_title)
    return " ".join(normalize_label(part) or "" for part in parts)


def _label_index(widget: WidgetDiscovery) -> dict[str, tuple[str, ...]]:
    """Exact labels from ``jaql.title`` and ``columnTitle`` only.

    Physical column names, including ``enrollment_status_description``, are not labels.
    """
    found: dict[str, list[str]] = {}

    def add(text: str | None, source: str) -> None:
        key = normalize_label(text)
        if key and source not in found.setdefault(key, []):
            found[key].append(source)

    for column in widget.structure.measures:
        add(column.title, "measure title")
    for role in widget.structure.metadata_roles:
        add(role.column_title, "columnTitle")
    return {key: tuple(sources) for key, sources in found.items()}


def _context_tokens(widget: WidgetDiscovery) -> tuple[str, ...]:
    tokens: list[str] = []
    for role in widget.structure.metadata_roles:
        for field in role.context_fields:
            token = ".".join(part for part in (field.table, field.column) if part)
            if token and token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def _column_titles(widget: WidgetDiscovery) -> tuple[str, ...]:
    titles: list[str] = []
    for role in widget.structure.metadata_roles:
        if role.column_title and role.column_title not in titles:
            titles.append(role.column_title)
    return tuple(titles)


def _has_hint(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _date_identity_ok(widget: WidgetDiscovery, metric_id: str) -> bool:
    """Observed daily identities must match their own date column. Others are not inferred."""
    candidate = CANCELLATION_CANDIDATES[metric_id]
    if not candidate.source_date_column:
        return True
    dates = [column for column in widget.structure.dimensions if column.is_date]
    if len(dates) != 1:
        return False
    date = dates[0]
    return (date.table == candidate.source_table and date.column == candidate.source_date_column
            and date.level == GRAIN_DAYS)


def _exact_measure_labels(widget: WidgetDiscovery) -> tuple[str, ...]:
    labels = _label_index(widget)
    matched = []
    for candidate in CANCELLATION_CANDIDATES.values():
        key = normalize_label(candidate.discovery_label)
        if key and key in labels and _date_identity_ok(widget, candidate.metric_id):
            matched.append(candidate.metric_id)
    return tuple(matched)


def _match_basis(widget: WidgetDiscovery, metric_ids: tuple[str, ...]) -> str:
    labels = _label_index(widget)
    sources: list[str] = []
    for metric_id in metric_ids:
        key = normalize_label(CANCELLATION_CANDIDATES[metric_id].discovery_label)
        for source in labels.get(key or "", ()):
            if source not in sources:
                sources.append(source)
    return " and ".join(sources) if sources else "measure title"


def _context_suffix(widget: WidgetDiscovery) -> str:
    tokens = _context_tokens(widget)
    if not tokens:
        return ""
    return "; context fields: " + ", ".join(tokens)


def safe_inventory_row(widget: WidgetDiscovery) -> dict[str, Any]:
    """Safe structural metadata only. No bodies, filter values, secrets, or metric values."""
    s = widget.structure
    r = widget.response
    return {
        "widget_id": s.widget_id,
        "widget_title": s.widget_title,
        "datasource_id": s.datasource_id,
        "datasource_title": s.datasource_title,
        "metadata_item_count": s.metadata_item_count,
        "metadata_roles": [
            {
                "panel": role.panel,
                "kind": role.kind,
                "jaql_type": role.jaql_type,
                "title": role.title,
                "column_title": role.column_title,
                "table": role.table,
                "column": role.column,
                "date_level": role.date_level,
                "context_fields": [
                    {"table": field.table, "column": field.column} for field in role.context_fields
                ],
                "is_dashboard_filter": role.is_dashboard_filter,
                "disabled": role.disabled,
                "locked": role.locked,
                "filter_kind": role.filter_kind,
                "filter_fingerprint": role.filter_fingerprint,
                "aggregation_functions": list(role.aggregation_functions),
            }
            for role in s.metadata_roles
        ],
        "measure_titles": [c.title for c in s.measures],
        "dimension_titles": [c.title for c in s.dimensions],
        "dimension_columns": [c.column for c in s.dimensions],
        "column_titles": list(_column_titles(widget)),
        "context_fields": list(_context_tokens(widget)),
        "has_date_dimension": any(c.is_date for c in s.dimensions),
        "date_grain": s.date_grain,
        "response_shape": None if r is None else r.shape,
        "http_status": None if r is None else r.http_status,
        "observations": widget.observations,
        "definition_fingerprint": widget.definition_fingerprint,
    }


def classify_cancellation_widget(widget: WidgetDiscovery) -> CancellationSourceCandidate | None:
    """Nominate a widget for review, or None when it has no cancellation-relevant evidence."""
    s = widget.structure
    shape = None if widget.response is None else widget.response.shape
    exact = _exact_measure_labels(widget)
    haystack = _haystack(widget)
    dates = [c for c in s.dimensions if c.is_date]
    people_structure = len(s.measures) >= 2 and not dates
    daily_structure = len(dates) == 1 and s.date_grain == GRAIN_DAYS and len(s.measures) >= 2
    people_hint = _has_hint(haystack, _PEOPLE_HINTS)
    daily_hint = _has_hint(haystack, _DAILY_HINTS)
    exact_people = tuple(
        metric_id for metric_id in exact
        if CANCELLATION_CANDIDATES[metric_id].kind == KIND_PEOPLE_AGGREGATE
    )
    exact_daily = tuple(
        metric_id for metric_id in exact
        if CANCELLATION_CANDIDATES[metric_id].kind == KIND_DAILY_POLICY_SERIES
    )

    role = ROLE_UNRESOLVED
    confidence = CONF_UNRESOLVED
    reason = ""
    if exact_people and not dates:
        role, confidence = ROLE_PEOPLE, CONF_EXACT
        reason = f"Exact {_match_basis(widget, exact_people)} for {', '.join(exact_people)}{_context_suffix(widget)}"
    elif exact_daily and dates:
        role, confidence = ROLE_DAILY, CONF_EXACT
        reason = f"Exact {_match_basis(widget, exact_daily)} for {', '.join(exact_daily)}{_context_suffix(widget)}"
    elif people_structure and people_hint and (shape in _PEOPLE_SHAPES or shape is None):
        role, confidence = ROLE_PEOPLE, CONF_STRUCTURAL
        reason = "Multiple measures, no date dimension, cancellation-related title metadata"
    elif daily_structure and daily_hint and (shape == SHAPE_TIME_SERIES or shape is None):
        role, confidence = ROLE_DAILY, CONF_STRUCTURAL
        reason = "Daily date dimension, multiple measures, cancellation-related title metadata"
    elif people_hint or daily_hint:
        reason = "Cancellation-related title metadata without matching request structure"
    else:
        return None

    return CancellationSourceCandidate(
        widget_id=s.widget_id,
        widget_title=s.widget_title,
        datasource=s.datasource_title,
        measure_titles=tuple(c.title for c in s.measures),
        dimension_titles=tuple(c.title for c in s.dimensions),
        date_grain=s.date_grain,
        response_shape=shape,
        fingerprint=widget.definition_fingerprint,
        semantic_role=role,
        confidence=confidence,
        reason=reason + ("" if confidence == CONF_EXACT else _context_suffix(widget)),
        column_titles=_column_titles(widget),
        context_fields=_context_tokens(widget),
    )


def identify_cancellation_sources(result: DiscoveryResult) -> tuple[CancellationSourceCandidate, ...]:
    found = []
    for widget in result.widgets:
        candidate = classify_cancellation_widget(widget)
        if candidate is not None:
            found.append(candidate)
    return tuple(found)


def candidate_document(candidates: tuple[CancellationSourceCandidate, ...]) -> list[dict[str, Any]]:
    return [
        {
            "widget_id": c.widget_id,
            "widget_title": c.widget_title,
            "datasource": c.datasource,
            "measure_titles": list(c.measure_titles),
            "dimension_titles": list(c.dimension_titles),
            "date_grain": c.date_grain,
            "response_shape": c.response_shape,
            "definition_fingerprint": c.fingerprint,
            "semantic_role": c.semantic_role,
            "confidence": c.confidence,
            "reason": c.reason,
            "column_titles": list(c.column_titles),
            "context_fields": list(c.context_fields),
        }
        for c in candidates
    ]
