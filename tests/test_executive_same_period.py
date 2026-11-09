"""Phase 7C: same-period PY2026 vs PY2027 comparison, status population and daily date labels."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from comparison import executive_py2026_py2027_report as ex
from metrics.date_labels import valid_date_label
from tests.test_executive_py2026_py2027_report import (
    LIVE_AT, LIVE_SERIES, LIVE_VALUES, OLD_AT, live_model, phase_6b, report_for,
)

INVALID = "012orADaAe"


# --- date labels ----------------------------------------------------------------------------

@pytest.mark.parametrize("value", [INVALID, "", None, 20261101, "11/01/2026", "Nov 1", "2026-13-01", "2026-02-30",
                                   "1899-01-01", "2026-11-01 junk"])
def test_invalid_date_strings_are_rejected(value):
    assert valid_date_label(value) is None


@pytest.mark.parametrize("value,expected", [("2026-11-01", "2026-11-01"), ("2026-11-01T00:00:00", "2026-11-01"),
                                            ("2026-11-01T00:00:00.000Z", "2026-11-01"),
                                            ("2026-11-01 05:00:00", "2026-11-01")])
def test_iso_dates_are_kept(value, expected):
    assert valid_date_label(value) == expected


def _invalid_series():
    return {"cancelled_policies_daily": {"points": [{"date": INVALID, "value": 2534}, {"date": INVALID, "value": 5724},
                                                    {"date": "2026-11-03", "value": 6752}],
                                         "retrieved_at": LIVE_AT}}


def test_invalid_cancellation_dates_never_render_as_calendar_dates(tmp_path):
    report = report_for(tmp_path, series=_invalid_series())
    item = report["cancellation_termination"]["py2027_daily_activity"]["cancelled_policies_daily"]
    assert item["axis"] == "ORDERED_OBSERVATION" and item["axis_note"] == ex.ORDERED_DAILY
    assert [p["date"] for p in item["points"]][:2] == [None, None]
    text = json.dumps(report) + ex.render_html(report)
    assert INVALID not in text


def test_ordered_observations_fall_back_to_day_labels(tmp_path):
    chart = ex.daily_chart(report_for(tmp_path, series=_invalid_series()))
    assert ex.ORDERED_DAILY in chart
    for label in ("Day 1", "Day 2", "Day 3"):
        assert label in chart
    assert "Nov 03" not in chart
    assert ex.daily_axis([{"date": INVALID}, {"date": None}]) == (["Day 1", "Day 2"], False)
    assert ex.daily_axis([{"date": "2026-11-01"}]) == (["2026-11-01"], True)


def test_valid_daily_dates_use_calendar_axis(tmp_path):
    chart = ex.daily_chart(report_for(tmp_path))
    assert "Nov 01" in chart and ex.ORDERED_DAILY not in chart


def test_parser_prefers_iso_data_over_masked_text():
    from metrics.source_series import _source_date
    assert _source_date({"data": "2026-11-01T00:00:00", "text": INVALID}) == "2026-11-01"
    assert _source_date({"data": None, "text": INVALID}) is None
    with pytest.raises(ValueError):
        _source_date({"data": None, "text": ""})


# --- same-period semantics -----------------------------------------------------------------

def test_same_period_never_pairs_incompatible_families(tmp_path):
    report = report_for(tmp_path)
    activity = report["same_period"]["activity"]
    assert activity["py2027_series"] is None and activity["note"] == ex.ACTIVITY_PY2027_UNAVAILABLE
    assert ex.NEW not in ex.COMPATIBLE_PY2027_ACTIVITY_METRICS
    (tmp_path / "forced").mkdir()
    model = live_model(tmp_path / "forced")
    model["py2027"]["activity_series"] = {ex.NEW: {"points": [{"date": "2026-11-08", "value": 4525}]}}
    forced = ex.build_report(model)["same_period"]["activity"]
    assert forced["py2027_series"] is None and forced["comparison_status"] == ex.PY2026_ONLY
    trend = report["same_period"]["currently_enrolled_trend"]
    assert trend["py2026_available"] is False and trend["note"] == ex.PY2026_POPULATION_SERIES_UNAVAILABLE
    assert all(r["py2026_value"] is None and r["absolute_difference"] is None for r in trend["rows"])
    chart = ex.trend_chart(report)
    assert ex.PY2026_POPULATION_SERIES_UNAVAILABLE in chart and "PY2026 population at same cutoff" not in chart


def test_trend_never_uses_activity_as_population(tmp_path):
    trend = report_for(tmp_path)["same_period"]["currently_enrolled_trend"]
    assert all(r["py2026_value"] is None for r in trend["rows"])


def test_trend_pairs_shifted_dates_when_population_series_exists(tmp_path):
    model = live_model(tmp_path)
    model["py2026"]["report"] = {**phase_6b(), "historical_population_series": [
        {"date": "2025-11-01", "distinct_people": 700000}, {"date": "2025-11-09", "distinct_people": 760000}]}
    trend = ex.build_report(model)["same_period"]["currently_enrolled_trend"]
    by_day = {r["py2027_date"]: r for r in trend["rows"]}
    assert by_day["2026-11-09"]["py2026_date"] == "2025-11-09"
    assert by_day["2026-11-09"]["py2026_value"] == 760000 and by_day["2026-11-09"]["absolute_difference"] == 190000
    assert trend["comparison_status"] == ex.DIAGNOSTIC
    assert ex.shift_plan_year("2028-02-29") == "2027-02-28"


def test_trend_uses_accumulated_history_plus_live(tmp_path):
    trend = report_for(tmp_path)["same_period"]["currently_enrolled_trend"]
    days = [r["py2027_date"] for r in trend["rows"]]
    assert days == ["2026-10-30", "2026-11-01", "2026-11-09"]
    assert trend["rows"][-1]["py2027_freshness"] == ex.LIVE
    assert trend["rows"][0]["py2027_freshness"] == ex.HISTORICAL


def test_partial_month_never_compares_to_full_month():
    result = ex.monthly_comparison({"2025-11": 100000}, {"2026-11": {"value": 5000, "complete": False,
                                                                       "through_day": 9}}, compatible=True)
    row = result["rows"][0]
    assert row["period"] == ex.MONTH_TO_DATE and row["py2026_value"] is None
    assert row["absolute_difference"] is None and result["message"] == ex.MTD_UNAVAILABLE
    mtd = ex.monthly_comparison({"2025-11": 100000}, {"2026-11": {"value": 5000, "complete": False, "through_day": 9}},
                                compatible=True, py2026_mtd={("2025-11", 9): 4000})
    assert mtd["rows"][0]["py2026_value"] == 4000 and mtd["rows"][0]["absolute_difference"] == 1000
    full = ex.monthly_comparison({"2025-10": 900}, {"2026-10": {"value": 1000, "complete": True}}, compatible=True)
    assert full["rows"][0]["period"] == ex.FULL_MONTH and full["rows"][0]["py2026_value"] == 900


def test_incompatible_monthly_comparison_is_unavailable(tmp_path):
    result = ex.monthly_comparison({"2025-11": 1}, {"2026-11": {"value": 1, "complete": True}}, compatible=False)
    assert result["available"] is False and result["comparison_status"] == ex.NOT_COMPARABLE
    assert result["rows"] == []
    assert ex.MTD_UNAVAILABLE in ex.mtd_card(report_for(tmp_path))


def test_na_remains_unavailable_in_same_date_table(tmp_path):
    values = {k: v for k, v in LIVE_VALUES.items() if k != "active_enrolled"}
    report = report_for(tmp_path, values=values, history=False)
    rows = {r["metric"]: r for r in report["same_date_table"]}
    assert rows["Active"]["py2027"] == "N/A"
    for label in ("Applications Started", "Submitted Applications", "New", "Active", "Passive"):
        assert rows[label]["py2026"] == ex.DASH and rows[label]["difference"] == ex.DASH
        assert rows[label]["status"] == ex.PY2027_ONLY
    assert "0" != rows["Active"]["py2027"]


# --- CE closest scenario -------------------------------------------------------------------

def test_closest_scenario_is_diagnostic_and_not_official(tmp_path):
    report = report_for(tmp_path)
    closest = report["closest_scenario"]
    assert closest["candidate_id"] == "CE1" and closest["selected_as_official"] is False
    assert closest["label"] == ex.CLOSEST_LABEL and closest["comparison_status"] == ex.DIAGNOSTIC
    chart = ex.ce_chart(report)
    assert ex.CLOSEST_LABEL in chart and "not evidence of semantic equivalence" in chart
    assert "865,946 → 1,000,000" in chart
    assert report["currently_enrolled_range"]["scenarios"][0]["candidate_id"] == "CE1"


# --- status populations ----------------------------------------------------------------------

def test_status_population_chart_is_py2026_only_in_order(tmp_path):
    report = report_for(tmp_path)
    data = report["py2026_status_population_chart"]
    assert [b["status"] for b in data["bars"]] == ["Enrolled", "Cancelled", "Terminated", "Pending"]
    assert data["comparison_status"] == ex.PY2026_ONLY and data["freshness"] == ex.LIVE
    chart = ex.status_population_chart(report)
    assert ex.STATUS_POP_TITLE in chart and "871,095" in chart and "PY2027" in chart


def test_status_population_never_compared_to_cancellation_activity(tmp_path):
    report = report_for(tmp_path)
    chart = ex.status_population_chart(report)
    assert "Cancelled Policies" not in chart and "<polyline" not in chart
    assert report["py2027_status_population"] == {"available": False, "message": ex.PY2027_STATUS_POP_UNAVAILABLE}
    assert ex.PY2027_STATUS_POP_UNAVAILABLE in ex.py2027_status_card(report)
    assert "absolute_difference" not in json.dumps(report["py2026_status_population_chart"])


def test_nap_never_maps_to_py2026_status(tmp_path):
    report = report_for(tmp_path)
    nap_chart = ex.nap_chart(report)
    for status in ("Enrolled", "Cancelled", "Terminated", "Pending"):
        assert f">{status}<" not in nap_chart
    assert "Not a proven partition of Currently Enrolled." in nap_chart
    assert report["new_active_passive"]["py2026"] == ex.NAP_PY2026
    assert all(r["metric"] not in ("New", "Active", "Passive") or r["py2026"] == ex.DASH
               for r in report["same_date_table"])


# --- statuses, freshness, layout -------------------------------------------------------------

def test_comparison_charts_preserve_semantic_status(tmp_path):
    report = report_for(tmp_path)
    assert ex._badge(ex.DIAGNOSTIC) in ex.ce_chart(report)
    assert ex._badge(ex.PY2027_ONLY) in ex.trend_chart(report)
    assert ex._badge(ex.OE_LABEL) in ex.activity_chart(report)
    assert ex._badge(ex.PY2026_ONLY) in ex.status_population_chart(report)
    assert report["production_comparable"] is False


def test_layout_order(tmp_path):
    html_text = ex.render_html(report_for(tmp_path))
    order = ["Plan Year Snapshot", "Plan Year Overview", "PY2026 Enrollment Status Composition",
             "PY2026 Enrollment Activity by Month", "PY2027 Enrollment Mix", "PY2027 Application Funnel",
             "PY2027 Enrollment Population Trend", "PY2027 Daily Policy Activity", "Technical details"]
    positions = [html_text.index(f">{title}<") for title in order]
    assert positions == sorted(positions)
    assert html_text.index("class='kpis'") < positions[0]


def test_live_values_override_stale_artifacts(tmp_path, monkeypatch):
    artifact = tmp_path / "6b.json"
    artifact.write_text(json.dumps(phase_6b(scale=2)), encoding="utf-8")
    monkeypatch.setattr(ex, "PHASE_6B_REPORT", artifact)
    report = report_for(tmp_path)
    assert report["py2026_status_population_chart"]["freshness"] == ex.LIVE
    assert report["currently_enrolled_range"]["low"] == 865946
    assert next(k for k in report["kpis"] if k["metric_id"] == ex.CE)["value"] == 950000


def test_no_auth_or_pii_in_enhanced_report(tmp_path):
    json_path, html_path = ex.write_report(report_for(tmp_path, series=_invalid_series()), tmp_path / "out")
    text = (json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")).lower()
    for token in ('"enrollee_id"', '"enrollment_id"', "cookie", "authorization", "bearer", "x-xsrf", "<script src",
                  "http://", "https://"):
        assert token not in text


# --- history accumulation ------------------------------------------------------------------

def test_history_accumulates_without_duplicate_slots(tmp_path):
    from history.store import SnapshotStore
    from sisense.models import MetricResult

    path = tmp_path / "history.sqlite3"
    moments = iter([datetime(2026, 11, 9, 12, tzinfo=timezone.utc)] * 20)
    with SnapshotStore(path, "hour", clock=lambda: next(moments)) as store:
        for run_id, at, value in (("a", "2026-11-08T12:00:00+00:00", 790000),
                                  ("b", "2026-11-09T12:05:00+00:00", 797762),
                                  ("c", "2026-11-09T12:40:00+00:00", 797999)):
            store.start_run(run_id)
            store.record_snapshot(run_id, MetricResult(metric="currently_enrolled_people", plan_year=2027,
                                                       value=value, retrieved_at=at, source="Sisense",
                                                       dashboard="PY 2027 - OE Executive Dashboard"))
    history = ex.read_history(path)
    points = history["snapshots"]["currently_enrolled_people"]
    assert [p["value"] for p in points] == [790000, 797762]
    assert {"plan_year", "retrieved_at_utc", "snapshot_slot_utc", "source", "validation_state"} <= set(points[0])
    assert all(p["validation_state"] == "VALIDATED" for p in points)
