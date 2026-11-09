"""Phase 8B: passive evidence of PY2027 enrollment create/confirm activity.

Consumes WidgetStructure records the existing discovery observer already holds
in memory. Nothing here sends, replays, mutates or builds a Sisense request.
Raw bodies, member lists, cookies, tokens and headers are never read or written.

    python -m comparison.py2027_enrollment_activity_evidence   (artifact rebuild from observer is live-only)
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.py2026_py2027_metric_bridge import ACTIVITY_WIDGET_ID, CURRENTLY_ENROLLED_WIDGET_ID

logger = logging.getLogger("oe_trend_pipeline.activity_evidence")

JSON_NAME = "py2027_enrollment_activity_evidence.json"
HTML_NAME = "py2027_enrollment_activity_evidence.html"

EXACT = "EXACT_COUNTERPART_CANDIDATE"
PARTIAL = "PARTIAL_EVIDENCE"
WRONG_GRAIN = "WRONG_GRAIN"
WRONG_DATE = "WRONG_DATE_SEMANTICS"
LIFECYCLE = "LIFECYCLE_CLASSIFICATION"
NO_EVIDENCE = "NO_RELEVANT_EVIDENCE"

CREATED_PROVEN = "CREATED_COUNTERPART_PROVEN"
CONFIRMED_PROVEN = "CONFIRMED_COUNTERPART_PROVEN"
BOTH_PROVEN = "BOTH_COUNTERPARTS_PROVEN"
PARTIAL_ONLY = "PARTIAL_EVIDENCE_ONLY"
NOT_FOUND = "NO_COUNTERPART_IN_OBSERVED_NATURAL_TRAFFIC"

PERSON, POLICY, APPLICATION, APPLICANT, UNKNOWN = "PERSON", "POLICY", "APPLICATION", "APPLICANT", "UNKNOWN"
CREATED, CONFIRMED = "created", "confirmed"

NAP_WIDGET_ID = "6a98048ccef60720fe7f9b6e"
STARTED_WIDGET_ID = "6a98048ccef60720fe7f9b44"
SUBMITTED_WIDGET_ID = "6a98048ccef60720fe7f9b66"
APPLICANTS_WIDGET_ID = "6a98048ccef60720fe7f9b4f"

KNOWN_WIDGETS = {
    CURRENTLY_ENROLLED_WIDGET_ID: "currently_enrolled",
    ACTIVITY_WIDGET_ID: "cancelled_terminated_daily",
    NAP_WIDGET_ID: "new_active_passive",
    STARTED_WIDGET_ID: "applications_started",
    SUBMITTED_WIDGET_ID: "submitted_applications",
    APPLICANTS_WIDGET_ID: "total_applicants",
}

COUNT_AGGS = frozenset({"count", "dupcount", "countduplicates"})
AUTH_KEYS = frozenset({"cookie", "cookies", "set-cookie", "authorization", "token", "access_token", "csrf",
                       "x-xsrf-token", "headers", "post_data", "password", "secret", "body", "request_body",
                       "members", "raw", "postdata"})

FORBIDDEN_OUTPUT = AUTH_KEYS | {
    "enrollee_id", "enrollment_id", "exchg_indiv_identifier", "ssn", "cookie", "authorization",
}


class EvidenceOutputError(ValueError):
    """The evidence report refused an unsafe key or value."""


def _name(table: str | None, column: str | None) -> str | None:
    if table and column:
        return f"{table}.{column}"
    return column or table


def _lower(value: str | None) -> str:
    return (value or "").strip().lower()


def counted_entity(measure) -> tuple[str | None, str | None]:
    if measure is None:
        return None, None
    if measure.table and measure.column:
        return measure.table, measure.column
    fields = tuple(measure.context_fields or ())
    if not fields:
        return None, None
    preferred = [item for item in fields if _grain(item.table, item.column) != UNKNOWN]
    chosen = (preferred or fields)[0]
    return chosen.table, chosen.column


def _grain(table: str | None, column: str | None) -> str:
    blob = f"{_lower(table)} {_lower(column)}"
    if "applicant" in blob:
        return APPLICANT
    if "application" in blob:
        return APPLICATION
    if "enrollee" in blob or "exchg_indiv" in blob or "person" in blob:
        return PERSON
    if "enrollment" in blob and ("id" in _lower(column) or _lower(column) in {"oid", "key"}):
        return POLICY
    if _lower(table) == "enrollment" and _lower(column) in {"id", "enrollment_id"}:
        return POLICY
    return UNKNOWN


def _date_family(table: str | None, column: str | None) -> str | None:
    col, tbl = _lower(column), _lower(table)
    if not col:
        return None
    if "last_update" in col:
        return "last_update"
    if "benefit_effective" in col or ( "benefit" in col and "effective" in col):
        return "benefit_effective"
    if "benefit_end" in col or ("benefit" in col and col.endswith("_end")):
        return "benefit_end"
    enrollment = tbl == "enrollment" or tbl.startswith("enrollment")
    if enrollment and "confirm" in col:
        return CONFIRMED
    if enrollment and ("creat" in col or col in {"created_ts", "create_ts", "created_at"}):
        return CREATED
    if "application" in tbl and "confirm" in col:
        return "application_other"
    if "application" in tbl and "creat" in col:
        return "application_created"
    if "application" in tbl and "submit" in col:
        return "application_submitted"
    if "confirm" in col:
        return CONFIRMED if enrollment else "unscoped_confirm"
    if "creat" in col:
        return CREATED if enrollment else "unscoped_create"
    return "other"


def _time_nature(level: str | None, has_date: bool) -> str:
    if not has_date:
        return "snapshot"
    grain = _lower(level)
    if grain in {"days", "day"}:
        return "daily"
    if grain in {"months", "month", "years", "year", "quarters", "weeks"}:
        return "monthly" if "month" in grain else grain
    return "unknown"


def _scope_flags(roles) -> dict:
    coverage = insurance = status = False
    context = []
    for role in roles or ():
        column = _lower(role.column)
        field = _name(role.table, role.column)
        kind = role.filter_kind
        if role.kind != "filter" and getattr(role, "panel", None) != "scope":
            continue
        if field:
            context.append({"field": field, "filter_kind": kind})
        if "coverage_year" in column or column in {"plan_year", "coverageyear"}:
            coverage = True
        if "insurance" in column:
            insurance = True
        if "status" in column:
            status = True
    return {"coverage_year": coverage, "insurance_type": insurance, "status": status, "filters": context}


def _known_classification(widget_id: str | None, date_family: str | None) -> tuple[str, str] | None:
    known = KNOWN_WIDGETS.get(widget_id or "")
    if known == "currently_enrolled":
        return WRONG_DATE, ("Currently Enrolled is a point-in-time population snapshot, not enrollment "
                            "created or confirmed activity.")
    if known == "cancelled_terminated_daily":
        return WRONG_DATE, ("Cancelled/Terminated daily activity uses last-update timestamp semantics, "
                            "not enrollment create or confirmation dates.")
    if known == "new_active_passive":
        return LIFECYCLE, "New/Active/Passive is a lifecycle classification, not enrollment event activity."
    if known in {"applications_started", "submitted_applications"}:
        return WRONG_GRAIN, "Applications Started/Submitted are application grain under current evidence."
    if known == "total_applicants":
        return WRONG_GRAIN, "Total Applicants is applicant grain, not enrollment-person activity."
    if date_family in {"last_update", "benefit_effective", "benefit_end"}:
        return WRONG_DATE, f"Date field event family '{date_family}' is not enrollment create or confirmation."
    return None


def classify_measure(structure, measure) -> dict:
    """Classify one naturally observed measure. Titles never create an exact match by themselves."""
    counted_table, counted_column = counted_entity(measure)
    measure_roles = [role for role in structure.metadata_roles if role.kind == "measure"]
    named = [role for role in measure_roles if measure is not None and role.title == measure.title]
    aggregations = tuple((named or measure_roles)[0].aggregation_functions if (named or measure_roles) else ())
    dates = [dim for dim in structure.dimensions if dim.is_date]
    date = dates[0] if len(dates) == 1 else None
    date_family = _date_family(date.table, date.column) if date is not None else None
    grain = _grain(counted_table, counted_column)
    scope = _scope_flags(structure.metadata_roles)
    counterpart = None
    if date_family == CREATED:
        counterpart = CREATED
    elif date_family == CONFIRMED:
        counterpart = CONFIRMED

    known = _known_classification(structure.widget_id, date_family)
    missing = []
    if counted_table is None or counted_column is None:
        missing.append("counted_field")
    if not aggregations:
        missing.append("aggregation")
    if date is None:
        missing.append("date_dimension")
    if not scope["coverage_year"]:
        missing.append("coverage_year_context")

    if known is not None:
        classification, reason = known
        counterpart = None
    elif grain in {APPLICATION, APPLICANT}:
        classification, reason = WRONG_GRAIN, f"{grain} grain is not enrollment-person activity."
        counterpart = None
    elif date_family in {"last_update", "benefit_effective", "benefit_end", "application_created",
                         "application_submitted", "application_other", "other"}:
        classification, reason = WRONG_DATE, f"Date field event family '{date_family}' is not enrollment create/confirm."
        counterpart = None
    elif date_family in {"unscoped_create", "unscoped_confirm"}:
        classification, reason = PARTIAL, ("A create/confirm-like date column is present but the table is not a "
                                           "captured enrollment entity; not inferred as an enrollment event.")
        counterpart = None
    elif grain == POLICY and date_family in {CREATED, CONFIRMED} and not missing:
        classification, reason = PARTIAL, ("Policy grain is recorded; it is not an automatic match to the PY2026 "
                                           "person-grain COUNT(DISTINCT enrollee_id) series.")
    elif not missing and grain == PERSON and date_family in {CREATED, CONFIRMED} and COUNT_AGGS.intersection(aggregations):
        classification, reason = EXACT, ("Person identifier, enrollment create/confirm date, aggregation and "
                                         "coverage-year context are all captured from natural traffic.")
    elif date_family in {CREATED, CONFIRMED}:
        classification, reason = PARTIAL, "Relevant create/confirm evidence is incomplete: " + ", ".join(missing or ["context"])
    elif "date_dimension" in missing and counted_column and COUNT_AGGS.intersection(aggregations):
        classification, reason = PARTIAL, "Relevant create/confirm evidence is incomplete: date_dimension"
        counterpart = None
    else:
        classification, reason = NO_EVIDENCE, "Observed definition does not contain enrollment create/confirm activity."
        counterpart = None

    return {
        "widget": structure.widget_id,
        "title": structure.widget_title,
        "measure": None if measure is None else measure.title,
        "aggregation": list(aggregations) or None,
        "counted_field": _name(counted_table, counted_column),
        "date_dimension": None if date is None else _name(date.table, date.column),
        "date_grain": None if date is None else date.level,
        "date_family": date_family,
        "grain": grain,
        "time_nature": _time_nature(None if date is None else date.level, date is not None),
        "coverage_year_context": scope["coverage_year"],
        "insurance_type_context": scope["insurance_type"],
        "status_context": scope["status"],
        "filters": scope["filters"],
        "known_metric": KNOWN_WIDGETS.get(structure.widget_id),
        "counterpart": counterpart,
        "classification": classification,
        "reason": reason,
    }


def inventory_from_structures(structures) -> list[dict]:
    """One pass over already-normalized widget structures. No request access."""
    rows = []
    for structure in structures:
        measures = tuple(structure.measures) or (None,)
        for measure in measures:
            rows.append(classify_measure(structure, measure))
    return rows


def structures_from_observer(observer) -> list:
    """Latest in-memory structure per retained definition. Does not touch Request objects."""
    if observer is None:
        return []
    out = []
    for entry in observer.entries():
        definitions = getattr(entry, "definitions", None) or {}
        if definitions:
            for item in definitions.values():
                structure = getattr(item, "structure", None) or getattr(entry, "structure", None)
                if structure is not None:
                    out.append(structure)
        elif getattr(entry, "structure", None) is not None:
            out.append(entry.structure)
    return out


def _summary(rows: list[dict]) -> dict:
    def status(kind: str) -> str:
        matches = [row for row in rows if row.get("counterpart") == kind]
        if any(row["classification"] == EXACT for row in matches):
            return "PROVEN"
        if any(row["classification"] == PARTIAL for row in matches):
            return "PARTIAL"
        return "NOT_FOUND"

    created, confirmed = status(CREATED), status(CONFIRMED)
    exact_created = any(row["classification"] == EXACT and row.get("counterpart") == CREATED for row in rows)
    exact_confirmed = any(row["classification"] == EXACT and row.get("counterpart") == CONFIRMED for row in rows)
    if exact_created and exact_confirmed:
        decision = BOTH_PROVEN
    elif exact_created:
        decision = CREATED_PROVEN
    elif exact_confirmed:
        decision = CONFIRMED_PROVEN
    elif any(row["classification"] == PARTIAL for row in rows):
        decision = PARTIAL_ONLY
    else:
        decision = NOT_FOUND
    return {"created_counterpart": created, "confirmed_counterpart": confirmed, "decision": decision}


def build_report(rows: list[dict], captured_at: str | None = None, requests_seen: int | None = None) -> dict:
    summary = _summary(rows)
    report = {
        "report_kind": "PY2027_ENROLLMENT_ACTIVITY_EVIDENCE",
        "phase": "8B",
        "captured_at_utc": captured_at,
        "origin": "Natural dashboard JAQL already retained by the discovery observer. No request sent, replayed or altered.",
        "dashboard_requests_seen": requests_seen,
        "summary": {
            "created_counterpart": summary["created_counterpart"],
            "confirmed_counterpart": summary["confirmed_counterpart"],
        },
        "candidates": rows,
        "final_decision": summary["decision"],
        "production_comparable": False,
        "combined_chart_wiring": "NOT_CHANGED",
        "note": ("An exact candidate is not promoted into COMPATIBLE_PY2027_ACTIVITY_METRICS or activity_monthly "
                 "in this phase."),
    }
    guard_report(report)
    return report


def guard_report(report: dict) -> None:
    guard_no_pii(report)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in FORBIDDEN_OUTPUT or str(key).lower() in AUTH_KEYS:
                    raise EvidenceOutputError(f"Evidence report refused key {key}")
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
    walk(report)


def render_html(report: dict) -> str:
    summary = report["summary"]
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('widget') or ''))}</td>"
        f"<td>{html.escape(str(item.get('title') or ''))}</td>"
        f"<td>{html.escape(str(item.get('measure') or '—'))}</td>"
        f"<td>{html.escape(str(item.get('counted_field') or '—'))}</td>"
        f"<td>{html.escape(str(item.get('date_dimension') or '—'))}</td>"
        f"<td>{html.escape(str(item.get('grain') or '—'))}</td>"
        f"<td>{html.escape(str(item.get('classification') or ''))}</td>"
        f"<td>{html.escape(str(item.get('reason') or ''))}</td>"
        "</tr>"
        for item in report.get("candidates") or []
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>PY2027 enrollment activity evidence</title>"
        "<style>body{font-family:sans-serif;margin:24px;color:#1e293b}"
        "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e2e8f0;padding:6px 8px;"
        "text-align:left;vertical-align:top;font-size:13px}th{color:#64748b}</style></head><body>"
        "<h1>PY2027 enrollment activity evidence</h1>"
        f"<p>Created counterpart: <b>{html.escape(summary['created_counterpart'])}</b><br>"
        f"Confirmed counterpart: <b>{html.escape(summary['confirmed_counterpart'])}</b></p>"
        f"<p>Final decision: <b>{html.escape(report['final_decision'])}</b></p>"
        f"<p>{html.escape(report['origin'])}</p>"
        f"<table><tr><th>Widget</th><th>Title</th><th>Measure</th><th>Counted field</th>"
        f"<th>Date dimension</th><th>Grain</th><th>Classification</th><th>Reason</th></tr>{rows or ''}</table>"
        "</body></html>"
    )


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_report(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def report_from_observer(observer) -> dict:
    rows = inventory_from_structures(structures_from_observer(observer))
    return build_report(
        rows,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        getattr(observer, "target_requests_seen", None),
    )


def write_enrollment_activity_evidence(observer, reports_dir: Path | None) -> Path | None:
    """Called from existing discovery finalization. Never raises. Never sends a request."""
    if reports_dir is None:
        return None
    try:
        report = report_from_observer(observer)
        json_path, _ = write_report(report, Path(reports_dir) / "comparison")
        logger.info("Phase 8B enrollment activity evidence: %s; %s", report["final_decision"], json_path)
        return json_path
    except Exception as exc:
        logger.error("Phase 8B enrollment activity evidence failed: %s", type(exc).__name__)
        return None
