"""main() with SISENSE_RUNTIME_VALIDATION: Phase 3D values beside the unchanged production flow. No network."""

import json
import shutil
from unittest import mock

import main as app
from tests.discovery_fixtures import FILTER_SENTINEL
from tests.test_pipeline_main import PipelineHarness, ok_body
from tests.test_runtime_validation import natural_wave1
from tests.test_session_context import FakeResponse

GUID = "RUNTIME-GUID-SENTINEL"


class RuntimeValidationMainTests(PipelineHarness):
    def run_validation(self, responses=None, values=None, **overrides):
        responses = responses or [FakeResponse(200, ok_body(700001))]
        return self.run_main(responses, extra_observed=natural_wave1(values, guid=GUID),
                             runtime_validation=True, **overrides)

    def validation_doc(self):
        path = self.reports / "validation" / "wave1_runtime_validation.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_values_extracted_with_one_application_request(self):
        code, out, logs, posts = self.run_validation()
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1, "only the existing verified application request")
        lines = [" ".join(line.split()) for line in out.splitlines()]
        for line in ("Applications Started VALUE_EXTRACTED 400,002",
                     "Submitted Applications VALUE_EXTRACTED 300,003",
                     "Total Applicants VALUE_EXTRACTED 500,004",
                     "% Effectuated VALUE_EXTRACTED 97.00% (raw 0.97)",
                     "New VALUE_EXTRACTED 100,005",
                     "Active VALUE_EXTRACTED 200,006",
                     "Passive VALUE_EXTRACTED 300,007",
                     "Cancelled PENDING",
                     "Terminated PENDING",
                     "Currently Enrolled Control EXACT_VALUE_MATCH"):
            self.assertIn(line, lines)
        self.assertTrue((self.reports / "validation" / "wave1_runtime_validation.html").exists())
        self.assertTrue((self.reports / "discovery" / "wave1_metric_discovery.html").exists())
        control = self.validation_doc()["currently_enrolled_control"]
        self.assertEqual((control["passive_value"], control["application_value"], control["difference"]),
                         (700001, 700001, 0))

    def test_control_mismatch_reported_and_application_authoritative(self):
        code, out, _, _ = self.run_validation(values={"ce": 699999})
        self.assertEqual(code, app.EXIT_OK)
        self.assertIn("Currently Enrolled Control LIVE_MOVEMENT",
                      [" ".join(line.split()) for line in out.splitlines()])
        self.assertIn("Currently Enrolled identity IDENTITY_PASS",
                      [" ".join(line.split()) for line in out.splitlines()])
        self.assertEqual([s.metric_value for s in self.snapshots()], [700001])

    def test_discovered_values_never_enter_history(self):
        self.run_validation()
        self.assertEqual([(s.metric_id, s.metric_value) for s in self.snapshots()],
                         [("currently_enrolled_people", 700001)])

    def test_executive_report_unchanged(self):
        self.run_validation()
        with_validation = self.report_html()
        for value in ("400,002", "300,003", "500,004", "100,005", "200,006", "300,007"):
            self.assertNotIn(value, with_validation)
        shutil.rmtree(self.root / "data")
        shutil.rmtree(self.reports)
        self.run_main([FakeResponse(200, ok_body(700001))], extra_observed=natural_wave1(guid=GUID))
        self.assertEqual(with_validation, self.report_html())

    def test_no_raw_bodies_or_secrets_persisted(self):
        _, out, logs, _ = self.run_validation()
        self.assert_no_secrets_anywhere(out, logs)
        files = [p for p in self.root.rglob("*") if p.is_file()]
        contents = [p.read_bytes().decode("utf-8", errors="ignore") for p in files] + [out, logs]
        for forbidden in (GUID, FILTER_SENTINEL, "Count(["):
            for content in contents:
                self.assertNotIn(forbidden, content)

    def test_application_failure_still_reports_validation_without_snapshot(self):
        code, out, _, posts = self.run_validation([FakeResponse(401, "{}")])
        self.assertEqual(code, app.EXIT_FAILURE)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.snapshots(), [])
        control = self.validation_doc()["currently_enrolled_control"]
        self.assertEqual((control["status"], control["application_value"]), ("SOURCE_UNAVAILABLE", None))

    def test_validation_error_does_not_affect_production(self):
        with mock.patch.object(app, "validate_runtime_values", side_effect=RuntimeError("boom")):
            code, _, logs, posts = self.run_validation()
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertIn("Wave 1 runtime validation failed: RuntimeError", logs)

    def test_diagnostics_only_stays_safe(self):
        code, out, _, posts = self.run_validation(diagnostics_only=True)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(posts, [])
        self.assertFalse(self.reports.exists())
        self.assertNotIn("runtime validation", out)

    def test_disabled_by_default(self):
        code, out, _, _ = self.run_main([FakeResponse(200, ok_body(700001))], extra_observed=natural_wave1())
        self.assertEqual(code, app.EXIT_OK)
        self.assertNotIn("runtime validation", out)
        self.assertFalse((self.reports / "validation").exists())
