"""Phase 3I: Enrollment Status Explorer (fakes only, network blocked, invented values)."""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from history.store import WRITE_DUPLICATE_SLOT, WRITE_SAVED, SnapshotStore
from metrics.cancellation_identification import identify_cancellation_sources
from metrics.catalog import (
    ACTIVE_ENROLLED,
    CANCELLATION_CANDIDATES,
    CURRENTLY_ENROLLED,
    METRICS,
    NEW_ENROLLED,
    PASSIVE_ENROLLED,
    PROMOTION_CANDIDATES,
    STATUS_ACTIVE,
)
from reporting.html_report import history_capture_label, render_html
from reporting.model import RunOutcome, build_metric_panel, build_report_model
from reporting.status_explorer import (
    EXPLORER_SCRIPT,
    EXPLORER_STATUSES,
    PENDING_ENROLLMENT,
    PENDING_SOURCE,
    VERIFIED_PARTITIONS,
    VERIFIED_STATUS_IDS,
    detail_lines,
    detail_records,
    distribution_figure,
    explorer_document,
    share_of_whole_allowed,
)
from sisense.discovery import DiscoveryResult
from tests.reporting_fixtures import T0, result, series, snapshot

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_IDS = (
    "currently_enrolled_people",
    "percent_effectuated",
    "applications_started",
    "submitted_applications",
    "new_enrolled",
    "active_enrolled",
    "passive_enrolled",
)
SELECTOR = ("ALL", "ENROLLED", "NEW", "ACTIVE", "PASSIVE", "CANCELLED", "TERMINATED", "PENDING")
NETWORK_MARKERS = ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "http://", "https://")
CHART_MARKERS = ("pie", "donut", "percent")
NEW_VALUE = 12_001
ACTIVE_VALUE = 34_002
PASSIVE_VALUE = 56_003
ENROLLED_VALUE = 78_004


def _panel(metric, value, prior=None):
    snaps = []
    if prior is not None:
        snaps.append(replace(snapshot(prior[1], T0 - timedelta(hours=prior[0]), id_=prior[0] + 40),
                             metric_id=metric.metric_id))
    return build_metric_panel(metric, snaps, result(value, metric=metric.metric_id), None, None)


def explorer_model(new=NEW_VALUE, active=ACTIVE_VALUE, passive=PASSIVE_VALUE, enrolled=ENROLLED_VALUE):
    snaps = series((3, enrolled - 100), (0, enrolled))
    panels = (
        _panel(NEW_ENROLLED, new, prior=(5, new - 50)),
        _panel(ACTIVE_ENROLLED, active),
        _panel(PASSIVE_ENROLLED, passive),
    )
    outcome = RunOutcome("run", result(enrolled), snapshot_write=None)
    return build_report_model(CURRENTLY_ENROLLED, snaps, outcome, generated_at=T0, panels=panels)


def explorer_source(html: str) -> str:
    marker = "/* enrollment-status-explorer */"
    start = html.index(marker)
    return html[start:html.index("</script>", start)]


def section(html: str, section_id: str) -> str:
    match = re.search(rf'data-section="{re.escape(section_id)}".*?</section>', html, re.S)
    if match is None:
        raise AssertionError(f"missing section {section_id}")
    return match.group(0)


class ExplorerPresentationTests(unittest.TestCase):
    def setUp(self):
        self.model = explorer_model()
        self.html = render_html(self.model)
        self.document = explorer_document(self.model)
        self.script = explorer_source(self.html)

    def test_selector_lists_every_status(self):
        labels = re.findall(r'data-status-key="[^"]+"[^>]*>([^<]+)</button>', self.html)
        self.assertEqual(tuple(labels), SELECTOR)
        self.assertEqual(tuple(status.label for status in EXPLORER_STATUSES), SELECTOR)
        self.assertIn('data-status-key="all" aria-pressed="true"', self.html)

    def test_verified_values_come_from_the_report_model(self):
        detail = self.html.split('id="status-detail"', 1)[1].split("</section>", 1)[0]
        for metric_id, value in (
            ("currently_enrolled_people", ENROLLED_VALUE),
            ("new_enrolled", NEW_VALUE),
            ("active_enrolled", ACTIVE_VALUE),
            ("passive_enrolled", PASSIVE_VALUE),
        ):
            shown = f"{value:,}"
            self.assertIn(shown, detail)
            self.assertEqual(self.document["metrics"][metric_id]["value"], value)
            self.assertIn(str(value), self.script)
        other = render_html(explorer_model(new=12_009))
        self.assertIn("12,009", other)
        self.assertNotIn("12,001", other.split('id="status-detail"', 1)[1].split("</section>", 1)[0])
        self.assertNotIn("12001", EXPLORER_SCRIPT)
        self.assertNotIn("12,001", EXPLORER_SCRIPT)
        self.assertNotIn("12001", (PROJECT_ROOT / "reporting/html_report.py").read_text(encoding="utf-8"))

    def test_unverified_statuses_have_no_count(self):
        for key, text in (
            ("cancelled", PENDING_SOURCE),
            ("terminated", PENDING_SOURCE),
            ("pending", PENDING_ENROLLMENT),
        ):
            status = next(item for item in self.document["statuses"] if item["key"] == key)
            self.assertEqual(status["metric_ids"], [])
            self.assertNotIn(key, self.document["metrics"])
        overview = section(self.html, "cancellation_overview")
        activity = section(self.html, "cancellation_activity")
        pending = self.html.split('data-explorer-panel="pending"', 1)[1].split("</div>", 1)[0]
        for block in (overview, activity, pending):
            self.assertNotRegex(block, r">\s*0\s*<")
            self.assertNotIn("metric-value", block)
        self.assertIn("Enrolled &amp; Cancelled", overview)
        self.assertIn("Cancelled — Not Enrolled", overview)
        self.assertIn(PENDING_SOURCE, overview)
        self.assertIn("Cancelled Policies — Daily", activity)
        self.assertIn("Terminated Policies — Daily", activity)
        self.assertIn(PENDING_SOURCE, activity)
        self.assertNotIn('class="chart"', activity)
        self.assertIn(PENDING_ENROLLMENT, pending)
        self.assertEqual(detail_records(self.document, "cancelled"), [])
        self.assertEqual(detail_records(self.document, "terminated"), [])
        self.assertEqual(detail_records(self.document, "pending"), [])

    def test_missing_value_is_not_zero(self):
        blank = explorer_model()
        document = explorer_document(blank)
        document["metrics"].pop("active_enrolled")
        record = next(item for item in detail_records(document, "active") if item["metric_id"] == "active_enrolled")
        self.assertIsNone(record["value"])
        self.assertEqual(dict(detail_lines(record))["Current value"], "Not available")
        figure = distribution_figure([
            {"metric_id": "new_enrolled", "display_name": "New", "value": NEW_VALUE},
            {"metric_id": "active_enrolled", "display_name": "Active", "value": None},
        ])
        self.assertEqual(figure["data"][0]["x"], [NEW_VALUE])
        self.assertNotIn(0, figure["data"][0]["x"])

    def test_selector_script_does_not_request_the_network(self):
        for marker in NETWORK_MARKERS:
            self.assertNotIn(marker, self.script)
        self.assertIn('addEventListener("click"', self.script)
        self.assertIn("preventDefault", self.script)
        self.assertIn('show("all")', self.script)
        tree = ast.parse((PROJECT_ROOT / "reporting/status_explorer.py").read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertTrue(modules.isdisjoint({"sisense", "requests", "httpx", "urllib", "socket", "playwright"}))

    def test_incompatible_metrics_stay_separate_counts(self):
        self.assertEqual(VERIFIED_PARTITIONS, frozenset())
        nap = ("new_enrolled", "active_enrolled", "passive_enrolled")
        combined = VERIFIED_STATUS_IDS
        self.assertFalse(share_of_whole_allowed(nap))
        self.assertFalse(share_of_whole_allowed(combined))
        records = [self.document["metrics"][metric_id] for metric_id in combined]
        figure = distribution_figure(records)
        self.assertEqual(figure["mode"], "separate_counts")
        self.assertFalse(figure["share_of_whole"])
        self.assertEqual(figure["data"][0]["type"], "bar")
        self.assertEqual(figure["data"][0]["x"], [ENROLLED_VALUE, NEW_VALUE, ACTIVE_VALUE, PASSIVE_VALUE])
        self.assertNotEqual(sum(figure["data"][0]["x"]), 100)
        encoded = json.dumps(figure).lower()
        for marker in CHART_MARKERS:
            self.assertNotIn(marker, encoded)
        for marker in CHART_MARKERS:
            self.assertNotIn(marker, self.script.lower())
        self.assertIn("share_of_whole === true", self.script)
        self.assertIn(self.document["note"], self.html)

    def test_preserved_sections_and_catalog(self):
        for heading in (
            "Applications",
            "EDE",
            "Financial Assistance",
            "Currently Enrolled — History &amp; Trends",
            "Currently Enrolled — Snapshot Trend",
            "Metric Definitions",
            "Plan Distribution",
        ):
            self.assertIn(f"<h2>{heading}</h2>", self.html)
        self.assertEqual(list(METRICS), list(PRODUCTION_IDS))
        self.assertTrue(all(metric.status == STATUS_ACTIVE for metric in METRICS.values()))
        self.assertEqual(list(PROMOTION_CANDIDATES), ["total_applicants"])
        self.assertNotIn("total_applicants", METRICS)
        self.assertIn("Total Applicants", self.html)
        self.assertEqual(tuple(CANCELLATION_CANDIDATES), (
            "enrolled_cancelled",
            "cancelled_not_enrolled",
            "cancelled_policies_daily",
            "terminated_policies_daily",
        ))
        self.assertEqual(
            identify_cancellation_sources(DiscoveryResult((), 0, 0, 0)),
            (),
        )
        explorer = (PROJECT_ROOT / "reporting/status_explorer.py").read_text(encoding="utf-8")
        self.assertNotIn("cancellation_identification", explorer)
        self.assertNotIn("history.store", explorer)


class RetentionUnchangedTests(unittest.TestCase):
    def test_first_value_in_an_hour_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "oe.sqlite3", "hour", clock=lambda: T0)
            try:
                store.start_run("r1")
                first = store.record_snapshot("r1", result(70_111, at=T0))
                store.start_run("r2")
                second = store.record_snapshot("r2", result(70_222, at=T0 + timedelta(minutes=20)))
            finally:
                store.close()
        self.assertEqual(first.status, WRITE_SAVED)
        self.assertEqual(second.status, WRITE_DUPLICATE_SLOT)
        self.assertEqual(second.snapshot.metric_value, 70_111)
        self.assertEqual(history_capture_label(WRITE_DUPLICATE_SLOT, "hour"), "Already captured this hour")
        self.assertEqual(history_capture_label(WRITE_DUPLICATE_SLOT, "day"), "Already captured for this period")

