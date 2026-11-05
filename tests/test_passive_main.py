"""main() with SISENSE_PASSIVE_PROOF: validation-only passive value next to the authoritative one. No network."""

import secrets
import sqlite3
import unittest

import main as app
from tests.passive_fixtures import PassiveRequest, payload, response_text, scope
from tests.test_pipeline_main import PipelineHarness, ok_body
from tests.test_session_context import FakeResponse


class PassiveProofMainTests(PipelineHarness):
    def setUp(self):
        super().setUp()
        self.request_sentinel = f"REQ-SENTINEL-{secrets.token_hex(8)}"
        self.response_sentinel = f"RESP-SENTINEL-{secrets.token_hex(8)}"

    def widget_request(self, value, mutate=None, **kw):
        return PassiveRequest(
            payload(mutate, query_guid=self.request_sentinel),
            text=response_text(value, {"sentinel": self.response_sentinel}), **kw)

    def run_proof(self, app_value, *passive, **overrides):
        return self.run_main([FakeResponse(200, ok_body(app_value))], extra_observed=passive,
                             passive_proof=True, **overrides)

    def assert_nothing_persisted(self, *texts):
        self.assert_no_secrets_anywhere(*texts)
        files = [p for p in self.root.rglob("*") if p.is_file()]
        contents = [p.read_bytes().decode("utf-8", errors="ignore") for p in files] + list(texts)
        for sentinel in (self.request_sentinel, self.response_sentinel):
            for content in contents:
                self.assertNotIn(sentinel, content)

    def test_matching_passive_value_passes(self):
        code, out, logs, posts = self.run_proof(812345, self.widget_request(812345))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1, "exactly one application JAQL request")
        for text in ("Candidate: FOUND", "Dashboard: MATCH", "Widget: MATCH", "Primary Measure: MATCH",
                     "Filters: MATCH", "HTTP Status: 200", "Parse: PASS", "Passive Value: 812,345",
                     "Application Value: 812,345", "Difference: 0", "Passive Validation: PASS"):
            self.assertIn(text, out)
        self.assertEqual([s.metric_value for s in self.snapshots()], [812345])
        self.assert_nothing_persisted(out, logs)

    def test_mismatch_fails_and_application_value_stays_authoritative(self):
        code, out, logs, posts = self.run_proof(812345, self.widget_request(799001))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertIn("Passive Validation: FAIL", out)
        self.assertIn("Difference: -13,344", out)
        self.assertIn("Current Value         812,345", out)
        self.assertEqual([s.metric_value for s in self.snapshots()], [812345])
        html = self.report_html()
        self.assertIn("812,345", html)
        self.assertNotIn("799,001", html)
        self.assert_nothing_persisted(out, logs)

    def test_passive_value_never_creates_snapshot(self):
        self.run_proof(812345, self.widget_request(812345), self.widget_request(812345))
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT metric_id, metric_value FROM metric_snapshots ORDER BY id").fetchall()
        finally:
            conn.close()
        # Only the application value for Currently Enrolled; % Effectuated is its own verified metric.
        self.assertEqual(rows, [("currently_enrolled_people", 812345), ("percent_effectuated", 0.97)])

    def test_filter_mismatch_fails_without_extra_request(self):
        mutate = lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"])
        code, out, _, posts = self.run_proof(812345, self.widget_request(5, mutate))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertIn("Filters: MISMATCH", out)
        self.assertIn("Passive Validation: FAIL", out)

    def test_unavailable_body_fails_closed(self):
        code, out, _, posts = self.run_proof(812345, self.widget_request(812345, body_error=True))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertIn("Response body unavailable", out)
        self.assertIn("Passive Validation: FAIL", out)
        self.assertEqual([s.metric_value for s in self.snapshots()], [812345])

    def test_no_candidate(self):
        code, out, _, posts = self.run_proof(812345)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertIn("Candidate: NOT FOUND", out)
        self.assertIn("Passive Validation: FAIL", out)

    def test_disabled_by_default(self):
        code, out, _, posts = self.run_main([FakeResponse(200, ok_body(812345))],
                                            extra_observed=[self.widget_request(812345)])
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        self.assertNotIn("Passive", out)

    def test_diagnostics_only_stays_safe(self):
        code, out, _, posts = self.run_proof(812345, self.widget_request(812345), diagnostics_only=True)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(posts, [])
        self.assertNotIn("Passive", out)
        self.assertFalse(self.db.exists())

    def test_application_failure_unaffected_by_passive_success(self):
        code, out, _, posts = self.run_main([FakeResponse(401, "{}")], extra_observed=[self.widget_request(812345)],
                                            passive_proof=True)
        self.assertEqual(code, app.EXIT_FAILURE)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.snapshots(), [])
        self.assertNotIn("Passive Value", out)


if __name__ == "__main__":
    unittest.main()
