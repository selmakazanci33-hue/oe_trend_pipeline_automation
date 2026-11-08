"""Monthly aggregation with two exclusive modes.

ACTIVITY metrics may SUM verified daily event counts into the month.
SNAPSHOT and lifecycle metrics use a supported month-end or latest snapshot.
Population snapshots are never summed.
"""

from __future__ import annotations

from datetime import date
from calendar import monthrange

from comparison.models import (
    AGG_ACTIVITY_SUM,
    AGG_LIFECYCLE,
    AGG_SNAPSHOT,
    FAMILY_ACTIVITY,
    FAMILY_LIFECYCLE,
    FAMILY_POPULATION,
    DailyObservation,
    MetricSpec,
    MonthlyObservation,
)
from comparison.validator import ComparisonValidationError, refuse_null_as_zero


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def sum_activity_month(points: list[DailyObservation], year: int, month: int) -> int | None:
    """SUM numeric daily event counts. An explicit NULL day keeps the month NULL."""
    selected = [
        item for item in points
        if item.calendar_date.year == year and item.calendar_date.month == month
    ]
    if not selected:
        return None
    if any(item.value is None for item in selected):
        return None
    total = 0
    for item in selected:
        number = refuse_null_as_zero(item.value)
        if number is None:
            return None
        total += number
    return total


def snapshot_month_value(points: list[DailyObservation], year: int, month: int) -> int | None:
    """Month-end point if present, otherwise the latest supported day in the month. Never a sum."""
    selected = [
        item for item in points
        if item.calendar_date.year == year and item.calendar_date.month == month
    ]
    if not selected:
        return None
    target = month_end(year, month)
    exact = [item for item in selected if item.calendar_date == target]
    chosen = exact[0] if exact else max(selected, key=lambda item: item.calendar_date)
    return refuse_null_as_zero(chosen.value)


def refuse_snapshot_sum(points: list[DailyObservation]) -> None:
    raise ComparisonValidationError(
        "Population snapshots cannot be summed to create a monthly value."
    )


def monthly_aggregate(
    spec: MetricSpec,
    points: list[DailyObservation],
    *,
    plan_year: int,
    year: int,
    month: int,
    source: str,
    comparison_status: str,
) -> MonthlyObservation:
    if spec.metric_family == FAMILY_ACTIVITY:
        method = AGG_ACTIVITY_SUM
        value = sum_activity_month(points, year, month)
        semantics = spec.date_semantics
    elif spec.metric_family in {FAMILY_POPULATION, FAMILY_LIFECYCLE}:
        method = AGG_SNAPSHOT if spec.metric_family == FAMILY_POPULATION else AGG_LIFECYCLE
        value = snapshot_month_value(points, year, month)
        semantics = spec.date_semantics
    else:
        raise ComparisonValidationError(
            f"{spec.metric_name} has no monthly aggregation method in this foundation."
        )
    return MonthlyObservation(
        metric_name=spec.metric_name,
        plan_year=plan_year,
        year_month=f"{year:04d}-{month:02d}",
        value=value,
        grain=spec.grain,
        date_semantics=semantics,
        source=source,
        comparison_status=comparison_status,
        aggregation_method=method,
        metric_family=spec.metric_family,
    )
