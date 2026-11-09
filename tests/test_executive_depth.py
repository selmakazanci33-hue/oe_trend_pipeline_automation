"""Phase 8D: restore analytical depth. Presentation only. Do not run in this phase."""

import json
from pathlib import Path

from comparison import executive_html as eh
from comparison import executive_py2026_py2027_report as ex
from tests.test_executive_html import executive_part
from tests.test_executive_py2026_py2027_report import live_model, report_for

PROJECT = Path(__file__).resolve().parents[1]
PHASE_8C_SECTIONS = (
    "Plan Year Overview",
    "PY2027 Enrollment Mix",
    "PY2027 Application Funnel",
    "PY2026 Enrollment Activity by Month",
    "PY2027 Enrollment Population Trend",
    "PY2027 Daily Policy Activity",
    "Technical details",
)


def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(ex, "MONTHLY_POPULATION_REPORT", tmp_path / "missing-population.json")
    monkeypatch.setattr(ex, "ACTIVITY_EVIDENCE_REPORT", tmp_path / "missing-evidence.json")


def _snapshots():
    return [
        {"snapshot_date": "2025-05-31", "enrolled_people": 0, "cancelled_people": 0,
         "terminated_people": 0, "pending_people": 0},
        {"snapshot_date": "2025-11-30", "enrolled_people": 800000, "cancelled_people": 400000,
         "terminated_people": 500000, "pending_people": 9000},
        {"snapshot_date": "2026-01-31", "enrolled_people": 871095, "cancelled_people": 529245,
         "terminated_people": 596598, "pending_people": 11322},
    ]


def test_plan_year_snapshot_shows_both_years_without_a_fake_difference(tmp_path, monkeypatch):
    _quiet(monkeypatch, tmp_path)
    page = executive_part(report_for(tmp_path))
    snapshot = page.split(eh.SNAPSHOT_TITLE, 1)[1].split(eh.OVERVIEW_TITLE, 1)[0]
    assert ">PY2026<" in snapshot and ">PY2027<" in snapshot
    assert "Latest Database State" in snapshot and "Current OE Dashboard" in snapshot
    assert "871,095" in snapshot and "950,000" in snapshot and "62.0%" in snapshot
    assert eh.SNAPSHOT_NOTE in snapshot and "Direct YoY calculation not applied" in snapshot
    assert "78,905" not in snapshot and "+78,905" not in snapshot
    assert "pp" not in snapshot


def test_status_composition_is_present_and_exact(tmp_path, monkeypatch):
    _quiet(monkeypatch, tmp_path)
    page = executive_part(report_for(tmp_path))
    assert eh.COMPOSITION_TITLE in page
    chart = eh.status_composition(report_for(tmp_path))
    for count in ("871,095", "529,245", "596,598", "11,322"):
        assert count in chart
    assert "estimate" not in chart.lower()
    assert "CE1" not in chart


def test_historical_evolution_renders_from_existing_model_rows(tmp_path):
    model = live_model(tmp_path)
    model["py2026"]["report"]["dataset_snapshot_population"] = _snapshots()
    report = ex.build_report(model)
    series = report["py2026_status_evolution"]
    assert series["available"] is True
    may = next(point for point in series["points"] if point["date"] == "2025-05-31")
    assert all(may[label] is None for label in ex.EVOLUTION_LABELS)
    chart = eh.status_evolution_chart(report)
    assert eh.EVOLUTION_TITLE in chart and eh.EVOLUTION_SUBTITLE in chart
    assert "May 31" in chart
    assert chart.count("<circle") == 8  # two real dates x four statuses; the zero date draws none
    assert "PY2026 vs PY2027" not in chart


def test_evolution_reads_existing_artifact_and_hides_when_absent(tmp_path, monkeypatch):
    _quiet(monkeypatch, tmp_path)
    assert eh.EVOLUTION_TITLE not in executive_part(report_for(tmp_path))
    artifact = tmp_path / "population.json"
    artifact.write_text(json.dumps({"dataset_snapshot_population": _snapshots()[1:]}), encoding="utf-8")
    monkeypatch.setattr(ex, "MONTHLY_POPULATION_REPORT", artifact)
    report = report_for(tmp_path)
    assert report["py2026_status_evolution"]["available"] is True
    assert eh.EVOLUTION_TITLE in eh.status_evolution_chart(report)


def test_created_confirmed_mix_funnel_trend_and_policy_remain(tmp_path, monkeypatch):
    _quiet(monkeypatch, tmp_path)
    page = executive_part(report_for(tmp_path))
    for title in PHASE_8C_SECTIONS[:-1]:
        assert title in page
    activity = page.split(eh.ACTIVITY_TITLE, 1)[1].split(eh.NAP_TITLE, 1)[0]
    assert ">Created<" in activity and ">Confirmed<" in activity
    assert "Peak Created Month" in activity and "Peak Confirmed Month" in activity
    assert "Largest Created–Confirmed gap" in activity
    mix = eh.nap_chart(report_for(tmp_path))
    assert eh.NAP_FOOTNOTE in mix and "New · " in mix and "Created" not in mix.split("<svg", 1)[0]
    funnel = eh.funnel_chart(report_for(tmp_path))
    assert "Submitted share" in funnel and eh.APPLICANT_REFERENCE in funnel
    trend = eh.population_trend_chart(report_for(tmp_path))
    assert eh.OBSERVED_CHANGE in trend and "new enrollment" not in trend.lower()
    assert "+150,000" in trend
    policy = eh.daily_chart(report_for(tmp_path))
    assert eh.DAILY_POLICY_TITLE in policy and "Cancelled Latest Day" in policy
    assert "Cancelled Period Total" in policy and "8,258" in policy
    assert "Terminated Latest Day" not in policy and "Terminated Period Total" not in policy


def test_one_snapshot_does_not_calculate_population_change():
    strip = eh._population_strip({"categories": ["2026-11-09"], "py2027": [950000], "calendar": True})
    assert "First Observed" in strip and eh.OBSERVED_CHANGE not in strip


def test_unavailable_policy_value_is_not_zero():
    item = {"label": "Terminated", "calendar": True, "categories": ["2026-11-01", "2026-11-02"],
            "py2027": [None, None], "selector_enabled": True}
    assert eh._policy_strip([("terminated", item)]) == ""


def test_ce_scenarios_stay_in_technical_details(tmp_path, monkeypatch):
    _quiet(monkeypatch, tmp_path)
    report = report_for(tmp_path)
    page = ex.render_html(report)
    management = executive_part(report)
    for cid in ("CE1", "CE2", "CE3", "CE4"):
        assert cid not in management
    assert "CE1–CE4" in page.split(f"<details id='{eh.TECHNICAL_ID}'", 1)[1]


def test_no_phase_8c_management_section_removed(tmp_path, monkeypatch):
    _quiet(monkeypatch, tmp_path)
    page = ex.render_html(report_for(tmp_path))
    for title in PHASE_8C_SECTIONS:
        assert f">{title}<" in page


def test_no_acquisition_or_database_work_added():
    html_src = (PROJECT / "comparison" / "executive_html.py").read_text(encoding="utf-8")
    runner = (PROJECT / "comparison" / "live_executive_runner.py").read_text(encoding="utf-8")
    report_src = (PROJECT / "comparison" / "executive_py2026_py2027_report.py").read_text(encoding="utf-8")
    helper = report_src.split("def existing_status_snapshots", 1)[1].split("\ndef ", 1)[0]
    for marker in ("retrieve_live", "run_pipeline", "playwright", "request.post", "pyodbc"):
        assert marker not in html_src
    assert "status_evolution" not in runner
    assert "SELECT" not in helper and "connect(" not in helper
