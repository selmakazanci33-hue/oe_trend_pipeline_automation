"""main() end to end with production passive metrics (fakes only, network blocked)."""

import json
import re
import secrets
import shutil
import sqlite3
from datetime import timedelta
from html import unescape
from unittest import mock

import main as app
from history.store import SnapshotStore
from metrics import catalog
from metrics.catalog import PERCENT_EFFECTUATED
from tests.discovery_fixtures import cell, widget_request
from tests.passive_fixtures import PassiveRequest, payload, scope
from tests.test_passive_metrics import candidate_payload, promoted
from tests.test_pipeline_main import RUN_TIME, PipelineHarness, ok_body
from tests.test_session_context import FakeResponse


def ce_widget_text(ce, pct, extra=None):
    body = {"headers": ["Currently Enrolled People", "% Effectuated"],
            "values": [{"data": ce, "text": str(ce)}, {"data": pct, "text": str(pct)}]}
    body.update(extra or {})
    return json.dumps(body)


class PassiveMetricsHarness(PipelineHarness):
    def setUp(self):
        super().setUp()
        self.request_sentinel = f"REQ-SENTINEL-{secrets.token_hex(8)}"
        self.response_sentinel = f"RESP-SENTINEL-{secrets.token_hex(8)}"

    def ce_widget(self, ce=812345, pct=0.931234, mutate=None, **kw):
        return PassiveRequest(payload(mutate, query_guid=self.request_sentinel),
                              text=ce_widget_text(ce, pct, {"sentinel": self.response_sentinel}), **kw)

    def run_pipeline(self, app_value=812345, *observed, responses=None, **kw):
        return self.run_main(responses or [FakeResponse(200, ok_body(app_value))], extra_observed=observed, **kw)

    def rows(self, metric_id):
        with SnapshotStore(self.db) as store:
            return [s.metric_value for s in store.snapshots(metric_id, 2027)]

    def card(self, metric_id):
        html = self.report_html()
        match = re.search(rf'<div class="card metric-card" data-metric="{metric_id}">(.*?)</div></div>', html, re.S)
        self.assertIsNotNone(match, metric_id)
        return match.group(1)

    def pending_chips(self):
        return {unescape(name) for name in re.findall(
            r'<li><span>([^<]+)</span><span class="badge badge-none">PENDING</span></li>', self.report_html())}


class PassiveMetricsMainTests(PassiveMetricsHarness):
    # --- LIVE / promotion -------------------------------------------------

    def test_live_percent_effectuated_with_one_application_request(self):
        code, out, logs, posts = self.run_pipeline(812345, self.ce_widget(pct=0.931234))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1, "exactly one application JAQL request")
        self.assertEqual(self.rows("percent_effectuated"), [0.931234])
        self.assertEqual(self.rows("currently_enrolled_people"), [812345])
        card = self.card("percent_effectuated")
        self.assertIn("93.12%", card)
        self.assertIn('class="badge badge-live"', card)
        self.assertIn('class="badge badge-source"', card)
        self.assertIn("Live value retrieved", card)
        self.assertIn("Insufficient history", card)
        self.assertRegex(out, r"% Effectuated\s+LIVE\s+93\.12%\s+snapshot SAVED")

    def test_percent_effectuated_no_longer_pending_but_others_remain(self):
        self.run_pipeline(812345, self.ce_widget())
        chips = self.pending_chips()
        self.assertNotIn("% Effectuated", chips)
        for name in ("Enrolled & Cancelled", "Cancelled — Not Enrolled", "Cancelled Policies — Daily",
                     "Terminated Policies — Daily", "Metal Tier", "Total Applicants"):
            self.assertIn(name, chips)
        for name in ("New", "Active", "Passive", "Applications Started", "Submitted Applications"):
            self.assertNotIn(name, chips)
        html = self.report_html()
        self.assertIn('data-view="applications">Applications</button>', html)
        self.assertNotIn('data-view="applications">Applications<span class="nav-tag">Pending</span>', html)
        self.assertIn('data-metric="applications_started"', html)

    def test_change_labelled_in_percentage_points(self):
        self.run_pipeline(812345, self.ce_widget(pct=0.9), at=RUN_TIME - timedelta(hours=3))
        self.run_pipeline(812345, self.ce_widget(pct=0.9032))
        card = self.card("percent_effectuated")
        self.assertIn("+0.32 pp", card)
        self.assertIn('class="badge badge-derived"', card)

    # --- failure semantics ------------------------------------------------

    def test_not_available_when_source_rejected(self):
        mutate = lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"])
        code, out, _, posts = self.run_pipeline(812345, self.ce_widget(mutate=mutate))
        self.assertEqual((code, len(posts)), (app.EXIT_OK, 1))
        self.assertEqual(self.rows("percent_effectuated"), [])
        self.assertEqual(self.rows("currently_enrolled_people"), [812345])
        card = self.card("percent_effectuated")
        self.assertIn("NOT AVAILABLE", card)
        self.assertNotIn("0.00%", card)
        self.assertIn("Request definition mismatch", out)

    def test_not_available_when_source_not_observed(self):
        self.run_pipeline(812345)
        self.assertEqual(self.rows("percent_effectuated"), [])
        self.assertIn("NOT AVAILABLE", self.card("percent_effectuated"))

    def test_control_mismatch_blocks_snapshot(self):
        self.run_pipeline(812345, self.ce_widget(ce=799001))
        self.assertEqual(self.rows("percent_effectuated"), [])
        self.assertEqual(self.rows("currently_enrolled_people"), [812345])
        self.assertIn("control mismatch", self.card("percent_effectuated"))

    def test_stale_after_later_failure(self):
        self.run_pipeline(812345, self.ce_widget(pct=0.911111), at=RUN_TIME - timedelta(hours=2))
        self.run_pipeline(812400, self.ce_widget(ce=812400, pct=0.5, status=503))
        self.assertEqual(self.rows("percent_effectuated"), [0.911111])
        self.assertEqual(self.rows("currently_enrolled_people"), [812345, 812400])
        card = self.card("percent_effectuated")
        self.assertIn('class="badge badge-stale"', card)
        self.assertIn("91.11%", card)
        self.assertIn("Historical value", card)
        self.assertNotIn("50.00%", self.report_html())

    def test_application_failure_stores_no_passive_value(self):
        self.run_pipeline(812345, self.ce_widget(pct=0.911111), at=RUN_TIME - timedelta(hours=2))
        code, _, _, posts = self.run_pipeline(None, self.ce_widget(pct=0.5), responses=[FakeResponse(401, "{}")])
        self.assertEqual((code, len(posts)), (app.EXIT_FAILURE, 1))
        self.assertEqual(self.rows("percent_effectuated"), [0.911111])
        card = self.card("percent_effectuated")
        self.assertIn('class="badge badge-stale"', card)
        self.assertNotIn("50.00%", self.report_html())

    def test_duplicate_slot_never_overwrites(self):
        self.run_pipeline(812345, self.ce_widget(pct=0.911111))
        code, out, _, _ = self.run_pipeline(812345, self.ce_widget(pct=0.922222), at=RUN_TIME + timedelta(minutes=10))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(self.rows("percent_effectuated"), [0.911111])
        self.assertIn("snapshot DUPLICATE_SLOT", out)
        self.assertIn("92.22%", self.card("percent_effectuated"))

    def test_no_backfill_and_currently_enrolled_history_intact(self):
        self.run_pipeline(800000, at=RUN_TIME - timedelta(days=3))
        self.run_pipeline(805000, at=RUN_TIME - timedelta(days=2))
        before = self.rows("currently_enrolled_people")
        self.run_pipeline(812345, self.ce_widget())
        self.assertEqual(self.rows("currently_enrolled_people"), before + [812345])
        self.assertEqual(len(self.rows("percent_effectuated")), 1)
        conn = sqlite3.connect(self.db)
        try:
            times = conn.execute("SELECT retrieved_at_utc FROM metric_snapshots WHERE metric_id = ?",
                                 ("percent_effectuated",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(times), 1)

    def test_currently_enrolled_passive_proof_still_passes(self):
        code, out, _, posts = self.run_pipeline(812345, self.ce_widget(), passive_proof=True)
        self.assertEqual((code, len(posts)), (app.EXIT_OK, 1))
        self.assertIn("Passive Validation: PASS", out)
        self.assertEqual(self.rows("currently_enrolled_people"), [812345])

    # --- isolation and the promotion path for the six ----------------------

    def promoted_started(self):
        body = candidate_payload("applications_started")
        return promoted("applications_started", body), widget_request(body, [cell(400_002)])

    def test_partial_failure_isolation_and_applications_card(self):
        started, request = self.promoted_started()
        mutate = lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"])
        with mock.patch.object(app, "passive_metrics", lambda: [PERCENT_EFFECTUATED, started]), \
                mock.patch.dict(catalog.METRICS, {"applications_started": started}):
            code, _, _, posts = self.run_pipeline(812345, self.ce_widget(mutate=mutate), request)
            self.assertEqual((code, len(posts)), (app.EXIT_OK, 1))
            self.assertEqual(self.rows("applications_started"), [400_002])
        self.assertEqual(self.rows("percent_effectuated"), [])
        self.assertIn("400,002", self.card("applications_started"))
        self.assertIn('class="badge badge-live"', self.card("applications_started"))
        self.assertIn("NOT AVAILABLE", self.card("percent_effectuated"))
        chips = self.pending_chips()
        self.assertNotIn("Applications Started", chips)
        self.assertNotIn("Submitted Applications", chips)
        self.assertIn("Total Applicants", chips)

    def test_unapproved_candidates_never_stored_even_if_observed(self):
        _, request = self.promoted_started()
        self.run_pipeline(812345, self.ce_widget(), request)
        self.assertEqual(self.rows("applications_started"), [])
        self.assertNotIn("400,002", self.report_html())

    # --- developer tooling isolation and safety ----------------------------

    def test_report_ignores_developer_artifacts(self):
        for sub, name in (("validation", "wave1_runtime_validation.json"),
                          ("discovery", "wave1_metric_discovery.json")):
            (self.reports / sub).mkdir(parents=True, exist_ok=True)
            (self.reports / sub / name).write_text(json.dumps({"runtime_value": 0.123456, "value": 4242424}))
        self.run_pipeline(812345, self.ce_widget(pct=0.931234))
        html = self.report_html()
        self.assertNotIn("12.35%", html)
        self.assertNotIn("4,242,424", html)
        self.assertIn("93.12%", html)

    def test_executive_report_same_with_discovery_tooling(self):
        self.run_pipeline(812345, self.ce_widget(), discovery=True, runtime_validation=True)
        with_tooling = self.report_html()
        shutil.rmtree(self.root / "data")
        shutil.rmtree(self.reports)
        self.run_pipeline(812345, self.ce_widget())
        self.assertEqual(with_tooling, self.report_html())

    def test_no_raw_bodies_or_secrets(self):
        _, out, logs, _ = self.run_pipeline(812345, self.ce_widget(), discovery=True, runtime_validation=True)
        self.assert_no_secrets_anywhere(out, logs)
        files = [p for p in self.root.rglob("*") if p.is_file()]
        contents = [p.read_bytes().decode("utf-8", errors="ignore") for p in files] + [out, logs]
        for forbidden in (self.request_sentinel, self.response_sentinel, "Count(["):
            for content in contents:
                self.assertNotIn(forbidden, content)

    def test_diagnostics_mode_reads_nothing(self):
        code, _, _, posts = self.run_pipeline(812345, self.ce_widget(), diagnostics_only=True)
        self.assertEqual((code, len(posts)), (app.EXIT_OK, 0))
        self.assertFalse(self.db.exists())
