"""Daily activity comparison infrastructure.

Snapshot and lifecycle metrics are not forced into daily activity rows.
OE-day is relative to a configured anchor. A year stays UNALIGNED when no
authoritative anchor is available. Official OE start dates are not hardcoded.
"""

from __future__ import annotations

import os
from datetime import date, datetime

from comparison.models import (
    ALIGNED,
    DATE_DAILY_EVENT,
    FAMILY_ACTIVITY,
    FAMILY_POPULATION,
    NOT_COMPARABLE,
    UNALIGNED,
    DailyObservation,
    MetricSpec,
    OeAnchor,
)
from comparison.validator import ComparisonValidationError

ANCHOR_ENV = {
    2026: "OE_ANCHOR_DATE_PY2026",
    2027: "OE_ANCHOR_DATE_PY2027",
}


def parse_anchor_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def load_oe_anchor(plan_year: int, *, environ: dict | None = None, explicit: date | None = None) -> OeAnchor:
    """Read an optional OE anchor. Blank or missing leaves the year UNALIGNED."""
    if explicit is not None:
        return OeAnchor(
            plan_year=plan_year,
            anchor_date=explicit,
            alignment=ALIGNED,
            reason="Anchor supplied by the caller. Not an official hardcoded OE start.",
        )
    env = environ if environ is not None else os.environ
    key = ANCHOR_ENV.get(plan_year)
    raw = None if key is None else env.get(key)
    parsed = parse_anchor_date(raw)
    if parsed is None:
        return OeAnchor(
            plan_year=plan_year,
            anchor_date=None,
            alignment=UNALIGNED,
            reason=f"No authoritative OE anchor is configured for plan year {plan_year}.",
        )
    return OeAnchor(
        plan_year=plan_year,
        anchor_date=parsed,
        alignment=ALIGNED,
        reason=f"Anchor read from {key}. Not treated as a hardcoded official OE start.",
    )


def oe_day(calendar_day: date, anchor: OeAnchor) -> int | None:
    """Day 1 is the anchor date. Unaligned years have no OE day."""
    if anchor.anchor_date is None or anchor.alignment != ALIGNED:
        return None
    return (calendar_day - anchor.anchor_date).days + 1


def daily_point(
    spec: MetricSpec,
    *,
    plan_year: int,
    calendar_day: date,
    value: int | None,
    source: str,
    anchor: OeAnchor,
    comparison_status: str = NOT_COMPARABLE,
) -> DailyObservation:
    if spec.metric_family == FAMILY_POPULATION:
        raise ComparisonValidationError("Snapshot metrics cannot be forced into daily activity.")
    if spec.metric_family != FAMILY_ACTIVITY or spec.date_semantics != DATE_DAILY_EVENT:
        raise ComparisonValidationError(
            f"{spec.metric_name} is not a daily activity metric and cannot enter the daily model."
        )
    return DailyObservation(
        metric_name=spec.metric_name,
        plan_year=plan_year,
        calendar_date=calendar_day,
        oe_day=oe_day(calendar_day, anchor),
        value=value,
        grain=spec.grain,
        date_semantics=spec.date_semantics,
        source=source,
        comparison_status=comparison_status,
        metric_family=spec.metric_family,
        alignment=anchor.alignment,
    )


def align_by_oe_day(
    py2026: list[DailyObservation],
    py2027: list[DailyObservation],
) -> list[dict]:
    """Pair PY2026 OE Day N with PY2027 OE Day N. Calendar dates are not the join key."""
    if any(item.alignment != ALIGNED or item.oe_day is None for item in py2026 + py2027):
        return []
    left = {item.oe_day: item for item in py2026 if item.oe_day is not None}
    right = {item.oe_day: item for item in py2027 if item.oe_day is not None}
    rows = []
    for day in sorted(set(left) & set(right)):
        rows.append({
            "oe_day": day,
            "py2026": left[day].as_dict(),
            "py2027": right[day].as_dict(),
        })
    return rows
