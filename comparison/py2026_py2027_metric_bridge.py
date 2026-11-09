"""Phase 6A: PY2026 database <-> PY2027 Sisense metric semantic bridge.

Planning/report only. No database connection, no Sisense request, no metric
logic change. PY2026 = dbo.Enrollments_TEST (coverage_year = 2026), chosen as
the canonical analytical query surface by Phase 5D data equivalence; that is
not a claim of system authority. PY2027 = current Sisense dashboard.

Classification order (first match wins):
  WRONG_SOURCE_FAMILY            metric grain is not in the enrollment table
  INSUFFICIENT_EVIDENCE          Sisense semantics or a DB field are not proven
  BLOCKED_EVENT_HISTORY          needs as-of event history a latest-state table lacks
  BLOCKED_LIFECYCLE_RULES        rule logic was never captured
  BLOCKED_IDENTIFIER_EQUIVALENCE identifier equivalence is unproven
  DIAGNOSTIC_CANDIDATE           computable, with stated filter/period gaps
  READY_FOR_AGGREGATE_VALIDATION no remaining gap
No metric is production comparable in this phase.

    python -m comparison.py2026_py2027_metric_bridge
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from comparison.currently_enrolled import (
    OUTPUT_DIR,
    VERIFIED_PAYLOAD_FILE,
    guard_no_pii,
    interpret_payload,
)

JSON_NAME = "py2026_py2027_metric_bridge.json"
HTML_NAME = "py2026_py2027_metric_bridge.html"
COMMAND = r".\.venv\Scripts\python.exe -m comparison.py2026_py2027_metric_bridge"

CANONICAL_SOURCE = "dbo.Enrollments_TEST"
PY2026_YEAR = 2026
PROVEN_FIELDS = (
    "coverage_year", "enrollment_id", "enrollee_id", "enrollment_status_description",
    "enrollee_status_description", "enrollment_create_date", "enrollment_confirmation_date",
    "enrollment_last_update_date", "benefit_effective_date", "benefit_end_date",
    "hios_issuer_id", "application_type",
)
ACTIVITY_WIDGET_ID = "6a98048ccef60720fe7f9b39"
CURRENTLY_ENROLLED_WIDGET_ID = "6a98048ccef60720fe7f9b47"

READY = "READY_FOR_AGGREGATE_VALIDATION"
DIAGNOSTIC = "DIAGNOSTIC_CANDIDATE"
BLOCKED_IDENTIFIER = "BLOCKED_IDENTIFIER_EQUIVALENCE"
BLOCKED_LIFECYCLE = "BLOCKED_LIFECYCLE_RULES"
BLOCKED_EVENT_HISTORY = "BLOCKED_EVENT_HISTORY"
WRONG_SOURCE_FAMILY = "WRONG_SOURCE_FAMILY"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
CLASSIFICATIONS = (READY, DIAGNOSTIC, BLOCKED_IDENTIFIER, BLOCKED_LIFECYCLE,
                   BLOCKED_EVENT_HISTORY, WRONG_SOURCE_FAMILY, INSUFFICIENT)

# Sisense scope field -> proven PY2026 field. Fields absent here have no proven DB column.
SCOPE_FIELD_MAP = {
    "coverage_year": "coverage_year",
    "benefit_effective_date": "benefit_effective_date",
    "enrollment_status_description": "enrollment_status_description",
    "enrollee_status_description": "enrollee_status_description",
}
DATASET_LABEL = "PY2026 Oct-31-equivalent latest-state dataset"
PLAN_YEAR_SHIFT_ASSUMPTION = "PLAN_YEAR_SHIFT_ASSUMPTION"
PERIOD_GAP = (
    f"Lifecycle point: dbo.Enrollments_TEST is a {DATASET_LABEL} (equivalent, for the tested PY2026 "
    "records and business fields, to dbo.Enrollments_PY2526_10312026); PY2027 Sisense is read during "
    "PY2027 Open Enrollment. The equivalent PY2026 lifecycle point is not established."
)
COVERAGE_YEAR_GAP = (
    "Sisense filters ssap_applications.coverage_year; the DB coverage_year column is on the "
    "enrollment table. Same name, unproven same meaning."
)
IDENTIFIER_GAP_PERSON = (
    "Sisense counts enrollee.exchg_indiv_identifier; the DB has enrollee_id only. "
    "enrollee_id is a proxy until equivalence is proven."
)
IDENTIFIER_GAP_POLICY = "Sisense counts enrollment.id; DB enrollment_id is name-similar, equivalence unproven."
NOT_CAPTURED = "not captured in repository evidence"


@dataclass(frozen=True)
class BridgeSpec:
    metric_id: str
    metric: str
    family: str
    widget: str
    measure: str | None
    sisense_grain: str | None
    sisense_identifier: str | None
    sisense_date: str
    sisense_filters: tuple[str, ...]
    db_fields: tuple[str, ...]
    candidate_grain: str | None
    candidate_formula: str | None
    identifier_candidate: str | None = None
    identifier_proven: bool = False
    source_family_supported: bool = True
    requires_event_history: bool = False
    lifecycle_rules_captured: bool | None = None
    unmapped_filters: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    phase_6b_diagnostic: bool = False
    notes: tuple[str, ...] = field(default=())


def classify(spec: BridgeSpec, proven_fields=PROVEN_FIELDS) -> str:
    if not spec.source_family_supported:
        return WRONG_SOURCE_FAMILY
    if spec.sisense_grain is None or any(name not in proven_fields for name in spec.db_fields):
        return INSUFFICIENT
    if spec.requires_event_history:
        return BLOCKED_EVENT_HISTORY
    if spec.lifecycle_rules_captured is False:
        return BLOCKED_LIFECYCLE
    if spec.identifier_candidate is None or spec.identifier_candidate not in proven_fields:
        return INSUFFICIENT
    if not spec.identifier_proven:
        return BLOCKED_IDENTIFIER
    if spec.unmapped_filters or spec.gaps:
        return DIAGNOSTIC
    return READY


def semantic_gaps(spec: BridgeSpec, proven_fields=PROVEN_FIELDS) -> list[str]:
    gaps = list(spec.gaps)
    missing = [name for name in spec.db_fields if name not in proven_fields]
    if missing:
        gaps.insert(0, "Fields not proven in the canonical source: " + ", ".join(missing))
    for item in spec.unmapped_filters:
        gaps.append(f"Sisense filter {item} has no proven PY2026 field.")
    return gaps


def matrix_row(spec: BridgeSpec, proven_fields=PROVEN_FIELDS) -> dict:
    classification = classify(spec, proven_fields)
    return {
        "metric_id": spec.metric_id,
        "metric": spec.metric,
        "family": spec.family,
        "py2027_widget": spec.widget,
        "py2027_measure": spec.measure,
        "py2027_sisense_grain": spec.sisense_grain or NOT_CAPTURED,
        "py2027_identifier": spec.sisense_identifier or NOT_CAPTURED,
        "py2027_date_semantics": spec.sisense_date,
        "py2027_filter_status_semantics": list(spec.sisense_filters),
        "available_py2026_db_fields": [name for name in spec.db_fields if name in proven_fields],
        "candidate_py2026_grain": spec.candidate_grain,
        "candidate_formula": spec.candidate_formula,
        "semantic_gaps": semantic_gaps(spec, proven_fields),
        "classification": classification,
        "phase_6b_diagnostic": spec.phase_6b_diagnostic and classification not in (WRONG_SOURCE_FAMILY, INSUFFICIENT),
        "notes": list(spec.notes),
        "production_comparable": False,
    }


# --- verified Currently Enrolled payload evidence ---------------------------------

def load_definition(path: Path = VERIFIED_PAYLOAD_FILE) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    definition = interpret_payload(payload)
    definition["effectuated"] = effectuated_terms(payload)
    return definition


def effectuated_terms(payload: dict) -> dict | None:
    """Numerator and denominator status members of the '% Effectuated' measure, in formula order."""
    for item in payload.get("metadata") or []:
        jaql = item.get("jaql") or {}
        if jaql.get("title") != "% Effectuated":
            continue
        context = jaql.get("context") or {}
        order = [key for key in re.findall(r"\[[^\]]+\]", str(jaql.get("formula") or ""))
                 if (context.get(key) or {}).get("filter")]
        if len(order) != 2:
            return None
        terms = [context[key] for key in order]
        return {
            "identifier": _identifier(context),
            "status_field": _field(terms[0].get("dim")),
            "numerator_members": list(terms[0]["filter"].get("members") or []),
            "denominator_members": list(terms[1]["filter"].get("members") or []),
        }
    return None


def _identifier(context: dict) -> str | None:
    for value in context.values():
        if not value.get("filter"):
            return str(value.get("dim") or "").strip("[]") or None
    return None


def _field(dim) -> str | None:
    text = str(dim or "").strip("[]")
    return text.split(".", 1)[1] if "." in text else (text or None)


def scope_filters(definition: dict) -> list[dict]:
    rows = []
    for item in definition.get("scope_filters") or []:
        nested = item.get("nested_filter") or {}
        excluded = list(((nested.get("exclude") or {}).get("members")) or [])
        rows.append({
            "field": f"{item.get('table')}.{item.get('column')}",
            "column": item.get("column"),
            "members": list(item.get("members") or []),
            "excluded": excluded,
            "from": item.get("from"),
            "to": item.get("to"),
            "py2026_field": SCOPE_FIELD_MAP.get(str(item.get("column"))),
        })
    return rows


def describe_filter(row: dict) -> str:
    if row["from"] or row["to"]:
        return f"{row['field']} from {row['from']} to {row['to']}"
    text = f"{row['field']} in {row['members']}"
    if row["excluded"]:
        text += f" with nested exclude {row['excluded']}"
    return text


def shift_year(text: str | None, years: int = -1) -> str | None:
    if not text:
        return None
    value = date.fromisoformat(str(text)[:10])
    try:
        return value.replace(year=value.year + years).isoformat()
    except ValueError:
        return value.replace(year=value.year + years, day=28).isoformat()


def _effective(row: dict) -> list[str]:
    return [member for member in row["members"] if member not in row["excluded"]]


def _in(column: str, members: list[str]) -> str:
    return f"{column} IN ({', '.join(repr(member) for member in members)})"


def condition(column: str, op: str, values: list, label: str | None = None) -> dict:
    """Structured predicate shared with Phase 6B. op: '=', 'in', 'like', 'day_range' (inclusive days)."""
    return {"column": column, "op": op, "values": list(values), "label": label}


def condition_text(item: dict) -> str:
    column, values = item["column"], item["values"]
    if item["op"] == "=":
        return f"{column} = {values[0]!r}" if isinstance(values[0], str) else f"{column} = {values[0]}"
    if item["op"] == "in":
        return _in(column, values)
    if item["op"] == "like":
        return f"{column} LIKE {values[0]!r}"
    if item["op"] == "day_range":
        return f"{column} BETWEEN '{values[0]}' AND '{values[1]}'"
    raise ValueError(f"Unknown condition operator {item['op']!r}")


def currently_enrolled_candidates(definition: dict | None) -> list[dict]:
    """Competing diagnostic definitions. None is preferred; Phase 6B validates semantics, not closeness."""
    if definition is None:
        return []
    filters = {row["column"]: row for row in scope_filters(definition)}
    base = [condition("coverage_year", "=", [PY2026_YEAR])]
    benefit = filters.get("benefit_effective_date")
    if benefit and (benefit["from"] or benefit["to"]):
        base.append(condition("benefit_effective_date", "day_range",
                              [shift_year(benefit["from"]), shift_year(benefit["to"])], PLAN_YEAR_SHIFT_ASSUMPTION))
    enrollee = filters.get("enrollee_status_description")
    enrollment = filters.get("enrollment_status_description")
    formula = "COUNT(DISTINCT enrollee_id)"
    candidates = [("CE1", "No status predicate (status filters treated as ignored by the widget).", [])]
    if enrollee:
        candidates.append(("CE2", "Enrollee status members minus nested exclusion; enrollment status ignored.",
                           [condition("enrollee_status_description", "in", _effective(enrollee))]))
        candidates.append(("CE3", "Enrollee status members, nested exclusion not applied; enrollment status ignored.",
                           [condition("enrollee_status_description", "in", enrollee["members"])]))
    if enrollee and enrollment:
        candidates.append(("CE4", "Both status filters as captured, nested exclusion applied (control: contradicts "
                                  "the confirmed ignore of the enrollment-status dashboard filter).",
                           [condition("enrollee_status_description", "in", _effective(enrollee)),
                            condition("enrollment_status_description", "in", _effective(enrollment))]))
    unmapped = [row for row in filters.values() if row["py2026_field"] is None]
    return [{
        "candidate_id": cid,
        "description": text,
        "conditions": base + extra,
        "predicates": [condition_text(item) for item in base + extra],
        "formula": f"{formula} WHERE " + " AND ".join(condition_text(item) for item in base + extra),
        "grain": "person (enrollee_id proxy)",
        "status": "DIAGNOSTIC_ONLY",
        "not_applicable_filters": [describe_filter(row) for row in unmapped],
        "unavailable_filter_fields": [row["column"] for row in unmapped],
        "assumptions": [f"{PLAN_YEAR_SHIFT_ASSUMPTION}: benefit window shifted back one plan year from the "
                        "PY2027 widget window; not proven equivalent."] if benefit else [],
        "production_comparable": False,
    } for cid, text, extra in candidates]


def effectuated_candidate(definition: dict | None) -> str | None:
    terms = (definition or {}).get("effectuated")
    if not terms or terms["status_field"] not in PROVEN_FIELDS:
        return None
    return (f"COUNT(DISTINCT enrollee_id WHERE {_in(terms['status_field'], terms['numerator_members'])}) / "
            f"COUNT(DISTINCT enrollee_id WHERE {_in(terms['status_field'], terms['denominator_members'])}), "
            "within a Currently Enrolled candidate scope")


# --- metric specifications --------------------------------------------------------

def build_specs(definition: dict | None) -> list[BridgeSpec]:
    filters = scope_filters(definition) if definition else []
    described = tuple(describe_filter(row) for row in filters)
    unmapped = tuple(row["field"] for row in filters if row["py2026_field"] is None)
    ce_identifier = None
    if definition:
        fields = (definition.get("measure") or {}).get("fields") or []
        ce_identifier = fields[0].get("dim").strip("[]") if fields and fields[0].get("dim") else None
    status_gap = ("The enrollment-status dashboard filter is confirmed ignored by the widget; whether the "
                  "enrollee-status filter and its nested exclusion apply is unresolved (see candidates CE1-CE4).")
    effectuated = (definition or {}).get("effectuated")
    person_fields = ("coverage_year", "enrollee_id", "enrollment_status_description", "enrollee_status_description",
                     "benefit_effective_date")
    lifecycle_gaps = (
        "Widget case logic (OE-boundary dates, prior-coverage rule) was never captured; only measure titles are known.",
        "Prior-plan-year coverage is required and is outside the locked PY2026 source.",
        "application_type values are not proven to encode New/Active/Passive.",
        IDENTIFIER_GAP_PERSON,
        PERIOD_GAP,
    )
    activity_gaps = (
        "Sisense buckets current rows by their current last-update day during PY2027 OE. "
        f"dbo.Enrollments_TEST is a {DATASET_LABEL}; its enrollment_last_update_date reflects each record's "
        "latest update, so it cannot place PY2026 events on their OE days.",
        "Phase 5C closed the snapshot-history reconstruction as DIAGNOSTIC_ONLY; it is not reused.",
        IDENTIFIER_GAP_POLICY,
        "Widget scope filters beyond coverage year and status (for example insurance type) have no proven DB field.",
    )
    application_gaps = (
        "dbo.Enrollments_TEST is policy-member grain; it has no application or applicant identifier, "
        "application status, or application start/submit dates among the proven fields.",
        "application_type is an enrollment attribute, not an application grain.",
        "The Sisense widget formula and identifier were not captured in repository evidence.",
    )
    return [
        BridgeSpec(
            "currently_enrolled", "Currently Enrolled", "enrollment population",
            f"{CURRENTLY_ENROLLED_WIDGET_ID} Currently Enrolled People", "Currently Enrolled People",
            "person" if definition else None, ce_identifier,
            "current indicator; benefit_effective_date window in scope",
            described, person_fields, "person (enrollee_id proxy)",
            "COUNT(DISTINCT enrollee_id) with competing candidate filters CE1-CE4",
            identifier_candidate="enrollee_id", unmapped_filters=unmapped,
            gaps=(IDENTIFIER_GAP_PERSON, status_gap, COVERAGE_YEAR_GAP, PERIOD_GAP),
            phase_6b_diagnostic=True,
            notes=("Sisense COUNT counts unique values.", "No confirmation-date requirement is proven."),
        ),
        BridgeSpec(
            "new_enrolled", "New", "enrollment lifecycle",
            "6a98048ccef60720fe7f9b6e New/Active/Passive - Enrolled People", "NEW",
            "person (lifecycle category)", None, "lifecycle window (rules not captured)", (),
            ("coverage_year", "enrollee_id", "enrollment_create_date", "application_type"), None, None,
            identifier_candidate="enrollee_id", lifecycle_rules_captured=False, gaps=lifecycle_gaps,
        ),
        BridgeSpec(
            "active_enrolled", "Active", "enrollment lifecycle",
            "6a98048ccef60720fe7f9b6e New/Active/Passive - Enrolled People", "ACTIVE",
            "person (lifecycle category)", None, "lifecycle window (rules not captured)", (),
            ("coverage_year", "enrollee_id", "enrollment_create_date", "application_type"), None, None,
            identifier_candidate="enrollee_id", lifecycle_rules_captured=False, gaps=lifecycle_gaps,
        ),
        BridgeSpec(
            "passive_enrolled", "Passive", "enrollment lifecycle",
            "6a98048ccef60720fe7f9b6e New/Active/Passive - Enrolled People", "PASSIVE",
            "person (lifecycle category)", None, "lifecycle window (rules not captured)", (),
            ("coverage_year", "enrollee_id", "enrollment_create_date", "application_type"), None, None,
            identifier_candidate="enrollee_id", lifecycle_rules_captured=False, gaps=lifecycle_gaps,
        ),
        BridgeSpec(
            "cancelled_policies_daily", "Cancelled Policies", "policy activity",
            f"{ACTIVITY_WIDGET_ID} Cancelled & Terminated Policies - By Day", "Cancelled Policies",
            "policy activity by calendar day", "enrollment.id",
            "enrollment.last_update_timestamp at day grain",
            ("enrollment.enrollment_status_description = Cancelled", "Sisense COUNT = unique values"),
            ("coverage_year", "enrollment_id", "enrollment_status_description", "enrollment_last_update_date"),
            "policy", None, identifier_candidate="enrollment_id", requires_event_history=True, gaps=activity_gaps,
        ),
        BridgeSpec(
            "terminated_policies_daily", "Terminated Policies", "policy activity",
            f"{ACTIVITY_WIDGET_ID} Cancelled & Terminated Policies - By Day", "Terminated Policies",
            "policy activity by calendar day", "enrollment.id",
            "enrollment.last_update_timestamp at day grain",
            ("enrollment.enrollment_status_description starts with Term", "Sisense COUNT = unique values"),
            ("coverage_year", "enrollment_id", "enrollment_status_description", "enrollment_last_update_date"),
            "policy", None, identifier_candidate="enrollment_id", requires_event_history=True, gaps=activity_gaps,
        ),
        BridgeSpec(
            "applications_started", "Applications Started", "application",
            "6a98048ccef60720fe7f9b44 Applications Started", None, "application", None, NOT_CAPTURED, (),
            (), None, None, source_family_supported=False, gaps=application_gaps,
        ),
        BridgeSpec(
            "submitted_applications", "Submitted Applications", "application",
            "6a98048ccef60720fe7f9b66 Submitted Applications", None, "application", None, NOT_CAPTURED, (),
            (), None, None, source_family_supported=False, gaps=application_gaps,
        ),
        BridgeSpec(
            "percent_effectuated", "% Effectuated", "enrollment population ratio",
            f"{CURRENTLY_ENROLLED_WIDGET_ID} Currently Enrolled People", "% Effectuated",
            "person ratio" if effectuated else None,
            (effectuated or {}).get("identifier"),
            "same scope as Currently Enrolled People",
            described + ((f"numerator {effectuated['status_field']} in {effectuated['numerator_members']}; "
                          f"denominator in {effectuated['denominator_members']}",) if effectuated else ()),
            person_fields, "person ratio (enrollee_id proxy)", effectuated_candidate(definition),
            identifier_candidate="enrollee_id", unmapped_filters=unmapped,
            gaps=(IDENTIFIER_GAP_PERSON, status_gap, COVERAGE_YEAR_GAP, PERIOD_GAP),
            phase_6b_diagnostic=effectuated is not None,
            notes=("Captured evidence places % Effectuated in the Currently Enrolled People widget: it is an "
                   "enrollment person ratio, not an application-family metric.",),
        ),
        BridgeSpec(
            "total_applicants", "Total Applicants / Applicants On Submitted Applications", "application",
            "6a98048ccef60720fe7f9b4f Applicants On Submitted Applications", "Total Applicants",
            "applicant", None, NOT_CAPTURED, (), (), None, None, source_family_supported=False,
            gaps=application_gaps + ("Total Applicants is a validation-only catalog candidate, not production-verified.",),
        ),
    ]


CANCELLED_POPULATION_LABEL = "PY2026_DB_CANCELLED_POPULATION_DIAGNOSTIC"
TERMINATED_POPULATION_LABEL = "PY2026_DB_TERMINATED_POPULATION_DIAGNOSTIC"


def population_definitions() -> list[dict]:
    """Latest-state population diagnostics. Never the Sisense Cancelled/Terminated Policies activity metrics."""
    return [
        {"label": "Cancelled policy population", "diagnostic_label": CANCELLED_POPULATION_LABEL,
         "conditions": [condition("coverage_year", "=", [PY2026_YEAR]),
                        condition("enrollment_status_description", "=", ["Cancelled"])]},
        {"label": "Terminated policy population", "diagnostic_label": TERMINATED_POPULATION_LABEL,
         "conditions": [condition("coverage_year", "=", [PY2026_YEAR]),
                        condition("enrollment_status_description", "like", ["Term%"])]},
    ]


def cancellation_split() -> dict:
    population = [{
        "label": item["label"],
        "diagnostic_label": item["diagnostic_label"],
        "conditions": item["conditions"],
        "interpretation": "latest-state population (DB-only diagnostic)",
        "formula": "COUNT(DISTINCT enrollment_id) WHERE " + " AND ".join(
            condition_text(cond) for cond in item["conditions"]),
        "grain": "policy",
        "classification": DIAGNOSTIC,
        "caveats": [
            f"Counts policies whose latest state in the {DATASET_LABEL} is cancelled/terminated; "
            "not daily activity.",
            "No proven PY2027 Sisense counterpart; must never be labelled Cancelled/Terminated Policies.",
            IDENTIFIER_GAP_POLICY,
        ],
        "production_comparable": False,
    } for item in population_definitions()]
    activity = {
        "classification": BLOCKED_EVENT_HISTORY,
        "sisense_metric": "COUNT(DISTINCT enrollment.id) per calendar day of enrollment.last_update_timestamp",
        "required_to_reproduce": [
            "PY2026 records as they stood at each PY2026 OE day (as-of state), or an immutable status-change "
            "event log with event timestamps and policy identifier.",
            "Proof that enrollment_id equals Sisense enrollment.id.",
            "The widget's scope filters mapped to proven PY2026 fields.",
        ],
        "why_blocked": (
            "dbo.Enrollments_TEST is one latest-state table; later updates overwrite enrollment_last_update_date "
            "and status, and Phase 5C showed snapshots drift (MIXED, DIAGNOSTIC_ONLY)."
        ),
        "production_comparable": False,
    }
    return {"population_state": population, "activity": activity}


def build_report(definition: dict | None, proven_fields=PROVEN_FIELDS) -> dict:
    matrix = [matrix_row(spec, proven_fields) for spec in build_specs(definition)]
    candidates = currently_enrolled_candidates(definition)
    ready = [row["metric"] for row in matrix if row["classification"] == READY]
    diagnostics = [row["metric"] for row in matrix if row["phase_6b_diagnostic"]]
    report = {
        "report_kind": "PY2026_PY2027_METRIC_BRIDGE",
        "phase": "6A",
        "locked_sources": {
            "py2026": {
                "source": CANONICAL_SOURCE,
                "filter": f"coverage_year = {PY2026_YEAR}",
                "role": "canonical analytical query surface (Phase 5D data equivalence); system authority not claimed",
                "proven_fields": list(proven_fields),
            },
            "py2027": {"source": "current Sisense OE Executive Dashboard", "database_search": False},
            "excluded": ["834", "inbound", "RCNI", "SFTP", "additional enrollment tables", "additional snapshots"],
        },
        "verified_payload_loaded": definition is not None,
        "metric_semantic_matrix": matrix,
        "currently_enrolled_candidates": candidates,
        "cancellation_termination": cancellation_split(),
        "application_source_family": [
            {"metric": row["metric"], "classification": row["classification"],
             "assessment": row["semantic_gaps"][0] if row["semantic_gaps"] else None}
            for row in matrix if row["metric_id"] in (
                "applications_started", "submitted_applications", "total_applicants", "percent_effectuated")
        ],
        "ready_for_next_validation": {
            "ready_for_aggregate_validation": ready,
            "phase_6b_diagnostic_validation": diagnostics,
        },
        "blocked_metrics": [
            {"metric": row["metric"], "classification": row["classification"]}
            for row in matrix if row["classification"] not in (READY, DIAGNOSTIC)
        ],
        "recommended_phase_6b": recommended_phase_6b(candidates),
        "production_comparable": False,
        "notes": [
            "Phase 6A is semantic planning. No database query and no Sisense request were made.",
            "Candidate formulas are hypotheses for validation, not equivalence claims.",
            "Candidates are never ranked by numeric closeness to Sisense.",
        ],
        "command": COMMAND,
    }
    guard_no_pii(report)
    return report


def recommended_phase_6b(candidates: list[dict]) -> dict:
    return {
        "title": "PY2026 Currently Enrolled / % Effectuated diagnostic aggregate validation",
        "source": f"{CANONICAL_SOURCE} only; coverage_year = {PY2026_YEAR}; read-only; aggregates only",
        "steps": [
            f"Column-existence check on {CANONICAL_SOURCE} only for person_type_description, "
            "insurance_type_description and exchg_indiv_identifier (no other tables).",
            "Status vocabulary: distinct enrollment and enrollee status values with row and person counts.",
            "Candidate counts for " + ", ".join(item["candidate_id"] for item in candidates)
            + " and the % Effectuated candidate ratio, each reported separately without ranking.",
            "Person-identifier behavior: rows, distinct enrollee_id, distinct enrollment_id, persons per policy.",
            "Cancelled/Terminated latest-state population counts, labelled as population diagnostics.",
            "Period evidence: benefit_effective_date and enrollment_last_update_date ranges to document the "
            "lifecycle point of the canonical table.",
        ],
        "out_of_scope": ["New/Active/Passive", "daily Cancelled/Terminated activity", "application metrics",
                         "any PY2027 database query", "production metric changes"],
    }


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def _e(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (list, tuple)):
        return "<br>".join(html.escape(str(item)) for item in value) or "none"
    return html.escape(str(value))


def render_html(report: dict) -> str:
    sources = report["locked_sources"]
    head = ("<tr><th>Metric</th><th>PY2027 Sisense Grain</th><th>PY2027 Identifier</th>"
            "<th>PY2027 Date Semantics</th><th>PY2027 Filter/Status Semantics</th>"
            "<th>Available PY2026 DB Fields</th><th>Candidate PY2026 Grain</th><th>Candidate Formula</th>"
            "<th>Semantic Gaps</th><th>Classification</th></tr>")
    matrix = "".join(
        f"<tr><td>{_e(r['metric'])}</td><td>{_e(r['py2027_sisense_grain'])}</td><td>{_e(r['py2027_identifier'])}</td>"
        f"<td>{_e(r['py2027_date_semantics'])}</td><td>{_e(r['py2027_filter_status_semantics'])}</td>"
        f"<td>{_e(r['available_py2026_db_fields'])}</td><td>{_e(r['candidate_py2026_grain'])}</td>"
        f"<td>{_e(r['candidate_formula'])}</td><td>{_e(r['semantic_gaps'])}</td><td>{_e(r['classification'])}</td></tr>"
        for r in report["metric_semantic_matrix"])
    candidates = "".join(
        f"<tr><td>{_e(c['candidate_id'])}</td><td>{_e(c['description'])}</td><td>{_e(c['formula'])}</td>"
        f"<td>{_e(c['not_applicable_filters'])}</td><td>{_e(c['assumptions'])}</td></tr>"
        for c in report["currently_enrolled_candidates"]) or "<tr><td colspan='5'>Verified payload not loaded.</td></tr>"
    split = report["cancellation_termination"]
    population = "".join(f"<li><b>{_e(p['label'])}</b>: {_e(p['formula'])} ({_e(p['classification'])})</li>"
                         for p in split["population_state"])
    applications = "".join(f"<li><b>{_e(a['metric'])}</b>: {_e(a['classification'])}. {_e(a['assessment'])}</li>"
                           for a in report["application_source_family"])
    ready = report["ready_for_next_validation"]
    blocked = "".join(f"<li>{_e(b['metric'])}: {_e(b['classification'])}</li>" for b in report["blocked_metrics"])
    phase = report["recommended_phase_6b"]
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>PY2026 ↔ PY2027 metric bridge</title></head>"
        "<body><h1>PY2026 Database ↔ PY2027 Sisense Metric Bridge (Phase 6A)</h1>"
        "<p><b>production_comparable = false</b> for every metric.</p>"
        f"<h2>Locked Sources</h2><p>PY2026: {_e(sources['py2026']['source'])} ({_e(sources['py2026']['filter'])}); "
        f"{_e(sources['py2026']['role'])}.<br>PY2027: {_e(sources['py2027']['source'])}.</p>"
        f"<h2>Metric Semantic Matrix</h2><table border='1' cellpadding='4'>{head}{matrix}</table>"
        "<h2>Currently Enrolled Candidate Definitions</h2><table border='1' cellpadding='4'>"
        "<tr><th>ID</th><th>Definition</th><th>Formula</th><th>Filters without PY2026 field</th>"
        f"<th>Assumptions</th></tr>{candidates}</table>"
        "<h2>Cancellation/Termination Population vs Activity</h2>"
        f"<h3>Population / state</h3><ul>{population}</ul>"
        f"<h3>Activity</h3><p>{_e(split['activity']['classification'])}: {_e(split['activity']['why_blocked'])}</p>"
        f"<p>Required: {_e(split['activity']['required_to_reproduce'])}</p>"
        f"<h2>Application Metric Source-Family Assessment</h2><ul>{applications}</ul>"
        f"<h2>Metrics Ready for Next Validation</h2><p>Ready for aggregate validation: "
        f"{_e(ready['ready_for_aggregate_validation'])}</p><p>Phase 6B diagnostic validation: "
        f"{_e(ready['phase_6b_diagnostic_validation'])}</p>"
        f"<h2>Blocked Metrics</h2><ul>{blocked}</ul>"
        f"<h2>Recommended Phase 6B</h2><p><b>{_e(phase['title'])}</b><br>{_e(phase['source'])}</p>"
        f"<ol>{''.join(f'<li>{_e(step)}</li>' for step in phase['steps'])}</ol>"
        f"<p>Out of scope: {_e(phase['out_of_scope'])}</p></body></html>"
    )


def main() -> int:
    report = build_report(load_definition())
    json_path, html_path = write_report(report)
    for row in report["metric_semantic_matrix"]:
        print(f"{row['metric']}: {row['classification']} (production_comparable=false)")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
