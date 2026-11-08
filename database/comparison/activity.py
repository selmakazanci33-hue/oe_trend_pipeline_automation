"""PY2026 cancellation/termination ACTIVITY series.

Event semantics only. Snapshot tables are not unioned. A missing Terminated
series stays missing and is never filled with zero.

This module does not send JAQL and does not query coverage_year 2027.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from comparison.lifecycle import PREVIOUS_PLAN_YEAR
from comparison.models import (
    DATE_DAILY_EVENT,
    DIAGNOSTIC,
    FAMILY_ACTIVITY,
    GRAIN_POLICY,
    DailyObservation,
    spec_by_id,
)
from comparison.monthly import monthly_aggregate
from comparison.validator import ComparisonValidationError
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.query_runner import assert_read_only

CANCELLED_STATUS = "Cancelled"
TERM_PREFIX = "Term%"
SOURCE_LABEL = "PY2026 historical snapshot (diagnostic activity; not summed across tables)"


def refuse_cross_snapshot_sum() -> None:
    raise ComparisonValidationError(
        "Cancellation records must not be summed across historical snapshot tables."
    )


def event_semantics_ready(columns: dict, *, cancelled_status_observed: bool,
                          last_update_present: bool) -> bool:
    """Require the Sisense event-date column by exact name. Substitutes are not used."""
    return bool(
        columns.get("enrollment_id")
        and columns.get("enrollment_status_description")
        and last_update_present
        and cancelled_status_observed
    )


def build_duplicate_enrollment_sql(qualified: str, enrollment_column: str,
                                   year_column: str | None) -> tuple[str, tuple]:
    enroll = quote_identifier(enrollment_column)
    year = None if year_column is None else quote_identifier(year_column)
    where = f"WHERE {enroll} IS NOT NULL"
    params: tuple = ()
    if year is not None:
        where += f"\n  AND {year} = ?"
        params = (PREVIOUS_PLAN_YEAR,)
    sql = (
        "SELECT COUNT_BIG(*) AS row_count,\n"
        f"       COUNT_BIG(DISTINCT {enroll}) AS distinct_enrollment_ids,\n"
        "       SUM(CASE WHEN dup_count > 1 THEN 1 ELSE 0 END) AS duplicate_enrollment_ids\n"
        "FROM (\n"
        f"    SELECT {enroll}, COUNT_BIG(*) AS dup_count\n"
        f"    FROM {qualified}\n"
        f"    {where}\n"
        f"    GROUP BY {enroll}\n"
        ") AS duplicates"
    )
    assert_read_only(sql)
    return sql, params


def build_status_values_sql(qualified: str, status_column: str,
                            year_column: str | None) -> tuple[str, tuple]:
    status = quote_identifier(status_column)
    year = None if year_column is None else quote_identifier(year_column)
    where = ""
    params: tuple = ()
    if year is not None:
        where = f"WHERE {year} = ?"
        params = (PREVIOUS_PLAN_YEAR,)
    sql = (
        f"SELECT CAST({status} AS varchar(256)) AS status_value,\n"
        "       COUNT_BIG(*) AS row_count\n"
        f"FROM {qualified}\n"
        f"{where}\n"
        f"GROUP BY {status}\n"
        f"ORDER BY {status}"
    )
    assert_read_only(sql)
    return sql, params


def build_null_and_range_sql(qualified: str, columns: dict) -> tuple[str, tuple]:
    year = columns.get("coverage_year")
    selects = ["COUNT_BIG(*) AS row_count"]
    for key in ("enrollment_id", "enrollment_status_description", "last_update_timestamp",
                "enrollment_last_update_date", "enrollee_id"):
        meta = columns.get(key)
        if meta is None:
            continue
        quoted = quote_identifier(meta["name"])
        selects.append(
            f"SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END) AS null_{key}"
        )
    for key in ("last_update_timestamp", "enrollment_last_update_date",
                "enrollment_create_date", "enrollment_confirmation_date"):
        meta = columns.get(key)
        if meta is None:
            continue
        quoted = quote_identifier(meta["name"])
        selects.append(f"MIN({quoted}) AS min_{key}")
        selects.append(f"MAX({quoted}) AS max_{key}")
    where = ""
    params: tuple = ()
    if year is not None:
        where = f"WHERE {quote_identifier(year['name'])} = ?"
        params = (PREVIOUS_PLAN_YEAR,)
    sql = "SELECT\n    " + ",\n    ".join(selects) + f"\nFROM {qualified}\n{where}"
    assert_read_only(sql)
    return sql, params


def build_overlap_sql(left: str, right: str, enrollment_column: str,
                      year_column: str | None) -> tuple[str, tuple]:
    enroll = quote_identifier(enrollment_column)
    year_pred = ""
    params: tuple = ()
    if year_column is not None:
        year = quote_identifier(year_column)
        year_pred = f" AND {year} = ?"
        params = (PREVIOUS_PLAN_YEAR, PREVIOUS_PLAN_YEAR)
    sql = (
        "SELECT COUNT_BIG(*) AS overlapping_enrollment_ids\n"
        "FROM (\n"
        f"    SELECT {enroll} FROM {left} WHERE {enroll} IS NOT NULL{year_pred}\n"
        "    INTERSECT\n"
        f"    SELECT {enroll} FROM {right} WHERE {enroll} IS NOT NULL{year_pred}\n"
        ") AS overlap"
    )
    assert_read_only(sql)
    return sql, params


def build_daily_activity_sql(
    qualified: str,
    columns: dict,
    *,
    cancelled: bool,
) -> tuple[str, tuple]:
    """One snapshot only. COUNT DISTINCT enrollment_id by event date."""
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(columns["last_update_timestamp"]["name"])
    year = columns.get("coverage_year")
    predicates = [
        f"{enroll} IS NOT NULL",
        f"{event} IS NOT NULL",
        f"{status} = ?" if cancelled else f"{status} LIKE ?",
    ]
    params: list = [CANCELLED_STATUS if cancelled else TERM_PREFIX]
    if year is not None:
        predicates.append(f"{quote_identifier(year['name'])} = ?")
        params.append(PREVIOUS_PLAN_YEAR)
    sql = (
        f"SELECT CAST({event} AS date) AS calendar_date,\n"
        f"       COUNT_BIG(DISTINCT {enroll}) AS value\n"
        f"FROM {qualified}\n"
        "WHERE " + "\n  AND ".join(predicates) + "\n"
        f"GROUP BY CAST({event} AS date)\n"
        "ORDER BY calendar_date"
    )
    assert_read_only(sql)
    return sql, tuple(params)


def build_identifier_cardinality_sql(qualified: str, enrollee_column: str,
                                     exchange_column: str, year_column: str | None) -> tuple[str, tuple]:
    enrollee = quote_identifier(enrollee_column)
    exchange = quote_identifier(exchange_column)
    year_pred = ""
    params: tuple = ()
    if year_column is not None:
        year_pred = f" AND {quote_identifier(year_column)} = ?"
        params = (PREVIOUS_PLAN_YEAR,)
    sql = (
        "SELECT COUNT_BIG(*) AS paired_rows,\n"
        f"       COUNT_BIG(DISTINCT {enrollee}) AS distinct_enrollee_keys,\n"
        f"       COUNT_BIG(DISTINCT {exchange}) AS distinct_exchange_keys,\n"
        f"       SUM(CASE WHEN {enrollee} IS NULL THEN 1 ELSE 0 END) AS null_enrollee_keys,\n"
        f"       SUM(CASE WHEN {exchange} IS NULL THEN 1 ELSE 0 END) AS null_exchange_keys,\n"
        "       (SELECT COUNT_BIG(*) FROM ("
        f"SELECT {enrollee} FROM {qualified} "
        f"WHERE {enrollee} IS NOT NULL AND {exchange} IS NOT NULL{year_pred} "
        f"GROUP BY {enrollee} HAVING COUNT(DISTINCT {exchange}) > 1"
        ") AS enrollee_fanout) AS enrollee_to_many_exchange,\n"
        "       (SELECT COUNT_BIG(*) FROM ("
        f"SELECT {exchange} FROM {qualified} "
        f"WHERE {enrollee} IS NOT NULL AND {exchange} IS NOT NULL{year_pred} "
        f"GROUP BY {exchange} HAVING COUNT(DISTINCT {enrollee}) > 1"
        ") AS exchange_fanout) AS exchange_to_many_enrollee\n"
        f"FROM {qualified}\n"
        f"WHERE 1 = 1{year_pred}"
    )
    assert_read_only(sql)
    extra = ()
    if year_column is not None:
        extra = (PREVIOUS_PLAN_YEAR, PREVIOUS_PLAN_YEAR, PREVIOUS_PLAN_YEAR)
    return sql, extra


def build_people_cancellation_sql(qualified: str, person_column: str, status_column: str,
                                  year_column: str | None) -> tuple[str, tuple]:
    """Diagnostic person-grain counts. Not policy activity. Not COMPARABLE."""
    person = quote_identifier(person_column)
    status = quote_identifier(status_column)
    year_pred = ""
    params: list = [CANCELLED_STATUS, "Enrolled", "Pending", "Terminated"]
    if year_column is not None:
        year_pred = f" AND {quote_identifier(year_column)} = ?"
        params.append(PREVIOUS_PLAN_YEAR)
    sql = (
        "WITH flags AS (\n"
        f"    SELECT {person} AS person_key,\n"
        f"           MAX(CASE WHEN {status} = ? THEN 1 ELSE 0 END) AS has_cancelled,\n"
        f"           MAX(CASE WHEN {status} IN (?, ?, ?) THEN 1 ELSE 0 END) AS has_enrolled_population\n"
        f"    FROM {qualified}\n"
        f"    WHERE {person} IS NOT NULL{year_pred}\n"
        f"    GROUP BY {person}\n"
        ")\n"
        "SELECT SUM(CASE WHEN has_cancelled = 1 AND has_enrolled_population = 1 THEN 1 ELSE 0 END)"
        " AS enrolled_cancelled_people,\n"
        "       SUM(CASE WHEN has_cancelled = 1 AND has_enrolled_population = 0 THEN 1 ELSE 0 END)"
        " AS cancelled_not_enrolled_people,\n"
        "       SUM(CASE WHEN has_cancelled = 1 THEN 1 ELSE 0 END) AS people_with_cancelled,\n"
        "       COUNT_BIG(*) AS people_in_scope\n"
        "FROM flags"
    )
    assert_read_only(sql)
    return sql, tuple(params)


def parse_daily_rows(rows, spec_id: str, source_table: str) -> list[DailyObservation]:
    spec = spec_by_id(spec_id)
    points = []
    for calendar_day, value in rows:
        day = _as_date(calendar_day)
        if day is None:
            continue
        points.append(DailyObservation(
            metric_name=spec.metric_name,
            plan_year=PREVIOUS_PLAN_YEAR,
            calendar_date=day,
            oe_day=None,
            value=_as_int(value),
            grain=GRAIN_POLICY,
            date_semantics=DATE_DAILY_EVENT,
            source=f"{SOURCE_LABEL}: {source_table}",
            comparison_status=DIAGNOSTIC,
            metric_family=FAMILY_ACTIVITY,
        ))
    return points


def monthly_from_daily(spec_id: str, points: list[DailyObservation], source_table: str) -> list[dict]:
    spec = spec_by_id(spec_id)
    months = sorted({(item.calendar_date.year, item.calendar_date.month) for item in points})
    rows = []
    for year, month in months:
        item = monthly_aggregate(
            spec, points, plan_year=PREVIOUS_PLAN_YEAR, year=year, month=month,
            source=f"{SOURCE_LABEL}: {source_table}", comparison_status=DIAGNOSTIC,
        )
        daily_sum = sum(p.value for p in points
                        if p.calendar_date.year == year and p.calendar_date.month == month
                        and p.value is not None)
        if any(p.value is None for p in points
               if p.calendar_date.year == year and p.calendar_date.month == month):
            daily_sum = None
        if daily_sum is not None and item.value != daily_sum:
            raise ComparisonValidationError(
                f"Monthly ACTIVITY_SUM {item.value} does not equal daily sum {daily_sum} "
                f"for {item.year_month}."
            )
        payload = item.as_dict()
        payload["daily_sum_matches"] = True
        rows.append(payload)
    return rows


def series_document(spec_id: str, points: list[DailyObservation], source_table: str) -> dict:
    monthly = monthly_from_daily(spec_id, points, source_table)
    return {
        "metric_id": spec_id,
        "metric_name": spec_by_id(spec_id).metric_name,
        "plan_year": PREVIOUS_PLAN_YEAR,
        "grain": GRAIN_POLICY,
        "date_semantics": DATE_DAILY_EVENT,
        "comparison_status": DIAGNOSTIC,
        "source_table": source_table,
        "py2027_daily_history": "PENDING",
        "daily": [item.as_dict() for item in points],
        "monthly": monthly,
    }


def _as_int(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value)


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
