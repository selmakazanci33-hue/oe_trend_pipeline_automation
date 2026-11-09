"""PY2026 vs PY2027 Cancelled/Terminated activity semantic bridge.

Read-only diagnostics. Does not send JAQL. Does not query coverage year 2027.
Does not union snapshots. Does not create official activity output.
Does not copy Currently Enrolled filters onto this metric.

    python -m database.comparison.activity_semantic_bridge
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import (
    OUTPUT_DIR,
    VERIFIED_PAYLOAD_FILE,
    guard_no_pii,
    interpret_payload,
)
from comparison.diagnostic_numbers import COLUMNS_SQL, TABLES_SQL
from comparison.lifecycle import (
    INBOUND_MARKERS,
    PREVIOUS_PLAN_YEAR,
    QUERY_TIMEOUT_SECONDS,
    SNAPSHOT_PREFIX,
)
from comparison.models import COMPARABLE, DIAGNOSTIC, NOT_COMPARABLE
from database.comparison.event_date_validation import (
    SELECTED_SNAPSHOTS,
    snapshot_date_context,
)
from database.comparison.field_mapping import (
    is_blocked_event_date_name,
    is_datetime_sql_type,
    select_event_date_candidate,
)
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only
from sisense.discovery import normalize_label
from sisense.status_metric_inventory import load_discovery_artifact

JSON_NAME = "py2026_activity_semantic_bridge.json"
HTML_NAME = "py2026_activity_semantic_bridge.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.activity_semantic_bridge"
DIAGNOSTIC_LABEL = "ACTIVITY_SEMANTIC_BRIDGE_DIAGNOSTIC"

WIDGET_TITLE = "Cancelled & Terminated Policies - By Day"
PY2027_EVENT_FIELD = "enrollment.last_update_timestamp"
PY2027_COUNT_FIELD = "enrollment.id"
CANCELLED_STATUS = "Cancelled"
TERM_PREFIX = "Term%"
HISTORICAL_CANDIDATE = "enrollment_last_update_date"

SUPPORTED = "SUPPORTED"
UNRESOLVED = "UNRESOLVED"
NOT_SUPPORTED = "NOT_SUPPORTED"
PASS = "PASS"
FAIL = "FAIL"

WIDGET_OWN = "WIDGET_OWN"
DASHBOARD_APPLIED = "DASHBOARD_APPLIED"
DASHBOARD_IGNORED = "DASHBOARD_IGNORED"
DISABLED = "DISABLED"

PLUS_ONE_LABEL = "POSSIBLE_BOUNDARY_OR_TIMEZONE_PATTERN"

RELEVANT_DATE_FIELDS = (
    "enrollment_last_update_date",
    "last_update_timestamp",
    "enrollment_update_date",
    "enrollment_create_date",
    "enrollment_confirmation_date",
    "benefit_effective_date",
    "benefit_end_date",
)
CURRENTLY_ENROLLED_FILTERS_NOT_COPIED = (
    "person_type_description",
    "insurance_type_description",
    "benefit_effective_date",
    "enrollee_status_description",
    "enrollment_status_description",
)


def duplicate_factor(count, distinct):
    if count is None or distinct in (None, 0):
        return None
    return round(count / distinct, 6)


def count_operator_note(factor) -> str:
    base = (
        "Sisense uses COUNT(enrollment.id), not DISTINCT. "
        "Historical extraction must not silently deduplicate."
    )
    if factor is None:
        return base
    if factor == 1:
        return (
            base
            + " COUNT and COUNT DISTINCT coincide numerically in this grouping; the operator remains COUNT."
        )
    return (
        base
        + " Multiple rows per enrollment_id exist. COUNT DISTINCT would under-count relative to Sisense COUNT."
    )


def decide_coverage_year(rows: list[dict]) -> dict:
    """Name identity is not semantic equivalence to ssap_applications.coverage_year."""
    years = []
    for item in rows:
        value = item.get("observed_coverage_year")
        if value is None:
            continue
        try:
            years.append(int(value))
        except (TypeError, ValueError):
            years.append(value)
    has_2026 = PREVIOUS_PLAN_YEAR in years
    if not rows:
        decision = NOT_SUPPORTED
        reason = "coverage_year was not inspected."
    elif not has_2026:
        decision = NOT_SUPPORTED
        reason = (
            "Selected snapshots did not show coverage_year = 2026. "
            "A PY2026 plan-year filter cannot be applied from this evidence."
        )
    else:
        decision = UNRESOLVED
        reason = (
            "Historical coverage_year = 2026 is a plausible plan-year filter analog to "
            "Sisense ssap_applications.coverage_year = 2027, but the source tables differ "
            "and name identity is not semantic equivalence."
        )
    return {
        "decision": decision,
        "reason": reason,
        "observed_years": years,
        "py2026_year_present": has_2026,
        "sisense_field": "ssap_applications.coverage_year",
        "sisense_member": "2027",
        "historical_field": "coverage_year",
        "historical_member": PREVIOUS_PLAN_YEAR,
    }


def label_plus_one_pattern(rows: list[dict]) -> str | None:
    """Report a consistent +1 day ceiling. Not proof of timezone conversion."""
    dated = [
        item for item in rows
        if item.get("slice") == "ALL" and item.get("event_on_snapshot_plus_one") is not None
    ]
    if not dated:
        return None
    after = [(item.get("event_after_snapshot_plus_one") or 0) for item in dated]
    plus_one = [(item.get("event_on_snapshot_plus_one") or 0) for item in dated]
    if all(value == 0 for value in after) and any(value > 0 for value in plus_one):
        return PLUS_ONE_LABEL
    return None


def classify_activity_filters(discovery: dict | None, columns: dict) -> dict:
    """Filters for Cancelled & Terminated Policies - By Day only.

    Currently Enrolled filters are not copied. Missing activity-widget JAQL
    leaves dashboard inheritance unresolved.
    """
    historical = {
        key: columns[key]["name"] for key in (
            "enrollment_id", "enrollment_status_description", "coverage_year",
            "enrollment_last_update_date", "last_update_timestamp",
            "person_type_description", "insurance_type_description",
            "benefit_effective_date",
        ) if key in columns
    }
    inventory = [
        {
            "sisense_field": PY2027_COUNT_FIELD,
            "role": "measure_identifier",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("enrollment_id"),
            "evidence": "Sisense COUNT(enrollment.id). Not DISTINCT.",
        },
        {
            "sisense_field": "enrollment.enrollment_status_description",
            "role": "cancelled_measure_filter",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("enrollment_status_description"),
            "evidence": "Exact member Cancelled on the Cancelled Policies measure.",
        },
        {
            "sisense_field": "enrollment.enrollment_status_description",
            "role": "terminated_measure_filter",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("enrollment_status_description"),
            "evidence": 'startsWith("Term") on the Terminated Policies measure.',
        },
        {
            "sisense_field": PY2027_EVENT_FIELD,
            "role": "date_dimension",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("last_update_timestamp") or historical.get(
                "enrollment_last_update_date"),
            "evidence": "Calendar level = days. Historical last_update_timestamp is not assumed equal.",
        },
    ]
    widget = _activity_widget(discovery)
    extra = []
    if widget:
        for role in widget.get("metadata_roles") or []:
            if (role.get("panel") or role.get("kind")) not in {"scope", "filter"}:
                continue
            extra.append(_classify_discovered_filter(role, historical))
        inventory.extend(extra)
    dashboard_unresolved = _dashboard_filters_from_currently_enrolled(historical)
    return {
        "widget_title": WIDGET_TITLE,
        "widget_id": None if widget is None else widget.get("widget_id"),
        "discovery_matched": widget is not None,
        "inventory": inventory,
        "dashboard_filters_unresolved_without_activity_jaql": dashboard_unresolved,
        "currently_enrolled_filters_not_copied": list(CURRENTLY_ENROLLED_FILTERS_NOT_COPIED),
        "note": (
            "Currently Enrolled People filters are not applied to this activity metric. "
            "Dashboard-filter inheritance is unresolved unless the activity widget JAQL is captured."
        ),
    }


def evaluate_strategies(count_rows: list[dict], plus_one_rows: list[dict],
                        coverage: dict) -> list[dict]:
    """Evaluate extraction strategies. None is activated."""
    factor_over_one = any(
        (item.get("duplicate_factor") or 0) > 1 for item in count_rows
        if item.get("grain") == "family"
    )
    plus_one = label_plus_one_pattern(plus_one_rows)
    return [
        {
            "strategy": "A",
            "name": "single sufficiently-late snapshot + event date",
            "decision": UNRESOLVED,
            "evidence_supports": False,
            "duplication_risk": (
                "Using one snapshot avoids summing repeats. Snapshot duplication still exists."
            ),
            "missing_event_risk": (
                "Events after the chosen snapshot, and the +1-day boundary pattern, can omit or shift days."
                if plus_one else
                "Events after the chosen snapshot are omitted. Which snapshot is sufficiently late is unproven."
            ),
            "can_reproduce_sisense_daily_count": False,
            "reason": (
                "Diagnostic candidate only. Event-date semantics are unproven, "
                "no PY2027 daily series is compared here, and no snapshot is proven sufficiently late."
            ),
            "plus_one_pattern": plus_one,
            "duplicate_rows_observed": factor_over_one,
        },
        {
            "strategy": "B",
            "name": "multiple snapshots with proven event-key deduplication",
            "decision": NOT_SUPPORTED,
            "evidence_supports": False,
            "duplication_risk": (
                "The same enrollment_id + status + event date repeats across snapshots. "
                "Deduplication is not proven."
            ),
            "missing_event_risk": "Dedup rules can drop real events or keep snapshot occupancy.",
            "can_reproduce_sisense_daily_count": False,
            "reason": "Snapshots must not be unioned or summed. No proven event-key dedup strategy exists.",
        },
        {
            "strategy": "C",
            "name": "snapshot delta",
            "decision": NOT_SUPPORTED,
            "evidence_supports": False,
            "duplication_risk": "Snapshot presence can be mistaken for event activity.",
            "missing_event_risk": "Rows that persist, disappear, or change status are not proven events.",
            "can_reproduce_sisense_daily_count": False,
            "reason": "Phase 5B.1 showed cross-snapshot repeats. Delta is not a proven event extractor.",
        },
        {
            "activated_strategy": None,
            "official_activity_output": False,
            "coverage_year_decision": coverage.get("decision"),
        },
    ]


def cancelled_policies_checks(**kwargs) -> list[dict]:
    return _activity_checks("Cancelled", **kwargs)


def terminated_policies_checks(**kwargs) -> list[dict]:
    return _activity_checks("Terminated", **kwargs)


def metric_from_checks(metric_id: str, metric_name: str, checks: list[dict]) -> dict:
    statuses = [item["status"] for item in checks]
    if statuses and all(status == PASS for status in statuses):
        comparison = COMPARABLE
    elif any(item["check"] == "enrollment_identifier" and item["status"] == FAIL for item in checks):
        comparison = NOT_COMPARABLE
    else:
        comparison = DIAGNOSTIC
    return {
        "metric_id": metric_id,
        "metric_name": metric_name,
        "comparison_status": comparison,
        "full_metric_comparable": comparison == COMPARABLE,
        "reason": (
            "All required checks PASS."
            if comparison == COMPARABLE else
            "COMPARABLE requires every required check to PASS. Remaining UNRESOLVED or FAIL checks block promotion."
        ),
        "checks": checks,
    }


def _require_event_column(name: str, data_type: str | None) -> None:
    if is_blocked_event_date_name(name):
        raise ValueError("event-date SQL refused identifier column")
    if not is_datetime_sql_type(data_type):
        raise ValueError("event-date SQL refused non-datetime column")


def _year_sql(columns: dict) -> tuple[str, tuple]:
    if "coverage_year" not in columns:
        return "", ()
    return f" AND {quote_identifier(columns['coverage_year']['name'])} = ?", (PREVIOUS_PLAN_YEAR,)


def _status_sql(columns: dict, mode: str) -> tuple[str, tuple]:
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    if mode == "cancelled":
        return f" AND {status} = ?", (CANCELLED_STATUS,)
    if mode == "term":
        return f" AND {status} LIKE ?", (TERM_PREFIX,)
    return "", ()


def build_count_family_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    sql = (
        "SELECT COUNT_BIG(*) AS count_star,\n"
        f"       COUNT({enroll}) AS count_enrollment_ids,\n"
        f"       COUNT(DISTINCT {enroll}) AS count_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def build_count_daily_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    event = quote_identifier(event_name)
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    sql = (
        f"SELECT CAST({event} AS date) AS event_calendar_date,\n"
        "       COUNT_BIG(*) AS count_star,\n"
        f"       COUNT({enroll}) AS count_enrollment_ids,\n"
        f"       COUNT(DISTINCT {enroll}) AS count_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        f"WHERE {event} IS NOT NULL"
        f"{year_sql}{status_sql}\n"
        f"GROUP BY CAST({event} AS date)\n"
        "ORDER BY event_calendar_date"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def build_status_value_sql(qualified: str, columns: dict) -> tuple[str, tuple]:
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    year_sql, params = _year_sql(columns)
    sql = (
        f"SELECT {status} AS observed_status_value,\n"
        "       COUNT_BIG(*) AS status_row_count,\n"
        f"       COUNT(DISTINCT {enroll}) AS status_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        f"WHERE ({status} = ? OR {status} LIKE ?)"
        f"{year_sql}\n"
        f"GROUP BY {status}\n"
        "ORDER BY observed_status_value"
    )
    assert_read_only(sql)
    return sql, (CANCELLED_STATUS, TERM_PREFIX) + params


def build_coverage_year_sql(qualified: str, columns: dict) -> tuple[str, tuple]:
    year = quote_identifier(columns["coverage_year"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    sql = (
        f"SELECT {year} AS observed_coverage_year,\n"
        "       COUNT_BIG(*) AS coverage_year_row_count,\n"
        f"       COUNT_BIG(CASE WHEN {status} = ? THEN 1 END) AS cancelled_row_count,\n"
        f"       COUNT_BIG(CASE WHEN {status} LIKE ? THEN 1 END) AS terminated_row_count\n"
        f"FROM {qualified}\n"
        f"GROUP BY {year}\n"
        "ORDER BY observed_coverage_year"
    )
    assert_read_only(sql)
    return sql, (CANCELLED_STATUS, TERM_PREFIX)


def build_plus_one_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    mode: str, snapshot_date: str,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    event = quote_identifier(event_name)
    date_expr = f"CAST({event} AS date)"
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, mode)
    sql = (
        f"SELECT COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} < ? THEN 1 END) "
        "AS event_before_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} = ? THEN 1 END) "
        "AS event_on_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} = "
        "DATEADD(day, 1, CAST(? AS date)) THEN 1 END) AS event_on_snapshot_plus_one,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} > "
        "DATEADD(day, 1, CAST(? AS date)) THEN 1 END) AS event_after_snapshot_plus_one\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    params = (snapshot_date, snapshot_date, snapshot_date, snapshot_date) + year_params + status_params
    return sql, params


def build_date_field_profile_sql(
    qualified: str, columns: dict, field_name: str, field_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(field_name, field_type)
    quoted = quote_identifier(field_name)
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    sql = (
        f"SELECT MIN({quoted}) AS date_field_min,\n"
        f"       MAX({quoted}) AS date_field_max,\n"
        f"       COUNT_BIG(CASE WHEN {quoted} IS NULL THEN 1 END) AS date_field_nulls,\n"
        f"       COUNT_BIG(*) AS date_field_rows,\n"
        f"       COUNT_BIG(DISTINCT CAST({quoted} AS date)) AS date_field_distinct_dates\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def build_date_relationship_sql(
    qualified: str, columns: dict, candidate_name: str, candidate_type: str | None,
    other_name: str, other_type: str | None, *, cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(candidate_name, candidate_type)
    _require_event_column(other_name, other_type)
    left = quote_identifier(candidate_name)
    right = quote_identifier(other_name)
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    sql = (
        f"SELECT COUNT_BIG(CASE WHEN CAST({left} AS date) = CAST({right} AS date) THEN 1 END) "
        "AS same_calendar_date_count,\n"
        f"       COUNT_BIG(CASE WHEN CAST({left} AS date) > CAST({right} AS date) THEN 1 END) "
        "AS candidate_after_other,\n"
        f"       COUNT_BIG(CASE WHEN CAST({left} AS date) < CAST({right} AS date) THEN 1 END) "
        "AS candidate_before_other,\n"
        f"       COUNT_BIG(CASE WHEN {left} IS NOT NULL AND {right} IS NULL THEN 1 END) "
        "AS other_null_when_candidate_present\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def relevant_date_fields(columns: dict) -> list[dict]:
    found = []
    for key in RELEVANT_DATE_FIELDS:
        meta = columns.get(key)
        if meta is None:
            continue
        if is_blocked_event_date_name(meta["name"]):
            continue
        if not is_datetime_sql_type(meta.get("data_type")):
            continue
        found.append({"name": meta["name"], "data_type": meta.get("data_type")})
    return found


def collect_evidence(client, discovery: dict | None = None) -> dict:
    failures = []
    tables = _list_snapshots(client, failures)
    for item in tables:
        item["columns"] = _columns(client, failures, item)
        item["event_date_candidate"] = select_event_date_candidate(item["columns"])
        item["snapshot_date"] = snapshot_date_context(item["table"])
        item["date_fields"] = relevant_date_fields(item["columns"])
    selected_names = {name.lower() for name in SELECTED_SNAPSHOTS}
    selected = [item for item in tables if item["table"].lower() in selected_names]
    count_rows = []
    status_rows = []
    coverage_rows = []
    plus_one_rows = []
    date_profiles = []
    relationships = []
    for item in selected:
        count_rows.extend(_count_semantics(client, failures, item))
        status_rows.extend(_status_values(client, failures, item))
        coverage_rows.extend(_coverage_years(client, failures, item))
        plus_one_rows.extend(_plus_one(client, failures, item))
        date_profiles.extend(_date_profiles(client, failures, item))
        relationships.extend(_date_relationships(client, failures, item))
    columns = {} if not selected else selected[0].get("columns") or {}
    return {
        "tables": tables,
        "selected_snapshots": [item["table"] for item in selected],
        "count_semantics": count_rows,
        "status_values": status_rows,
        "coverage_years": coverage_rows,
        "plus_one": plus_one_rows,
        "date_profiles": date_profiles,
        "date_relationships": relationships,
        "filter_inventory": classify_activity_filters(discovery, columns),
        "failures": failures,
    }


def build_report(evidence: dict) -> dict:
    tables = evidence.get("tables") or []
    columns = {}
    for item in tables:
        if item.get("table") in (evidence.get("selected_snapshots") or []) and item.get("columns"):
            columns = item["columns"]
            break
    candidate = select_event_date_candidate(columns)
    coverage = decide_coverage_year(evidence.get("coverage_years") or [])
    plus_one_label = label_plus_one_pattern(evidence.get("plus_one") or [])
    strategies = evaluate_strategies(
        evidence.get("count_semantics") or [],
        evidence.get("plus_one") or [],
        coverage,
    )
    filters = evidence.get("filter_inventory") or classify_activity_filters(None, columns)
    identifier_present = "enrollment_id" in columns
    cancelled_status = _status_observed(evidence.get("status_values") or [], exact_cancelled=True)
    term_values = _term_values(evidence.get("status_values") or [])
    count_ready = _count_ready(evidence.get("count_semantics") or [], "Cancelled")
    count_ready_term = _count_ready(evidence.get("count_semantics") or [], "Terminated")
    event_decision = UNRESOLVED if candidate.get("py2026_candidate") else NOT_SUPPORTED
    filter_decision = UNRESOLVED
    strategy_decision = UNRESOLVED
    cancelled_checks = cancelled_policies_checks(
        identifier_present=identifier_present,
        count_ready=count_ready,
        count_factor=_family_factor(evidence.get("count_semantics") or [], "Cancelled"),
        status_observed=cancelled_status,
        event_decision=event_decision,
        coverage_decision=coverage["decision"],
        filter_decision=filter_decision,
        strategy_decision=strategy_decision,
        term_values=term_values,
    )
    terminated_checks = terminated_policies_checks(
        identifier_present=identifier_present,
        count_ready=count_ready_term,
        count_factor=_family_factor(evidence.get("count_semantics") or [], "Terminated"),
        status_observed=bool(term_values),
        event_decision=event_decision,
        coverage_decision=coverage["decision"],
        filter_decision=filter_decision,
        strategy_decision=strategy_decision,
        term_values=term_values,
    )
    cancelled = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", cancelled_checks)
    terminated = metric_from_checks("terminated_policies_daily", "Terminated Policies", terminated_checks)
    report = {
        "report_kind": "PY2026_ACTIVITY_SEMANTIC_BRIDGE",
        "label": DIAGNOSTIC_LABEL,
        "not_official_activity": True,
        "queries_py2027_enrollment": False,
        "visualization_created": False,
        "py2027_widget": WIDGET_TITLE,
        "py2027_event_field": PY2027_EVENT_FIELD,
        "py2027_count": "COUNT(enrollment.id)",
        "event_date_candidate": candidate,
        "count_semantics": evidence.get("count_semantics") or [],
        "status_values": evidence.get("status_values") or [],
        "term_status_values_observed": term_values,
        "coverage_year": coverage,
        "coverage_year_rows": evidence.get("coverage_years") or [],
        "filter_inventory": filters,
        "date_profiles": evidence.get("date_profiles") or [],
        "date_relationships": evidence.get("date_relationships") or [],
        "event_date_decision": {
            "candidate_field": candidate.get("py2026_candidate"),
            "sql_datatype": candidate.get("sql_datatype"),
            "decision": event_decision,
            "reason": (
                "Date presence, 100% family coverage, and a zero same-snapshot multi-date rate "
                "are not proof that enrollment_last_update_date is enrollment.last_update_timestamp. "
                "Name similarity is not used as the decision."
                if event_decision == UNRESOLVED else
                "No date/time-compatible event-date candidate was selected."
            ),
        },
        "plus_one_pattern": {
            "label": plus_one_label,
            "note": "Not proof of timezone conversion.",
            "rows": evidence.get("plus_one") or [],
        },
        "extraction_strategies": strategies,
        "cancelled_policies": cancelled,
        "terminated_policies": terminated,
        "out_of_scope_this_phase": {
            "currently_enrolled_people": "unchanged",
            "new_active_passive": "unchanged",
            "applications": "unchanged",
            "pending_people": "unchanged",
        },
        "comparable": [
            item["metric_id"] for item in (cancelled, terminated)
            if item["comparison_status"] == COMPARABLE
        ],
        "failures": evidence.get("failures") or [],
        "command": COMMAND,
    }
    for metric in (cancelled, terminated):
        if metric["comparison_status"] == COMPARABLE and not all(
            check["status"] == PASS for check in metric["checks"]
        ):
            raise AssertionError("COMPARABLE requires every required check to PASS")
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
    cancelled = report.get("cancelled_policies") or {}
    terminated = report.get("terminated_policies") or {}
    coverage = report.get("coverage_year") or {}
    plus = report.get("plus_one_pattern") or {}
    filters = report.get("filter_inventory") or {}
    counts = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('grain'))}</td><td>{_e(item.get('event_calendar_date'))}</td>"
        f"<td>{_e(item.get('count_star'))}</td><td>{_e(item.get('count_enrollment_ids'))}</td>"
        f"<td>{_e(item.get('count_distinct_enrollment_keys'))}</td>"
        f"<td>{_e(item.get('duplicate_factor'))}</td></tr>"
        for item in report.get("count_semantics") or []
    )
    statuses = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('observed_status_value'))}</td>"
        f"<td>{_e(item.get('status_row_count'))}</td>"
        f"<td>{_e(item.get('status_distinct_enrollment_keys'))}</td></tr>"
        for item in report.get("status_values") or []
    )
    years = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('observed_coverage_year'))}</td>"
        f"<td>{_e(item.get('coverage_year_row_count'))}</td>"
        f"<td>{_e(item.get('cancelled_row_count'))}</td>"
        f"<td>{_e(item.get('terminated_row_count'))}</td></tr>"
        for item in report.get("coverage_year_rows") or []
    )
    filter_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('sisense_field'))}</td><td>{_e(item.get('role'))}</td>"
        f"<td>{_e(item.get('classification'))}</td><td>{_e(item.get('py2026_field'))}</td>"
        f"<td>{_e(item.get('evidence'))}</td></tr>"
        for item in filters.get("inventory") or []
    )
    dates = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('field'))}</td><td>{_e(item.get('sql_datatype'))}</td>"
        f"<td>{_e(item.get('date_field_min'))}</td><td>{_e(item.get('date_field_max'))}</td>"
        f"<td>{_e(item.get('null_rate'))}</td><td>{_e(item.get('date_field_distinct_dates'))}</td></tr>"
        for item in report.get("date_profiles") or []
    )
    rels = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('other_field'))}</td>"
        f"<td>{_e(item.get('same_calendar_date_count'))}</td>"
        f"<td>{_e(item.get('candidate_after_other'))}</td>"
        f"<td>{_e(item.get('candidate_before_other'))}</td></tr>"
        for item in report.get("date_relationships") or []
    )
    plus_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('slice'))}</td>"
        f"<td>{_e(item.get('snapshot_date'))}</td>"
        f"<td>{_e(item.get('event_before_snapshot'))}</td>"
        f"<td>{_e(item.get('event_on_snapshot'))}</td>"
        f"<td>{_e(item.get('event_on_snapshot_plus_one'))}</td>"
        f"<td>{_e(item.get('event_after_snapshot_plus_one'))}</td></tr>"
        for item in plus.get("rows") or []
    )
    strategies = "".join(
        "<tr>"
        f"<td>{_e(item.get('strategy') or item.get('activated_strategy'))}</td>"
        f"<td>{_e(item.get('name'))}</td><td>{_e(item.get('decision'))}</td>"
        f"<td>{_e(item.get('duplication_risk'))}</td>"
        f"<td>{_e(item.get('missing_event_risk'))}</td>"
        f"<td>{_e(item.get('can_reproduce_sisense_daily_count'))}</td>"
        f"<td>{_e(item.get('reason'))}</td></tr>"
        for item in report.get("extraction_strategies") or []
        if item.get("strategy")
    )
    cancelled_checks = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in cancelled.get("checks") or []
    )
    terminated_checks = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in terminated.get("checks") or []
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 activity semantic bridge</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:1200px}"
        "table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#f4f7fb}</style></head><body>"
        "<h1>PY2026 activity semantic bridge</h1>"
        f"<p>Widget: {_e(WIDGET_TITLE)}. Label: {_e(DIAGNOSTIC_LABEL)}. "
        "Not official activity. Snapshots are not summed. NULL is not zero.</p>"
        "<h2>Metric status</h2>"
        f"<p>Cancelled Policies: {_e(cancelled.get('comparison_status'))}. "
        f"Terminated Policies: {_e(terminated.get('comparison_status'))}. "
        "COMPARABLE only if every required check PASS.</p>"
        "<h2>COUNT semantics</h2>"
        "<p>Sisense COUNT(enrollment.id) is not DISTINCT.</p>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Grain</th><th>Date</th>"
        "<th>COUNT(*)</th><th>COUNT(enrollment_id)</th><th>COUNT DISTINCT</th>"
        "<th>duplicate_factor</th></tr></thead>"
        f"<tbody>{counts}</tbody></table>"
        "<h2>Status values</h2>"
        "<p>Every Term* value is listed. Terminated is not assumed to be the only member.</p>"
        "<table><thead><tr><th>Table</th><th>Status</th><th>Rows</th>"
        "<th>Distinct enrollment keys</th></tr></thead>"
        f"<tbody>{statuses}</tbody></table>"
        "<h2>Coverage year</h2>"
        f"<p>decision={_e(coverage.get('decision'))}. {_e(coverage.get('reason'))}</p>"
        "<table><thead><tr><th>Table</th><th>coverage_year</th><th>Rows</th>"
        "<th>Cancelled</th><th>Terminated</th></tr></thead>"
        f"<tbody>{years}</tbody></table>"
        "<h2>Activity widget filters</h2>"
        f"<p>{_e(filters.get('note'))}</p>"
        "<table><thead><tr><th>Sisense field</th><th>Role</th><th>Classification</th>"
        "<th>PY2026 field</th><th>Evidence</th></tr></thead>"
        f"<tbody>{filter_rows}</tbody></table>"
        "<h2>Event-date field comparison</h2>"
        "<p>Name similarity is not the decision rule.</p>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Field</th><th>Datatype</th>"
        "<th>Min</th><th>Max</th><th>Null rate</th><th>Distinct dates</th></tr></thead>"
        f"<tbody>{dates}</tbody></table>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Other field</th>"
        "<th>Same calendar date</th><th>Candidate after</th><th>Candidate before</th>"
        "</tr></thead>"
        f"<tbody>{rels}</tbody></table>"
        "<h2>+1 day pattern</h2>"
        f"<p>label={_e(plus.get('label'))}. {_e(plus.get('note'))}</p>"
        "<table><thead><tr><th>Table</th><th>Slice</th><th>Snapshot date</th>"
        "<th>Before</th><th>On date</th><th>On date + 1</th><th>After date + 1</th>"
        "</tr></thead>"
        f"<tbody>{plus_rows}</tbody></table>"
        "<h2>Extraction strategies</h2>"
        "<p>None is activated. Snapshots are not unioned.</p>"
        "<table><thead><tr><th>Strategy</th><th>Name</th><th>Decision</th>"
        "<th>Duplication risk</th><th>Missing-event risk</th>"
        "<th>Can reproduce Sisense COUNT</th><th>Reason</th></tr></thead>"
        f"<tbody>{strategies}</tbody></table>"
        "<h2>Cancelled Policies checks</h2>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{cancelled_checks}</tbody></table>"
        "<h2>Terminated Policies checks</h2>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{terminated_checks}</tbody></table>"
        "</body></html>"
    )


def print_summary(report: dict) -> None:
    cancelled = report.get("cancelled_policies") or {}
    terminated = report.get("terminated_policies") or {}
    coverage = report.get("coverage_year") or {}
    event = report.get("event_date_decision") or {}
    plus = report.get("plus_one_pattern") or {}
    print("PHASE 5B ACTIVITY SEMANTIC BRIDGE")
    print(f"Widget: {WIDGET_TITLE}")
    print(f"COUNT: COUNT(enrollment.id) is not DISTINCT")
    print(f"Event-date decision: {event.get('decision')} ({event.get('candidate_field')})")
    print(f"Coverage-year decision: {coverage.get('decision')}")
    print(f"+1 day label: {plus.get('label')}")
    print(f"Cancelled Policies: {cancelled.get('comparison_status')}")
    print(f"Terminated Policies: {terminated.get('comparison_status')}")
    print("No extraction strategy activated. No official activity output.")
    print("Currently Enrolled / New-Active-Passive / Applications / Pending: unchanged")


def main() -> int:
    evidence = {
        "tables": [], "selected_snapshots": [], "count_semantics": [],
        "status_values": [], "coverage_years": [], "plus_one": [],
        "date_profiles": [], "date_relationships": [],
        "filter_inventory": classify_activity_filters(None, {}),
        "failures": [],
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            client = DatabaseClient(connection)
            evidence = collect_evidence(client, load_discovery_artifact())
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(evidence)
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


def _activity_checks(family: str, *, identifier_present: bool, count_ready: bool,
                     count_factor=None, status_observed: bool, event_decision: str,
                     coverage_decision: str, filter_decision: str, strategy_decision: str,
                     term_values: list[str]) -> list[dict]:
    status_name = "status_semantics"
    if family == "Cancelled":
        status_detail = "Exact Cancelled. Canceled is not treated as Cancelled."
        status_state = PASS if status_observed else UNRESOLVED
    else:
        listed = ", ".join(term_values) if term_values else "(none observed)"
        status_detail = f"startsWith('Term') / LIKE 'Term%'. Observed Term* values: {listed}."
        status_state = PASS if status_observed else UNRESOLVED
    event_state = PASS if event_decision == SUPPORTED else FAIL if event_decision == NOT_SUPPORTED else UNRESOLVED
    coverage_state = PASS if coverage_decision == SUPPORTED else FAIL if coverage_decision == NOT_SUPPORTED else UNRESOLVED
    filter_state = PASS if filter_decision == SUPPORTED else FAIL if filter_decision == NOT_SUPPORTED else UNRESOLVED
    strategy_state = PASS if strategy_decision == SUPPORTED else FAIL if strategy_decision == NOT_SUPPORTED else UNRESOLVED
    return [
        _check("enrollment_identifier", PASS if identifier_present else FAIL,
               "enrollment_id is present." if identifier_present else "enrollment_id is absent."),
        _check("count_semantics", PASS if count_ready else UNRESOLVED,
               count_operator_note(None if not count_ready else count_factor)),
        _check(status_name, status_state, status_detail),
        _check("event_date_semantics", event_state,
               "enrollment_last_update_date is not auto-promoted to last_update_timestamp."),
        _check("coverage_year_semantics", coverage_state,
               "ssap_applications.coverage_year is not assumed equal to snapshot coverage_year."),
        _check("actual_widget_filter_equivalence", filter_state,
               "Currently Enrolled filters are not copied. Dashboard inheritance is unresolved without activity JAQL."),
        _check("historical_extraction_strategy", strategy_state,
               "No strategy is activated. Snapshots are not unioned or summed."),
        _check("daily_aggregation_semantics", UNRESOLVED,
               "Sisense day grain vs CAST(event AS date) and the +1-day pattern are unproven."),
    ]


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def _activity_widget(discovery: dict | None) -> dict | None:
    if not discovery:
        return None
    wanted = normalize_label(WIDGET_TITLE)
    cancelled = normalize_label("Cancelled Policies")
    terminated = normalize_label("Terminated Policies")
    for widget in _iter_widgets(discovery):
        title = normalize_label(widget.get("widget_title") or widget.get("widget"))
        if title == wanted:
            return widget
        measures = _measure_labels(widget)
        if cancelled in measures and terminated in measures:
            return widget
    return None


def _iter_widgets(discovery: dict) -> list[dict]:
    widgets = list(discovery.get("widgets") or [])
    widgets.extend(discovery.get("widget_inventory") or [])
    widgets.extend(discovery.get("cancellation_source_identification") or [])
    return widgets


def _measure_labels(widget: dict) -> set[str]:
    labels = set()
    for item in widget.get("measures") or []:
        if isinstance(item, dict):
            labels.add(normalize_label(item.get("title")))
        else:
            labels.add(normalize_label(item))
    for item in widget.get("measure_titles") or []:
        labels.add(normalize_label(item))
    labels.discard(None)
    return labels


def _classify_discovered_filter(role: dict, historical: dict) -> dict:
    column = role.get("column")
    disabled = role.get("disabled")
    dashboard = role.get("is_dashboard_filter")
    if disabled is True:
        classification = DISABLED
    elif dashboard is True:
        classification = DASHBOARD_APPLIED
    elif dashboard is False:
        classification = WIDGET_OWN
    else:
        classification = UNRESOLVED
    py2026 = None if column is None else historical.get(str(column).lower())
    return {
        "sisense_field": None if not column else f"{role.get('table')}.{column}",
        "role": "discovered_scope_filter",
        "classification": classification,
        "py2026_field": py2026,
        "evidence": "Captured activity-widget metadata_roles. Filter values are not invented.",
    }


def _dashboard_filters_from_currently_enrolled(historical: dict) -> list[dict]:
    """Dashboard filters seen on Currently Enrolled are not assumed to apply here."""
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    definition = interpret_payload(payload)
    rows = []
    for item in definition.get("scope_filters") or []:
        if item.get("is_dashboard_filter") is not True:
            continue
        column = item.get("column")
        rows.append({
            "sisense_field": None if not column else f"{item.get('table')}.{column}",
            "classification": UNRESOLVED,
            "py2026_field": None if column is None else historical.get(str(column).lower()),
            "evidence": (
                "Present as isDashboardFilter on the verified Currently Enrolled request. "
                "Whether this activity widget applies it is unproven."
            ),
        })
    return rows


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


def _count_semantics(client, failures, item) -> list[dict]:
    usable, reason, candidate = _usable(item)
    rows = []
    for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
        family = {
            "table": item["table"],
            "status_family": label,
            "grain": "family",
            "event_calendar_date": None,
            "count_star": None,
            "count_enrollment_ids": None,
            "count_distinct_enrollment_keys": None,
            "duplicate_factor": None,
            "operator": "COUNT",
            "not_distinct": True,
            "skip_reason": reason,
        }
        if usable:
            sql, params = build_count_family_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate["py2026_candidate"],
                candidate.get("sql_datatype"), cancelled=cancelled,
            )
            result = _run(client, failures, f"count family {label} {item['table']}", sql, params)
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                count = _as_int(mapped.get("count_enrollment_ids"))
                distinct = _as_int(mapped.get("count_distinct_enrollment_keys"))
                factor = duplicate_factor(count, distinct)
                family.update({
                    "count_star": _as_int(mapped.get("count_star")),
                    "count_enrollment_ids": count,
                    "count_distinct_enrollment_keys": distinct,
                    "duplicate_factor": factor,
                    "note": count_operator_note(factor),
                    "skip_reason": None,
                })
            sql, params = build_count_daily_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate["py2026_candidate"],
                candidate.get("sql_datatype"), cancelled=cancelled,
            )
            daily = _run(client, failures, f"count daily {label} {item['table']}", sql, params)
            if daily is not None:
                for raw in daily.rows:
                    mapped = dict(zip(daily.columns, raw))
                    count = _as_int(mapped.get("count_enrollment_ids"))
                    distinct = _as_int(mapped.get("count_distinct_enrollment_keys"))
                    rows.append({
                        "table": item["table"],
                        "status_family": label,
                        "grain": "day",
                        "event_calendar_date": _json_value(mapped.get("event_calendar_date")),
                        "count_star": _as_int(mapped.get("count_star")),
                        "count_enrollment_ids": count,
                        "count_distinct_enrollment_keys": distinct,
                        "duplicate_factor": duplicate_factor(count, distinct),
                        "operator": "COUNT",
                        "not_distinct": True,
                    })
        rows.append(family)
    return rows


def _status_values(client, failures, item) -> list[dict]:
    usable, reason, _candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    sql, params = build_status_value_sql(
        qualified_name(item["schema_name"], item["table"]), item["columns"])
    result = _run(client, failures, f"status values {item['table']}", sql, params)
    if result is None:
        return [{"table": item["table"], "skip_reason": "status query failed"}]
    rows = []
    for raw in result.rows:
        mapped = dict(zip(result.columns, raw))
        value = mapped.get("observed_status_value")
        rows.append({
            "table": item["table"],
            "observed_status_value": None if value is None else str(value),
            "status_row_count": _as_int(mapped.get("status_row_count")),
            "status_distinct_enrollment_keys": _as_int(mapped.get("status_distinct_enrollment_keys")),
            "term_family": bool(value) and str(value).strip().casefold().startswith("term"),
            "cancelled_exact": bool(value) and str(value).strip().casefold() == "cancelled",
        })
    return rows


def _coverage_years(client, failures, item) -> list[dict]:
    columns = item.get("columns") or {}
    if "coverage_year" not in columns or "enrollment_status_description" not in columns:
        return [{"table": item["table"], "skip_reason": "coverage_year or status absent"}]
    sql, params = build_coverage_year_sql(
        qualified_name(item["schema_name"], item["table"]), columns)
    result = _run(client, failures, f"coverage year {item['table']}", sql, params)
    if result is None:
        return [{"table": item["table"], "skip_reason": "coverage_year query failed"}]
    rows = []
    for raw in result.rows:
        mapped = dict(zip(result.columns, raw))
        rows.append({
            "table": item["table"],
            "observed_coverage_year": _json_value(mapped.get("observed_coverage_year")),
            "coverage_year_row_count": _as_int(mapped.get("coverage_year_row_count")),
            "cancelled_row_count": _as_int(mapped.get("cancelled_row_count")),
            "terminated_row_count": _as_int(mapped.get("terminated_row_count")),
        })
    return rows


def _plus_one(client, failures, item) -> list[dict]:
    context = item.get("snapshot_date") or snapshot_date_context(item["table"])
    usable, reason, candidate = _usable(item)
    if not context.get("snapshot_date"):
        return [{
            "table": item["table"],
            "slice": "ALL",
            "snapshot_date": None,
            "skip_reason": "SKIPPED_NO_PROVEN_SNAPSHOT_DATE",
        }]
    if not usable:
        return [{"table": item["table"], "slice": "ALL", "skip_reason": reason}]
    rows = []
    for mode, label in (("all", "ALL"), ("cancelled", "Cancelled"), ("term", "Terminated")):
        sql, params = build_plus_one_sql(
            qualified_name(item["schema_name"], item["table"]),
            item["columns"], candidate["py2026_candidate"], candidate.get("sql_datatype"),
            mode=mode, snapshot_date=context["snapshot_date"],
        )
        result = _run(client, failures, f"plus one {label} {item['table']}", sql, params)
        row = {
            "table": item["table"],
            "slice": label,
            "snapshot_date": context["snapshot_date"],
            "event_before_snapshot": None,
            "event_on_snapshot": None,
            "event_on_snapshot_plus_one": None,
            "event_after_snapshot_plus_one": None,
        }
        if result is not None and result.row_count == 1:
            mapped = _row_dict(result)
            row.update({
                "event_before_snapshot": _as_int(mapped.get("event_before_snapshot")),
                "event_on_snapshot": _as_int(mapped.get("event_on_snapshot")),
                "event_on_snapshot_plus_one": _as_int(mapped.get("event_on_snapshot_plus_one")),
                "event_after_snapshot_plus_one": _as_int(mapped.get("event_after_snapshot_plus_one")),
            })
        rows.append(row)
    return rows


def _date_profiles(client, failures, item) -> list[dict]:
    usable, reason, _candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    rows = []
    for field in item.get("date_fields") or relevant_date_fields(item["columns"]):
        for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
            sql, params = build_date_field_profile_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], field["name"], field.get("data_type"), cancelled=cancelled,
            )
            result = _run(
                client, failures, f"date profile {field['name']} {label} {item['table']}",
                sql, params)
            row = {
                "table": item["table"],
                "status_family": label,
                "field": field["name"],
                "sql_datatype": field.get("data_type"),
                "date_field_min": None,
                "date_field_max": None,
                "date_field_nulls": None,
                "date_field_rows": None,
                "date_field_distinct_dates": None,
                "null_rate": None,
            }
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                total = _as_int(mapped.get("date_field_rows"))
                nulls = _as_int(mapped.get("date_field_nulls"))
                row.update({
                    "date_field_min": _json_value(mapped.get("date_field_min")),
                    "date_field_max": _json_value(mapped.get("date_field_max")),
                    "date_field_nulls": nulls,
                    "date_field_rows": total,
                    "date_field_distinct_dates": _as_int(mapped.get("date_field_distinct_dates")),
                    "null_rate": None if total in (None, 0) or nulls is None else round(100.0 * nulls / total, 4),
                })
            rows.append(row)
    return rows


def _date_relationships(client, failures, item) -> list[dict]:
    usable, reason, candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    candidate_name = candidate["py2026_candidate"]
    candidate_type = candidate.get("sql_datatype")
    rows = []
    for field in item.get("date_fields") or []:
        if field["name"].lower() == candidate_name.lower():
            continue
        for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
            sql, params = build_date_relationship_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate_name, candidate_type,
                field["name"], field.get("data_type"), cancelled=cancelled,
            )
            result = _run(
                client, failures,
                f"date rel {field['name']} {label} {item['table']}", sql, params)
            row = {
                "table": item["table"],
                "status_family": label,
                "candidate_field": candidate_name,
                "other_field": field["name"],
                "same_calendar_date_count": None,
                "candidate_after_other": None,
                "candidate_before_other": None,
                "other_null_when_candidate_present": None,
            }
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                row.update({
                    "same_calendar_date_count": _as_int(mapped.get("same_calendar_date_count")),
                    "candidate_after_other": _as_int(mapped.get("candidate_after_other")),
                    "candidate_before_other": _as_int(mapped.get("candidate_before_other")),
                    "other_null_when_candidate_present": _as_int(
                        mapped.get("other_null_when_candidate_present")),
                })
            rows.append(row)
    return rows


def _family_factor(rows: list[dict], family: str):
    for item in rows:
        if item.get("status_family") == family and item.get("grain") == "family":
            return item.get("duplicate_factor")
    return None


def _count_ready(rows: list[dict], family: str) -> bool:
    return any(
        item.get("status_family") == family
        and item.get("grain") == "family"
        and item.get("count_enrollment_ids") is not None
        and item.get("count_distinct_enrollment_keys") is not None
        for item in rows
    )


def _status_observed(rows: list[dict], *, exact_cancelled: bool) -> bool:
    if exact_cancelled:
        return any(item.get("cancelled_exact") for item in rows)
    return any(item.get("term_family") for item in rows)


def _term_values(rows: list[dict]) -> list[str]:
    values = []
    for item in rows:
        if not item.get("term_family"):
            continue
        value = item.get("observed_status_value")
        if value and value not in values:
            values.append(value)
    return values


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


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


if __name__ == "__main__":
    raise SystemExit(main())
