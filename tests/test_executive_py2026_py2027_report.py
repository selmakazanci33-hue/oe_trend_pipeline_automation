import json
import sqlite3

import pytest

from comparison import executive_py2026_py2027_report as ex
from comparison.currently_enrolled import ComparisonOutputError


def make_history(path, with_series=True):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metric_snapshots (run_id TEXT, metric_id TEXT, metric_value REAL, plan_year INT, "
                       "retrieved_at_utc TEXT, retrieval_status TEXT)")
    rows = [
        ("r1", "currently_enrolled_people", 900000, "2026-11-01T12:00:00+00:00"),
        ("r2", "currently_enrolled_people", 950000, "2026-11-08T12:00:00+00:00"),
        ("r2", "percent_effectuated", 0.62, "2026-11-08T12:00:00+00:00"),
        ("r2", "applications_started", 400000, "2026-11-08T12:00:00+00:00"),
        ("r2", "submitted_applications", 300000, "2026-11-08T12:00:00+00:00"),
        ("r2", "new_enrolled", 4400, "2026-11-08T12:00:00+00:00"),
        ("r2", "active_enrolled", 58800, "2026-11-08T12:00:00+00:00"),
        ("r2", "passive_enrolled", 734500, "2026-11-08T12:00:00+00:00"),
    ]
    connection.executemany("INSERT INTO metric_snapshots VALUES (?, ?, ?, 2027, ?, 'VALIDATED')", rows)
    if with_series:
        connection.execute("CREATE TABLE source_series_points (id INTEGER PRIMARY KEY, metric_id TEXT, source_date TEXT, "
                           "time_grain TEXT, date_semantics TEXT, value INT, definition_fingerprint TEXT, "
                           "observed_at_utc TEXT)")
        connection.executemany(
            "INSERT INTO source_series_points (metric_id, source_date, time_grain, date_semantics, value, "
            "definition_fingerprint, observed_at_utc) VALUES ('cancelled_policies_daily', ?, 'days', 'last_update', ?, "
            "'fp', '2026-11-08T12:00:00+00:00')", [("2026-11-01", 2534), ("2026-11-02", 5724)])
    connection.commit()
    connection.close()


PHASE_6B = {
    "currently_enrolled_candidates": [
        {"candidate_id": "CE1", "description": "status filter", "distinct_people": 1000000},
        {"candidate_id": "CE2", "description": "benefit window", "distinct_people": 980000},
    ],
    "percent_effectuated": {"per_candidate": [{"candidate_id": "CE1", "ratio": 0.9}]},
    "identifier_behavior": {"distinct_people": 1320502, "distinct_policies": 1688696},
    "population_diagnostics": [{"label": "PY2026_LATEST_CANCELLED_POPULATION", "distinct_policies": 200000,
                                "distinct_people": 150000}],
    "oe_aligned_temporal_diagnostics": {"fields": {"enrollment_create_date": {
        "monthly": [{"month": "2025-10", "row_count": 10, "distinct_people": 900000, "distinct_policies": 9},
                    {"month": "2025-11", "row_count": 10, "distinct_people": 100000, "distinct_policies": 9}],
        "cumulative": [{"through": "2025-11-08", "row_count": 1, "distinct_people": 1037238, "distinct_policies": 1},
                       {"through": "2025-12-31", "row_count": 1, "distinct_people": 1172362, "distinct_policies": 1}]}}},
}


def evidence(tmp_path, six_b=PHASE_6B, history=True, series=True, validation=True):
    path = tmp_path / "h.sqlite3"
    if history:
        make_history(path, series)
    return {
        "phase_6b": six_b,
        "phase_6d": None,
        "validation": {"generated_at_utc": "2026-11-08T12:00:00+00:00",
                       "metrics": [{"metric_id": "total_applicants", "runtime_value": 500000}]} if validation else None,
        "history": ex.read_history(path if history else None),
    }


def rows_by_metric(report):
    return {row["metric"]: row for row in report["population_comparison"]["rows"]}


def test_invalid_comparisons_never_get_differences(tmp_path):
    report = ex.build_report(evidence(tmp_path))
    for row in report["population_comparison"]["rows"]:
        if row["comparison_status"] not in ex.DIFF_STATUSES:
            assert row["absolute_difference"] is None and row["percent_difference"] is None
    for status in (ex.PY2027_ONLY, ex.PY2026_ONLY, ex.NOT_COMPARABLE):
        assert ex.difference(10, 20, status) == {"absolute_difference": None, "percent_difference": None,
                                                 "difference_unit": None}


def test_diagnostic_rows_are_labelled_and_get_differences(tmp_path):
    rows = rows_by_metric(ex.build_report(evidence(tmp_path)))
    ce1 = rows["Currently Enrolled vs PY2026 CE1"]
    assert ce1["comparison_status"] == ex.DIAGNOSTIC
    assert ce1["absolute_difference"] == -50000 and ce1["percent_difference"] == -5.0
    pe = rows["% Effectuated vs PY2026 CE1"]
    assert pe["comparison_status"] == ex.DIAGNOSTIC and pe["percent_difference"] is None
    assert pe["absolute_difference"] == -28.0 and pe["difference_unit"] == "percentage points"


def test_na_stays_na_never_zero(tmp_path):
    report = ex.build_report(evidence(tmp_path, validation=False))
    snapshot = {row["metric_id"]: row for row in report["py2027_current_snapshot"]}
    assert snapshot["total_applicants"]["value"] is None
    assert snapshot["total_applicants"]["verification"] == ex.UNAVAILABLE
    view = {row["metric"]: row for row in report["management_view"]}
    assert view["Total Applicants"]["py2027"] == "N/A"
    for row in report["management_view"]:
        if row["status"].startswith(ex.PY2027_ONLY):
            assert row["py2026"] == ex.DASH and row["change"] == ex.DASH
    assert ex.fmt(None) == "N/A"


def test_nap_is_py2027_only_and_bridge_preserved(tmp_path):
    report = ex.build_report(evidence(tmp_path))
    nap = report["new_active_passive"]
    assert nap["comparison_status"] == ex.PY2027_ONLY
    assert nap["py2026"] == ex.NAP_PY2026
    assert nap["phase_6d"]["new_classification"] == "OTHER_LIFECYCLE_CLASS"
    assert nap["phase_6d"]["activity_bridge"] == "NO_ACTIVITY_BRIDGE"
    matrix = {row["metric"]: row for row in report["comparison_quality_matrix"]}
    assert all(matrix[m]["semantic_status"] == ex.PY2027_ONLY for m in ("New", "Active", "Passive"))
    assert "NO_ACTIVITY_BRIDGE" in " ".join(report["caveats"])


def test_cancellation_population_and_activity_never_compared(tmp_path):
    report = ex.build_report(evidence(tmp_path))
    cancel = report["cancellation_termination"]
    assert cancel["statement"] == ex.CANCEL_STATEMENT
    assert cancel["py2026_population"][0]["comparison_status"] == ex.PY2026_ONLY
    assert cancel["py2027_daily_activity"]["cancelled_policies_daily"]["points"][0] == {"date": "2026-11-01",
                                                                                      "value": 2534}
    assert cancel["py2027_daily_activity"]["terminated_policies_daily"]["points"] is None
    text = json.dumps(cancel)
    assert "absolute_difference" not in text and "percent_difference" not in text
    assert all("Cancel" not in row["metric"] for row in report["management_view"])


def test_application_metrics_never_use_py2026_table(tmp_path):
    report = ex.build_report(evidence(tmp_path))
    for row in report["applications"]["rows"]:
        assert row["py2026_value"] == ex.NOT_FROM_ENROLLMENT
        assert row["comparison_status"] == ex.PY2027_ONLY
    assert report["applications"]["funnel"]["not_yet_submitted"] == 100000
    applicants = [r for r in report["applications"]["rows"] if r["metric"] == "Total Applicants"][0]
    assert applicants["verification"] == ex.VALIDATION_ONLY


def test_temporal_section_labelled_and_anchor_unproven(tmp_path):
    temporal = ex.build_report(evidence(tmp_path))["oe_temporal_diagnostic"]
    assert temporal["label"] == "OE_LIFECYCLE_ALIGNMENT_DIAGNOSTIC"
    assert temporal["anchor"] == "Official OE start anchor not proven; calendar-date alignment only."
    roles = [m["period_role"] for m in temporal["fields"]["enrollment_create_date"]["monthly"]]
    assert roles[0].startswith("context") and roles[1].startswith("OE emphasis")


def test_works_with_optional_artifacts_missing(tmp_path):
    report = ex.build_report({"phase_6b": None, "phase_6d": None, "validation": None,
                              "history": ex.read_history(None)})
    assert report["population_comparison"]["available"] is False
    assert report["oe_temporal_diagnostic"]["available"] is False
    assert report["cancellation_termination"]["py2026_population"] is None
    assert report["executive_summary"]["py2027_observed_at_utc"] is None
    assert all(row["value"] is None for row in report["py2027_current_snapshot"])
    json_path, html_path = ex.write_report(report, tmp_path / "out")
    assert json_path.is_file() and "No data available" in html_path.read_text(encoding="utf-8")


def test_history_without_series_table(tmp_path):
    report = ex.build_report(evidence(tmp_path, series=False))
    assert all(item["points"] is None for item in report["cancellation_termination"]["py2027_daily_activity"].values())


def test_findings_are_factual_without_causal_language(tmp_path):
    findings = " ".join(ex.build_report(evidence(tmp_path))["key_findings"]).lower()
    assert "950,000" in findings and "900,000" in findings
    for word in ("renewal", "migration", "bulk load", "because", "caused", "issuer", "behavior", "behaviour"):
        assert word not in findings


def test_summary_statement_and_observation_time(tmp_path):
    summary = ex.build_report(evidence(tmp_path))["executive_summary"]
    assert summary["statement"] == ex.SUMMARY_STATEMENT
    assert summary["py2027_observed_at_utc"] == "2026-11-08T12:00:00+00:00"


def test_guards_reject_pii_and_auth(tmp_path):
    with pytest.raises(ComparisonOutputError):
        ex.guard({"enrollee_id": 1})
    for key in ("cookie", "Authorization", "token", "headers"):
        with pytest.raises(ex.ExecutiveOutputError):
            ex.guard({"nested": [{key: "x"}]})


def test_output_contains_no_pii_or_auth(tmp_path):
    report = ex.build_report(evidence(tmp_path))
    json_path, html_path = ex.write_report(report, tmp_path / "out")
    text = (json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")).lower()
    for token in ('"enrollee_id"', '"enrollment_id"', "cookie", "authorization", "bearer"):
        assert token not in text
    assert "<svg" in text and "diagnostic" in text and "py2027_only" in text
