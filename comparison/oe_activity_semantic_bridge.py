"""Phase 6C: PY2026 <-> PY2027 OE activity semantic bridge (evidence analysis only).

No database query, no Sisense request. Consumes evidence already in the
repository: the verified Currently Enrolled payload, the metric catalog, the
Phase 6A bridge specs, and the Phase 6B OE activity cumulatives (the optional
Phase 6B report when present, otherwise the values locked from the USER run).

A cross-year bridge needs captured evidence of BOTH a compatible event meaning
and a compatible grain. Title or date-name similarity is never evidence.
Nothing is promoted: production_comparable = false everywhere.

    python -m comparison.oe_activity_semantic_bridge
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.py2026_py2027_metric_bridge import (
    ACTIVITY_WIDGET_ID,
    CURRENTLY_ENROLLED_WIDGET_ID,
    DATASET_LABEL,
    build_specs,
    load_definition,
    scope_filters,
)
from metrics.catalog import METRICS, PROMOTION_CANDIDATES

JSON_NAME = "oe_activity_semantic_bridge.json"
HTML_NAME = "oe_activity_semantic_bridge.html"
PHASE_6B_REPORT = OUTPUT_DIR / "py2026_population_diagnostics.json"
COMMAND = r".\.venv\Scripts\python.exe -m comparison.oe_activity_semantic_bridge"

PERSON, POLICY, APPLICATION, APPLICANT, UNKNOWN = "PERSON", "POLICY", "APPLICATION", "APPLICANT", "UNKNOWN"
GRAINS = (PERSON, POLICY, APPLICATION, APPLICANT, UNKNOWN)
ENROLLMENT_GRAINS = (PERSON, POLICY)

APPLICATION_CREATED = "application created"
APPLICATION_SUBMITTED = "application submitted"
ENROLLMENT_CREATED = "enrollment created"
ENROLLMENT_CONFIRMED = "enrollment confirmed"
STATUS_LAST_UPDATED = "enrollment status last updated"
CURRENT_POPULATION = "current population"
LIFECYCLE_CLASSIFICATION = "lifecycle classification"
UNKNOWN_EVENT = "unknown"

PY2026_CREATE = "enrollment_create_date"
PY2026_CONFIRM = "enrollment_confirmation_date"
NONE = "NONE"
PY2026_FIELD_EVENTS = {PY2026_CREATE: ENROLLMENT_CREATED, PY2026_CONFIRM: ENROLLMENT_CONFIRMED}
PY2026_FIELD_GRAINS = (PERSON, POLICY)

STRONG = "STRONG_SEMANTIC_CANDIDATE"
DIAGNOSTIC = "DIAGNOSTIC_SEMANTIC_CANDIDATE"
GRAIN_MISMATCH = "GRAIN_MISMATCH"
DATE_MISMATCH = "DATE_SEMANTICS_MISMATCH"
CURRENT_STATE = "CURRENT_STATE_NOT_ACTIVITY"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
ALIGNMENT_STATUSES = (STRONG, DIAGNOSTIC, GRAIN_MISMATCH, DATE_MISMATCH, CURRENT_STATE, INSUFFICIENT)
BRIDGE_STATUSES = (STRONG, DIAGNOSTIC)

CAPTURED = "CAPTURED_REPOSITORY_EVIDENCE"
USER_CONFIRMED = "USER_CONFIRMED_SEMANTICS"
TITLE_ONLY = "TITLE_AND_CATALOG_FAMILY_ONLY"
NOT_CAPTURED = "NOT_CAPTURED"
USER_REPORTED_UNVERIFIED = "USER_REPORTED_NOT_IN_REPOSITORY_EVIDENCE"

BULK_LOAD_FLAG = "POTENTIAL_RENEWAL_MIGRATION_OR_INITIAL_LOAD_EFFECT"
OE_ANCHOR_UNPROVEN = "OE_START_ANCHOR_UNPROVEN"
OBSERVED_NOT_OFFICIAL = "OBSERVED_DASHBOARD_WINDOW_NOT_OFFICIAL_ANCHOR"
NEW_UNRESOLVED = "NEW_SEMANTICS_UNRESOLVED"
NO_BRIDGE = "NO_DEFENSIBLE_ACTIVITY_BRIDGE"

CUTOFFS = ("2025-11-08", "2025-11-15", "2025-11-30", "2025-12-15", "2025-12-31")
LOCKED_PHASE_6B_PEOPLE = {
    PY2026_CREATE: (1037238, 1050525, 1075266, 1153513, 1172362),
    PY2026_CONFIRM: (1014037, 1018942, 1042372, 1080781, 1128635),
}
ACTIVITY_OBSERVED_DAYS = ("2026-11-01", "2026-11-08")


@dataclass
class MetricEvidence:
    metric_id: str
    metric: str
    widget: str
    measure: str | None
    grain: str
    grain_evidence: str
    identifier: str | None
    date_field: str | None
    date_level: str | None
    date_role: str | None
    event_meaning: str
    event_meaning_evidence: str
    cumulative_semantics: str
    notes: list[str] = field(default_factory=list)
    user_reported: list[str] = field(default_factory=list)


def _benefit(definition: dict | None) -> dict | None:
    if definition is None:
        return None
    return next((row for row in scope_filters(definition) if row["column"] == "benefit_effective_date"), None)


def _widget(metric_id: str) -> tuple[str, str | None]:
    metric = METRICS.get(metric_id) or PROMOTION_CANDIDATES.get(metric_id)
    source = getattr(metric, "passive_source", None)
    if source is None:
        return f"{CURRENTLY_ENROLLED_WIDGET_ID} Currently Enrolled People", None
    return f"{source.widget_id} {source.widget_title}", source.measure_title


def captured_metrics(definition: dict | None) -> list[MetricEvidence]:
    """PY2027 candidate metrics, restricted to semantics already captured in the repository."""
    specs = {spec.metric_id: spec for spec in build_specs(definition)}
    ce_identifier = specs["currently_enrolled"].sisense_identifier
    benefit = _benefit(definition)
    benefit_field = benefit["field"] if benefit else None
    activity = specs["cancelled_policies_daily"]
    started_widget, _ = _widget("applications_started")
    submitted_widget, _ = _widget("submitted_applications")
    applicants_widget, applicants_measure = _widget("total_applicants")
    lifecycle_widget, _ = _widget("new_enrolled")
    application_note = "Formula, identifier and date dimension were never captured; only widget id and title."
    lifecycle = [
        MetricEvidence(
            metric_id, name, lifecycle_widget, measure, UNKNOWN,
            TITLE_ONLY + " (title says 'Enrolled People'; counted field never captured)", None, None, None, None,
            LIFECYCLE_CLASSIFICATION, TITLE_ONLY, "UNKNOWN: lifecycle category of a population, not an event count.",
            notes=["Widget case logic (date boundaries, prior-coverage rule, identifier) was never captured."],
            user_reported=([
                "USER recalls that the New measure references an application creation timestamp beginning in "
                "November 2026. Not present in repository evidence."] if metric_id == "new_enrolled" else []),
        ) for metric_id, name, measure in (("new_enrolled", "New", "NEW"), ("active_enrolled", "Active", "ACTIVE"),
                                           ("passive_enrolled", "Passive", "PASSIVE"))
    ]
    return [
        MetricEvidence(
            "applications_started", "Applications Started", started_widget, None, APPLICATION, TITLE_ONLY,
            None, None, None, None, APPLICATION_CREATED, TITLE_ONLY,
            "UNKNOWN: date dimension not captured.", notes=[application_note]),
        MetricEvidence(
            "submitted_applications", "Submitted Applications", submitted_widget, None, APPLICATION, TITLE_ONLY,
            None, None, None, None, APPLICATION_SUBMITTED, TITLE_ONLY,
            "UNKNOWN: date dimension not captured.", notes=[application_note]),
        *lifecycle,
        MetricEvidence(
            "currently_enrolled", "Currently Enrolled", _widget("currently_enrolled")[0], "Currently Enrolled People",
            PERSON if ce_identifier else UNKNOWN, CAPTURED if ce_identifier else NOT_CAPTURED, ce_identifier,
            benefit_field, "days" if benefit else None, "scope filter (benefit window), not an event date",
            CURRENT_POPULATION, CAPTURED,
            "NO: point-in-time population; accumulating it through a cutoff has no meaning.",
            notes=["Verified payload contains no creation, confirmation or submission date."]),
        MetricEvidence(
            "percent_effectuated", "% Effectuated", _widget("currently_enrolled")[0], "% Effectuated",
            PERSON if ce_identifier else UNKNOWN, CAPTURED if ce_identifier else NOT_CAPTURED, ce_identifier,
            benefit_field, "days" if benefit else None, "scope filter (benefit window), not an event date",
            CURRENT_POPULATION, CAPTURED, "NO: ratio of current populations."),
        *[MetricEvidence(
            spec.metric_id, spec.metric, spec.widget, spec.measure, POLICY, USER_CONFIRMED, spec.sisense_identifier,
            "enrollment.last_update_timestamp", "days", "event axis (calendar day of current last update)",
            STATUS_LAST_UPDATED, USER_CONFIRMED,
            "LIMITED: daily counts by current last-update day; a record re-updated later moves days, so a "
            "cumulative is not a stable entry cohort.")
          for spec in (activity, specs["terminated_policies_daily"])],
        MetricEvidence(
            "total_applicants", "Total Applicants", applicants_widget, applicants_measure, APPLICANT, TITLE_ONLY,
            None, None, None, None, UNKNOWN_EVENT, NOT_CAPTURED, "UNKNOWN: date dimension not captured.",
            notes=[application_note, "Validation-only catalog candidate."]),
    ]


def align(metric: MetricEvidence, py2026_field: str, identifier_equivalence_proven: bool = False) -> dict:
    """Alignment of one PY2027 metric with one PY2026 activity date. Precedence is fixed."""
    target_event = PY2026_FIELD_EVENTS[py2026_field]
    event_proven = metric.event_meaning_evidence in (CAPTURED, USER_CONFIRMED)
    if metric.event_meaning == CURRENT_POPULATION:
        status, reason = CURRENT_STATE, "Current-state population metric; PY2026 field is an activity date."
    elif metric.grain in (APPLICATION, APPLICANT):
        status, reason = GRAIN_MISMATCH, (f"{metric.grain} grain vs PY2026 enrollment (person/policy) grain; "
                                          "enrollment_id and enrollee_id are not application identifiers.")
    elif metric.grain not in ENROLLMENT_GRAINS:
        status, reason = INSUFFICIENT, "Grain not established by captured evidence."
    elif metric.event_meaning in (LIFECYCLE_CLASSIFICATION, UNKNOWN_EVENT) or not event_proven:
        status, reason = INSUFFICIENT, "Event meaning not captured; date or title names are not evidence."
    elif metric.date_field is None:
        status, reason = INSUFFICIENT, "No captured date dimension."
    elif metric.event_meaning != target_event:
        status, reason = DATE_MISMATCH, f"PY2027 event '{metric.event_meaning}' vs PY2026 '{target_event}'."
    elif metric.identifier is None:
        status, reason = INSUFFICIENT, "Identifier not captured."
    elif identifier_equivalence_proven:
        status, reason = STRONG, "Event meaning, grain and identifier equivalence proven."
    else:
        status, reason = DIAGNOSTIC, "Event meaning and grain compatible; identifier equivalence unproven."
    return {
        "metric_id": metric.metric_id,
        "metric": metric.metric,
        "py2026_field": py2026_field,
        "py2026_event": target_event,
        "py2027_event": metric.event_meaning,
        "py2027_grain": metric.grain,
        "alignment_status": status,
        "reason": reason,
        "production_comparable": False,
    }


def assess_metric(metric: MetricEvidence, identifier_equivalence_proven: bool = False) -> dict:
    results = [align(metric, name, identifier_equivalence_proven) for name in PY2026_FIELD_EVENTS]
    bridges = [item for item in results if item["alignment_status"] in BRIDGE_STATUSES]
    best = min(bridges, key=lambda item: BRIDGE_STATUSES.index(item["alignment_status"])) if bridges else None
    if best:
        status = best["alignment_status"]
    else:
        statuses = {item["alignment_status"] for item in results}
        status = next(s for s in (CURRENT_STATE, GRAIN_MISMATCH, DATE_MISMATCH, INSUFFICIENT) if s in statuses)
    return {
        **asdict(metric),
        "py2026_candidate": best["py2026_field"] if best else NONE,
        "alignment_status": status,
        "per_field": results,
        "production_comparable": False,
    }


def new_metric_assessment(metrics: list[dict]) -> dict:
    new = next(item for item in metrics if item["metric_id"] == "new_enrolled")
    return {
        "status": NEW_UNRESOLVED,
        "alignment_status": new["alignment_status"],
        "captured": {"widget": new["widget"], "measure": new["measure"], "grain": new["grain"],
                     "grain_evidence": new["grain_evidence"], "identifier": new["identifier"],
                     "date_field": new["date_field"]},
        "user_reported": new["user_reported"],
        "user_reported_status": USER_REPORTED_UNVERIFIED,
        "interpretations": [
            {"interpretation": "person population classified using an application creation date",
             "evidence": "Consistent with the widget title 'Enrolled People' and the USER recollection; the "
                         "measure formula, identifier and date dimension were never captured.",
             "verdict": "PLAUSIBLE_UNPROVEN",
             "if_confirmed": f"Still not aligned: application-created is not enrollment-created "
                             f"({DATE_MISMATCH}), and a classified current population is not an activity "
                             f"cohort ({CURRENT_STATE})."},
            {"interpretation": "count of new enrollment activity",
             "evidence": "No captured evidence of an enrollment creation or confirmation event axis.",
             "verdict": "NOT_SUPPORTED_BY_CAPTURED_EVIDENCE"},
            {"interpretation": "something else (prior-coverage lifecycle rule)",
             "evidence": "Phase 6A: New/Active/Passive depend on uncaptured case logic and prior-year coverage.",
             "verdict": "PLAUSIBLE_UNPROVEN"},
        ],
        "py2026_formula": "Not implemented in Phase 6C.",
        "required_evidence": [
            "Exact NEW measure definition from the dashboard's own 9b6e request: counted field, context "
            "(case/filter) logic, date dimension and its window, status context.",
            "Whether NEW counts people currently enrolled (population) or events in a window (activity).",
            "Prior-coverage rule, if any, and whether it needs PY2026 data outside dbo.Enrollments_TEST.",
        ],
        "production_comparable": False,
    }


def load_phase_6b(path: Path = PHASE_6B_REPORT) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def py2026_activity_evidence(report: dict | None) -> dict:
    fields = ((report or {}).get("oe_aligned_temporal_diagnostics") or {}).get("fields") or {}
    out = {}
    for name, locked in LOCKED_PHASE_6B_PEOPLE.items():
        captured = (fields.get(name) or {}).get("cumulative")
        captured_people = None if not captured else tuple(item.get("distinct_people") for item in captured)
        rows = []
        for index, cutoff in enumerate(CUTOFFS):
            previous = locked[index - 1] if index else None
            rows.append({"through": cutoff, "distinct_people": locked[index],
                         "increment_people": None if previous is None else locked[index] - previous})
        monthly = (fields.get(name) or {}).get("monthly")
        out[name] = {
            "event_meaning": PY2026_FIELD_EVENTS[name],
            "grain_available": list(PY2026_FIELD_GRAINS),
            "cumulative_people": rows,
            "source": "USER-run Phase 6B (locked values)",
            "phase_6b_report_match": None if captured_people is None else captured_people == locked,
            "october_2025": next((item for item in monthly or [] if item.get("month") == "2025-10"), None),
            "interpretation": "Activity/cohort accumulation by date; NOT a historical Currently Enrolled population.",
        }
    return {"source": f"dbo.Enrollments_TEST ({DATASET_LABEL}), coverage_year = 2026", "fields": out,
            "historical_state_reconstructable": False}


def october_bulk_risk(activity: dict) -> dict:
    per_field = {}
    for name, item in activity["fields"].items():
        first, last = item["cumulative_people"][0]["distinct_people"], item["cumulative_people"][-1]["distinct_people"]
        per_field[name] = {
            "people_through_first_cutoff": first,
            "people_through_last_cutoff": last,
            "share_present_by_first_cutoff": round(first / last, 4) if last else None,
            "october_2025": item["october_2025"],
        }
    return {
        "flag": BULK_LOAD_FLAG,
        "explanation_proven": False,
        "evidence": per_field,
        "statement": "Most PY2026 people already have a create/confirmation date on or before 2025-11-08, "
                     "consistent with a renewal, migration or initial-load effect. Existing evidence does not "
                     "prove the cause. The October/pre-OE bulk must never be trended as ordinary new enrollment.",
        "required_handling": "Report pre-window bulk separately from in-window increments in any future trend.",
    }


def oe_anchor_status(definition: dict | None) -> dict:
    benefit = _benefit(definition)
    return {
        "official_oe_anchor": {"status": OE_ANCHOR_UNPROVEN, "PY2026": None, "PY2027": None,
                               "evidence": "No proven official OE start date in the repository."},
        "observed_dashboard_activity_window": {
            "status": OBSERVED_NOT_OFFICIAL,
            "widget": f"{ACTIVITY_WIDGET_ID} Cancelled & Terminated Policies - By Day",
            "date_field": "enrollment.last_update_timestamp",
            "date_level": "days",
            "observed_from": ACTIVITY_OBSERVED_DAYS[0],
            "observed_to": ACTIVITY_OBSERVED_DAYS[1],
            "evidence": "USER-confirmed daily values for these days. Days with data in the captured view; "
                        "the first observed day is not an OE start.",
            "defensible_for": "Operational calendar window for that widget's own last-update activity only.",
            "not_defensible_for": "Create/confirmation-date bridges (different event axis).",
        },
        "dashboard_scope_date_filters": [{
            "widget": f"{CURRENTLY_ENROLLED_WIDGET_ID} Currently Enrolled People",
            "date_field": benefit["field"],
            "from": benefit["from"], "to": benefit["to"],
            "role": "Population scope (benefit window); not an activity window and not an OE anchor.",
        }] if benefit else [],
        "operational_comparison_window": {
            "status": "CALENDAR_SHIFT_ONLY",
            "py2026_equivalent": [f"2025{ACTIVITY_OBSERVED_DAYS[0][4:]}", f"2025{ACTIVITY_OBSERVED_DAYS[1][4:]}"],
            "note": "One-year calendar shift of the observed PY2027 window. Usable for diagnostics only; OE Day N "
                    "alignment still needs proven anchors.",
        },
        "production_comparable": False,
    }


def decide(assessments: list[dict]) -> dict:
    bridges = [item for item in assessments if item["alignment_status"] in BRIDGE_STATUSES]
    strongest = min(bridges, key=lambda item: BRIDGE_STATUSES.index(item["alignment_status"])) if bridges else None
    new_unresolved = any(item["metric_id"] == "new_enrolled" and item["alignment_status"] == INSUFFICIENT
                         for item in assessments)
    return {
        "strongest_bridge": None if strongest is None else {
            "metric_id": strongest["metric_id"], "py2026_field": strongest["py2026_candidate"],
            "alignment_status": strongest["alignment_status"]},
        "status": NO_BRIDGE if strongest is None else strongest["alignment_status"],
        "statement": ("No captured PY2027 metric has evidence of an enrollment-created or enrollment-confirmed "
                      "event at person or policy grain. No defensible activity bridge exists yet.")
        if strongest is None else "A candidate exists but remains diagnostic.",
        "evidence_still_required": [
            "A PY2027 metric whose captured definition counts enrollment.id or a person identifier by an "
            "enrollment creation or confirmation date (none captured).",
            "Exact NEW measure definition (widget 6a98048ccef60720fe7f9b6e) from the dashboard's own request.",
            "Proven official PY2026 and PY2027 OE start anchors.",
            "Person identifier equivalence (enrollee_id vs enrollee.exchg_indiv_identifier).",
            "Explanation of the pre-November PY2026 bulk (renewal, migration or initial load).",
        ],
        "targeted_sisense_validation_justified": new_unresolved,
        "targeted_sisense_validation": (
            "USER-run passive capture of the New/Active/Passive widget's own request definition (no arbitrary "
            "JAQL), to read the NEW measure's identifier, date dimension and context logic. It is the only "
            "enrollment-family, person-grain candidate whose event semantics are uncaptured. Application "
            "widgets are not worth validating for this bridge: their grain already mismatches."),
        "promotion": "None. No metric is promoted in Phase 6C.",
        "production_comparable": False,
    }


def build_report(definition: dict | None, phase_6b: dict | None = None,
                 metrics: list[MetricEvidence] | None = None) -> dict:
    assessed = [assess_metric(item) for item in (metrics if metrics is not None else captured_metrics(definition))]
    activity = py2026_activity_evidence(phase_6b)
    rows = [cell for item in assessed for cell in item["per_field"]]
    report = {
        "report_kind": "OE_ACTIVITY_SEMANTIC_BRIDGE",
        "phase": "6C",
        "py2026_activity_evidence": activity,
        "py2027_candidate_metrics": assessed,
        "grain_comparison": [{"metric_id": item["metric_id"], "py2027_grain": item["grain"],
                              "grain_evidence": item["grain_evidence"], "identifier": item["identifier"],
                              "py2026_grains": list(PY2026_FIELD_GRAINS),
                              "compatible": item["grain"] in ENROLLMENT_GRAINS} for item in assessed],
        "date_semantic_comparison": [{"metric_id": item["metric_id"], "py2027_date_field": item["date_field"],
                                      "py2027_date_role": item["date_role"], "py2027_event": item["event_meaning"],
                                      "event_evidence": item["event_meaning_evidence"],
                                      "py2026_events": dict(PY2026_FIELD_EVENTS)} for item in assessed],
        "new_metric_assessment": new_metric_assessment(assessed) if any(
            item["metric_id"] == "new_enrolled" for item in assessed) else None,
        "october_bulk_load_risk": october_bulk_risk(activity),
        "oe_anchor_status": oe_anchor_status(definition),
        "candidate_cross_year_bridges": [cell for cell in rows if cell["alignment_status"] in BRIDGE_STATUSES],
        "rejected_bridges": [cell for cell in rows if cell["alignment_status"] not in BRIDGE_STATUSES],
        "phase_6c_decision": decide(assessed),
        "production_comparable": False,
        "command": COMMAND,
    }
    guard_no_pii(report)
    return report


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def _e(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (list, tuple)):
        return "<br>".join(_e(item) for item in value) or "none"
    if isinstance(value, dict):
        return "<br>".join(f"{html.escape(str(k))}: {_e(v)}" for k, v in value.items()) or "none"
    return html.escape(str(value))


def _table(rows: list[dict], columns: tuple[str, ...]) -> str:
    head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
    body = "".join("<tr>" + "".join(f"<td>{_e(row.get(name))}</td>" for name in columns) + "</tr>" for row in rows)
    return f"<table border='1' cellpadding='4'><tr>{head}</tr>{body}</table>"


def render_html(report: dict) -> str:
    activity = report["py2026_activity_evidence"]
    bridge_cols = ("metric", "py2026_field", "py2027_event", "py2027_grain", "alignment_status", "reason")
    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>OE activity semantic bridge</title></head>"
             "<body><h1>PY2026 &harr; PY2027 OE Activity Semantic Bridge (Phase 6C)</h1>"
             "<p><b>production_comparable = false</b> for every bridge.</p>",
             f"<h2>PY2026 Activity Evidence</h2><p>{_e(activity['source'])}</p>"]
    for name, item in activity["fields"].items():
        parts.append(f"<h3>{_e(name)}</h3><p>{_e(item['interpretation'])}</p>"
                     + _table(item["cumulative_people"], ("through", "distinct_people", "increment_people")))
    parts += [
        "<h2>PY2027 Candidate Metrics</h2>" + _table(report["py2027_candidate_metrics"], (
            "metric", "widget", "measure", "grain", "identifier", "date_field", "event_meaning",
            "cumulative_semantics", "py2026_candidate", "alignment_status", "user_reported")),
        "<h2>Grain Comparison</h2>" + _table(report["grain_comparison"], (
            "metric_id", "py2027_grain", "grain_evidence", "identifier", "py2026_grains", "compatible")),
        "<h2>Date Semantic Comparison</h2>" + _table(report["date_semantic_comparison"], (
            "metric_id", "py2027_date_field", "py2027_date_role", "py2027_event", "event_evidence", "py2026_events")),
        f"<h2>New Metric Assessment</h2><p>{_e(report['new_metric_assessment'])}</p>",
        f"<h2>October Bulk-Load Risk</h2><p>{_e(report['october_bulk_load_risk'])}</p>",
        f"<h2>OE Anchor Status</h2><p>{_e(report['oe_anchor_status'])}</p>",
        "<h2>Candidate Cross-Year Bridges</h2>" + (
            _table(report["candidate_cross_year_bridges"], bridge_cols) if report["candidate_cross_year_bridges"]
            else "<p>None.</p>"),
        "<h2>Rejected Bridges</h2>" + _table(report["rejected_bridges"], bridge_cols),
        f"<h2>Phase 6C Decision</h2><p>{_e(report['phase_6c_decision'])}</p></body></html>",
    ]
    return "".join(parts)


def main() -> int:
    report = build_report(load_definition(), load_phase_6b())
    json_path, html_path = write_report(report)
    decision = report["phase_6c_decision"]
    print(f"Phase 6C decision: {decision['status']}")
    for item in report["py2027_candidate_metrics"]:
        print(f"  {item['metric']}: {item['alignment_status']} (PY2026 candidate: {item['py2026_candidate']})")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
