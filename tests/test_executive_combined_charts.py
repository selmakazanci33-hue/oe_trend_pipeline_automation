"""Phase 7E: combined YoY charts only when both years are comparable; no estimate language."""

from comparison import executive_html as eh
from comparison import executive_py2026_py2027_report as ex
from tests.test_executive_html import executive_part, visible
from tests.test_executive_py2026_py2027_report import live_model, report_for

FORBIDDEN_ESTIMATE = ("high estimate", "low estimate", "estimated diagnostic range", "estimated")


def test_no_estimate_language_in_management_html(tmp_path):
    text = visible(executive_part(report_for(tmp_path))).lower()
    for phrase in FORBIDDEN_ESTIMATE:
        assert phrase not in text, phrase


def test_status_counts_labelled_exact_latest_state(tmp_path):
    chart = eh.status_population(report_for(tmp_path))
    assert "Exact database count" in chart
    assert ex.STATUS_POP_SUBTITLE in chart
    assert ex.STATUS_POP_TITLE in chart
    for phrase in ("estimate", "indicative", "scenario"):
        assert phrase not in chart.lower()


def test_currently_enrolled_difference_blank_until_definition_selected(tmp_path):
    report = report_for(tmp_path)
    row = next(r for r in report["same_date_table"] if r["metric"] == "Currently Enrolled")
    assert row["difference"] == ex.DASH and row["status"] == ex.ALIGNMENT
    view = next(r for r in report["management_view"] if r["metric"] == "Currently Enrolled")
    assert view["change"] == ex.DASH
    table = eh.same_date_table(report) + eh.management_table(report)
    assert "Definition alignment in progress" in table
    assert "-68,184" not in table and "+68,184" not in table


def test_effectuated_difference_blank_until_alignment_proven(tmp_path):
    report = report_for(tmp_path)
    row = next(r for r in report["same_date_table"] if r["metric"] == "% Effectuated")
    assert row["difference"] == ex.DASH and "across candidate definitions" in row["py2026"]
    view = next(r for r in report["management_view"] if r["metric"] == "% Effectuated")
    assert view["change"] == ex.DASH and "across candidate definitions" in view["py2026"]


def test_one_year_chart_never_titled_as_vs(tmp_path):
    page = executive_part(report_for(tmp_path))
    for heading in page.split("<h4>")[1:]:
        title = heading.split("</h4>", 1)[0]
        if " vs " in title:
            raise AssertionError(f"one-year panel titled as comparison: {title}")
    monthly = eh.monthly_chart(report_for(tmp_path))
    assert "Monthly Enrollment Created — PY2026" in monthly
    assert "Monthly Enrollment Confirmation — PY2026" in monthly
    assert ex.PY2027_PENDING in monthly
    trend = eh.population_trend_chart(report_for(tmp_path))
    assert "Enrollment Population Trend — PY2027" in trend
    daily = eh.daily_chart(report_for(tmp_path))
    assert "Daily Cancelled Policies — PY2027" in daily
    assert "PY2026 vs PY2027" not in daily
    assert "data-metric='enrolled'" not in daily
    assert "Monthly Enrollment Comparison — PY2026 vs PY2027" not in page
    assert "Daily Enrollment Comparison — PY2026 vs PY2027" not in page


def test_two_compatible_series_render_in_one_svg(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "COMPATIBLE_PY2027_ACTIVITY_METRICS", frozenset({"created"}))
    (tmp_path / "yoy").mkdir()
    model = live_model(tmp_path / "yoy")
    model["py2027"]["activity_series"] = {"created": {"points": []}}
    model["py2027"]["activity_monthly"] = {"created": {"2026-10": {"value": 10}, "2026-11": {"value": 20}}}
    chart = eh.monthly_chart(ex.build_report(model))
    panel = [p for p in chart.split("<div class='panel'") if "data-metric='created'" in p][0]
    assert panel.count("<svg") == 1
    assert "Monthly Enrollment Created — PY2026 vs PY2027" in panel
    assert ">PY2026<" in panel and ">PY2027<" in panel


def test_monthly_two_year_values_are_side_by_side_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "COMPATIBLE_PY2027_ACTIVITY_METRICS", frozenset({"created"}))
    (tmp_path / "bars").mkdir()
    model = live_model(tmp_path / "bars")
    model["py2027"]["activity_series"] = {"created": {"points": []}}
    model["py2027"]["activity_monthly"] = {"created": {"2026-10": {"value": 10}}}
    panel = [p for p in eh.monthly_chart(ex.build_report(model)).split("<div class='panel'")
             if "data-metric='created'" in p][0]
    assert panel.count("<rect") >= 3  # two month bars (PY2026 Oct + PY2027 Oct) plus legend swatches
    assert "<polyline" not in panel


def test_daily_two_year_values_are_two_lines(tmp_path):
    (tmp_path / "lines").mkdir()
    model = live_model(tmp_path / "lines")
    model["py2026"]["enrolled_history_comparable"] = True
    model["py2026"]["report"]["historical_population_series"] = [
        {"date": "2025-10-30", "distinct_people": 650000},
        {"date": "2025-11-01", "distinct_people": 700000},
        {"date": "2025-11-09", "distinct_people": 760000}]
    panel = [p for p in eh.population_trend_chart(ex.build_report(model)).split("<div class='panel'")
             if "data-metric='enrolled'" in p][0]
    assert panel.count("<polyline") == 2
    assert "Enrollment Population Trend — PY2026 vs PY2027" in panel


def test_future_null_months_remain_blank(tmp_path):
    created = report_for(tmp_path)["monthly_yoy"]["metrics"]["created"]
    assert created["py2027"] == [None] * 12
    assert all(row["py2027_value"] is None for row in created["rows"])


def test_incompatible_families_never_produce_two_series_chart(tmp_path):
    report = report_for(tmp_path)
    for key in ("cancelled", "terminated"):
        assert report["monthly_yoy"]["metrics"][key]["yoy_enabled"] is False
        assert report["daily_yoy"]["metrics"][key]["yoy_enabled"] is False
    cancelled = eh.daily_chart(report)
    panel = [p for p in cancelled.split("<div class='panel'") if "data-metric='cancelled'" in p]
    assert panel and ">PY2026<" not in panel[0]
    assert "Daily Cancelled Policies — PY2026 vs PY2027" not in cancelled


def test_ce_scenarios_remain_in_technical_details(tmp_path):
    page = ex.render_html(report_for(tmp_path))
    technical = page.split(f"<details id='{eh.TECHNICAL_ID}'", 1)[1]
    for cid in ("CE1", "CE2"):
        assert cid in technical
    assert ex.CE_TECHNICAL_NOTE in technical
    assert "CE1" not in executive_part(report_for(tmp_path))


def test_no_auth_or_pii_leakage(tmp_path):
    json_path, html_path = ex.write_report(report_for(tmp_path), tmp_path / "out")
    text = (json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")).lower()
    for token in ('"enrollee_id"', '"enrollment_id"', "cookie", "authorization", "bearer", "x-xsrf", "password"):
        assert token not in text
