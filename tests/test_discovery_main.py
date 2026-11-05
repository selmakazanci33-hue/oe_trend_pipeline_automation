"""main() with SISENSE_DISCOVERY: passive Wave 1 discovery beside the unchanged production flow. No network."""

import json
import shutil
import unittest
from unittest import mock

import main as app
from tests.discovery_fixtures import FILTER_SENTINEL, cell, widget_payload, widget_request
from tests.passive_fixtures import PassiveRequest, payload, response_text
from tests.test_pipeline_main import PipelineHarness, ok_body
from tests.test_session_context import FakeResponse

GUID_SENTINEL = "DISCOVERY-GUID-SENTINEL"


def natural_dashboard_requests():
    return [
        PassiveRequest(payload(query_guid=GUID_SENTINEL), text=response_text(812345, {"x": "RESP-SENTINEL"})),
        widget_request(widget_payload("w1", "Applications Started", measures=["Applications Started"],
                                      query_guid=GUID_SENTINEL), [cell(5550001)]),
        widget_request(widget_payload("w5", "Enrollment Type", measures=["New", "Active", "Passive"]),
                       [cell(5550002), cell(5550003), cell(5550004)]),
        widget_request(widget_payload("w7", "Submitted Applications", measures=["Submitted Applications"],
                                      dimensions=[("Date", "datetime", "days")]),
                       [[cell("2026-11-01", "11/01/2026"), cell(5550005)]]),
    ]


class DiscoveryMainTests(PipelineHarness):
    def run_discovery(self, responses=None, **overrides):
        responses = responses or [FakeResponse(200, ok_body(812345))]
        return self.run_main(responses, extra_observed=natural_dashboard_requests(), discovery=True, **overrides)

    def discovery_paths(self):
        directory = self.reports / "discovery"
        return directory / "wave1_metric_discovery.html", directory / "wave1_metric_discovery.json"

    def test_discovery_report_from_natural_requests_only(self):
        code, out, logs, posts = self.run_discovery()
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1, "only the existing verified application request")
        html_path, json_path = self.discovery_paths()
        self.assertTrue(html_path.exists())
        self.assertIn(str(html_path), out)
        doc = json.loads(json_path.read_text(encoding="utf-8"))
        states = {m["metric_id"]: m["state"] for m in doc["metrics"]}
        self.assertEqual(states, {
            "currently_enrolled_people": "VERIFIED", "pct_effectuated": "DISCOVERED",
            "applications_started": "DISCOVERED", "submitted_applications": "DISCOVERED",
            "new": "DISCOVERED", "active": "DISCOVERED", "passive": "DISCOVERED",
            "total_applicants": "PENDING", "cancelled": "PENDING", "terminated": "PENDING",
        })
        shapes = {w["widget_id"]: w["response"]["shape"] for w in doc["widgets"]}
        self.assertEqual(shapes["w7"], "TIME_SERIES")
        self.assertEqual(shapes["w5"], "MULTI_MEASURE")

    def test_nothing_sensitive_or_raw_persisted(self):
        _, out, logs, _ = self.run_discovery()
        self.assert_no_secrets_anywhere(out, logs)
        files = [p for p in self.root.rglob("*") if p.is_file()]
        contents = [p.read_bytes().decode("utf-8", errors="ignore") for p in files] + [out, logs]
        for forbidden in (GUID_SENTINEL, "RESP-SENTINEL", FILTER_SENTINEL, "5550001", "5,550,001", "5550003",
                          "5550005", "Count(["):
            for content in contents:
                self.assertNotIn(forbidden, content)

    def test_discovered_metrics_never_reach_history_or_executive_report(self):
        self.run_discovery()
        self.assertEqual([(s.metric_id, s.metric_value) for s in self.snapshots()],
                         [("currently_enrolled_people", 812345)])
        with_discovery = self.report_html()

        shutil.rmtree(self.root / "data")
        shutil.rmtree(self.reports)
        self.run_main([FakeResponse(200, ok_body(812345))], extra_observed=natural_dashboard_requests())
        self.assertEqual(with_discovery, self.report_html())
        self.assertFalse((self.reports / "discovery").exists())

    def test_discovery_failure_does_not_affect_production(self):
        with mock.patch.object(app, "evaluate_discovery", side_effect=RuntimeError("boom")):
            code, out, logs, posts = self.run_discovery()
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertIn("Wave 1 discovery failed: RuntimeError", logs)

    def test_application_failure_still_writes_discovery_and_no_snapshot(self):
        code, _, _, posts = self.run_discovery([FakeResponse(401, "{}")])
        self.assertEqual(code, app.EXIT_FAILURE)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.snapshots(), [])
        self.assertTrue(self.discovery_paths()[0].exists())

    def test_diagnostics_only_stays_safe(self):
        code, out, _, posts = self.run_discovery(diagnostics_only=True)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(posts, [])
        self.assertFalse(self.reports.exists())
        self.assertNotIn("Wave 1 discovery", out)

    def test_works_with_passive_proof(self):
        code, out, _, posts = self.run_discovery(passive_proof=True)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertIn("Passive Validation: PASS", out)
        self.assertTrue(self.discovery_paths()[0].exists())


if __name__ == "__main__":
    unittest.main()
