"""Phase 8C: compact management dashboard. Presentation only."""

from pathlib import Path

from comparison import executive_html as eh
from comparison import executive_py2026_py2027_report as ex
from tests.test_executive_html import executive_part, visible
from tests.test_executive_py2026_py2027_report import report_for

PROJECT = Path(__file__).resolve().parents[1]


def test_no_estimate_language_on_management_page(tmp_path):
    text = visible(executive_part(report_for(tmp_path))).lower()
    for phrase in ("high estimate", "low estimate", "estimated diagnostic range"):
        assert phrase not in text


def test_ce_scenarios_not_prominent_but_in_technical_details(tmp_path):
    report = report_for(tmp_path)
    page = ex.render_html(report)
    management = executive_part(report)
    technical = page.split(f"<details id='{eh.TECHNICAL_ID}'", 1)[1]
    for cid in ("CE1", "CE2", "CE3", "CE4"):
        assert cid not in management
    assert "CE1–CE4" in technical and "CE1" in technical and "CE2" in technical
    assert ex.CE_TECHNICAL_NOTE in page


def test_currently_enrolled_and_effectuated_have_no_numeric_py2026_difference(tmp_path):
    report = report_for(tmp_path)
    overview = eh.plan_year_overview(report)
    assert eh.NOT_COMPARABLE_TEXT in overview
    assert eh.ALIGNMENT_TEXT not in overview and eh.CE_CONTEXT not in overview
    assert "-68,184" not in overview and "+68,184" not in overview
    ce_cells = overview.split("Currently Enrolled", 1)[1].split("% Effectuated", 1)[0]
    pe_cells = overview.split("% Effectuated", 1)[1].split("Enrollment status", 1)[0]
    assert "865,946" not in ce_cells and "1,000,000" not in ce_cells
    assert "90.0%" not in pe_cells
    page = executive_part(report)
    assert "Change" not in page.split(eh.COMPARISON_TITLE, 1)[1].split("PY2027 Enrollment Mix", 1)[0]


def test_created_and_confirmed_share_one_monthly_activity_chart(tmp_path):
    page = executive_part(report_for(tmp_path))
    assert page.count(eh.ACTIVITY_TITLE) == 1
    card = page.split(eh.ACTIVITY_TITLE, 1)[1].split("<section", 1)[0]
    assert ">Created<" in card and ">Confirmed<" in card
    assert card.split("<details", 1)[0].count("<svg") == 1
    for month in ("Oct 2025", "Nov 2025", "Dec 2025", "Jan 2026"):
        assert month in card
    assert eh.ACTIVITY_NOTE in card
    assert "Monthly Enrollment Created" not in page
    assert "Enrollment Activity — Cumulative People by Date" not in page
    assert "PY2026 Enrollment Lifecycle Activity by Month" not in page
    assert ex.SAME_PERIOD_TITLE not in page
    assert ex.CE_YOY_TITLE not in page
    assert ">Same-Date Comparison<" not in page
    assert ">Management View<" not in page
    assert ">Monthly Enrollment<" not in page


def test_population_trend_is_not_daily_enrollment_activity(tmp_path):
    page = executive_part(report_for(tmp_path))
    assert eh.POPULATION_TREND_TITLE in page
    assert eh.POPULATION_TREND_SUBTITLE in page
    assert "daily enrollment activity" not in page.lower()
    assert "Daily Enrollment" not in page
    assert eh.DAILY_POLICY_TITLE in page
    pop = page.split(eh.POPULATION_TREND_TITLE, 1)[1].split(eh.DAILY_POLICY_TITLE, 1)[0]
    assert "Cancelled" not in pop and "Terminated" not in pop
    daily = page.split(eh.DAILY_POLICY_TITLE, 1)[1]
    assert "Currently Enrolled snapshots" not in daily


def test_unavailable_counterpart_is_dash_not_zero(tmp_path):
    overview = eh.plan_year_overview(report_for(tmp_path))
    assert f"<td>{ex.DASH}</td>" in overview
    assert ">0<" not in overview


def test_total_applicants_is_applicant_level_reference(tmp_path):
    chart = eh.funnel_chart(report_for(tmp_path))
    assert eh.APPLICANT_REFERENCE in chart
    assert "funnel stage" not in chart.lower()


def test_technical_details_remain_collapsed(tmp_path):
    page = ex.render_html(report_for(tmp_path))
    assert f"<details id='{eh.TECHNICAL_ID}'" in page
    assert f"<details id='{eh.TECHNICAL_ID}' open" not in page
    assert "<summary>Technical details</summary>" in page


def test_no_additional_acquisition_in_html_renderer():
    text = (PROJECT / "comparison" / "executive_html.py").read_text(encoding="utf-8")
    for marker in ("retrieve_live", "run_pipeline", "read_history", "playwright", "request.post"):
        assert marker not in text


def test_two_series_renderer_remains_available():
    assert callable(eh.monthly_chart) and callable(ex.grouped_vbar_svg) and callable(ex.line_svg)


def test_kpis_use_live_py2027_values(tmp_path):
    page = executive_part(report_for(tmp_path))
    kpis = page.split("class='kpis'", 1)[1].split("<section", 1)[0]
    assert "Currently Enrolled" in kpis and "950,000" in kpis
    assert "% Effectuated" in kpis and "62.0%" in kpis
    assert "Applications Started" in kpis and "Submitted Applications" in kpis
    assert "Current" in kpis and "PY2027 dashboard" not in kpis
    assert "LIVE_THIS_RUN" not in kpis and "STALE_FALLBACK" not in kpis


def test_status_population_rows_are_exact_database_counts(tmp_path):
    snapshot = eh.plan_year_snapshot(report_for(tmp_path))
    assert "871,095" in snapshot and "529,245" in snapshot
    assert "596,598" in snapshot and "11,322" in snapshot
    chart = eh.status_composition(report_for(tmp_path))
    assert "Exact database count" in chart


def test_no_auth_or_pii_leakage(tmp_path):
    json_path, html_path = ex.write_report(report_for(tmp_path), tmp_path / "out")
    text = (json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")).lower()
    for token in ('"enrollee_id"', '"enrollment_id"', "cookie", "authorization", "bearer", "x-xsrf", "password"):
        assert token not in text
