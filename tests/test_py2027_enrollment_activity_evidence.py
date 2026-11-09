"""Phase 8B: natural-traffic enrollment create/confirm evidence. Collector sends no requests."""

from pathlib import Path
from types import SimpleNamespace

from comparison import py2027_enrollment_activity_evidence as ev
from comparison.py2026_py2027_metric_bridge import ACTIVITY_WIDGET_ID, CURRENTLY_ENROLLED_WIDGET_ID
from comparison.executive_py2026_py2027_report import COMPATIBLE_PY2027_ACTIVITY_METRICS
from sisense.discovery import Column, MetadataRole, WidgetStructure

PROJECT = Path(__file__).resolve().parents[1]


def _structure(widget_id, title, measures, dimensions=(), scopes=()):
    columns, roles = [], []
    for measure_title, table, column, aggs in measures:
        columns.append(Column(measure_title, "measure", None, None, table, column, None, ()))
        roles.append(MetadataRole("result", "measure", "measure", measure_title, None, None, table, column, (),
                                  aggregation_functions=aggs))
    for dim_title, table, column, level in dimensions:
        columns.append(Column(dim_title, "dimension", "datetime", level, table, column, None, ()))
        roles.append(MetadataRole("result", "dimension", "datetime", dim_title, level, None, table, column, ()))
    for table, column, kind in scopes:
        roles.append(MetadataRole("scope", "filter", None, column, None, None, table, column, (), filter_kind=kind))
    return WidgetStructure("dashboard", widget_id, title, "chart", "ds", "OE", tuple(columns),
                           len(scopes), 0, len(columns) + len(scopes), tuple(roles))


def _row(structure, measure_index=0):
    measure = structure.measures[measure_index] if structure.measures else None
    return ev.classify_measure(structure, measure)


def test_currently_enrolled_is_not_created_or_confirmed():
    structure = _structure(
        CURRENTLY_ENROLLED_WIDGET_ID, "Currently Enrolled People",
        [("Currently Enrolled People", "enrollee", "exchg_indiv_identifier", ("dupcount",))],
        [("Benefit", "enrollment", "benefit_effective_date", "days")],
        [("enrollment", "coverage_year", "member_list")],
    )
    row = _row(structure)
    assert row["classification"] == ev.WRONG_DATE
    assert row["counterpart"] is None
    assert row["classification"] != ev.EXACT


def test_cancelled_terminated_last_update_is_wrong_date_semantics():
    structure = _structure(
        ACTIVITY_WIDGET_ID, "Cancelled & Terminated Policies - By Day",
        [("Cancelled Policies", "enrollment", "id", ("count",))],
        [("Day", "enrollment", "last_update_timestamp", "days")],
    )
    row = _row(structure)
    assert row["classification"] == ev.WRONG_DATE
    assert row["counterpart"] is None


def test_new_active_passive_is_lifecycle_not_created():
    structure = _structure(
        ev.NAP_WIDGET_ID, "New Created Enrollments",
        [("NEW", "enrollee", "exchg_indiv_identifier", ("count",))],
    )
    row = _row(structure)
    assert row["classification"] == ev.LIFECYCLE
    assert row["counterpart"] is None
    assert row["classification"] != ev.EXACT


def test_applications_are_not_enrollment_person_activity_from_title():
    started = _structure(ev.STARTED_WIDGET_ID, "Applications Started",
                         [("Applications Started", "application", "id", ("count",))])
    submitted = _structure(ev.SUBMITTED_WIDGET_ID, "Submitted Applications",
                           [("Submitted Applications", "application", "id", ("count",))])
    assert _row(started)["classification"] == ev.WRONG_GRAIN
    assert _row(submitted)["classification"] == ev.WRONG_GRAIN
    assert _row(started)["counterpart"] is None


def test_applicant_grain_is_rejected():
    structure = _structure(ev.APPLICANTS_WIDGET_ID, "Applicants On Submitted Applications",
                           [("Total Applicants", "applicant", "id", ("dupcount",))])
    row = _row(structure)
    assert row["grain"] == ev.APPLICANT
    assert row["classification"] == ev.WRONG_GRAIN
    assert row["counterpart"] is None


def test_policy_grain_does_not_auto_match_person_grain():
    structure = _structure(
        "widget-policy-create", "Enrollment Created Policies",
        [("Policies", "enrollment", "id", ("count",))],
        [("Created", "enrollment", "enrollment_create_date", "months")],
        [("enrollment", "coverage_year", "member_list")],
    )
    row = _row(structure)
    assert row["grain"] == ev.POLICY
    assert row["classification"] == ev.PARTIAL
    assert row["classification"] != ev.EXACT
    assert row["counterpart"] == ev.CREATED


def test_exact_candidate_requires_all_semantic_components():
    structure = _structure(
        "widget-person-create", "Enrollment Created People",
        [("People", "enrollee", "exchg_indiv_identifier", ("dupcount",))],
        [("Created", "enrollment", "enrollment_create_date", "months")],
        [("enrollment", "coverage_year", "member_list")],
    )
    row = _row(structure)
    assert row["classification"] == ev.EXACT
    assert row["counterpart"] == ev.CREATED
    assert row["grain"] == ev.PERSON
    assert row["coverage_year_context"] is True
    assert ev.build_report([row])["final_decision"] == ev.CREATED_PROVEN
    assert ev.build_report([row])["summary"]["created_counterpart"] == "PROVEN"


def test_missing_date_dimension_is_partial():
    structure = _structure(
        "widget-person-nodate", "People",
        [("People", "enrollee", "exchg_indiv_identifier", ("dupcount",))],
        (),
        [("enrollment", "coverage_year", "member_list")],
    )
    row = _row(structure)
    assert row["classification"] == ev.PARTIAL
    assert row["counterpart"] is None
    assert "date_dimension" in row["reason"]


def test_missing_counted_field_is_partial():
    structure = _structure(
        "widget-create-nocolumn", "Created",
        [("Created", None, None, ("count",))],
        [("Created", "enrollment", "enrollment_create_date", "months")],
        [("enrollment", "coverage_year", "member_list")],
    )
    row = _row(structure)
    assert row["counted_field"] is None
    assert row["classification"] == ev.PARTIAL
    assert row["counterpart"] == ev.CREATED


def test_no_candidate_is_not_found():
    report = ev.build_report([])
    assert report["final_decision"] == ev.NOT_FOUND
    assert report["summary"] == {"created_counterpart": "NOT_FOUND", "confirmed_counterpart": "NOT_FOUND"}
    empty = SimpleNamespace(entries=lambda: [], target_requests_seen=0)
    assert ev.report_from_observer(empty)["final_decision"] == ev.NOT_FOUND


def test_collector_sends_no_requests():
    text = (PROJECT / "comparison" / "py2027_enrollment_activity_evidence.py").read_text(encoding="utf-8")
    for marker in (".request.post", "fetch(", "urllib", "httpx", "playwright", "retrieve_live", "run_pipeline"):
        assert marker not in text
    assert "Nothing here sends" in text or "No request sent" in text


def test_raw_bodies_members_and_auth_are_never_written(tmp_path):
    row = _row(_structure(
        CURRENTLY_ENROLLED_WIDGET_ID, "Currently Enrolled People",
        [("Currently Enrolled People", "enrollee", "exchg_indiv_identifier", ("dupcount",))],
        [("Benefit", "enrollment", "benefit_effective_date", "days")],
    ))
    json_path, html_path = ev.write_report(ev.build_report([row]), tmp_path)
    text = json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")
    lowered = text.lower()
    for token in ("post_data", "request_body", '"members"', "cookie", "authorization", "bearer",
                  '"enrollee_id"', '"enrollment_id"'):
        assert token not in lowered
    assert row["filters"][0]["filter_kind"] != "raw" if row["filters"] else True


def test_combined_chart_wiring_not_changed():
    assert COMPATIBLE_PY2027_ACTIVITY_METRICS == frozenset()
    report = ev.build_report([])
    assert report["combined_chart_wiring"] == "NOT_CHANGED"
    assert report["production_comparable"] is False


def test_write_from_observer_uses_in_memory_structures(tmp_path):
    structure = _structure(
        ACTIVITY_WIDGET_ID, "Cancelled & Terminated Policies - By Day",
        [("Cancelled Policies", "enrollment", "id", ("count",))],
        [("Day", "enrollment", "last_update_timestamp", "days")],
    )
    observer = SimpleNamespace(
        entries=lambda: [SimpleNamespace(structure=structure, definitions={"fp": SimpleNamespace(structure=structure)})],
        target_requests_seen=3,
    )
    path = ev.write_enrollment_activity_evidence(observer, tmp_path)
    assert path is not None and path.name == ev.JSON_NAME
    payload = path.read_text(encoding="utf-8")
    assert ev.WRONG_DATE in payload and ev.NOT_FOUND in payload
