"""Read-only PY2026 event-date semantic validation.

Compares Sisense ``enrollment.last_update_timestamp`` against historical
``enrollment_last_update_date`` using aggregate diagnostics only.

This is not official PY2026 cancellation activity. Output is labeled
``EVENT_DATE_CANDIDATE_DIAGNOSTIC`` until semantic validation passes.

One snapshot is queried at a time. Snapshots are not unioned or summed.
Raw identifiers are not returned. Coverage year 2027 is not queried.

    python -m database.comparison.event_date_validation
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.diagnostic_numbers import COLUMNS_SQL, TABLES_SQL
from comparison.latest_state import classify_snapshot_name
from comparison.lifecycle import (
    INBOUND_MARKERS,
    PREVIOUS_PLAN_YEAR,
    QUERY_TIMEOUT_SECONDS,
    SNAPSHOT_PREFIX,
)
from comparison.models import DIAGNOSTIC, NOT_COMPARABLE
from database.comparison.field_mapping import (
    ABSENT,
    is_blocked_event_date_name,
    is_datetime_sql_type,
    select_event_date_candidate,
)
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_event_date_validation.json"
HTML_NAME = "py2026_event_date_validation.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.event_date_validation"

DIAGNOSTIC_LABEL = "EVENT_DATE_CANDIDATE_DIAGNOSTIC"
PY2027_EVENT_FIELD = "enrollment.last_update_timestamp"
HISTORICAL_CANDIDATE = "enrollment_last_update_date"

SUPPORTED = "SUPPORTED"
UNRESOLVED = "UNRESOLVED"
NOT_SUPPORTED = "NOT_SUPPORTED"

FIELD_EXISTS = "FIELD_EXISTS"
FIELD_SEMANTICS_SUPPORTED = "FIELD_SEMANTICS_SUPPORTED"
FULL_METRIC_COMPARABLE = "FULL_METRIC_COMPARABLE"

CANCELLED_STATUS = "Cancelled"
TERM_PREFIX = "Term%"
DIAGNOSTIC_LOOKBACK_DAYS = 90

SELECTED_SNAPSHOTS = (
    "Enrollments_PY2526_NOV30",
    "Enrollments_PY2526_DEC31",
    "Enrollments_PY2526_01312026",
    "Enrollments_PY2526_02272026",
)
ADJACENT_PAIRS = (
    ("Enrollments_PY2526_NOV30", "Enrollments_PY2526_DEC31"),
    ("Enrollments_PY2526_01312026", "Enrollments_PY2526_02272026"),
)


def snapshot_date_context(table_name: str) -> dict:
    """Filename date is diagnostic only. Month-day tokens do not invent a year."""
    classified = classify_snapshot_name(table_name)
    inferred = classified.get("inferred_date")
    return {
        "table": table_name,
        "filename_status": classified.get("filename_status"),
        "snapshot_date": inferred,
        "month_day": classified.get("month_day"),
        "relative_date_comparison": (
            "RUN" if inferred else "SKIPPED_NO_PROVEN_SNAPSHOT_DATE"
        ),
    }


def _require_event_column(name: str, data_type: str | None) -> None:
    if is_blocked_event_date_name(name):
        raise ValueError("event-date SQL refused identifier column")
    if not is_datetime_sql_type(data_type):
        raise ValueError("event-date SQL refused non-datetime column")


def _year_predicate(columns: dict) -> tuple[str, tuple]:
    year = columns.get("coverage_year")
    if year is None:
        return "", ()
    return f" AND {quote_identifier(year['name'])} = ?", (PREVIOUS_PLAN_YEAR,)


def _status_predicate(mode: str, status_sql: str) -> tuple[str, tuple]:
    if mode == "cancelled":
        return f" AND {status_sql} = ?", (CANCELLED_STATUS,)
    if mode == "term":
        return f" AND {status_sql} LIKE ?", (TERM_PREFIX,)
    return "", ()


def build_snapshot_profile_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    event = quote_identifier(event_name)
    year_sql, params = _year_predicate(columns)
    sql = (
        "SELECT COUNT_BIG(*) AS total_rows,\n"
        f"       COUNT_BIG(CASE WHEN {enroll} IS NOT NULL THEN 1 END) "
        "AS non_null_enrollment_keys,\n"
        f"       COUNT_BIG(DISTINCT {enroll}) AS distinct_enrollment_keys,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL THEN 1 END) "
        "AS non_null_event_dates,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NULL THEN 1 END) "
        "AS null_event_dates,\n"
        f"       MIN({event}) AS min_event_date,\n"
        f"       MAX({event}) AS max_event_date\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}"
    )
    assert_read_only(sql)
    return sql, params


def build_status_family_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(event_name)
    year_sql, year_params = _year_predicate(columns)
    status_sql, status_params = _status_predicate(
        "cancelled" if cancelled else "term", status)
    sql = (
        "SELECT COUNT_BIG(*) AS family_row_count,\n"
        f"       COUNT_BIG(DISTINCT {enroll}) AS family_distinct_enrollment_keys,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL THEN 1 END) "
        "AS family_non_null_event_dates,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NULL THEN 1 END) "
        "AS family_null_event_dates,\n"
        f"       MIN({event}) AS family_min_event_date,\n"
        f"       MAX({event}) AS family_max_event_date,\n"
        f"       COUNT_BIG(DISTINCT CAST({event} AS date)) "
        "AS family_distinct_event_dates\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def build_identifier_date_cardinality_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(event_name)
    year_sql, year_params = _year_predicate(columns)
    status_sql, status_params = _status_predicate(
        "cancelled" if cancelled else "term", status)
    sql = (
        "SELECT COUNT_BIG(CASE WHEN distinct_event_dates = 1 THEN 1 END) "
        "AS one_date_enrollment_keys,\n"
        "       COUNT_BIG(CASE WHEN distinct_event_dates > 1 THEN 1 END) "
        "AS multi_date_enrollment_keys,\n"
        "       COUNT_BIG(CASE WHEN distinct_event_dates = 0 THEN 1 END) "
        "AS no_date_enrollment_keys,\n"
        "       COUNT_BIG(*) AS enrollment_keys_in_family\n"
        "FROM (\n"
        f"    SELECT {enroll} AS enrollment_key,\n"
        f"           COUNT(DISTINCT CAST({event} AS date)) AS distinct_event_dates\n"
        f"    FROM {qualified}\n"
        f"    WHERE {enroll} IS NOT NULL"
        f"{year_sql}{status_sql}\n"
        f"    GROUP BY {enroll}\n"
        ") AS per_identifier"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def build_snapshot_relative_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool, snapshot_date: str,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(event_name)
    year_sql, year_params = _year_predicate(columns)
    status_sql, status_params = _status_predicate(
        "cancelled" if cancelled else "term", status)
    date_expr = f"CAST({event} AS date)"
    sql = (
        f"SELECT COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} > ? "
        "THEN 1 END) AS event_dates_after_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} < ? "
        "THEN 1 END) AS event_dates_before_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} = ? "
        "THEN 1 END) AS event_dates_on_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} < ? "
        f"AND DATEDIFF(day, {date_expr}, ?) > ? THEN 1 END) "
        "AS event_dates_more_than_lookback_days_before_snapshot\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    params = (
        snapshot_date, snapshot_date, snapshot_date,
        snapshot_date, snapshot_date, DIAGNOSTIC_LOOKBACK_DAYS,
    ) + year_params + status_params
    return sql, params


def build_daily_distribution_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None,
) -> tuple[str, tuple]:
    """SQL Server CAST-to-date distribution. Not official cancellation activity."""
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(event_name)
    year_sql, params = _year_predicate(columns)
    family = (
        f"CASE WHEN {status} = '{CANCELLED_STATUS}' THEN 'Cancelled' "
        f"WHEN {status} LIKE '{TERM_PREFIX}' THEN 'Terminated' "
        "END"
    )
    sql = (
        f"SELECT CAST({event} AS date) AS event_calendar_date,\n"
        f"       {family} AS status_family,\n"
        "       COUNT_BIG(*) AS diagnostic_row_count,\n"
        f"       COUNT_BIG(DISTINCT {enroll}) AS diagnostic_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        f"WHERE ({status} = ? OR {status} LIKE ?)"
        f"{year_sql}\n"
        f"GROUP BY CAST({event} AS date), {family}\n"
        "ORDER BY event_calendar_date, status_family"
    )
    assert_read_only(sql)
    return sql, (CANCELLED_STATUS, TERM_PREFIX) + params


def build_cross_snapshot_sql(
    left_qualified: str, right_qualified: str, columns: dict,
    event_name: str, event_type: str | None, *, mode: str = "all",
) -> tuple[str, tuple]:
    """Distinct (enrollment, status, event-date) triples. Not an activity sum."""
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(event_name)
    year_sql, year_params = _year_predicate(columns)
    status_sql, status_params = _status_predicate(mode, status)
    select_list = (
        f"SELECT DISTINCT {enroll} AS enrollment_key, "
        f"{status} AS status_value, {event} AS event_value"
    )
    earlier = (
        f"{select_list}\n"
        f"    FROM {left_qualified}\n"
        f"    WHERE {enroll} IS NOT NULL AND {event} IS NOT NULL"
        f"{year_sql}{status_sql}"
    )
    later = (
        f"{select_list}\n"
        f"    FROM {right_qualified}\n"
        f"    WHERE {enroll} IS NOT NULL AND {event} IS NOT NULL"
        f"{year_sql}{status_sql}"
    )
    sql = (
        "WITH earlier AS (\n"
        f"    {earlier}\n"
        "), later AS (\n"
        f"    {later}\n"
        ")\n"
        "SELECT\n"
        "    (SELECT COUNT_BIG(*) FROM ("
        "SELECT * FROM earlier INTERSECT SELECT * FROM later"
        ") AS both_rows) AS present_in_both,\n"
        "    (SELECT COUNT_BIG(*) FROM ("
        "SELECT * FROM earlier EXCEPT SELECT * FROM later"
        ") AS earlier_only) AS only_earlier_snapshot,\n"
        "    (SELECT COUNT_BIG(*) FROM ("
        "SELECT * FROM later EXCEPT SELECT * FROM earlier"
        ") AS later_only) AS only_later_snapshot"
    )
    assert_read_only(sql)
    return sql, year_params + status_params + year_params + status_params


def decide_event_date_semantics(candidate: dict, diagnostics: dict) -> dict:
    """Date presence is not event semantics. This command never auto-assigns SUPPORTED."""
    field_exists = bool(candidate.get("py2026_candidate")) and bool(
        candidate.get("datetime_compatible"))
    if not field_exists:
        decision = NOT_SUPPORTED
        behavior = "NOT_AN_EVENT_DATE_CANDIDATE"
        confidence = "NONE"
        reason = candidate.get("reason") or (
            "No date/time-compatible EVENT_DATE candidate was selected."
        )
    else:
        decision = UNRESOLVED
        behavior = "UNPROVEN_EVENT_VS_RECORD_UPDATE"
        confidence = "LOW"
        reason = (
            "The column exists and has a date/time SQL datatype. "
            "That is not proof it is a cancellation/termination event timestamp "
            "rather than a generic record-update timestamp. "
            "SUPPORTED is not assigned automatically."
        )
    cancelled = diagnostics.get("cancelled_coverage")
    terminated = diagnostics.get("terminated_coverage")
    return {
        "candidate_field": candidate.get("py2026_candidate"),
        "sql_datatype": candidate.get("sql_datatype"),
        "null_rate": diagnostics.get("null_rate"),
        "date_range": diagnostics.get("date_range"),
        "cancelled_coverage": cancelled,
        "terminated_coverage": terminated,
        "multi_date_identifier_rate": diagnostics.get("multi_date_identifier_rate"),
        "future_date_anomaly_count": diagnostics.get("future_date_anomaly_count"),
        "cross_snapshot_repeat_rate": diagnostics.get("cross_snapshot_repeat_rate"),
        "event_behavior_status": behavior,
        "confidence": confidence,
        "decision": decision,
        "reason": reason,
        "states": {
            FIELD_EXISTS: field_exists,
            FIELD_SEMANTICS_SUPPORTED: decision == SUPPORTED,
            FULL_METRIC_COMPARABLE: False,
        },
    }


def cancelled_policies_checks(*, identifier_present: bool, cancelled_observed: bool,
                              event_decision: str, coverage_year_present: bool) -> list[dict]:
    event_status = (
        "PASS" if event_decision == SUPPORTED
        else "FAIL" if event_decision == NOT_SUPPORTED
        else "UNRESOLVED"
    )
    return [
        _check(
            "enrollment_identifier",
            "PASS" if identifier_present else "FAIL",
            "enrollment_id is present." if identifier_present else "enrollment_id is absent.",
        ),
        _check(
            "count_semantics",
            "UNRESOLVED",
            "Sisense COUNT(enrollment.id) is not proven equivalent to historical COUNT or COUNT DISTINCT.",
        ),
        _check(
            "status_cancelled",
            "PASS" if cancelled_observed else "UNRESOLVED",
            "Exact Cancelled. Canceled is not treated as Cancelled.",
        ),
        _check(
            "event_date_semantics",
            event_status,
            "Event-date semantics require a separate SUPPORTED decision. Date presence is not enough.",
        ),
        _check(
            "coverage_year",
            "UNRESOLVED",
            (
                "coverage_year = 2026 is applied when the column exists. "
                "Equivalence to Sisense ssap_applications.coverage_year is unproven."
                if coverage_year_present else
                "coverage_year column was not present; year filter was not applied."
            ),
        ),
        _check(
            "relevant_filters",
            "UNRESOLVED",
            "Verified Currently Enrolled filters are not applied to this activity diagnostic.",
        ),
        _check(
            "aggregation",
            "UNRESOLVED",
            "One snapshot at a time. Snapshots are not summed. Daily grain is diagnostic only.",
        ),
    ]


def terminated_policies_checks(*, identifier_present: bool, term_observed: bool,
                               event_decision: str, coverage_year_present: bool) -> list[dict]:
    event_status = (
        "PASS" if event_decision == SUPPORTED
        else "FAIL" if event_decision == NOT_SUPPORTED
        else "UNRESOLVED"
    )
    return [
        _check(
            "enrollment_identifier",
            "PASS" if identifier_present else "FAIL",
            "enrollment_id is present." if identifier_present else "enrollment_id is absent.",
        ),
        _check(
            "count_semantics",
            "UNRESOLVED",
            "Sisense COUNT(enrollment.id) is not proven equivalent to historical COUNT or COUNT DISTINCT.",
        ),
        _check(
            "status_term_prefix",
            "PASS" if term_observed else "UNRESOLVED",
            "Terminated family uses LIKE 'Term%'. Exact member mapping is unproven.",
        ),
        _check(
            "event_date_semantics",
            event_status,
            "Event-date semantics require a separate SUPPORTED decision. Date presence is not enough.",
        ),
        _check(
            "coverage_year",
            "UNRESOLVED",
            (
                "coverage_year = 2026 is applied when the column exists. "
                "Equivalence to Sisense ssap_applications.coverage_year is unproven."
                if coverage_year_present else
                "coverage_year column was not present; year filter was not applied."
            ),
        ),
        _check(
            "relevant_filters",
            "UNRESOLVED",
            "Verified Currently Enrolled filters are not applied to this activity diagnostic.",
        ),
        _check(
            "aggregation",
            "UNRESOLVED",
            "One snapshot at a time. Snapshots are not summed. Daily grain is diagnostic only.",
        ),
    ]


def collect_evidence(client) -> dict:
    failures = []
    tables = _list_snapshots(client, failures)
    for item in tables:
        columns = _columns(client, failures, item)
        item["columns"] = columns
        item["event_date_candidate"] = select_event_date_candidate(columns)
        item["snapshot_date"] = snapshot_date_context(item["table"])
    overview = [_snapshot_overview(client, failures, item) for item in tables]
    selected_names = {name.lower() for name in SELECTED_SNAPSHOTS}
    selected = [item for item in tables if item["table"].lower() in selected_names]
    missing_selected = [
        name for name in SELECTED_SNAPSHOTS
        if name.lower() not in {item["table"].lower() for item in tables}
    ]
    families = []
    behavior = []
    daily = []
    for item in selected:
        families.append(_status_families(client, failures, item))
        behavior.append(_event_behavior(client, failures, item))
        daily.extend(_daily_distribution(client, failures, item))
    by_name = {item["table"].lower(): item for item in tables}
    pairs = []
    for left_name, right_name in ADJACENT_PAIRS:
        left = by_name.get(left_name.lower())
        right = by_name.get(right_name.lower())
        if left is None or right is None:
            pairs.append({
                "earlier": left_name,
                "later": right_name,
                "status": "SKIPPED_TABLE_ABSENT",
                "present_in_both": None,
                "only_earlier_snapshot": None,
                "only_later_snapshot": None,
            })
            continue
        pairs.append(_cross_snapshot(client, failures, left, right))
    return {
        "tables": tables,
        "overview": overview,
        "selected_snapshots": [item["table"] for item in selected],
        "missing_selected_snapshots": missing_selected,
        "status_families": families,
        "event_behavior": behavior,
        "daily_distribution": daily,
        "cross_snapshot": pairs,
        "failures": failures,
    }


def build_report(evidence: dict) -> dict:
    tables = evidence.get("tables") or []
    chosen_columns = {}
    for item in tables:
        if item.get("table") in (evidence.get("selected_snapshots") or []) and item.get("columns"):
            chosen_columns = item["columns"]
            break
    if not chosen_columns and tables:
        chosen_columns = tables[0].get("columns") or {}
    candidate = select_event_date_candidate(chosen_columns)
    if candidate.get("py2026_candidate") is None:
        for item in tables:
            selected = item.get("event_date_candidate") or {}
            if selected.get("py2026_candidate"):
                candidate = selected
                chosen_columns = item.get("columns") or {}
                break
    diagnostics = _aggregate_diagnostics(evidence)
    evidence_object = decide_event_date_semantics(candidate, diagnostics)
    identifier_present = "enrollment_id" in chosen_columns
    coverage_year_present = "coverage_year" in chosen_columns
    cancelled_observed = _family_observed(evidence, "Cancelled")
    term_observed = _family_observed(evidence, "Terminated")
    event_decision = evidence_object["decision"]
    cancelled_checks = cancelled_policies_checks(
        identifier_present=identifier_present,
        cancelled_observed=cancelled_observed,
        event_decision=event_decision,
        coverage_year_present=coverage_year_present,
    )
    terminated_checks = terminated_policies_checks(
        identifier_present=identifier_present,
        term_observed=term_observed,
        event_decision=event_decision,
        coverage_year_present=coverage_year_present,
    )
    cancelled_metric = _metric_status(
        "cancelled_policies", "Cancelled Policies", cancelled_checks, identifier_present)
    terminated_metric = _metric_status(
        "terminated_policies", "Terminated Policies", terminated_checks, identifier_present)
    report = {
        "report_kind": "PY2026_EVENT_DATE_VALIDATION",
        "label": DIAGNOSTIC_LABEL,
        "not_official_py2026_cancellation_activity": True,
        "queries_py2027_enrollment": False,
        "visualization_created": False,
        "py2027_field": PY2027_EVENT_FIELD,
        "historical_candidate_name": HISTORICAL_CANDIDATE,
        "states": {
            FIELD_EXISTS: evidence_object["states"][FIELD_EXISTS],
            FIELD_SEMANTICS_SUPPORTED: False,
            FULL_METRIC_COMPARABLE: False,
            "note": (
                "These are three different states. A field can exist without supported "
                "event semantics. Supported event semantics would still not make the "
                "full Cancelled or Terminated metric COMPARABLE."
            ),
        },
        "event_date_candidate": candidate,
        "event_date_evidence": evidence_object,
        "snapshot_overview": evidence.get("overview") or [],
        "selected_snapshots": evidence.get("selected_snapshots") or [],
        "missing_selected_snapshots": evidence.get("missing_selected_snapshots") or [],
        "status_families": evidence.get("status_families") or [],
        "event_behavior": evidence.get("event_behavior") or [],
        "cross_snapshot": evidence.get("cross_snapshot") or [],
        "daily_distribution": {
            "label": DIAGNOSTIC_LABEL,
            "not_official_activity": True,
            "rows": evidence.get("daily_distribution") or [],
        },
        "lookback_bin": {
            "days": DIAGNOSTIC_LOOKBACK_DAYS,
            "note": (
                "Descriptive reporting bin for dates before the filename-inferred "
                "snapshot date. Not a business definition of far-before."
            ),
        },
        "cancelled_policies": cancelled_metric,
        "terminated_policies": terminated_metric,
        "out_of_scope_this_phase": {
            "currently_enrolled_people": "unchanged",
            "new_active_passive": "unchanged",
            "applications": "unchanged",
            "pending_people": "unchanged",
        },
        "activity_extractor": {
            "still_requires_last_update_timestamp_exact_name": True,
            "enrollment_last_update_date_not_auto_promoted": True,
            "must_use_one_snapshot_unless_dedup_proven": True,
        },
        "comparable": [],
        "failures": evidence.get("failures") or [],
        "command": COMMAND,
    }
    if cancelled_metric["comparison_status"] == "COMPARABLE" or terminated_metric["comparison_status"] == "COMPARABLE":
        raise AssertionError("Event-date validation must not assign COMPARABLE")
    guard_no_pii(report)
    return report


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def render_html(report: dict) -> str:
    states = report.get("states") or {}
    evidence = report.get("event_date_evidence") or {}
    overview = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td>"
        f"<td>{_e(item.get('total_rows'))}</td>"
        f"<td>{_e(item.get('non_null_enrollment_keys'))}</td>"
        f"<td>{_e(item.get('distinct_enrollment_keys'))}</td>"
        f"<td>{_e(item.get('non_null_event_dates'))}</td>"
        f"<td>{_e(item.get('null_event_dates'))}</td>"
        f"<td>{_e(item.get('null_percentage'))}</td>"
        f"<td>{_e(item.get('min_event_date'))}</td>"
        f"<td>{_e(item.get('max_event_date'))}</td>"
        f"<td>{_e(item.get('skip_reason'))}</td>"
        "</tr>"
        for item in report.get("snapshot_overview") or []
    )
    families = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td>"
        f"<td>{_e(family.get('status_family'))}</td>"
        f"<td>{_e(family.get('family_row_count'))}</td>"
        f"<td>{_e(family.get('family_distinct_enrollment_keys'))}</td>"
        f"<td>{_e(family.get('family_non_null_event_dates'))}</td>"
        f"<td>{_e(family.get('family_null_event_dates'))}</td>"
        f"<td>{_e(family.get('family_min_event_date'))}</td>"
        f"<td>{_e(family.get('family_max_event_date'))}</td>"
        f"<td>{_e(family.get('family_distinct_event_dates'))}</td>"
        "</tr>"
        for item in report.get("status_families") or []
        for family in item.get("families") or []
    )
    behavior = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td>"
        f"<td>{_e(family.get('status_family'))}</td>"
        f"<td>{_e(family.get('one_date_enrollment_keys'))}</td>"
        f"<td>{_e(family.get('multi_date_enrollment_keys'))}</td>"
        f"<td>{_e(family.get('no_date_enrollment_keys'))}</td>"
        f"<td>{_e((family.get('relative') or {}).get('event_dates_after_snapshot'))}</td>"
        f"<td>{_e((family.get('relative') or {}).get('event_dates_before_snapshot'))}</td>"
        f"<td>{_e((family.get('relative') or {}).get('event_dates_more_than_lookback_days_before_snapshot'))}</td>"
        f"<td>{_e((family.get('relative') or {}).get('skip_reason'))}</td>"
        "</tr>"
        for item in report.get("event_behavior") or []
        for family in item.get("families") or []
    )
    pairs = "".join(
        "<tr>"
        f"<td>{_e(item.get('earlier'))}</td>"
        f"<td>{_e(item.get('later'))}</td>"
        f"<td>{_e(item.get('mode'))}</td>"
        f"<td>{_e(item.get('present_in_both'))}</td>"
        f"<td>{_e(item.get('only_earlier_snapshot'))}</td>"
        f"<td>{_e(item.get('only_later_snapshot'))}</td>"
        f"<td>{_e(item.get('repeat_rate'))}</td>"
        f"<td>{_e(item.get('status'))}</td>"
        "</tr>"
        for item in report.get("cross_snapshot") or []
        for item in (item.get("modes") or [item])
    )
    daily = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td>"
        f"<td>{_e(item.get('event_calendar_date'))}</td>"
        f"<td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('diagnostic_row_count'))}</td>"
        f"<td>{_e(item.get('diagnostic_distinct_enrollment_keys'))}</td>"
        f"<td>{_e(item.get('label'))}</td>"
        "</tr>"
        for item in (report.get("daily_distribution") or {}).get("rows") or []
    )
    cancelled_rows = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in (report.get("cancelled_policies") or {}).get("checks") or []
    )
    terminated_rows = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in (report.get("terminated_policies") or {}).get("checks") or []
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 event-date validation</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:1200px}"
        "table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#f4f7fb}"
        ".state{padding:.8rem 1rem;margin-bottom:1rem;border:1px solid #c5d0dc;background:#f7fafc}</style>"
        "</head><body>"
        "<h1>PY2026 event-date validation</h1>"
        f"<p>Label: {_e(DIAGNOSTIC_LABEL)}. Not official PY2026 cancellation activity. "
        "No visualization. NULL is not zero.</p>"
        "<div class=\"state\">"
        "<h2>Three different states</h2>"
        f"<p><strong>{FIELD_EXISTS}:</strong> {_e(states.get(FIELD_EXISTS))}</p>"
        f"<p><strong>{FIELD_SEMANTICS_SUPPORTED}:</strong> {_e(states.get(FIELD_SEMANTICS_SUPPORTED))}</p>"
        f"<p><strong>{FULL_METRIC_COMPARABLE}:</strong> {_e(states.get(FULL_METRIC_COMPARABLE))}</p>"
        f"<p>{_e(states.get('note'))}</p>"
        "</div>"
        "<h2>Evidence object</h2>"
        f"<p>decision={_e(evidence.get('decision'))}; "
        f"event_behavior_status={_e(evidence.get('event_behavior_status'))}; "
        f"confidence={_e(evidence.get('confidence'))}</p>"
        f"<p>{_e(evidence.get('reason'))}</p>"
        f"<p>{_e(evidence)}</p>"
        "<h2>Snapshot overview</h2>"
        "<p>One snapshot per query. Tables are not unioned.</p>"
        "<table><thead><tr><th>Table</th><th>Total rows</th><th>Non-null enrollment keys</th>"
        "<th>Distinct enrollment keys</th><th>Non-null event date</th><th>Null event date</th>"
        "<th>Null percentage</th><th>Min event date</th><th>Max event date</th>"
        "<th>Skip reason</th></tr></thead>"
        f"<tbody>{overview}</tbody></table>"
        "<h2>Cancelled and Terminated families</h2>"
        "<table><thead><tr><th>Table</th><th>Status family</th><th>Row count</th>"
        "<th>Distinct enrollment keys</th><th>Non-null event date</th><th>Null event date</th>"
        "<th>Min event date</th><th>Max event date</th><th>Distinct event dates</th>"
        "</tr></thead>"
        f"<tbody>{families}</tbody></table>"
        "<h2>Event behavior</h2>"
        "<table><thead><tr><th>Table</th><th>Status family</th><th>One distinct date</th>"
        "<th>Multiple distinct dates</th><th>No date</th><th>After snapshot date</th>"
        "<th>Before snapshot date</th><th>Lookback bin</th><th>Relative skip</th>"
        "</tr></thead>"
        f"<tbody>{behavior}</tbody></table>"
        "<h2>Cross-snapshot duplication</h2>"
        "<p>Same enrollment key + status + event date. This is not activity.</p>"
        "<table><thead><tr><th>Earlier</th><th>Later</th><th>Mode</th><th>Present in both</th>"
        "<th>Only earlier</th><th>Only later</th><th>Repeat rate</th><th>Status</th>"
        "</tr></thead>"
        f"<tbody>{pairs}</tbody></table>"
        f"<h2>{_e(DIAGNOSTIC_LABEL)} daily distribution</h2>"
        "<p>Not official PY2026 cancellation activity.</p>"
        "<table><thead><tr><th>Table</th><th>Calendar date</th><th>Status family</th>"
        "<th>Row count</th><th>Distinct enrollment keys</th><th>Label</th></tr></thead>"
        f"<tbody>{daily}</tbody></table>"
        "<h2>Cancelled Policies checks</h2>"
        "<p>Independent of Terminated. FIELD SEMANTICS SUPPORTED does not imply FULL METRIC COMPARABLE.</p>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{cancelled_rows}</tbody></table>"
        "<h2>Terminated Policies checks</h2>"
        "<p>Independent of Cancelled. FIELD SEMANTICS SUPPORTED does not imply FULL METRIC COMPARABLE.</p>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{terminated_rows}</tbody></table>"
        "</body></html>"
    )


def print_summary(report: dict) -> None:
    evidence = report.get("event_date_evidence") or {}
    states = report.get("states") or {}
    candidate = report.get("event_date_candidate") or {}
    print("PHASE 5B EVENT-DATE VALIDATION")
    print(f"Label: {DIAGNOSTIC_LABEL}")
    print(f"FIELD EXISTS: {states.get(FIELD_EXISTS)}")
    print(f"FIELD SEMANTICS SUPPORTED: {states.get(FIELD_SEMANTICS_SUPPORTED)}")
    print(f"FULL METRIC COMPARABLE: {states.get(FULL_METRIC_COMPARABLE)}")
    print(
        "PY2026 event-date candidate: "
        f"{candidate.get('py2026_candidate')} "
        f"({candidate.get('confidence')}, {candidate.get('sql_datatype')})"
    )
    print(f"decision: {evidence.get('decision')}")
    print(f"event_behavior_status: {evidence.get('event_behavior_status')}")
    cancelled = report.get("cancelled_policies") or {}
    terminated = report.get("terminated_policies") or {}
    print(f"Cancelled Policies: {cancelled.get('comparison_status')}")
    print(f"Terminated Policies: {terminated.get('comparison_status')}")
    print("Currently Enrolled / New-Active-Passive / Applications / Pending: unchanged")


def main() -> int:
    evidence = {
        "tables": [], "overview": [], "selected_snapshots": [],
        "missing_selected_snapshots": [], "status_families": [],
        "event_behavior": [], "daily_distribution": [],
        "cross_snapshot": [], "failures": [],
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            client = DatabaseClient(connection)
            evidence = collect_evidence(client)
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(evidence)
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def _metric_status(metric_id: str, metric_name: str, checks: list[dict],
                   identifier_present: bool) -> dict:
    status = DIAGNOSTIC if identifier_present else NOT_COMPARABLE
    return {
        "metric_id": metric_id,
        "metric_name": metric_name,
        "comparison_status": status,
        "full_metric_comparable": False,
        "reason": (
            "Independent semantic checks are not all PASS. "
            "FIELD EXISTS and even FIELD SEMANTICS SUPPORTED would not make this COMPARABLE."
        ),
        "checks": checks,
    }


def _list_snapshots(client, failures) -> list[dict]:
    result = _run(client, failures, "snapshot tables", TABLES_SQL, None)
    if result is None:
        return []
    rows = []
    for schema_name, table_name, table_type in result.rows:
        name = str(table_name)
        if not name.lower().startswith(SNAPSHOT_PREFIX.lower()):
            continue
        if any(marker in name.lower() for marker in INBOUND_MARKERS):
            continue
        rows.append({
            "schema_name": str(schema_name),
            "table": name,
            "table_type": str(table_type or ""),
        })
    return rows


def _columns(client, failures, item) -> dict:
    result = _run(
        client, failures, f"columns {item['table']}", COLUMNS_SQL,
        (item["schema_name"], item["table"]),
    )
    if result is None:
        return {}
    return {
        str(name).lower(): {"name": str(name), "data_type": str(dtype or "")}
        for name, dtype in result.rows if name
    }


def _usable(item: dict) -> tuple[bool, str | None, dict]:
    columns = item.get("columns") or {}
    candidate = item.get("event_date_candidate") or select_event_date_candidate(columns)
    if "enrollment_id" not in columns:
        return False, "enrollment_id absent", candidate
    if "enrollment_status_description" not in columns:
        return False, "enrollment_status_description absent", candidate
    if "coverage_year" not in columns:
        return False, "coverage_year absent", candidate
    if not candidate.get("py2026_candidate"):
        return False, "no datetime event-date candidate", candidate
    return True, None, candidate


def _snapshot_overview(client, failures, item) -> dict:
    context = item.get("snapshot_date") or snapshot_date_context(item["table"])
    usable, reason, candidate = _usable(item)
    row = {
        "table": item["table"],
        "snapshot_date": context,
        "event_date_candidate": candidate.get("py2026_candidate"),
        "sql_datatype": candidate.get("sql_datatype"),
        "total_rows": None,
        "non_null_enrollment_keys": None,
        "distinct_enrollment_keys": None,
        "non_null_event_dates": None,
        "null_event_dates": None,
        "null_percentage": None,
        "min_event_date": None,
        "max_event_date": None,
        "skip_reason": reason,
    }
    if not usable:
        return row
    sql, params = build_snapshot_profile_sql(
        qualified_name(item["schema_name"], item["table"]),
        item["columns"], candidate["py2026_candidate"], candidate.get("sql_datatype"),
    )
    result = _run(client, failures, f"profile {item['table']}", sql, params)
    if result is None or result.row_count != 1:
        row["skip_reason"] = "profile query returned no row"
        return row
    mapped = _row_dict(result)
    total = _as_int(mapped.get("total_rows"))
    nulls = _as_int(mapped.get("null_event_dates"))
    row.update({
        "total_rows": total,
        "non_null_enrollment_keys": _as_int(mapped.get("non_null_enrollment_keys")),
        "distinct_enrollment_keys": _as_int(mapped.get("distinct_enrollment_keys")),
        "non_null_event_dates": _as_int(mapped.get("non_null_event_dates")),
        "null_event_dates": nulls,
        "null_percentage": _percentage(nulls, total),
        "min_event_date": _json_value(mapped.get("min_event_date")),
        "max_event_date": _json_value(mapped.get("max_event_date")),
        "skip_reason": None,
    })
    return row


def _status_families(client, failures, item) -> dict:
    usable, reason, candidate = _usable(item)
    families = []
    for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
        family = {
            "status_family": label,
            "family_row_count": None,
            "family_distinct_enrollment_keys": None,
            "family_non_null_event_dates": None,
            "family_null_event_dates": None,
            "family_min_event_date": None,
            "family_max_event_date": None,
            "family_distinct_event_dates": None,
            "skip_reason": reason,
        }
        if usable:
            sql, params = build_status_family_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate["py2026_candidate"],
                candidate.get("sql_datatype"), cancelled=cancelled,
            )
            result = _run(
                client, failures, f"{label} family {item['table']}", sql, params)
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                family.update({
                    "family_row_count": _as_int(mapped.get("family_row_count")),
                    "family_distinct_enrollment_keys": _as_int(
                        mapped.get("family_distinct_enrollment_keys")),
                    "family_non_null_event_dates": _as_int(
                        mapped.get("family_non_null_event_dates")),
                    "family_null_event_dates": _as_int(mapped.get("family_null_event_dates")),
                    "family_min_event_date": _json_value(mapped.get("family_min_event_date")),
                    "family_max_event_date": _json_value(mapped.get("family_max_event_date")),
                    "family_distinct_event_dates": _as_int(
                        mapped.get("family_distinct_event_dates")),
                    "skip_reason": None,
                })
        families.append(family)
    return {"table": item["table"], "families": families}


def _event_behavior(client, failures, item) -> dict:
    usable, reason, candidate = _usable(item)
    context = item.get("snapshot_date") or snapshot_date_context(item["table"])
    families = []
    for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
        family = {
            "status_family": label,
            "one_date_enrollment_keys": None,
            "multi_date_enrollment_keys": None,
            "no_date_enrollment_keys": None,
            "enrollment_keys_in_family": None,
            "multi_date_identifier_rate": None,
            "relative": {
                "snapshot_date": context.get("snapshot_date"),
                "filename_status": context.get("filename_status"),
                "event_dates_after_snapshot": None,
                "event_dates_before_snapshot": None,
                "event_dates_on_snapshot": None,
                "event_dates_more_than_lookback_days_before_snapshot": None,
                "lookback_days": DIAGNOSTIC_LOOKBACK_DAYS,
                "skip_reason": reason or (
                    None if context.get("snapshot_date")
                    else "SKIPPED_NO_PROVEN_SNAPSHOT_DATE"
                ),
            },
            "skip_reason": reason,
        }
        if usable:
            sql, params = build_identifier_date_cardinality_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate["py2026_candidate"],
                candidate.get("sql_datatype"), cancelled=cancelled,
            )
            result = _run(
                client, failures, f"{label} cardinality {item['table']}", sql, params)
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                one = _as_int(mapped.get("one_date_enrollment_keys"))
                multi = _as_int(mapped.get("multi_date_enrollment_keys"))
                family.update({
                    "one_date_enrollment_keys": one,
                    "multi_date_enrollment_keys": multi,
                    "no_date_enrollment_keys": _as_int(mapped.get("no_date_enrollment_keys")),
                    "enrollment_keys_in_family": _as_int(
                        mapped.get("enrollment_keys_in_family")),
                    "multi_date_identifier_rate": _rate(multi, _sum_optional(one, multi)),
                    "skip_reason": None,
                })
            if context.get("snapshot_date"):
                rel_sql, rel_params = build_snapshot_relative_sql(
                    qualified_name(item["schema_name"], item["table"]),
                    item["columns"], candidate["py2026_candidate"],
                    candidate.get("sql_datatype"), cancelled=cancelled,
                    snapshot_date=context["snapshot_date"],
                )
                rel = _run(
                    client, failures, f"{label} relative {item['table']}",
                    rel_sql, rel_params)
                if rel is not None and rel.row_count == 1:
                    mapped = _row_dict(rel)
                    family["relative"].update({
                        "event_dates_after_snapshot": _as_int(
                            mapped.get("event_dates_after_snapshot")),
                        "event_dates_before_snapshot": _as_int(
                            mapped.get("event_dates_before_snapshot")),
                        "event_dates_on_snapshot": _as_int(
                            mapped.get("event_dates_on_snapshot")),
                        "event_dates_more_than_lookback_days_before_snapshot": _as_int(
                            mapped.get("event_dates_more_than_lookback_days_before_snapshot")),
                        "skip_reason": None,
                    })
        families.append(family)
    return {"table": item["table"], "families": families}


def _daily_distribution(client, failures, item) -> list[dict]:
    usable, reason, candidate = _usable(item)
    if not usable:
        return [{
            "table": item["table"],
            "label": DIAGNOSTIC_LABEL,
            "skip_reason": reason,
            "event_calendar_date": None,
            "status_family": None,
            "diagnostic_row_count": None,
            "diagnostic_distinct_enrollment_keys": None,
        }]
    sql, params = build_daily_distribution_sql(
        qualified_name(item["schema_name"], item["table"]),
        item["columns"], candidate["py2026_candidate"], candidate.get("sql_datatype"),
    )
    result = _run(client, failures, f"daily {item['table']}", sql, params)
    if result is None:
        return [{
            "table": item["table"],
            "label": DIAGNOSTIC_LABEL,
            "skip_reason": "daily query failed",
            "event_calendar_date": None,
            "status_family": None,
            "diagnostic_row_count": None,
            "diagnostic_distinct_enrollment_keys": None,
        }]
    rows = []
    for raw in result.rows:
        mapped = dict(zip(result.columns, raw))
        rows.append({
            "table": item["table"],
            "label": DIAGNOSTIC_LABEL,
            "not_official_activity": True,
            "event_calendar_date": _json_value(mapped.get("event_calendar_date")),
            "status_family": None if mapped.get("status_family") is None
            else str(mapped.get("status_family")),
            "diagnostic_row_count": _as_int(mapped.get("diagnostic_row_count")),
            "diagnostic_distinct_enrollment_keys": _as_int(
                mapped.get("diagnostic_distinct_enrollment_keys")),
        })
    return rows


def _cross_snapshot(client, failures, left: dict, right: dict) -> dict:
    usable_left, reason_left, candidate = _usable(left)
    usable_right, reason_right, _candidate_right = _usable(right)
    event_name = candidate.get("py2026_candidate")
    event_type = candidate.get("sql_datatype")
    right_candidate = (right.get("event_date_candidate") or {}).get("py2026_candidate")
    document = {
        "earlier": left["table"],
        "later": right["table"],
        "not_activity": True,
        "modes": [],
    }
    if not usable_left or not usable_right or event_name != right_candidate:
        document["status"] = "SKIPPED"
        document["skip_reason"] = reason_left or reason_right or "event-date candidate mismatch"
        document["present_in_both"] = None
        document["only_earlier_snapshot"] = None
        document["only_later_snapshot"] = None
        return document
    for mode, label in (("all", "all"), ("cancelled", "Cancelled"), ("term", "Terminated")):
        sql, params = build_cross_snapshot_sql(
            qualified_name(left["schema_name"], left["table"]),
            qualified_name(right["schema_name"], right["table"]),
            left["columns"], event_name, event_type, mode=mode,
        )
        result = _run(
            client, failures,
            f"cross {left['table']} {right['table']} {label}", sql, params)
        row = {
            "earlier": left["table"],
            "later": right["table"],
            "mode": label,
            "present_in_both": None,
            "only_earlier_snapshot": None,
            "only_later_snapshot": None,
            "repeat_rate": None,
            "status": "RAN",
            "not_activity": True,
        }
        if result is not None and result.row_count == 1:
            mapped = _row_dict(result)
            both = _as_int(mapped.get("present_in_both"))
            earlier_only = _as_int(mapped.get("only_earlier_snapshot"))
            later_only = _as_int(mapped.get("only_later_snapshot"))
            row.update({
                "present_in_both": both,
                "only_earlier_snapshot": earlier_only,
                "only_later_snapshot": later_only,
                "repeat_rate": _rate(both, _sum_optional(both, earlier_only, later_only)),
            })
        document["modes"].append(row)
    return document


def _aggregate_diagnostics(evidence: dict) -> dict:
    overview = evidence.get("overview") or []
    dated = next((item for item in overview if item.get("total_rows") is not None), None)
    families = evidence.get("status_families") or []
    behavior = evidence.get("event_behavior") or []
    pairs = evidence.get("cross_snapshot") or []

    def _family(label: str) -> dict | None:
        for item in families:
            for family in item.get("families") or []:
                if family.get("status_family") == label and family.get("family_row_count") is not None:
                    return family
        return None

    cancelled = _family("Cancelled")
    terminated = _family("Terminated")
    cancelled_coverage = None
    if cancelled:
        cancelled_coverage = _rate(
            cancelled.get("family_non_null_event_dates"),
            cancelled.get("family_row_count"),
        )
    terminated_coverage = None
    if terminated:
        terminated_coverage = _rate(
            terminated.get("family_non_null_event_dates"),
            terminated.get("family_row_count"),
        )
    multi_rate = None
    future_count = None
    for item in behavior:
        for family in item.get("families") or []:
            if family.get("status_family") == "Cancelled" and family.get("multi_date_identifier_rate") is not None:
                if multi_rate is None:
                    multi_rate = family.get("multi_date_identifier_rate")
            relative = family.get("relative") or {}
            after = relative.get("event_dates_after_snapshot")
            if family.get("status_family") == "Cancelled" and after is not None and future_count is None:
                future_count = after
    repeat_rate = None
    for item in pairs:
        for mode in item.get("modes") or []:
            if mode.get("mode") == "all" and mode.get("repeat_rate") is not None:
                repeat_rate = mode.get("repeat_rate")
                break
        if repeat_rate is not None:
            break
    return {
        "null_rate": None if dated is None else dated.get("null_percentage"),
        "date_range": None if dated is None else {
            "min": dated.get("min_event_date"),
            "max": dated.get("max_event_date"),
            "source_table": dated.get("table"),
        },
        "cancelled_coverage": cancelled_coverage,
        "terminated_coverage": terminated_coverage,
        "multi_date_identifier_rate": multi_rate,
        "future_date_anomaly_count": future_count,
        "cross_snapshot_repeat_rate": repeat_rate,
    }


def _family_observed(evidence: dict, label: str) -> bool:
    for item in evidence.get("status_families") or []:
        for family in item.get("families") or []:
            if family.get("status_family") == label and (family.get("family_row_count") or 0) > 0:
                return True
    return False


def _row_dict(result) -> dict:
    return {str(column): value for column, value in zip(result.columns, result.rows[0])}


def _run(client, failures, step, sql, params):
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


def _as_int(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value)


def _json_value(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _percentage(part, whole):
    if part is None or whole in (None, 0):
        return None
    return round(100.0 * part / whole, 4)


def _rate(part, whole):
    if part is None or whole in (None, 0):
        return None
    return round(part / whole, 6)


def _sum_optional(*values):
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


if __name__ == "__main__":
    raise SystemExit(main())
