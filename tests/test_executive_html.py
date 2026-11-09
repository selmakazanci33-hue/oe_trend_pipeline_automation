"""Phase 7D: management-facing HTML, plan-year monthly / daily comparison models."""

import json
import re

from comparison import executive_html as eh
from comparison import executive_py2026_py2027_report as ex
from tests.test_executive_py2026_py2027_report import LIVE_AT, LIVE_VALUES, OLD_AT, live_model, report_for

RAW_ENUMS = ("STALE_FALLBACK", "LIVE_THIS_RUN", "HISTORICAL_SNAPSHOT", "PY2027_ONLY", "PY2026_ONLY",
             "VALIDATION_ONLY", "NOT_COMPARABLE", "OE_LIFECYCLE_ALIGNMENT_DIAGNOSTIC", "DIAGNOSTIC", "UNAVAILABLE",
             "NO_ACTIVITY_BRIDGE", "OTHER_LIFECYCLE_CLASS", "CE1", "CE2", "CE3", "CE4")
ENGINEERING_PHRASES = ("duplicate", "dedup", "existing slot", "production_comparable", "semantic bridge",
                       "identifier proxy", "source-series store", "source_series", "validation state", "artifact",
                       "phase 6", "parser", "fallback", "stale", "comparable mtd series unavailable",
                       "not selected as official", "not a proven partition")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def executive_part(report) -> str:
    page = ex.render_html(report)
    return page.split(f"<details id='{eh.TECHNICAL_ID}'", 1)[0]


def visible(fragment: str) -> str:
    fragment = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", fragment, flags=re.S)
    return re.sub(r"<[^>]+>", " ", fragment)


# --- terminology -----------------------------------------------------------------------------

def test_stale_fallback_renders_as_last_validated(tmp_path):
    values = {k: v for k, v in LIVE_VALUES.items() if k != "applications_started"}
    report = report_for(tmp_path, values=values)
    text = visible(executive_part(report))
    assert "Last validated · Nov 1, 12:00 PM UTC" in text
    assert "STALE_FALLBACK" not in text and "stale" not in text.lower()


def test_live_renders_as_current(tmp_path):
    text = visible(executive_part(report_for(tmp_path)))
    assert "Current" in text and "LIVE_THIS_RUN" not in text


def test_no_engineering_terms_in_executive_html(tmp_path):
    for index, kwargs in enumerate(({}, {"live_ok": False}, {"db_live": False})):
        folder = tmp_path / f"case{index}"
        folder.mkdir()
        text = visible(executive_part(report_for(folder, **kwargs)))
        for term in RAW_ENUMS:
            assert term not in text, term
        for phrase in ENGINEERING_PHRASES:
            assert phrase not in text.lower(), phrase


def test_duplicate_slot_language_never_in_executive_html(tmp_path):
    report = report_for(tmp_path)
    text = executive_part(report).lower()
    for term in ("duplicate", "dedup", "existing slot", "skipped", "suppressed"):
        assert term not in text


def test_internal_enums_remain_in_json_and_technical_details(tmp_path):
    report = report_for(tmp_path, live_ok=False)
    assert ex.STALE in json.dumps(report)
    page = ex.render_html(report)
    technical = page.split(f"<details id='{eh.TECHNICAL_ID}'", 1)[1]
    assert "STALE_FALLBACK" in technical and "production_comparable" in technical and "CE1" in technical
    assert "<summary>Technical details</summary>" in page
    assert page.count(f"<details id='{eh.TECHNICAL_ID}'") == 1


def test_old_last_validated_warns_only_beyond_threshold():
    item = {"freshness": ex.STALE, "retrieved_at": "2026-11-09T11:22:00+00:00"}
    assert "old" not in eh.freshness_note(item, "2026-11-09T12:16:00+00:00", 24)
    assert "Last validated · Nov 9, 11:22 AM UTC" in eh.freshness_note(item, "2026-11-09T12:16:00+00:00", 24)
    assert "fresh old" in eh.freshness_note(item, "2026-11-11T12:16:00+00:00", 24)


def test_header_is_simple_and_iso_free(tmp_path):
    page = ex.render_html(report_for(tmp_path))
    head = page.split("<main>", 1)[0].split("<body>", 1)[1]
    text = visible(head)
    assert "Enrollment Dashboard" in text
    assert "Report as of: Nov 9, 2026 · 12:00 PM UTC" in text
    assert "PY2026 Database · Current" in " ".join(text.split())
    assert "PY2027 Dashboard · Current" in " ".join(text.split())
    assert not ISO.search(text)


# --- monthly -------------------------------------------------------------------------------

def test_monthly_supports_two_plan_year_series(tmp_path, monkeypatch):
    monthly = report_for(tmp_path)["monthly_yoy"]
    assert monthly["months"][:4] == ["Oct", "Nov", "Dec", "Jan"]
    assert monthly["py2026_months"][0] == "2025-10" and monthly["py2027_months"][0] == "2026-10"
    created = monthly["metrics"]["created"]
    assert created["py2026"][0] == 900000 and created["py2026"][1] == 100000
    assert created["mode"] == ex.MODE_PY2026 and created["rows"][0]["comparable"] is False
    monkeypatch.setattr(ex, "COMPATIBLE_PY2027_ACTIVITY_METRICS", frozenset({"created"}))
    (tmp_path / "pair").mkdir()
    model = live_model(tmp_path / "pair")
    model["py2027"]["activity_series"] = {"created": {"points": []}}
    model["py2027"]["activity_monthly"] = {"created": {"2026-10": {"value": 880000}, "2026-11": {"value": 120000}}}
    report = ex.build_report(model)
    combined = report["monthly_yoy"]["metrics"]["created"]
    assert combined["mode"] == ex.MODE_YOY and combined["yoy_enabled"] is True
    assert combined["py2026"][0] == 900000 and combined["py2027"][0] == 880000
    assert combined["rows"][0]["comparable"] is True and combined["rows"][1]["comparable"] is False
    chart = eh.monthly_chart(report)
    panel = [p for p in chart.split("<div class='panel'") if "data-metric='created'" in p][0]
    assert "Monthly Enrollment Created — PY2026 vs PY2027" in panel
    assert panel.count("<svg") == 1 and ">PY2026<" in panel and ">PY2027<" in panel
    assert "data-yoy='1'" in panel


def test_future_py2027_months_are_null_never_zero(tmp_path):
    monthly = report_for(tmp_path)["monthly_yoy"]
    for item in monthly["metrics"].values():
        assert item["py2027"][2:] == [None] * 10
    enrolled = monthly["metrics"]["enrolled"]
    assert enrolled["mode"] == ex.MODE_PY2027
    assert enrolled["py2027"][0] == 800000 and enrolled["py2027"][1] == 950000
    assert enrolled["py2027_period"][:3] == [ex.FULL_MONTH, ex.MONTH_TO_DATE, None]


def test_partial_month_never_compared_to_full_month(tmp_path):
    model = live_model(tmp_path)
    model["py2026"]["report"]["historical_population_series"] = [{"date": "2025-11-30", "distinct_people": 760000},
                                                                  {"date": "2025-10-31", "distinct_people": 700000}]
    model["py2026"]["enrolled_history_comparable"] = True
    enrolled = ex.build_report(model)["monthly_yoy"]["metrics"]["enrolled"]
    nov = enrolled["rows"][1]
    assert nov["py2027_period_type"] == ex.MONTH_TO_DATE and nov["comparable"] is False
    assert nov["absolute_difference"] is None
    assert nov["percent_difference"] is None
    oct_ = enrolled["rows"][0]
    assert oct_["py2027_period_type"] == ex.FULL_MONTH and oct_["comparable"] is True
    assert oct_["absolute_difference"] == 100000
    current = ex.build_report(model)["monthly_yoy"]["current_month"]
    assert current["message"] == ex.SAME_PERIOD_PENDING and current["same_period"] is None
    chart = eh.monthly_chart(ex.build_report(model))
    assert "PY2026 Full Month" in chart and "Same-period comparison not yet available" in chart
    assert "Comparable MTD series unavailable" not in chart


def test_new_never_maps_to_created(tmp_path):
    model = live_model(tmp_path)
    model["py2027"]["activity_series"] = {ex.NEW: {"points": []}}
    model["py2027"]["activity_monthly"] = {"created": {"2026-11": {"value": 4525}}}
    created = ex.build_report(model)["monthly_yoy"]["metrics"]["created"]
    assert created["py2027"] == [None] * 12 and created["mode"] == ex.MODE_PY2026
    assert ex.NEW not in ex.COMPATIBLE_PY2027_ACTIVITY_METRICS


def test_selector_disables_unsupported_comparisons(tmp_path):
    report = report_for(tmp_path)
    metrics = report["monthly_yoy"]["metrics"]
    assert metrics["cancelled"]["selector_enabled"] is False and metrics["terminated"]["selector_enabled"] is False
    assert metrics["created"]["yoy_enabled"] is False and metrics["confirmed"]["yoy_enabled"] is False
    chart = eh.monthly_chart(report)
    for label in ("Cancelled", "Terminated"):
        assert re.search(rf"<button type='button' disabled title='{ex.SELECTOR_DISABLED_TIP}' "
                         rf"data-metric='{label.lower()}'>{label}</button>", chart)
    assert "data-metric='created'" in chart and "class='sel on' data-metric='created'" in chart
    assert "<script src" not in ex.render_html(report)


def test_incompatible_cancellation_never_on_same_yoy_axis(tmp_path):
    report = report_for(tmp_path)
    for key in ("cancelled", "terminated"):
        monthly = report["monthly_yoy"]["metrics"][key]
        assert all(v is None for v in monthly["py2026"]) and not monthly["yoy_enabled"]
        daily = report["daily_yoy"]["metrics"][key]
        assert all(v is None for v in daily["py2026"]) and not daily["yoy_enabled"]
    cancelled = report["daily_yoy"]["metrics"]["cancelled"]
    assert cancelled["mode"] == ex.MODE_PY2027 and cancelled["py2027"] == [2534, 5724]
    assert report["daily_yoy"]["metrics"]["terminated"]["mode"] == ex.MODE_DISABLED


# --- daily ---------------------------------------------------------------------------------

def test_daily_supports_two_series_when_compatible(tmp_path):
    model = live_model(tmp_path)
    model["py2026"]["enrolled_history_comparable"] = True
    model["py2026"]["report"]["historical_population_series"] = [{"date": "2025-11-09", "distinct_people": 760000},
                                                                  {"date": "2025-11-01", "distinct_people": 700000}]
    report = ex.build_report(model)
    enrolled = report["daily_yoy"]["metrics"]["enrolled"]
    assert enrolled["mode"] == ex.MODE_YOY
    assert enrolled["categories"] == ["2026-10-30", "2026-11-01", "2026-11-09"]
    assert enrolled["py2026"] == [None, 700000, 760000]
    chart = eh.population_trend_chart(report)
    assert chart.count("<svg") == 1 and ">PY2026<" in chart and ">PY2027<" in chart
    assert "PY2026 vs PY2027" not in chart
    assert "<polyline" in chart
    assert eh.POPULATION_TREND_SUBTITLE in chart
    assert "daily enrollment" not in chart.lower()


def test_daily_py2027_only_shows_plain_note(tmp_path):
    report = report_for(tmp_path)
    chart = eh.population_trend_chart(report)
    assert eh.POPULATION_TREND_NOTE in chart and ">PY2026<" not in chart
    assert eh.POPULATION_TREND_TITLE in chart
    assert "PY2026 vs PY2027" not in chart
    assert "Nov 01" in chart
    assert "daily enrollment" not in executive_part(report).lower()
    policy = eh.daily_chart(report)
    assert eh.DAILY_POLICY_TITLE in policy and "Cancelled" in policy
    assert "data-metric='enrolled'" not in policy


# --- sections ------------------------------------------------------------------------------

def test_ce_section_is_simplified(tmp_path):
    chart = eh.ce_chart(report_for(tmp_path))
    assert ex.CE_ALIGNMENT_SUBTITLE in chart and "PY2026 database scenarios" in chart
    assert "PY2027 dashboard" in chart and "865,946 → 1,000,000" in chart
    assert "Exact database count" in chart and "Dashboard count" in chart
    assert eh.CE_FOOTNOTE in chart and "CE1" not in chart
    for phrase in ("high estimate", "low estimate", "estimated diagnostic range"):
        assert phrase not in chart.lower()


def test_funnel_uses_friendly_freshness(tmp_path):
    values = {k: v for k, v in LIVE_VALUES.items() if k not in ("applications_started", "submitted_applications")}
    chart = eh.funnel_chart(report_for(tmp_path, values=values))
    assert chart.count("Last validated · Nov 1, 12:00 PM UTC") == 2
    assert eh.APPLICANT_REFERENCE in chart and "VALIDATION_ONLY" not in chart


def test_status_population_groups_other_states(tmp_path):
    model = live_model(tmp_path)
    model["py2026"]["report"]["status_distributions"]["enrollment_status"].append(
        {"status_value": "Pend canceled", "row_count": 1, "distinct_people": 12, "distinct_policies": 1})
    chart = eh.status_population(ex.build_report(model))
    assert "Other / pending states: Pend canceled 12" in chart
    assert ">Pend canceled<" not in chart and ">Enrolled<" in chart


def test_nap_mix_footnote(tmp_path):
    chart = eh.nap_chart(report_for(tmp_path))
    assert eh.NAP_TITLE in chart and eh.NAP_FOOTNOTE in chart


def test_population_snapshots_are_not_called_daily_enrollment(tmp_path):
    page = executive_part(report_for(tmp_path))
    assert eh.POPULATION_TREND_TITLE in page and eh.POPULATION_TREND_SUBTITLE in page
    assert eh.DAILY_POLICY_TITLE in page
    assert ">Daily Enrollment<" not in page
    assert "daily enrolled activity" not in page.lower()
    assert "daily enrollment activity" not in page.lower()
    assert eh.POPULATION_TREND_NOTE in page


def test_management_table_uses_business_labels(tmp_path):
    table = eh.management_table(report_for(tmp_path))
    assert "<th>Comparison</th>" in table and "Freshness" not in table
    assert "Definition alignment in progress" in table and ">PY2027 only<" in table
    assert table.count(">—<") >= 2
    for raw in ("DIAGNOSTIC", "PY2027_ONLY", "LIVE_THIS_RUN", "CE1"):
        assert raw not in table


def test_no_auth_or_pii_leakage(tmp_path):
    json_path, html_path = ex.write_report(report_for(tmp_path), tmp_path / "out")
    text = (json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")).lower()
    for token in ('"enrollee_id"', '"enrollment_id"', "cookie", "authorization", "bearer", "x-xsrf", "password",
                  "<script src", "http://", "https://"):
        assert token not in text
