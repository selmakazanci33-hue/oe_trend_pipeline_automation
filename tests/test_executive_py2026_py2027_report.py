import json
import sqlite3

import pytest

from comparison import executive_py2026_py2027_report as ex
from comparison.currently_enrolled import ComparisonOutputError

LIVE_AT = "2026-11-09T12:00:00+00:00"
OLD_AT = "2026-11-01T12:00:00+00:00"


def make_history(path, with_series=True):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metric_snapshots (id INTEGER PRIMARY KEY, run_id TEXT, metric_id TEXT, "
                       "metric_value REAL, plan_year INT, retrieved_at_utc TEXT, snapshot_slot_utc TEXT, source TEXT, "
                       "retrieval_status TEXT)")
    rows = [
        ("r0", "currently_enrolled_people", 800000, "2026-10-30T12:00:00+00:00"),
        ("r1", "currently_enrolled_people", 900000, OLD_AT),
        ("r1", "percent_effectuated", 0.5, OLD_AT),
        ("r1", "applications_started", 1, OLD_AT),
        ("r1", "submitted_applications", 1, OLD_AT),
        ("r1", "new_enrolled", 1, OLD_AT),
        ("r1", "active_enrolled", 1, OLD_AT),
        ("r1", "passive_enrolled", 1, OLD_AT),
    ]
    connection.executemany(
        "INSERT INTO metric_snapshots (run_id, metric_id, metric_value, plan_year, retrieved_at_utc, "
        "snapshot_slot_utc, source, retrieval_status) VALUES (?, ?, ?, 2027, ?, substr(?, 1, 13), 'Sisense', "
        "'VALIDATED')", [(run, metric, value, at, at) for run, metric, value, at in rows])
    if with_series:
        connection.execute("CREATE TABLE source_series_points (id INTEGER PRIMARY KEY, metric_id TEXT, source_date TEXT, "
                           "time_grain TEXT, date_semantics TEXT, value INT, definition_fingerprint TEXT, "
                           "observed_at_utc TEXT)")
        connection.executemany(
            "INSERT INTO source_series_points (metric_id, source_date, time_grain, date_semantics, value, "
            "definition_fingerprint, observed_at_utc) VALUES ('cancelled_policies_daily', ?, 'days', 'last_update', ?, "
            "'fp', ?)", [("2026-10-30", 111, OLD_AT), ("2026-10-31", 222, OLD_AT)])
    connection.commit()
    connection.close()


def phase_6b(scale=1):
    return {
        "currently_enrolled_candidates": [
            {"candidate_id": "CE1", "description": "status filter", "distinct_people": 1000000 * scale},
            {"candidate_id": "CE2", "description": "benefit window", "distinct_people": 865946 * scale},
        ],
        "percent_effectuated": {"per_candidate": [{"candidate_id": "CE1", "ratio": 0.9}]},
        "identifier_behavior": {"distinct_people": 1320502, "distinct_policies": 1688696},
        "status_distributions": {
            "enrollment_status": [
                {"status_value": "Pending", "row_count": 5, "distinct_people": 11322, "distinct_policies": 3},
                {"status_value": "Terminated", "row_count": 5, "distinct_people": 596598, "distinct_policies": 3},
                {"status_value": "Enrolled", "row_count": 5, "distinct_people": 871095, "distinct_policies": 3},
                {"status_value": "Cancelled", "row_count": 5, "distinct_people": 529245, "distinct_policies": 3}],
            "enrollee_status": None},
        "population_diagnostics": [{"label": "PY2026_LATEST_CANCELLED_POPULATION", "distinct_policies": 200000,
                                    "distinct_people": 150000}],
        "oe_aligned_temporal_diagnostics": {"fields": {
            "enrollment_create_date": {
                "monthly": [{"month": "2025-10", "row_count": 10, "distinct_people": 900000, "distinct_policies": 9},
                            {"month": "2025-11", "row_count": 10, "distinct_people": 100000, "distinct_policies": 9},
                            {"month": "2026-01", "row_count": 10, "distinct_people": 5000, "distinct_policies": 9}],
                "cumulative": [{"through": "2025-11-08", "distinct_people": 1037238, "distinct_policies": 1},
                               {"through": "2025-12-31", "distinct_people": 1172362, "distinct_policies": 1}]},
            "enrollment_confirmation_date": {
                "monthly": [{"month": "2025-10", "row_count": 10, "distinct_people": 800000, "distinct_policies": 9},
                            {"month": "2025-12", "row_count": 10, "distinct_people": 90000, "distinct_policies": 9}],
                "cumulative": None}}},
    }


LIVE_VALUES = {
    "currently_enrolled_people": {"value": 950000, "retrieved_at": LIVE_AT},
    "percent_effectuated": {"value": 0.62, "retrieved_at": LIVE_AT},
    "applications_started": {"value": 400000, "retrieved_at": LIVE_AT},
    "submitted_applications": {"value": 300000, "retrieved_at": LIVE_AT},
    "total_applicants": {"value": 500000, "retrieved_at": LIVE_AT},
    "new_enrolled": {"value": 4400, "retrieved_at": LIVE_AT},
    "active_enrolled": {"value": 58800, "retrieved_at": LIVE_AT},
    "passive_enrolled": {"value": 734500, "retrieved_at": LIVE_AT},
}
LIVE_SERIES = {"cancelled_policies_daily": {"points": [{"date": "2026-11-01", "value": 2534},
                                                       {"date": "2026-11-02", "value": 5724}],
                                            "retrieved_at": LIVE_AT}}


def live_model(tmp_path, values=LIVE_VALUES, series=LIVE_SERIES, live_ok=True, db_live=True, history=True,
               with_series_table=True):
    path = tmp_path / "h.sqlite3"
    if history:
        make_history(path, with_series_table)
    hist = ex.read_history(path if history else None)
    py2026 = ex.py2026_live(phase_6b(), LIVE_AT) if db_live else ex.py2026_fallback(phase_6b(scale=2), OLD_AT, "x")
    py2027 = ex.py2027_model(values, series, hist, None, live_ok, None if live_ok else "SisenseError")
    return ex.build_model(py2026, py2027, hist, None, LIVE_AT)


def report_for(tmp_path, **kwargs):
    return ex.build_report(live_model(tmp_path, **kwargs))


def snapshot(report):
    return {row["metric_id"]: row for row in report["py2027_current_snapshot"]}


def rows_by_metric(report):
    return {row["metric"]: row for row in report["population_comparison"]["rows"]}


# --- freshness ---------------------------------------------------------------------------

def test_live_db_values_override_historical_phase_6b_artifact(tmp_path, monkeypatch):
    artifact = tmp_path / "6b.json"
    artifact.write_text(json.dumps(phase_6b(scale=2)), encoding="utf-8")
    monkeypatch.setattr(ex, "PHASE_6B_REPORT", artifact)
    report = report_for(tmp_path)
    ce1 = rows_by_metric(report)["Currently Enrolled vs PY2026 CE1"]
    assert ce1["py2026_value"] == 1000000 and ce1["py2026_freshness"] == ex.LIVE
    assert ce1["py2026_retrieved_at"] == LIVE_AT
    assert report["executive_summary"]["py2026"]["freshness"] == ex.LIVE


def test_live_sisense_values_override_sqlite_current_values(tmp_path):
    report = report_for(tmp_path)
    ce = snapshot(report)["currently_enrolled_people"]
    assert ce["value"] == 950000 and ce["freshness"] == ex.LIVE and ce["retrieved_at"] == LIVE_AT
    assert ce["source"] == ex.PY2027_SOURCE
    assert report["executive_summary"]["py2027"]["note"] == f"PY2027 Sisense: {ex.LIVE} / {LIVE_AT}"


def test_successful_live_values_are_live_this_run(tmp_path):
    report = report_for(tmp_path)
    assert all(row["freshness"] == ex.LIVE for row in report["py2027_current_snapshot"])
    assert all(k["freshness"] == ex.LIVE for k in report["kpis"])
    series = report["cancellation_termination"]["py2027_daily_activity"]["cancelled_policies_daily"]
    assert series["freshness"] == ex.LIVE and series["points"][0] == {"date": "2026-11-01", "value": 2534}


def test_sisense_failure_is_visibly_stale_fallback(tmp_path):
    report = report_for(tmp_path, live_ok=False)
    ce = snapshot(report)["currently_enrolled_people"]
    assert ce["value"] == 900000 and ce["freshness"] == ex.STALE and ce["retrieved_at"] == OLD_AT
    summary = report["executive_summary"]["py2027"]
    assert summary["freshness"] == ex.STALE
    assert summary["note"].startswith("PY2027 Sisense unavailable") and OLD_AT in summary["note"]
    executive_part = ex.render_html(report).split("<details id='technical'", 1)[0]
    assert "Last validated" in executive_part and "STALE_FALLBACK" not in executive_part
    series = report["cancellation_termination"]["py2027_daily_activity"]["cancelled_policies_daily"]
    assert series["freshness"] == ex.STALE and series["points"][0]["date"] == "2026-10-30"


def test_db_failure_is_visibly_stale_fallback(tmp_path):
    report = report_for(tmp_path, db_live=False)
    py26 = report["executive_summary"]["py2026"]
    assert py26["freshness"] == ex.STALE and "PY2026 DB unavailable" in py26["note"]
    assert rows_by_metric(report)["Currently Enrolled vs PY2026 CE1"]["py2026_freshness"] == ex.STALE


def test_missing_live_metric_falls_back_per_metric(tmp_path):
    values = {k: v for k, v in LIVE_VALUES.items() if k != "new_enrolled"}
    item = snapshot(report_for(tmp_path, values=values))
    assert item["new_enrolled"]["freshness"] == ex.STALE and item["new_enrolled"]["retrieved_at"] == OLD_AT
    assert item["active_enrolled"]["freshness"] == ex.LIVE


def test_unavailable_without_live_or_history(tmp_path):
    report = report_for(tmp_path, live_ok=False, history=False)
    assert all(row["freshness"] == ex.UNAVAILABLE and row["value"] is None
               for row in report["py2027_current_snapshot"])
    assert report["executive_summary"]["py2027"]["freshness"] == ex.UNAVAILABLE


def test_freshness_does_not_alter_comparison_status(tmp_path):
    statuses = []
    for index, kwargs in enumerate(({}, {"live_ok": False}, {"db_live": False})):
        folder = tmp_path / f"m{index}"
        folder.mkdir()
        report = report_for(folder, **kwargs)
        statuses.append(tuple((r["metric"], r["comparison_status"]) for r in report["population_comparison"]["rows"]))
        assert report["production_comparable"] is False
        assert report["new_active_passive"]["comparison_status"] == ex.PY2027_ONLY
    assert statuses[0] == statuses[1] == statuses[2]
    assert dict(statuses[0])["Currently Enrolled vs PY2026 CE1"] == ex.DIAGNOSTIC


# --- semantics -------------------------------------------------------------------------------

def test_invalid_comparisons_never_get_differences(tmp_path):
    report = report_for(tmp_path)
    for row in report["population_comparison"]["rows"]:
        if row["comparison_status"] not in ex.DIFF_STATUSES:
            assert row["absolute_difference"] is None and row["percent_difference"] is None
    for status in (ex.PY2027_ONLY, ex.PY2026_ONLY, ex.NOT_COMPARABLE):
        assert ex.difference(10, 20, status)["absolute_difference"] is None


def test_diagnostic_rows_get_labelled_differences(tmp_path):
    rows = rows_by_metric(report_for(tmp_path))
    ce1 = rows["Currently Enrolled vs PY2026 CE1"]
    assert ce1["absolute_difference"] == -50000 and ce1["percent_difference"] == -5.0
    pe = rows["% Effectuated vs PY2026 CE1"]
    assert pe["percent_difference"] is None and pe["absolute_difference"] == -28.0


def test_nap_is_py2027_only_and_not_a_proven_partition(tmp_path):
    report = report_for(tmp_path)
    nap = report["new_active_passive"]
    assert nap["proven_partition"] is False and nap["py2026"] == ex.NAP_PY2026
    assert nap["phase_6d"]["new_classification"] == "OTHER_LIFECYCLE_CLASS"
    assert nap["phase_6d"]["activity_bridge"] == "NO_ACTIVITY_BRIDGE"
    findings = " ".join(report["key_findings"])
    assert "Not a proven partition" in findings
    assert ex.NAP_FOOTER in ex.nap_chart(report)
    assert "PY2027 Enrollment Mix" in ex.render_html(report)


def test_application_grains_remain_separate(tmp_path):
    report = report_for(tmp_path)
    apps = report["applications"]
    assert apps["funnel"] == {"started": 400000, "submitted": 300000, "not_yet_submitted": 100000,
                              "submitted_share_of_started": 75.0, "grain": "application"}
    grains = {row["metric"]: row["grain"] for row in apps["rows"]}
    assert grains["Total Applicants"] == "applicant" and grains["Applications Started"] == "application"
    assert all(row["py2026_value"] == ex.NOT_FROM_ENROLLMENT for row in apps["rows"])
    chart = ex.funnel_chart(report)
    assert "75.0%" in chart and "separate grain" in chart


def test_cancellation_population_never_compared_to_activity(tmp_path):
    report = report_for(tmp_path)
    cancel = report["cancellation_termination"]
    text = json.dumps(cancel)
    assert "absolute_difference" not in text and "percent_difference" not in text
    assert cancel["statement"] == ex.CANCEL_STATEMENT
    assert all("Cancel" not in row["metric"] for row in report["management_view"])


def test_management_view_uses_dash_not_zero(tmp_path):
    view = {row["metric"]: row for row in report_for(tmp_path)["management_view"]}
    assert view["Currently Enrolled (PY2026 CE1-CE4 range)"]["py2026"] == "865,946 – 1,000,000"
    for label in ("New", "Active", "Passive", "Applications Started"):
        assert view[label]["py2026"] == ex.DASH and view[label]["change"] == ex.DASH


# --- charts ------------------------------------------------------------------------------

def test_charts_render_when_values_exist(tmp_path):
    report = report_for(tmp_path)
    html_text = ex.render_html(report)
    assert html_text.count("<svg") >= 9
    assert ex.CE_YOY_TITLE in html_text and ex.CE_CHART_STATUS in ex.ce_chart(report)
    assert "865,946 – 1,000,000" in ex.ce_chart(report) and "950,000" in ex.ce_chart(report)
    monthly = ex.monthly_chart(report)
    assert "PY2026 Enrollment Lifecycle Activity by Month" in monthly and ex.OE_LABEL in monthly
    assert ex.MONTHLY_FOOTNOTE in monthly
    for month in ("Oct 2025", "Nov 2025", "Dec 2025", "Jan 2026"):
        assert month in monthly
    assert "http://" not in html_text and "https://" not in html_text and "<script src" not in html_text


def test_unavailable_chart_data_creates_card_not_zeros(tmp_path):
    report = ex.build_report(ex.build_model(
        ex.py2026_fallback(None, None, "x"),
        ex.py2027_model({}, {}, ex.read_history(None), None, False, "x"), ex.read_history(None), None, LIVE_AT))
    html_text = ex.render_html(report)
    assert "<svg" not in html_text
    assert "Daily series not available for this run." in html_text
    assert ex.SERIES_UNAVAILABLE in ex.daily_chart(report)
    assert "unavailable for this run" in ex.nap_chart(report)
    assert "unavailable for this run" in ex.monthly_chart(report)
    assert ">0<" not in html_text


def test_missing_month_value_draws_no_bar():
    svg = ex.grouped_vbar_svg(["2025-10", "2025-11"], [("a", "#000", {"2025-10": 5, "2025-11": None})])
    assert svg.count("<rect") == 2  # one bar + one legend swatch


def test_daily_chart_only_with_series(tmp_path):
    assert ex.SERIES_UNAVAILABLE in ex.daily_chart(report_for(tmp_path, series={}, with_series_table=False))
    assert "<svg" in ex.daily_chart(report_for(tmp_path))


# --- safety -------------------------------------------------------------------------------

def test_guards_reject_pii_and_auth():
    with pytest.raises(ComparisonOutputError):
        ex.guard({"enrollee_id": 1})
    for key in ("cookie", "Authorization", "token", "headers", "csrf", "password", "body"):
        with pytest.raises(ex.ExecutiveOutputError):
            ex.guard({"nested": [{key: "x"}]})


def test_output_contains_no_pii_or_auth(tmp_path):
    json_path, html_path = ex.write_report(report_for(tmp_path), tmp_path / "out")
    text = (json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")).lower()
    for token in ('"enrollee_id"', '"enrollment_id"', "cookie", "authorization", "bearer", "x-xsrf"):
        assert token not in text


def test_artifact_fallback_path_is_never_live(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "PHASE_6B_REPORT", tmp_path / "missing.json")
    monkeypatch.setattr(ex, "VALIDATION_REPORT", tmp_path / "missing2.json")
    monkeypatch.setattr(ex, "PHASE_6D_REPORT", tmp_path / "missing3.json")
    make_history(tmp_path / "h.sqlite3")
    report = ex.build_report(ex.model_from_artifacts(tmp_path / "h.sqlite3"))
    assert report["executive_summary"]["py2027"]["freshness"] == ex.STALE
    assert report["executive_summary"]["py2026"]["freshness"] == ex.UNAVAILABLE
    assert all(row["freshness"] != ex.LIVE for row in report["py2027_current_snapshot"])
