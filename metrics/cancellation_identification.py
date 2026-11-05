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


def _haystack(widget: WidgetDiscovery) -> str:
    parts = [widget.structure.widget_title, *(c.title for c in widget.structure.columns)]
    return " ".join(normalize_label(part) or "" for part in parts)


def _has_hint(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _exact_measure_labels(widget: WidgetDiscovery) -> tuple[str, ...]:
    titles = {normalize_label(c.title) for c in widget.structure.measures}
    matched = []
    for candidate in CANCELLATION_CANDIDATES.values():
        key = normalize_label(candidate.discovery_label)
        if key and key in titles:
            matched.append(candidate.metric_id)
    return tuple(matched)


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
                "date_level": role.date_level,
            }
            for role in s.metadata_roles
        ],
        "measure_titles": [c.title for c in s.measures],
        "dimension_titles": [c.title for c in s.dimensions],
        "has_date_dimension": any(c.is_date for c in s.dimensions),
        "date_grain": s.date_grain,
        "response_shape": None if r is None else r.shape,
        "http_status": None if r is None else r.http_status,
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
        reason = f"Exact measure title(s) for {', '.join(exact_people)}"
    elif exact_daily and dates:
        role, confidence = ROLE_DAILY, CONF_EXACT
        reason = f"Exact measure title(s) for {', '.join(exact_daily)}"
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
        reason=reason,
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
        }
        for c in candidates
    ]
