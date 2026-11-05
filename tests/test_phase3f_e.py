"""Phase 3F-E: cancellation taxonomy correction (fakes only, network blocked, invented values)."""

from pathlib import Path

from metrics.catalog import (
    METRICS,
    PROMOTION_CANDIDATES,
    ROADMAP,
    SECTION_CANCELLATION_ACTIVITY,
    SECTION_CANCELLATION_OVERVIEW,
    SECTION_ENROLLMENT_STATUS,
    SECTION_PLAN_DISTRIBUTION,
)
from reporting.html_report import LATEST_REPORT
from reporting.series_semantics import (
    CANCELLED_POLICIES_DAILY,
    CURRENTLY_ENROLLED_SNAPSHOT_TREND,
    TERMINATED_POLICIES_DAILY,
    SeriesIdentity,
    series_equivalent,
)
from tests.test_phase3f_d import applications_view, enrollment_view, section
from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_report_consistency import VALIDATION_ONLY, visible

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_SOURCES = (
    "metrics/catalog.py",
    "reporting/html_report.py",
    "reporting/series_semantics.py",
)
# Invented / previously observed numbers that must never appear as cancellation values.
FORBIDDEN_VALUES = (
    "566051", "566,051", "565200", "565,200", "2510", "2,510", "35266", "35,266",
    "756009", "756,009", "77001", "77,001", "88002", "88,002",
)
CANCELLATION_PENDING = (
    "Enrolled & Cancelled",
    "Cancelled — Not Enrolled",
    "Cancelled Policies — Daily",
    "Terminated Policies — Daily",
)


class CancellationTaxonomyTests(PassiveMetricsHarness):
    def test_generic_cancelled_and_terminated_roadmap_presentation_is_gone(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        chips = self.pending_chips()
        self.assertNotIn("Cancelled", chips)
        self.assertNotIn("Terminated", chips)
        names = [item.name for item in ROADMAP]
        self.assertNotIn("Cancelled", names)
        self.assertNotIn("Terminated", names)
        html = self.report_html()
        self.assertNotIn("Enrollment Status Changes", html)
        self.assertNotIn("<span>Cancelled</span>", html)
        self.assertNotIn("<span>Terminated</span>", html)

    def test_cancellation_overview_pending_concepts(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        block = section(self.report_html(), SECTION_CANCELLATION_OVERVIEW)
        self.assertIn("Cancellation Overview", block)
        self.assertIn("Enrolled &amp; Cancelled", block)
        self.assertIn("Cancelled — Not Enrolled", block)
        self.assertNotIn("Cancelled Policies", block)
        self.assertNotIn("Terminated Policies", block)
        self.assertNotIn("Metal Tier", block)
        self.assertNotIn('data-metric="new_enrolled"', block)
        self.assertIn("PENDING", block)
        self.assertNotIn("badge-source", block)
        self.assertNotIn("LIVE", block)
        self.assertNotIn("STALE", block)
        self.assertNotIn("metric-value", block)

    def test_cancellation_activity_pending_daily_series(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        block = section(self.report_html(), SECTION_CANCELLATION_ACTIVITY)
        self.assertIn("Cancellation Activity", block)
        self.assertIn("Cancelled Policies — Daily", block)
        self.assertIn("Terminated Policies — Daily", block)
        self.assertNotIn("Enrolled &amp; Cancelled", block)
        self.assertNotIn("Metal Tier", block)
        self.assertIn("PENDING", block)
        self.assertNotIn("badge-source", block)
        self.assertNotIn("LIVE", block)
        self.assertNotIn("STALE", block)
        self.assertNotIn("metric-value", block)
        self.assertNotIn('class="chart"', block)
        self.assertNotIn('id="trend-chart', block)

    def test_all_four_remain_pending_and_unpromoted(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        chips = self.pending_chips()
        for name in CANCELLATION_PENDING:
            self.assertIn(name, chips)
            self.assertNotIn(name, [m.display_name for m in METRICS.values()])
        self.assertEqual(set(PROMOTION_CANDIDATES), {"total_applicants"})
        self.assertNotIn("cancelled", METRICS)
        self.assertNotIn("terminated", METRICS)
        self.assertNotIn("enrolled_cancelled", METRICS)
        self.assertNotIn("cancelled_not_enrolled", METRICS)

    def test_enrollment_status_and_plan_distribution_unchanged(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        html = self.report_html()
        status = section(html, SECTION_ENROLLMENT_STATUS)
        self.assertIn('data-metric="new_enrolled"', status)
        self.assertIn('data-metric="active_enrolled"', status)
        self.assertIn('data-metric="passive_enrolled"', status)
        self.assertNotIn("PENDING", status)
        self.assertNotIn("Enrolled &amp; Cancelled", status)
        self.assertNotIn("Cancelled Policies", status)
        plan = section(html, SECTION_PLAN_DISTRIBUTION)
        self.assertIn("Metal Tier", plan)
        self.assertIn("PENDING", plan)
        self.assertNotIn("Enrolled &amp; Cancelled", plan)
        self.assertNotIn("Cancelled Policies", plan)

    def test_currently_enrolled_snapshot_trend_is_not_cancellation_activity(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        html = self.report_html()
        page = enrollment_view(html)
        self.assertIn("Currently Enrolled — Snapshot Trend", page)
        self.assertIn("Stored validated observations over time", page)
        self.assertNotIn("<h2>Enrollment Trend</h2>", page)
        trend = section(html, "enrollment_trend")
        self.assertIn("Currently Enrolled — Snapshot Trend", trend)
        self.assertEqual(CURRENTLY_ENROLLED_SNAPSHOT_TREND.label, "Currently Enrolled — Snapshot Trend")
        self.assertIn(f'data-series="{CURRENTLY_ENROLLED_SNAPSHOT_TREND.metric_id}"', trend)
        self.assertNotIn("Cancelled Policies", trend)
        activity = section(html, SECTION_CANCELLATION_ACTIVITY)
        self.assertNotIn("Currently Enrolled — Snapshot Trend", activity)
        self.assertFalse(series_equivalent(CURRENTLY_ENROLLED_SNAPSHOT_TREND, CANCELLED_POLICIES_DAILY))
        self.assertFalse(series_equivalent(CURRENTLY_ENROLLED_SNAPSHOT_TREND, TERMINATED_POLICIES_DAILY))
        self.assertFalse(series_equivalent(CANCELLED_POLICIES_DAILY, TERMINATED_POLICIES_DAILY))
        self.assertFalse(series_equivalent(CANCELLED_POLICIES_DAILY, CANCELLED_POLICIES_DAILY))
        self.assertFalse(series_equivalent(SeriesIdentity("A"), SeriesIdentity("B")))
        self.assertTrue(series_equivalent(CURRENTLY_ENROLLED_SNAPSHOT_TREND, CURRENTLY_ENROLLED_SNAPSHOT_TREND))

    def test_production_metrics_and_report_path_unchanged(self):
        self.assertEqual(list(METRICS), [
            "currently_enrolled_people", "percent_effectuated", "applications_started", "submitted_applications",
            "new_enrolled", "active_enrolled", "passive_enrolled",
        ])
        self.assertEqual(LATEST_REPORT, Path("latest") / "index.html")
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        self.assertTrue((self.reports / "latest" / "index.html").exists())
        apps = applications_view(self.report_html())
        self.assertIn('data-metric="applications_started"', apps)
        self.assertIn("Total Applicants", apps)

    def test_no_screenshot_or_validation_values_and_no_promotion(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.812345))
        html = visible(self.report_html())
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value, html)
        for value in VALIDATION_ONLY.values():
            self.assertNotIn(f"{value:,}", html)
        for relative in PRODUCTION_SOURCES:
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for value in FORBIDDEN_VALUES:
                self.assertNotIn(value, text, relative)
        semantics = (PROJECT_ROOT / "reporting/series_semantics.py").read_text(encoding="utf-8")
        self.assertNotIn("expected_fingerprint", semantics)
        self.assertNotIn("expected_definition", semantics)
        self.assertNotIn("6a98048c", semantics)
