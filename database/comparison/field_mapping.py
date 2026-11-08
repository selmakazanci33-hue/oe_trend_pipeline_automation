"""Evidence-based PY2026 field mapping for Phase 5B.

Read-only. Does not invent tables, columns, status maps, OE dates, or values.
Does not query coverage_year 2027. Does not modify Sisense extraction.

    python -m database.comparison.field_mapping
"""

from __future__ import annotations

import html
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import (
    OUTPUT_DIR,
    VERIFIED_PAYLOAD_FILE,
    guard_no_pii,
    interpret_payload,
    translate_filters,
)
from comparison.daily import load_oe_anchor
from comparison.diagnostic_numbers import COLUMNS_SQL, TABLES_SQL, find_exchange_column
from comparison.lifecycle import (
    INBOUND_MARKERS,
    PREVIOUS_PLAN_YEAR,
    QUERY_TIMEOUT_SECONDS,
    SNAPSHOT_PREFIX,
    infer_filename_date,
)
from comparison.models import (
    COMPARABLE,
    DIAGNOSTIC,
    IDENTIFIER_UNPROVEN,
    NOT_COMPARABLE,
    PENDING_UNAVAILABLE,
    SemanticChecks,
    default_identifier_requirement,
    spec_by_id,
)
from comparison.validator import assign_status, assert_grains_compatible
from database.comparison.activity import (
    build_daily_activity_sql,
    build_duplicate_enrollment_sql,
    build_identifier_cardinality_sql,
    build_null_and_range_sql,
    build_overlap_sql,
    build_people_cancellation_sql,
    build_status_values_sql,
    event_semantics_ready,
    parse_daily_rows,
    series_document,
)
from database.discovery.enrollment_source import qualified_name
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_field_mapping.json"
HTML_NAME = "py2026_field_mapping.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.field_mapping"

EXACT_NAME = "EXACT_NAME"
NAME_SIMILAR = "NAME_SIMILAR_UNPROVEN"
ABSENT = "ABSENT"
UNRELATED = "UNRELATED"
UNKNOWN = "UNKNOWN"
DISCOVERY_WINDOW = "DISCOVERY_WINDOW"
OFFICIAL = "OFFICIAL / VERIFIED"
INFERRED = "INFERRED"

EVENT_DATE_CANDIDATES = (
    "last_update_timestamp",
    "enrollment_last_update_date",
    "enrollment_update_date",
    "enrollment_create_date",
    "enrollment_confirmation_date",
    "benefit_effective_date",
)
DATETIME_SQL_TYPES = frozenset({
    "date",
    "datetime",
    "datetime2",
    "datetimeoffset",
    "smalldatetime",
})
EVENT_DATE_PREFERRED = (
    ("last_update_timestamp", EXACT_NAME),
    ("enrollment_last_update_date", NAME_SIMILAR),
    ("enrollment_update_date", NAME_SIMILAR),
)
NOT_DATETIME = "NOT_DATETIME"
REJECTED_IDENTIFIER = "REJECTED_IDENTIFIER"
LIFECYCLE_FIELD_HINTS = (
    "enrollment_create_date",
    "enrollment_confirmation_date",
    "benefit_effective_date",
    "benefit_end_date",
)
PII_NAME_MARKERS = (
    "ssn", "email", "first_name", "last_name", "address", "birth", "phone", "dob",
)
CONFIDENCE_FOR_SISENSE_DATE = {
    "last_update_timestamp": EXACT_NAME,
    "enrollment_last_update_date": NAME_SIMILAR,
    "enrollment_update_date": NAME_SIMILAR,
    "enrollment_create_date": UNRELATED,
    "enrollment_confirmation_date": UNRELATED,
    "benefit_effective_date": UNRELATED,
}


def oe_anchor_evidence() -> dict:
    """Repository evidence only. October 1 floors are discovery windows, not OE starts."""
    py2026 = load_oe_anchor(2026)
    py2027 = load_oe_anchor(2027)
    return {
        "py2026": {
            "classification": UNKNOWN if py2026.anchor_date is None else INFERRED,
            "anchor_date": None if py2026.anchor_date is None else py2026.anchor_date.isoformat(),
            "alignment": py2026.alignment,
            "reason": py2026.reason,
        },
        "py2027": {
            "classification": UNKNOWN if py2027.anchor_date is None else INFERRED,
            "anchor_date": None if py2027.anchor_date is None else py2027.anchor_date.isoformat(),
            "alignment": py2027.alignment,
            "reason": py2027.reason,
        },
        "rejected_as_official_oe_start": [
            {
                "dates": ["2024-10-01", "2025-10-01"],
                "classification": DISCOVERY_WINDOW,
                "reason": (
                    "Historical confirmation/discovery floors. "
                    "They are not treated as official OE start dates."
                ),
            },
            {
                "dates": ["benefit window October 1 before plan year"],
                "classification": DISCOVERY_WINDOW,
                "reason": (
                    "The verified Currently Enrolled benefit window starts on October 1 "
                    "of the calendar year before the plan year. That is not an OE-day anchor."
                ),
            },
        ],
        "official_verified_found": False,
    }


def classify_identifier_column(name: str) -> str | None:
    """Label an existing schema column. Unknown names are not invented as metrics."""
    key = name.lower()
    if any(marker in key for marker in PII_NAME_MARKERS):
        return None
    compact = key.replace("_", "")
    if "ident" in compact and ("exchgindiv" in compact or "exchangeindiv" in compact):
        return "exchange_individual"
    if key == "enrollee_id":
        return "enrollee"
    if "subscriber" in key and ("id" in key or "ident" in key):
        return "subscriber"
    if "applicant" in key and ("id" in key or "ident" in key):
        return "applicant"
    if key in {"person_id", "person_identifier"}:
        return "person"
    if key == "enrollment_id":
        return "enrollment"
    return None


def is_datetime_sql_type(data_type: str | None) -> bool:
    """True only for date/time SQL types. SQL Server timestamp/rowversion is excluded."""
    if not data_type:
        return False
    base = str(data_type).split("(")[0].strip().lower()
    return base in DATETIME_SQL_TYPES


def is_blocked_event_date_name(name: str | None) -> bool:
    """Identifiers are never event-date candidates, regardless of datatype or name score."""
    if not name:
        return True
    if classify_identifier_column(name) is not None:
        return True
    key = name.lower()
    if key.endswith("_id") or key.endswith("_ids"):
        return True
    if key.endswith("_proxy") or "_proxy_" in key or key.endswith("_id_proxy"):
        return True
    if "identifier" in key:
        return True
    return False


def select_event_date_candidate(columns: dict) -> dict:
    """Fail-closed EVENT_DATE selection. Name matching alone is not enough.

    Requires a date/time-compatible SQL datatype. Identifiers are rejected.
    Unrelated date columns are not used as fallbacks. If none qualify, the
    candidate is null.
    """
    empty = {
        "py2026_candidate": None,
        "sql_datatype": None,
        "confidence": ABSENT,
        "datetime_compatible": False,
        "reason": (
            "No date/time-compatible event-date candidate. "
            "Name matching is not sufficient and identifiers are never selected."
        ),
    }
    if not columns:
        return empty
    for key, confidence in EVENT_DATE_PREFERRED:
        meta = columns.get(key)
        if meta is None:
            continue
        name = meta.get("name") or key
        dtype = meta.get("data_type")
        if is_blocked_event_date_name(name):
            continue
        if not is_datetime_sql_type(dtype):
            continue
        return {
            "py2026_candidate": name,
            "sql_datatype": dtype,
            "confidence": confidence,
            "datetime_compatible": True,
            "reason": (
                "Column name and date/time SQL datatype both match. "
                "Semantic equivalence to Sisense enrollment.last_update_timestamp is unproven."
            ),
        }
    return empty


def field_mapping_rows(columns: dict, status_values: list[str] | None = None) -> list[dict]:
    """Inventory of possible Sisense last_update_timestamp counterparts.

    Identifier and status fields are not event-date rows. Presence of a date
    column name does not select it; see select_event_date_candidate.
    """
    del status_values
    rows = []
    for candidate in EVENT_DATE_CANDIDATES:
        meta = columns.get(candidate)
        present = meta is not None
        name = None if not present else meta["name"]
        dtype = None if not present else meta.get("data_type")
        blocked = bool(name) and is_blocked_event_date_name(name)
        datetime_ok = is_datetime_sql_type(dtype)
        if not present:
            confidence = ABSENT
            note = "Column not in this snapshot schema."
        elif blocked:
            confidence = REJECTED_IDENTIFIER
            note = "Identifier columns cannot be event-date candidates."
        elif not datetime_ok:
            confidence = NOT_DATETIME
            note = (
                "Column name is not sufficient. EVENT_DATE requires a date/time-compatible "
                "SQL datatype. This column was not selected."
            )
        else:
            confidence = CONFIDENCE_FOR_SISENSE_DATE[candidate]
            note = (
                "Exact Sisense event-date column name and date/time SQL datatype."
                if confidence == EXACT_NAME else
                "Present with a date/time SQL datatype. Not proven equal to Sisense last_update_timestamp."
                if confidence == NAME_SIMILAR else
                "Not treated as the cancellation/termination event date."
            )
        rows.append({
            "py2027_field": "enrollment.last_update_timestamp",
            "py2026_candidate": name,
            "sql_datatype": dtype,
            "datetime_compatible": datetime_ok,
            "identifier_rejected": blocked,
            "evidence": "INFORMATION_SCHEMA datatype plus column name",
            "confidence": confidence,
            "notes": note,
        })
    return rows


def currently_enrolled_checklist(definition: dict, columns: dict, identifier: dict) -> list[dict]:
    mappings = translate_filters(definition, set(columns) if columns else None)
    checks = [
        {
            "check": "person_identifier_equivalence",
            "status": "UNRESOLVED" if identifier.get("identity_status") != "EXACT" else "PASS",
            "detail": identifier.get("reason"),
        },
        {
            "check": "coverage_year",
            "status": _filter_status(mappings, "Plan year"),
            "detail": _filter_reason(mappings, "Plan year"),
        },
        {
            "check": "insurance_type_health",
            "status": _filter_status(mappings, "Insurance type"),
            "detail": _filter_reason(mappings, "Insurance type"),
        },
        {
            "check": "person_type_enrollee_subscriber",
            "status": _filter_status(mappings, "Person type"),
            "detail": _filter_reason(mappings, "Person type"),
        },
        {
            "check": "enrollment_status_logic",
            "status": "UNRESOLVED",
            "detail": "Visible members are not the full filter. Nested turnedOff remains unresolved.",
        },
        {
            "check": "enrollee_status_logic",
            "status": _filter_status(mappings, "Enrollee status"),
            "detail": _filter_reason(mappings, "Enrollee status"),
        },
        {
            "check": "benefit_effective_date",
            "status": _filter_status(mappings, "Benefit effective date"),
            "detail": _filter_reason(mappings, "Benefit effective date"),
        },
        {
            "check": "snapshot_as_of_semantics",
            "status": "UNRESOLVED",
            "detail": "Filename dates are not proven as-of dates. Currently Enrolled is a current indicator.",
        },
    ]
    return checks


def people_cancellation_checklist(*, person_ready: bool, status_ready: bool,
                                  cancelled_observed: bool, multi_status_people: int | None,
                                  identifier_exact: bool) -> list[dict]:
    return [
        {
            "check": "person_grain",
            "status": "PASS" if person_ready else "FAIL",
            "detail": "Person key column is present." if person_ready else "No person identifier column.",
        },
        {
            "check": "identifier_equivalence",
            "status": "PASS" if identifier_exact else "UNRESOLVED",
            "detail": "Exchange individual identifier is not proven equivalent to enrollee_id."
            if not identifier_exact else "Exchange individual identifier column is present.",
        },
        {
            "check": "status_equivalence",
            "status": "PASS" if cancelled_observed and status_ready else "UNRESOLVED",
            "detail": "Cancelled observed on enrollment_status_description."
            if cancelled_observed else "Cancelled was not observed as a status value.",
        },
        {
            "check": "coverage_year_equivalence",
            "status": "UNRESOLVED",
            "detail": "Sisense uses ssap_applications.coverage_year. Snapshot coverage_year is not proven to be that field.",
        },
        {
            "check": "multiple_enrollment_records_per_person",
            "status": "PASS" if (multi_status_people or 0) > 0 else "UNRESOLVED",
            "detail": "People with more than one enrollment status were counted without returning identifiers."
            if (multi_status_people or 0) > 0 else "No multi-status people were observed, or the query did not run.",
        },
        {
            "check": "not_policy_activity",
            "status": "PASS",
            "detail": "These measures stay person-grain and are not combined with Cancelled Policies.",
        },
    ]


def activity_checks(*, last_update_exact: bool, cancelled_observed: bool,
                    term_observed: bool, overlap: int | None) -> SemanticChecks:
    return SemanticChecks(
        identifier_equivalence_proven=True,
        grain_match=True,
        family_match=True,
        date_semantics_match=last_update_exact,
        py2026_definition_verified=last_update_exact and cancelled_observed,
        py2027_definition_verified=False,
        filter_mapping_proven=False,
        both_values_present=False,
        people_policy_not_mixed=True,
        snapshot_not_summed=not (overlap and overlap > 0),
        py2026_sql_not_invented=True,
    )


def classify_activity_metric(spec_id: str, checks: SemanticChecks, *, observed: bool,
                             last_update_exact: bool) -> dict:
    spec = spec_by_id(spec_id)
    status = assign_status(spec, checks)
    if status == COMPARABLE:
        status = DIAGNOSTIC
    if not observed:
        status = NOT_COMPARABLE
        reason = (
            "Required status member was not observed. "
            "Not treated as a current cancelled/terminated population. Missing values stay missing."
        )
    elif not last_update_exact:
        status = DIAGNOSTIC
        reason = (
            "Historical snapshots do not expose last_update_timestamp by that name. "
            "enrollment_last_update_date is not assumed equal. Event semantics unproven."
        )
    else:
        status = DIAGNOSTIC
        reason = (
            "Single-snapshot COUNT DISTINCT enrollment_id by last_update_timestamp is diagnostic. "
            "PY2027 daily history is not persistently available. Not COMPARABLE."
        )
    return {"metric_id": spec_id, "metric_name": spec.metric_name, "comparison_status": status, "reason": reason}


def collect_evidence(client, definition: dict) -> dict:
    failures = []
    tables = _list_snapshots(client, failures)
    profiles = []
    for item in tables:
        columns = _columns(client, failures, item)
        item["columns"] = columns
        item["identifier_columns"] = _identifier_columns(columns)
        item["lifecycle_fields"] = [
            name for name in LIFECYCLE_FIELD_HINTS if name in columns
        ] + [
            meta["name"] for key, meta in columns.items()
            if key not in LIFECYCLE_FIELD_HINTS
            and classify_identifier_column(meta["name"]) is None
            and "create" in key and not any(marker in key for marker in PII_NAME_MARKERS)
        ]
        profile = _profile_table(client, failures, item, columns)
        profiles.append(profile)
    overlap = _overlap_pairs(client, failures, profiles)
    return {
        "tables": tables,
        "profiles": profiles,
        "overlap": overlap,
        "failures": failures,
        "definition": definition,
    }


def build_report(evidence: dict, *, py2027_activity_history: str = "PENDING") -> dict:
    definition = evidence.get("definition") or interpret_payload(
        json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    )
    profiles = evidence.get("profiles") or []
    overlap_total = max((item.get("overlapping_enrollment_ids") or 0) for item in evidence.get("overlap") or []) if evidence.get("overlap") else 0
    chosen = _choose_activity_source(profiles)
    columns = {} if chosen is None else chosen.get("columns") or {}
    statuses = [] if chosen is None else [item["status_value"] for item in chosen.get("status_values") or [] if item.get("status_value") is not None]
    cancelled_observed = any(_is_cancelled(item) for item in statuses)
    term_observed = any(_is_term(item) for item in statuses)
    last_update_exact = "last_update_timestamp" in columns
    mapping_rows = field_mapping_rows(columns, statuses) if columns else field_mapping_rows({}, [])
    identifier = _person_identifier(columns)
    enrolled_checks = currently_enrolled_checklist(definition, columns, identifier)
    people_checks = people_cancellation_checklist(
        person_ready=identifier.get("column") is not None,
        status_ready="enrollment_status_description" in columns,
        cancelled_observed=cancelled_observed,
        multi_status_people=None if chosen is None else chosen.get("multi_status_people"),
        identifier_exact=identifier.get("identity_status") == "EXACT",
    )
    checks = activity_checks(
        last_update_exact=last_update_exact,
        cancelled_observed=cancelled_observed,
        term_observed=term_observed,
        overlap=overlap_total,
    )
    cancelled = classify_activity_metric(
        "cancelled_policies_daily", checks, observed=cancelled_observed, last_update_exact=last_update_exact)
    terminated = classify_activity_metric(
        "terminated_policies_daily", checks, observed=term_observed, last_update_exact=last_update_exact)
    currently = {
        "metric_id": "currently_enrolled_people",
        "metric_name": "Currently Enrolled People",
        "comparison_status": DIAGNOSTIC,
        "reason": (
            "Verified PY2027 extraction is unchanged. Identifier, filter, and as-of checks "
            "are not all proven. Failed/unresolved: "
            + ", ".join(item["check"] for item in enrolled_checks if item["status"] != "PASS")
        ),
        "failed_or_unresolved_checks": [item["check"] for item in enrolled_checks if item["status"] != "PASS"],
    }
    people_blocked = [item["check"] for item in people_checks if item["status"] != "PASS"]
    enrolled_cancelled = {
        "metric_id": "enrolled_cancelled",
        "metric_name": "Enrolled & Cancelled People",
        "comparison_status": DIAGNOSTIC if chosen and chosen.get("people_cancellation") else NOT_COMPARABLE,
        "reason": (
            "Person metric, not policy activity. Diagnostic query ran on one snapshot."
            if chosen and chosen.get("people_cancellation")
            else "Person-level rule is not proven. " + "; ".join(people_blocked)
        ),
    }
    cancelled_not_enrolled = {
        "metric_id": "cancelled_not_enrolled",
        "metric_name": "Cancelled - Not Enrolled People",
        "comparison_status": enrolled_cancelled["comparison_status"],
        "reason": enrolled_cancelled["reason"],
    }
    daily_ready = bool(
        chosen
        and event_semantics_ready(
            columns,
            cancelled_status_observed=cancelled_observed,
            last_update_present=last_update_exact,
        )
        and evidence.get("activity_series", {}).get("cancelled_policies_daily")
    )
    term_ready = bool(
        chosen
        and last_update_exact
        and term_observed
        and evidence.get("activity_series", {}).get("terminated_policies_daily")
    )
    assert_grains_compatible(spec_by_id("enrolled_cancelled"), spec_by_id("enrolled_cancelled"))
    oe = oe_anchor_evidence()
    report = {
        "report_kind": "PY2026_FIELD_MAPPING",
        "queries_py2027_enrollment": False,
        "visualization_created": False,
        "enrollment_identifier_mapping": {
            "py2027_field": "enrollment.id",
            "py2026_candidate": None if "enrollment_id" not in columns else columns["enrollment_id"]["name"],
            "confidence": EXACT_NAME if "enrollment_id" in columns else ABSENT,
            "source_table": None if chosen is None else chosen.get("table"),
        },
        "person_identifier_mapping": identifier,
        "status_mapping": {
            "py2027_field": "enrollment.enrollment_status_description",
            "py2026_candidate": None if "enrollment_status_description" not in columns
            else columns["enrollment_status_description"]["name"],
            "observed_values": statuses,
            "cancelled_observed": cancelled_observed,
            "canceled_variant_observed": any(str(item).strip().casefold() == "canceled" for item in statuses),
            "term_prefix_observed": term_observed,
        },
        "event_date_candidate": select_event_date_candidate(columns),
        "event_date_mapping": mapping_rows,
        "snapshot_duplication_risk": {
            "overlapping_enrollment_ids": overlap_total,
            "pairs": evidence.get("overlap") or [],
            "must_not_sum_across_snapshots": True,
            "risk": overlap_total > 0,
        },
        "currently_enrolled_semantic_checklist": enrolled_checks,
        "people_cancellation_semantic_checklist": people_checks,
        "lifecycle_supporting_fields": {
            "note": "Schema inspection only. New/Active/Passive PY2026 logic is not implemented.",
            "fields_present": [] if chosen is None else chosen.get("lifecycle_fields") or [],
        },
        "oe_anchor_evidence": oe,
        "metric_classifications": {
            "cancelled_policies": cancelled,
            "terminated_policies": terminated,
            "currently_enrolled_people": currently,
            "enrolled_cancelled": enrolled_cancelled,
            "cancelled_not_enrolled": cancelled_not_enrolled,
            "pending_people": {
                "comparison_status": NOT_COMPARABLE,
                "reason": f"{PENDING_UNAVAILABLE}: no standalone Pending People definition.",
            },
            "applications": {
                "comparison_status": NOT_COMPARABLE,
                "reason": "No equivalent PY2026 application source is evidenced in this phase.",
            },
            "new_active_passive": {
                "comparison_status": NOT_COMPARABLE,
                "reason": "Lifecycle classification is schema-only this phase. Status NEW/ACTIVE/PASSIVE is not used.",
            },
        },
        "activity_readiness": {
            "daily_py2026": "READY" if daily_ready else "BLOCKED",
            "monthly_py2026": "READY" if daily_ready else "BLOCKED",
            "terminated_daily_py2026": "READY" if term_ready else "BLOCKED",
            "py2027_daily_history": py2027_activity_history,
            "source_table": None if chosen is None else chosen.get("table"),
        },
        "activity_series": evidence.get("activity_series") or {},
        "people_cancellation_diagnostic": None if chosen is None else chosen.get("people_cancellation"),
        "profiles": [_public_profile(item) for item in profiles],
        "remaining_blockers": _blockers(
            identifier, last_update_exact, cancelled_observed, term_observed,
            overlap_total, enrolled_checks, py2027_activity_history, oe,
        ),
        "failures": evidence.get("failures") or [],
        "comparable": [],
        "command": COMMAND,
    }
    if any(item["comparison_status"] == COMPARABLE for item in (
        cancelled, terminated, currently, enrolled_cancelled, cancelled_not_enrolled,
    )):
        raise AssertionError("Phase 5B must not assign COMPARABLE without every semantic check")
    guard_no_pii(report)
    return report


def attach_activity_series(client, evidence: dict) -> dict:
    profiles = evidence.get("profiles") or []
    chosen = _choose_activity_source(profiles)
    series = {}
    if chosen is None:
        evidence["activity_series"] = series
        return evidence
    columns = chosen["columns"]
    statuses = [item["status_value"] for item in chosen.get("status_values") or []]
    cancelled_observed = any(_is_cancelled(item) for item in statuses)
    term_observed = any(_is_term(item) for item in statuses)
    last_update = "last_update_timestamp" in columns
    if evidence.get("overlap") and max((item.get("overlapping_enrollment_ids") or 0) for item in evidence["overlap"]) > 0:
        # Extraction still uses one table; overlap is recorded, never summed.
        pass
    qualified = qualified_name(chosen["schema_name"], chosen["table"])
    if event_semantics_ready(columns, cancelled_status_observed=cancelled_observed,
                             last_update_present=last_update):
        sql, params = build_daily_activity_sql(qualified, columns, cancelled=True)
        result = _run(client, evidence["failures"], "cancelled daily", sql, params)
        if result is not None:
            points = parse_daily_rows(result.rows, "cancelled_policies_daily", chosen["table"])
            series["cancelled_policies_daily"] = series_document(
                "cancelled_policies_daily", points, chosen["table"])
    if last_update and term_observed and "enrollment_id" in columns:
        sql, params = build_daily_activity_sql(qualified, columns, cancelled=False)
        result = _run(client, evidence["failures"], "terminated daily", sql, params)
        if result is not None:
            points = parse_daily_rows(result.rows, "terminated_policies_daily", chosen["table"])
            series["terminated_policies_daily"] = series_document(
                "terminated_policies_daily", points, chosen["table"])
    evidence["activity_series"] = series
    return evidence


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def render_html(report: dict) -> str:
    mapping = "".join(
        "<tr>"
        f"<td>{_e(item.get('py2027_field'))}</td>"
        f"<td>{_e(item.get('py2026_candidate'))}</td>"
        f"<td>{_e(item.get('sql_datatype'))}</td>"
        f"<td>{_e(item.get('evidence'))}</td>"
        f"<td>{_e(item.get('confidence'))}</td>"
        f"<td>{_e(item.get('notes'))}</td>"
        "</tr>"
        for item in report.get("event_date_mapping") or []
    )
    checklist = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in report.get("currently_enrolled_semantic_checklist") or []
    )
    people = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in report.get("people_cancellation_semantic_checklist") or []
    )
    blockers = "".join(f"<li>{_e(item)}</li>" for item in report.get("remaining_blockers") or [])
    classifications = report.get("metric_classifications") or {}
    metrics = "".join(
        f"<tr><td>{_e(item.get('metric_name') or key)}</td>"
        f"<td>{_e(item.get('comparison_status'))}</td><td>{_e(item.get('reason'))}</td></tr>"
        for key, item in classifications.items() if isinstance(item, dict)
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 field mapping</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:1100px}"
        "table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#f4f7fb}</style></head><body>"
        "<h1>PY2026 field mapping</h1>"
        "<p>Read-only evidence. No visualization. NULL is not zero. COMPARABLE is not assigned.</p>"
        "<h2>1. Enrollment identifier mapping</h2>"
        f"<p>{_e(report.get('enrollment_identifier_mapping'))}</p>"
        "<h2>2. Person identifier mapping</h2>"
        f"<p>{_e(report.get('person_identifier_mapping'))}</p>"
        "<h2>3. Status mapping</h2>"
        f"<p>{_e(report.get('status_mapping'))}</p>"
        "<h2>4. Event-date mapping</h2>"
        "<p><strong>Selected EVENT_DATE candidate (fail-closed):</strong> "
        f"{_e(report.get('event_date_candidate'))}</p>"
        "<p>An identifier is never selected. Name matching without a date/time SQL datatype is not enough.</p>"
        "<table><thead><tr><th>PY2027_FIELD</th><th>PY2026_CANDIDATE</th><th>SQL_DATATYPE</th>"
        "<th>EVIDENCE</th><th>CONFIDENCE</th><th>NOTES</th></tr></thead>"
        f"<tbody>{mapping}</tbody></table>"
        "<h2>5. Snapshot duplication risk</h2>"
        f"<p>{_e(report.get('snapshot_duplication_risk'))}</p>"
        "<h2>6. Currently Enrolled semantic checklist</h2>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{checklist}</tbody></table>"
        "<h2>7. People cancellation semantic checklist</h2>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{people}</tbody></table>"
        "<h2>8. Lifecycle-supporting fields</h2>"
        f"<p>{_e(report.get('lifecycle_supporting_fields'))}</p>"
        "<h2>9. OE anchor evidence</h2>"
        f"<p>{_e(report.get('oe_anchor_evidence'))}</p>"
        "<h2>10. Remaining blockers</h2>"
        f"<ul>{blockers}</ul>"
        "<h2>Metric classifications</h2>"
        "<table><thead><tr><th>Metric</th><th>Status</th><th>Reason</th></tr></thead>"
        f"<tbody>{metrics}</tbody></table>"
        "</body></html>"
    )


def print_summary(report: dict) -> None:
    cls = report.get("metric_classifications") or {}
    ident = report.get("enrollment_identifier_mapping") or {}
    person = report.get("person_identifier_mapping") or {}
    status = report.get("status_mapping") or {}
    event = report.get("event_date_candidate") or {
        "py2026_candidate": None, "confidence": ABSENT, "sql_datatype": None,
    }
    ready = report.get("activity_readiness") or {}
    oe = report.get("oe_anchor_evidence") or {}
    tables = ", ".join(item.get("table") or "" for item in report.get("profiles") or []) or "(schema not read)"
    print("PHASE 5B STATUS")
    print(f"Database source(s) inspected: {tables}")
    print(f"PY2026 enrollment-id candidate: {ident.get('py2026_candidate')} ({ident.get('confidence')})")
    print(f"PY2026 person-id candidate: {person.get('label')} ({person.get('identity_status')})")
    print(f"PY2026 status field: {status.get('py2026_candidate')}")
    datatype = event.get("sql_datatype")
    print(
        "PY2026 event-date candidate: "
        f"{event.get('py2026_candidate')} "
        f"({event.get('confidence')}"
        f"{'' if not datatype else f', {datatype}'})"
    )
    for key in ("cancelled_policies", "terminated_policies", "currently_enrolled_people",
                "enrolled_cancelled", "cancelled_not_enrolled"):
        item = cls.get(key) or {}
        print(f"{item.get('metric_name') or key}: {item.get('comparison_status')}")
        print(f"reason: {item.get('reason')}")
    print(f"OE anchors: PY2026 = {oe.get('py2026', {}).get('classification')} "
          f"PY2027 = {oe.get('py2027', {}).get('classification')}")
    print(f"Daily PY2026 activity: {ready.get('daily_py2026')}")
    print(f"Monthly PY2026 activity: {ready.get('monthly_py2026')}")
    print(f"PY2027 daily history: {ready.get('py2027_daily_history')}")


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    definition = interpret_payload(payload)
    evidence = {
        "tables": [], "profiles": [], "overlap": [], "failures": [],
        "definition": definition, "activity_series": {},
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            client = DatabaseClient(connection)
            evidence = collect_evidence(client, definition)
            evidence = attach_activity_series(client, evidence)
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(evidence)
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


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
            "inferred_filename_date": infer_filename_date(name),
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


def _identifier_columns(columns: dict) -> list[dict]:
    found = []
    for meta in columns.values():
        role = classify_identifier_column(meta["name"])
        if role:
            found.append({"column": meta["name"], "role": role, "data_type": meta.get("data_type")})
    return found


def _profile_table(client, failures, item, columns) -> dict:
    qualified = qualified_name(item["schema_name"], item["table"])
    profile = {
        **item,
        "columns": columns,
        "status_values": [],
        "duplicates": None,
        "nulls_and_ranges": None,
        "cardinality": None,
        "people_cancellation": None,
        "multi_status_people": None,
        "lifecycle_fields": item.get("lifecycle_fields") or [],
    }
    status_meta = columns.get("enrollment_status_description")
    year = None if "coverage_year" not in columns else columns["coverage_year"]["name"]
    if status_meta:
        sql, params = build_status_values_sql(qualified, status_meta["name"], year)
        result = _run(client, failures, f"status {item['table']}", sql, params)
        if result is not None:
            profile["status_values"] = [
                {"status_value": None if row[0] is None else str(row[0]), "row_count": _as_int(row[1])}
                for row in result.rows
            ]
    if "enrollment_id" in columns:
        sql, params = build_duplicate_enrollment_sql(
            qualified, columns["enrollment_id"]["name"], year)
        result = _run(client, failures, f"duplicates {item['table']}", sql, params)
        if result is not None and result.row_count == 1:
            profile["duplicates"] = {
                "row_count": _as_int(result.rows[0][0]),
                "distinct_enrollment_ids": _as_int(result.rows[0][1]),
                "duplicate_enrollment_ids": _as_int(result.rows[0][2]),
            }
    sql, params = build_null_and_range_sql(qualified, columns)
    result = _run(client, failures, f"nulls {item['table']}", sql, params)
    if result is not None and result.row_count == 1:
        profile["nulls_and_ranges"] = {
            str(column): _json_value(value)
            for column, value in zip(result.columns, result.rows[0])
        }
    person = _person_identifier(columns)
    exchange = find_exchange_column(columns)
    if "enrollee_id" in columns and exchange:
        sql, params = build_identifier_cardinality_sql(
            qualified, columns["enrollee_id"]["name"], exchange, year)
        result = _run(client, failures, f"cardinality {item['table']}", sql, params)
        if result is not None and result.row_count == 1:
            profile["cardinality"] = _cardinality(result)
    if person.get("column") and status_meta and any(
        _is_cancelled(item["status_value"]) for item in profile["status_values"]
    ):
        sql, params = build_people_cancellation_sql(
            qualified, person["column"], status_meta["name"], year)
        result = _run(client, failures, f"people cancel {item['table']}", sql, params)
        if result is not None and result.row_count == 1:
            row = result.rows[0]
            profile["people_cancellation"] = {
                "enrolled_cancelled_people": _as_int(row[0]),
                "cancelled_not_enrolled_people": _as_int(row[1]),
                "people_with_cancelled": _as_int(row[2]),
                "people_in_scope": _as_int(row[3]),
                "comparison_status": DIAGNOSTIC,
            }
            profile["multi_status_people"] = _as_int(row[0])
    return profile


def _overlap_pairs(client, failures, profiles: list[dict]) -> list[dict]:
    usable = [
        item for item in profiles
        if "enrollment_id" in (item.get("columns") or {})
    ]
    pairs = []
    for left, right in zip(usable, usable[1:]):
        if len(pairs) >= 3:
            break
        year = None
        if "coverage_year" in left["columns"] and "coverage_year" in right["columns"]:
            year = left["columns"]["coverage_year"]["name"]
        sql, params = build_overlap_sql(
            qualified_name(left["schema_name"], left["table"]),
            qualified_name(right["schema_name"], right["table"]),
            left["columns"]["enrollment_id"]["name"],
            year,
        )
        result = _run(client, failures, f"overlap {left['table']} {right['table']}", sql, params)
        overlapping = None if result is None or result.row_count != 1 else _as_int(result.rows[0][0])
        pairs.append({
            "left": left["table"],
            "right": right["table"],
            "overlapping_enrollment_ids": overlapping,
        })
    return pairs


def _choose_activity_source(profiles: list[dict]) -> dict | None:
    complete = [
        item for item in profiles
        if "enrollment_id" in (item.get("columns") or {})
        and "enrollment_status_description" in item["columns"]
        and "last_update_timestamp" in item["columns"]
    ]
    if complete:
        return complete[0]
    if profiles:
        return profiles[0]
    return None


def _person_identifier(columns: dict) -> dict:
    exchange = find_exchange_column(columns)
    requirement = default_identifier_requirement()
    if exchange is not None:
        return {
            "column": exchange,
            "label": exchange,
            "role": "exchange_individual",
            "identity_status": "EXACT",
            "reason": "A snapshot column matches an exchange individual identifier name. Equivalence of values is not proven against Sisense.",
        }
    if "enrollee_id" in columns:
        return {
            "column": columns["enrollee_id"]["name"],
            "label": "ENROLLEE_ID_PROXY",
            "role": "enrollee",
            "identity_status": IDENTIFIER_UNPROVEN,
            "reason": requirement.reason,
        }
    return {
        "column": None,
        "label": None,
        "role": None,
        "identity_status": IDENTIFIER_UNPROVEN,
        "reason": "No exchange individual identifier or enrollee_id column was found.",
    }


def _cardinality(result) -> dict:
    row = {str(column): _as_int(value) for column, value in zip(result.columns, result.rows[0])}
    enrollee_many = row.get("enrollee_to_many_exchange") or 0
    exchange_many = row.get("exchange_to_many_enrollee") or 0
    if enrollee_many == 0 and exchange_many == 0:
        mapping = "1:1"
    elif enrollee_many > 0 and exchange_many == 0:
        mapping = "1:many"
    elif enrollee_many == 0 and exchange_many > 0:
        mapping = "many:1"
    else:
        mapping = "many:many"
    return {
        "paired_rows": row.get("paired_rows"),
        "distinct_enrollee_keys": row.get("distinct_enrollee_keys"),
        "distinct_exchange_keys": row.get("distinct_exchange_keys"),
        "null_enrollee_keys": row.get("null_enrollee_keys"),
        "null_exchange_keys": row.get("null_exchange_keys"),
        "enrollee_to_many_exchange": enrollee_many,
        "exchange_to_many_enrollee": exchange_many,
        "mapping_cardinality": mapping,
    }


def _public_profile(item: dict) -> dict:
    return {
        "schema_name": item.get("schema_name"),
        "table": item.get("table"),
        "inferred_filename_date": item.get("inferred_filename_date"),
        "identifier_columns": item.get("identifier_columns") or [],
        "lifecycle_fields": item.get("lifecycle_fields") or [],
        "status_values": item.get("status_values") or [],
        "duplicates": item.get("duplicates"),
        "nulls_and_ranges": item.get("nulls_and_ranges"),
        "cardinality": item.get("cardinality"),
        "people_cancellation": item.get("people_cancellation"),
        "column_names": sorted((item.get("columns") or {}).keys()),
    }


def _blockers(identifier, last_update_exact, cancelled_observed, term_observed,
              overlap, enrolled_checks, py2027_history, oe) -> list[str]:
    blockers = []
    if identifier.get("identity_status") != "EXACT":
        blockers.append("Person identifier equivalence is unproven.")
    if not last_update_exact:
        blockers.append("Sisense last_update_timestamp is not present as that column name.")
    if not cancelled_observed:
        blockers.append("Cancelled was not observed as an enrollment status value.")
    if not term_observed:
        blockers.append("No Term* enrollment status values were observed.")
    if overlap:
        blockers.append("The same enrollment_id appears in more than one snapshot; tables must not be summed.")
    blockers.extend(
        f"Currently Enrolled check unresolved: {item['check']}"
        for item in enrolled_checks if item["status"] != "PASS"
    )
    if py2027_history != "READY":
        blockers.append("PY2027 daily activity history is not persistently available.")
    if oe.get("py2026", {}).get("classification") == UNKNOWN:
        blockers.append("PY2026 OE anchor is unknown; OE-day alignment is unresolved.")
    if oe.get("py2027", {}).get("classification") == UNKNOWN:
        blockers.append("PY2027 OE anchor is unknown; OE-day alignment is unresolved.")
    return blockers


def _map_row(py2027, py2026, confidence, notes) -> dict:
    return {
        "py2027_field": py2027,
        "py2026_candidate": py2026,
        "evidence": "INFORMATION_SCHEMA plus aggregate status/date inspection",
        "confidence": confidence,
        "notes": notes,
    }


def _status_note(cancelled_observed, term_observed, present) -> str:
    if not present:
        return "Status column is absent."
    bits = []
    if cancelled_observed:
        bits.append("Cancelled observed")
    if term_observed:
        bits.append("Term* observed")
    return "; ".join(bits) if bits else "Column present; Sisense members not observed in this snapshot."


def _filter_status(mappings, concept) -> str:
    row = next((item for item in mappings if item.get("sisense_concept") == concept), None)
    if row is None:
        return "UNRESOLVED"
    status = row.get("translation_status")
    if status in {"EXACT", "PLAN_YEAR_SHIFTED_EQUIVALENT"}:
        return "PASS"
    return "UNRESOLVED"


def _filter_reason(mappings, concept) -> str | None:
    row = next((item for item in mappings if item.get("sisense_concept") == concept), None)
    return None if row is None else row.get("reason")


def _is_cancelled(value) -> bool:
    if value is None:
        return False
    return str(value).strip().casefold() == "cancelled"


def _is_term(value) -> bool:
    if value is None:
        return False
    return str(value).strip().casefold().startswith("term")


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
    if isinstance(value, date):
        return value.isoformat()
    return value


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


if __name__ == "__main__":
    raise SystemExit(main())
