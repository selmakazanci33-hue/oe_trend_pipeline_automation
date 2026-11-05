"""Phase 3H-B: cancellation source identification + duplicate-slot wording (fakes, network blocked)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE
from history.store import WRITE_DUPLICATE_SLOT, SnapshotStore, SnapshotWriteResult
from metrics.cancellation import bind_exact_measure, extract_cancellation
from metrics.catalog import CANCELLATION_CANDIDATES, CURRENTLY_ENROLLED, METRICS, PROMOTION_CANDIDATES
from metrics.cancellation_identification import (
    CONF_EXACT,
    CONF_STRUCTURAL,
    CONF_UNRESOLVED,
    ROLE_DAILY,
    ROLE_PEOPLE,
    ROLE_UNRESOLVED,
    classify_cancellation_widget,
    identify_cancellation_sources,
    safe_inventory_row,
)
from metrics.discovery import resolve_targets
from metrics.runtime_validation import BindingError
from reporting.html_report import history_capture_label
from reporting.model import DATA_LIVE, RunOutcome, build_report_model
from sisense.discovery import DiscoveryObserver, evaluate_discovery, normalize_label
from tests.discovery_fixtures import FILTER_SENTINEL, cell, widget_payload, widget_request
from tests.passive_fixtures import PassiveRequest, payload, response_text
from tests.reporting_fixtures import result, series
from tests.test_discovery import EXPECTED
from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_phase3f_d import section
from tests.test_report_consistency import visible
from tests.test_pipeline_main import RUN_TIME
from tests.test_reporting import visible_html
from tests.test_runtime_validation import natural_wave1

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    "metrics/cancellation_identification.py",
    "reporting/discovery_report.py",
    "reporting/validation_report.py",
    "reporting/html_report.py",
    "metrics/cancellation.py",
    "metrics/catalog.py",
)
FORBIDDEN = (
    "566051", "566,051", "565200", "565,200", "2510", "2,510", "35266", "35,266",
    "756009", "756,009", "793512", "793,512", "791826", "791,826",
)


def observe_all(*requests, **kw):
    observer = DiscoveryObserver(
        VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE["id"],
        ["Applications Started"], EXPECTED.widget_id, **kw,
    )
    for request in requests:
        observer._on_request_finished(request)
    return observer


def discovered(*requests):
    observer = observe_all(*requests)
    return evaluate_discovery(observer, EXPECTED), observer


def people_alias_payload():
    return widget_payload(
        "w-people-alias",
        "Enrolled/Cancelled vs Cancelled/Not Enrolled People",
        measures=["Count of enrolled cancelled people", "Count of cancelled not enrolled"],
    )


def daily_alias_payload():
    return widget_payload(
        "w-daily-alias",
        "Cancellation Activity",
        measures=["Policy cancel count", "Policy termination count"],
        dimensions=[("Date", "datetime", "days")],
        widget_type="chart/line",
    )


class InventoryTests(unittest.TestCase):
    def test_safe_inventory_uses_passive_traffic_only(self):
        request = widget_request(people_alias_payload(), [cell(12_101), cell(12_102)])
        observer = observe_all(request)
        self.assertEqual(observer.target_requests_seen, 1)
        self.assertEqual(request.response_calls, 0)
        result_ = evaluate_discovery(observer, EXPECTED)
        row = safe_inventory_row(result_.widgets[0])
        self.assertEqual(row["widget_id"], "w-people-alias")
        self.assertEqual(row["widget_title"], "Enrolled/Cancelled vs Cancelled/Not Enrolled People")
        self.assertEqual(row["datasource_title"], "MAIN")
        self.assertEqual(row["datasource_id"], VERIFIED_DATASOURCE["id"])
        self.assertGreaterEqual(row["metadata_item_count"], 2)
        self.assertTrue(any(role["kind"] == "measure" for role in row["metadata_roles"]))
        self.assertFalse(row["has_date_dimension"])
        self.assertIsNone(row["date_grain"])
        self.assertTrue(str(row["definition_fingerprint"]).startswith("sha256:"))
        self.assertNotIn("values", row)
        self.assertNotIn(FILTER_SENTINEL, json.dumps(row))
        self.assertNotIn("members", json.dumps(row))

    def test_no_extra_http_and_no_raw_or_secret_persistence(self):
        guid = "INV-GUID-SENTINEL"
        body = people_alias_payload()
        body["queryGuid"] = guid
        request = widget_request(body, [cell(12_101), cell(12_102)])
        result_, _ = discovered(
            request,
            widget_request(daily_alias_payload(), [
                [cell("2026-02-01", "02/01/2026"), cell(901), cell(811)],
            ]),
            PassiveRequest(payload(query_guid=guid), text=response_text(700_001)),
        )
        from reporting.discovery_report import write_discovery_report
        with tempfile.TemporaryDirectory() as tmp:
            path = write_discovery_report(resolve_targets(result_), result_, Path(tmp),
                                         datetime(2026, 11, 5, tzinfo=timezone.utc))
            html = path.read_text(encoding="utf-8")
            doc = (path.parent / "wave1_metric_discovery.json").read_text(encoding="utf-8")
        self.assertIn("Safe widget inventory", html)
        self.assertIn("Cancellation Source Identification", html)
        for forbidden in (guid, FILTER_SENTINEL, "Count([AAA-1])", "Count([17769-998]", "[17769-998]",
                          "86D64-0B4", "12,101", "12101",
                          "xsrf", "cookie", "authorization", "server.session"):
            self.assertNotIn(forbidden.lower(), html.lower())
            self.assertNotIn(forbidden.lower(), doc.lower())
        parsed = json.loads(doc)
        blob = json.dumps(parsed["widget_inventory"])
        self.assertNotIn(FILTER_SENTINEL, blob)
        self.assertNotIn("members", blob)

    def test_discovery_does_not_require_display_label_to_equal_measure_title(self):
        result_, _ = discovered(widget_request(people_alias_payload(), [cell(1), cell(2)]))
        widget = result_.widgets[0]
        self.assertEqual(widget.structure.widget_title, "Enrolled/Cancelled vs Cancelled/Not Enrolled People")
        self.assertNotIn("enrolled & cancelled", {normalize_label(c.title) for c in widget.structure.measures})
        candidate = classify_cancellation_widget(widget)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.semantic_role, ROLE_PEOPLE)
        self.assertEqual(candidate.confidence, CONF_STRUCTURAL)
        metrics = {m.metric_id: m for m in resolve_targets(result_)}
        self.assertEqual(metrics["enrolled_cancelled"].state, "PENDING")


class IdentificationIsolationTests(unittest.TestCase):
    def test_broader_discovery_cannot_promote(self):
        before = list(METRICS)
        result_, _ = discovered(
            widget_request(people_alias_payload(), [cell(1), cell(2)]),
            widget_request(daily_alias_payload(), [[cell("2026-02-01", "02/01/2026"), cell(1), cell(2)]]),
        )
        candidates = identify_cancellation_sources(result_)
        self.assertTrue(candidates)
        self.assertEqual(list(METRICS), before)
        self.assertNotIn("enrolled_cancelled", METRICS)
        self.assertTrue(all(c.confidence != "VERIFIED" for c in candidates))

    def test_structural_candidate_is_not_production(self):
        result_, _ = discovered(widget_request(people_alias_payload(), [cell(1), cell(2)]))
        candidate = identify_cancellation_sources(result_)[0]
        self.assertEqual(candidate.confidence, CONF_STRUCTURAL)
        self.assertNotIn(candidate.widget_id, {m.passive_source.widget_id for m in METRICS.values()
                                               if m.passive_source})
        with self.assertRaises(BindingError):
            bind_exact_measure(people_alias_payload(), "Enrolled & Cancelled")
        row = extract_cancellation(
            CANCELLATION_CANDIDATES["enrolled_cancelled"], people_alias_payload(),
            json.dumps({"values": [cell(1), cell(2)]}),
            dict(value_kind="count", widget_id="w-people-alias", http_status=200, datasource="MAIN"),
        )
        self.assertNotEqual(row.status, "VALUE_EXTRACTED")

    def test_exact_identity_still_does_not_promote(self):
        body = widget_payload(
            "w-people-exact", "People cancellation",
            measures=["Enrolled & Cancelled", "Cancelled - Not Enrolled"],
        )
        result_, _ = discovered(widget_request(body, [cell(1), cell(2)]))
        candidate = classify_cancellation_widget(result_.widgets[0])
        self.assertEqual((candidate.semantic_role, candidate.confidence), (ROLE_PEOPLE, CONF_EXACT))
        self.assertNotIn("enrolled_cancelled", METRICS)

    def test_daily_structural_candidate(self):
        result_, _ = discovered(widget_request(daily_alias_payload(), [
            [cell("2026-02-01", "02/01/2026"), cell(1), cell(2)],
            [cell("2026-02-02", "02/02/2026"), cell(3), cell(4)],
        ]))
        candidate = classify_cancellation_widget(result_.widgets[0])
        self.assertEqual(candidate.semantic_role, ROLE_DAILY)
        self.assertEqual(candidate.confidence, CONF_STRUCTURAL)
        self.assertEqual(candidate.date_grain, "days")
        self.assertNotIn("cancelled_policies_daily", METRICS)

    def test_unrelated_widget_is_not_nominated(self):
        result_, _ = discovered(widget_request(
            widget_payload("w-age", "Average Age", measures=["Average Age"]), [cell(40)]))
        self.assertEqual(identify_cancellation_sources(result_), ())

    def test_hint_without_structure_is_unresolved(self):
        result_, _ = discovered(widget_request(
            widget_payload("w-hint", "Cancellation notes", measures=["Note count"]), [cell(1)]))
        candidate = classify_cancellation_widget(result_.widgets[0])
        self.assertEqual(candidate.semantic_role, ROLE_UNRESOLVED)
        self.assertEqual(candidate.confidence, CONF_UNRESOLVED)


class ProductionIsolationTests(PassiveMetricsHarness):
    def test_production_cancellation_metrics_remain_pending(self):
        self.run_pipeline(
            700_001, self.ce_widget(ce=700_001, pct=0.97),
            widget_request(people_alias_payload(), [cell(12_101), cell(12_102)]),
            widget_request(daily_alias_payload(), [[cell("2026-02-01", "02/01/2026"), cell(1), cell(2)]]),
            runtime_validation=True,
        )
        html = self.report_html()
        self.assertIn("PENDING", section(html, "cancellation_overview"))
        self.assertIn("PENDING", section(html, "cancellation_activity"))
        self.assertNotIn("12,101", visible(html))
        chips = self.pending_chips()
        for name in ("Enrolled & Cancelled", "Cancelled — Not Enrolled",
                     "Cancelled Policies — Daily", "Terminated Policies — Daily"):
            self.assertIn(name, chips)
        self.assertNotIn("enrolled_cancelled", METRICS)

    def test_total_applicants_remains_unpromoted(self):
        self.assertEqual(set(PROMOTION_CANDIDATES), {"total_applicants"})
        self.assertNotIn("total_applicants", METRICS)
        self.run_pipeline(700_001, self.ce_widget(ce=700_001, pct=0.97), *natural_wave1(), runtime_validation=True)
        self.assertIn("Total Applicants", self.pending_chips())
        self.assertNotIn('data-metric="total_applicants"', self.report_html())

    def test_one_application_request(self):
        code, _, _, posts = self.run_pipeline(
            700_001, self.ce_widget(ce=700_001, pct=0.97),
            widget_request(people_alias_payload(), [cell(1), cell(2)]),
            runtime_validation=True,
        )
        self.assertEqual((code, len(posts)), (0, 1))
        doc = json.loads((self.reports / "discovery" / "wave1_metric_discovery.json").read_text(encoding="utf-8"))
        self.assertTrue(doc["widget_inventory"])
        self.assertTrue(doc["cancellation_source_identification"])
        validation = json.loads((self.reports / "validation" / "wave1_runtime_validation.json").read_text())
        self.assertTrue(validation["cancellation_source_identification"])
        self.assertNotIn("total_applicants", METRICS)


class DuplicateSlotPresentationTests(unittest.TestCase):
    def test_storage_behavior_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.sqlite3"
            store = SnapshotStore(path, "hour", clock=lambda: RUN_TIME)
            with store:
                store.start_run("r1")
                first = store.record_snapshot("r1", result(700_001, at=RUN_TIME))
                store.finish_run("r1", "SUCCESS")
                store.start_run("r2")
                second = store.record_snapshot("r2", result(700_999, at=RUN_TIME + timedelta(minutes=10)))
                store.finish_run("r2", "SUCCESS")
                values = [s.metric_value for s in store.snapshots("currently_enrolled_people", 2027)]
        self.assertEqual(first.status, "SAVED")
        self.assertEqual(second.status, WRITE_DUPLICATE_SLOT)
        self.assertEqual(values, [700_001])

    def test_production_html_hides_raw_duplicate_slot_and_stays_live(self):
        snaps = series((0, 700_001))
        write = SnapshotWriteResult(WRITE_DUPLICATE_SLOT, snaps[-1])
        outcome = RunOutcome("run", result(700_999, at=RUN_TIME), snapshot_write=write)
        model = build_report_model(CURRENTLY_ENROLLED, snaps, outcome, generated_at=RUN_TIME)
        self.assertEqual(model.data_status, DATA_LIVE)
        html = visible_html(model)
        self.assertNotIn("DUPLICATE_SLOT", html)
        self.assertIn("History: Already captured this hour", html)
        self.assertIn('class="badge badge-live"', html)
        self.assertEqual(history_capture_label(WRITE_DUPLICATE_SLOT, "hour"), "Already captured this hour")
        self.assertEqual(history_capture_label(WRITE_DUPLICATE_SLOT, "day"), "Already captured for this period")
        self.assertEqual(history_capture_label("SAVED"), "SAVED")

    def test_developer_wording_may_still_be_duplicate_slot(self):
        self.assertEqual(WRITE_DUPLICATE_SLOT, "DUPLICATE_SLOT")
        text = (PROJECT_ROOT / "history/store.py").read_text(encoding="utf-8")
        self.assertIn("WRITE_DUPLICATE_SLOT = \"DUPLICATE_SLOT\"", text)


class SafetyTests(unittest.TestCase):
    def test_no_screenshot_values_or_network_capability(self):
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        forbidden_attrs = {"request", "post", "fetch", "evaluate", "goto", "reload", "route", "new_page",
                           "new_context", "api_request", "wait_for_response", "expect_response"}
        for relative in SOURCES:
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for literal in FORBIDDEN:
                self.assertNotIn(literal, text, relative)
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden_modules, relative)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], forbidden_modules, relative)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden_attrs, f"{relative}:{getattr(node, 'lineno', None)}")


if __name__ == "__main__":
    unittest.main()
