"""Phase 3F-D: production HTML semantic cleanup (fakes only, network blocked, invented values)."""

import re
import tempfile
import unittest
from datetime import timedelta
from html import escape
from pathlib import Path

from analytics.derived import DERIVED, SNAPSHOT, SOURCE
from metrics.catalog import (
    CURRENTLY_ENROLLED,
    METRICS,
    MODULES,
    PERCENT_EFFECTUATED,
    ROADMAP,
    SECTION_ENROLLMENT_STATUS,
    SECTION_ENROLLMENT_STATUS_CHANGES,
    SECTION_PLAN_DISTRIBUTION,
    roadmap_for,
)
from reporting.html_report import LATEST_REPORT, render_html, write_report
from tests.reporting_fixtures import series
from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_pipeline_main import RUN_TIME
from tests.test_report_consistency import VALIDATION_ONLY, visible
from tests.test_reporting import live_model
from tests.test_runtime_validation import natural_wave1
from tests.test_session_context import FakeResponse

DEVELOPER_TERMS = (
    "VERIFIED", "Verified", "DISCOVERED", "VALUE_EXTRACTED", "NOT_AVAILABLE", "VALIDATION",
    "fingerprint", "sha256", "ELIGIBLE", "Observation", "widget_id", "JAQL", "queryGuid",
)
ENROLLMENT_HEADINGS = (
    "Current Enrollment",
    "Enrollment Metrics",
    "Enrollment Status",
    "Enrollment Status Changes",
    "Plan Distribution",
    "Currently Enrolled — History &amp; Trends",
    "Enrollment Trend",
    "Metric Definitions",
)
SIDEBAR = (
    "Executive Overview", "Enrollment", "Applications", "Issuers", "EDE", "Financial Assistance",
    "Operations", "DMI &amp; Notices", "Contact Center", "Agent / Broker",
)


def enrollment_view(html: str) -> str:
    return html.split('id="view-enrollment"')[1].split('id="view-applications"')[0]


def applications_view(html: str) -> str:
    return html.split('id="view-applications"')[1].split('id="view-issuers"')[0]


def section(html: str, section_id: str) -> str:
    match = re.search(rf'data-section="{re.escape(section_id)}".*?</section>', html, re.S)
    if match is None:
        raise AssertionError(f"missing section {section_id}")
    return match.group(0)


def history_table(html: str) -> str:
    return section(html, "currently_enrolled_history")


class SnapshotSemanticTests(unittest.TestCase):
    def test_source_derived_snapshot_legend_and_latest_snapshot_time(self):
        html = render_html(live_model(series((1, 611_000), (0, 612_000))))
        text = visible(html)
        self.assertIn("SOURCE", text)
        self.assertIn("values come from validated Sisense retrievals", text)
        self.assertIn("DERIVED", text)
        self.assertIn("calculated from stored production snapshot history", text)
        self.assertIn("SNAPSHOT", text)
        self.assertIn("metadata associated with a stored validated observation", text)
        table = history_table(html)
        self.assertIn("Latest Snapshot Time", table)
        self.assertIn('class="badge badge-snapshot">SNAPSHOT</span>', table)
        self.assertRegex(table, r"Historical Minimum</td><td class=\"num\">[^<]+</td><td>.*badge-derived")
        self.assertNotIn('badge-derived">SNAPSHOT', table)
        self.assertIn('class="badge badge-source">SOURCE</span>', table)

    def test_history_table_is_currently_enrolled_only(self):
        html = render_html(live_model(series((1, 611_000), (0, 612_000))))
        table = history_table(html)
        self.assertIn("Currently Enrolled — History &amp; Trends", table)
        self.assertIn("Currently Enrolled People", table)
        self.assertIn("Historical Minimum", table)
        self.assertIn("Historical Maximum", table)
        self.assertIn("Average Daily Change", table)
        self.assertIn("Previous Snapshot Change", table)
        self.assertIn("Daily Change", table)
        self.assertIn("7-Day Change", table)
        self.assertIn("30-Day Change", table)
        self.assertNotIn("% Effectuated", table)
        self.assertNotIn("Enrollment Movement", html)
        self.assertNotIn("Source &amp; Derived Values", html)

    def test_production_report_path_unchanged(self):
        self.assertEqual(LATEST_REPORT, Path("latest") / "index.html")
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(live_model(series((0, 612_000))), Path(tmp))
            self.assertEqual(path, Path(tmp) / "latest" / "index.html")


class EnrollmentTaxonomyTests(PassiveMetricsHarness):
    def full_run(self, ce=611_000, pct=0.812345, **kw):
        wave = natural_wave1({"ce": ce, **VALIDATION_ONLY})
        wave[0] = self.ce_widget(ce=ce, pct=pct)
        return self.run_pipeline(ce, *wave, discovery=True, runtime_validation=True, **kw)

    def test_enrollment_page_section_order(self):
        self.full_run()
        page = enrollment_view(self.report_html())
        positions = [page.index(f"<h2>{heading}</h2>") for heading in ENROLLMENT_HEADINGS]
        self.assertEqual(positions, sorted(positions))

    def test_new_active_passive_under_enrollment_status(self):
        self.full_run()
        block = section(self.report_html(), SECTION_ENROLLMENT_STATUS)
        for metric_id, name in (("new_enrolled", "New"), ("active_enrolled", "Active"),
                                ("passive_enrolled", "Passive")):
            self.assertIn(f'data-metric="{metric_id}"', block)
            self.assertIn(name.upper() if name != "New" else "NEW", self.card(metric_id))
            self.assertNotIn("PENDING", self.card(metric_id))
        self.assertNotIn("Cancelled", block)
        self.assertNotIn("Terminated", block)
        self.assertNotIn("Metal Tier", block)

    def test_cancelled_terminated_under_status_changes(self):
        self.full_run()
        block = section(self.report_html(), SECTION_ENROLLMENT_STATUS_CHANGES)
        self.assertIn("Enrollment Status Changes", block)
        self.assertIn("Cancelled", block)
        self.assertIn("Terminated", block)
        self.assertNotIn("New", block)
        self.assertNotIn("Metal Tier", block)
        self.assertIn("not the historical change in Currently Enrolled", block)
        self.assertNotIn("badge-source", block)

    def test_metal_tier_under_plan_distribution(self):
        self.full_run()
        block = section(self.report_html(), SECTION_PLAN_DISTRIBUTION)
        self.assertIn("Metal Tier", block)
        self.assertNotIn("Cancelled", block)
        self.assertNotIn("badge-source", block)

    def test_applications_order_started_submitted_total(self):
        self.full_run()
        names = [item.name for item in roadmap_for("applications")]
        self.assertEqual(names, ["Total Applicants"])
        page = applications_view(self.report_html())
        self.assertLess(page.index('data-metric="applications_started"'),
                        page.index('data-metric="submitted_applications"'))
        self.assertLess(page.index('data-metric="submitted_applications"'), page.index("Total Applicants"))

    def test_percent_effectuated_definition_is_independent(self):
        self.full_run()
        html = self.report_html()
        ce_def = section(html, "metric_definitions")
        pct = re.search(r'data-definition="percent_effectuated".*?</article>', ce_def, re.S).group(0)
        ce = re.search(r'data-definition="currently_enrolled_people".*?</article>', ce_def, re.S).group(0)
        self.assertIn("% Effectuated", pct)
        self.assertIn(PERCENT_EFFECTUATED.category, pct)
        self.assertIn(PERCENT_EFFECTUATED.unit, pct)
        self.assertIn(PERCENT_EFFECTUATED.source, pct)
        self.assertIn(escape(PERCENT_EFFECTUATED.description), pct)
        self.assertNotIn(CURRENTLY_ENROLLED.description, pct)
        self.assertIn(escape(CURRENTLY_ENROLLED.description), ce)
        self.assertNotIn(PERCENT_EFFECTUATED.description, ce)

    def test_metric_definitions_are_catalog_driven(self):
        self.full_run()
        defs = section(self.report_html(), "metric_definitions")
        self.assertEqual(list(METRICS)[:2], ["currently_enrolled_people", "percent_effectuated"])
        for metric in (m for m in METRICS.values() if m.module_id == "enrollment"):
            block = re.search(rf'data-definition="{metric.metric_id}".*?</article>', defs, re.S).group(0)
            self.assertIn(metric.display_name, block)
            self.assertIn(metric.category, block)
            self.assertIn(metric.unit, block)
            self.assertIn(metric.source, block)
            self.assertIn(escape(metric.description), block)
            self.assertIn("SOURCE", block)
            self.assertNotIn("PENDING", block)

    def test_validation_values_absent_from_production_html(self):
        self.full_run()
        executive = visible(self.report_html())
        for value in VALIDATION_ONLY.values():
            self.assertNotIn(f"{value:,}", executive)
        self.assertNotIn("400,101", executive)
        self.assertTrue((self.reports / "latest" / "index.html").exists())
        self.assertTrue((self.reports / "validation" / "wave1_runtime_validation.html").exists())

    def test_production_metrics_never_pending_and_pending_never_source(self):
        self.full_run()
        html = self.report_html()
        for card in re.findall(r'<section class="card kpi">(.*?)</section>', html, re.S):
            self.assertNotIn("PENDING", card)
            self.assertIn("SOURCE", card)
        self.assertNotIn("PENDING", self.card("percent_effectuated"))
        self.assertIn("SOURCE", self.card("percent_effectuated"))
        self.assertNotIn("PENDING", section(html, SECTION_ENROLLMENT_STATUS))
        for section_id in (SECTION_ENROLLMENT_STATUS_CHANGES, SECTION_PLAN_DISTRIBUTION):
            block = section(html, section_id)
            self.assertIn("PENDING", block)
            self.assertNotIn("badge-source", block)
            self.assertNotIn("LIVE", block)
        apps = applications_view(html)
        self.assertIn("PENDING", apps)
        self.assertIn("badge-source", apps)

    def test_percent_effectuated_history_stays_on_its_card(self):
        self.run_pipeline(611_000, self.ce_widget(ce=611_000, pct=0.800000), at=RUN_TIME - timedelta(hours=3))
        self.run_pipeline(612_000, self.ce_widget(ce=612_000, pct=0.812345))
        html = self.report_html()
        table = history_table(html)
        self.assertIn("612,000", table)
        self.assertNotIn("% Effectuated", table)
        self.assertNotIn("81.23%", table)
        self.assertNotIn("+1.23 pp", table)
        self.assertIn("81.23%", self.card("percent_effectuated"))
        self.assertIn("+1.23 pp", self.card("percent_effectuated"))

    def test_sidebar_taxonomy_unchanged(self):
        self.full_run()
        nav = self.report_html().split('<nav class="sidebar"')[1].split("</nav>")[0]
        titles = re.findall(r'data-view="[^"]+">([^<]+)', nav)
        self.assertEqual(titles, list(SIDEBAR))
        self.assertEqual([m.title.replace("&", "&amp;") if "&" in m.title else m.title for m in MODULES],
                         list(SIDEBAR))

    def test_developer_terminology_absent_from_production_html(self):
        self.full_run()
        text = visible(self.report_html())
        for term in DEVELOPER_TERMS:
            self.assertNotIn(term, text)
        self.assertNotIn("Enrollment Movement", text)
        self.assertNotIn("Additional Enrollment Metrics", text)

    def test_production_catalog_unchanged(self):
        self.assertEqual(list(METRICS)[:2], ["currently_enrolled_people", "percent_effectuated"])
        self.assertEqual(CURRENTLY_ENROLLED.status, "active")
        self.assertEqual(PERCENT_EFFECTUATED.status, "active")
        pending_names = {item.name for item in ROADMAP}
        for name in ("Total Applicants", "Cancelled", "Terminated", "Metal Tier"):
            self.assertIn(name, pending_names)
        for name in ("Applications Started", "Submitted Applications", "New", "Active", "Passive"):
            self.assertNotIn(name, pending_names)

    def test_missing_live_value_does_not_invent_pending_or_source_for_roadmap(self):
        self.run_pipeline(None, responses=[FakeResponse(401, "{}")])
        html = self.report_html()
        kpi = re.search(r'<section class="card kpi">(.*?)</section>', html, re.S).group(1)
        self.assertIn("NOT AVAILABLE", kpi)
        self.assertNotIn("PENDING", kpi)
        self.assertNotIn("PENDING", section(html, SECTION_ENROLLMENT_STATUS))
        self.assertIn("badge-source", section(html, SECTION_ENROLLMENT_STATUS))
