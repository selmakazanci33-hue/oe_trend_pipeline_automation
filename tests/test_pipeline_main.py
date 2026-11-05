"""End-to-end main() with fakes: retrieval -> snapshot -> analytics -> report. No network."""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import main as app
from history.store import SnapshotStore
from tests.test_login import make_settings
from tests.test_request_metadata import DashboardContext, RuntimeSecrets
from tests.test_session_context import FakePlaywright, FakeResponse, ObservedRequest

RUN_TIME = datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)


def ok_body(value):
    return f'{{"values": [{{"data": {value}, "text": "{value}"}}, {{"data": 0.97, "text": "0.97"}}]}}'


class FixedDatetime(datetime):
    moment = RUN_TIME

    @classmethod
    def now(cls, tz=None):
        return cls.moment


class PipelineHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "data" / "oe.sqlite3"
        self.reports = self.root / "reports"
        self.secrets = RuntimeSecrets()
        self.operator_prompts = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, responses, at=RUN_TIME, extra_observed=(), **overrides):
        observed = [ObservedRequest(self.secrets.observed_headers()), *extra_observed]
        pw = FakePlaywright(responses)
        browser = pw.chromium.browser
        browser.new_context = lambda **kw: browser.contexts.append(
            DashboardContext(responses, observed, self.secrets.cookies())) or browser.contexts[-1]
        base = make_settings()
        settings = type(base)(**{
            **base.__dict__,
            "storage_state_path": self.root / "auth" / "state.json",
            "artifacts_dir": self.root / "artifacts",
            "history_db_path": self.db,
            "reports_dir": self.reports,
            **overrides,
        })

        @contextmanager
        def fake_sync_playwright():
            yield pw

        FixedDatetime.moment = at
        printed = []

        def operator_completed():
            self.operator_prompts.append("enter")
            return ""

        with mock.patch.object(app, "load_settings", return_value=settings), \
             mock.patch.object(app, "sync_playwright", fake_sync_playwright), \
             mock.patch.object(app, "datetime", FixedDatetime), \
             mock.patch.object(app, "read_operator_completion", operator_completed), \
             mock.patch("sisense.auth.getpass.getpass", return_value="pw"), \
             mock.patch("sisense.retry.time.sleep", side_effect=AssertionError("no retry sleep")), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))), \
             self.assertLogs(level="DEBUG") as logs:
            code = app.main()
        posts = [call for context in browser.contexts for call in context.request.calls]
        return code, "\n".join(printed), "\n".join(logs.output), posts

    def snapshots(self):
        with SnapshotStore(self.db) as store:
            return store.snapshots("currently_enrolled_people", 2027)

    def runs(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT status, failure_type FROM pipeline_runs ORDER BY started_at_utc").fetchall()
        finally:
            conn.close()

    def report_html(self):
        return (self.reports / "latest" / "index.html").read_text(encoding="utf-8")

    def assert_no_secrets_anywhere(self, *texts):
        files = [p for p in self.root.rglob("*") if p.is_file() and "auth" not in p.parts]
        contents = [p.read_bytes().decode("utf-8", errors="ignore") for p in files] + list(texts)
        for secret in self.secrets.all():
            for content in contents:
                self.assertNotIn(secret, content)


class PipelineMainTests(PipelineHarness):
    def test_success_stores_snapshot_and_writes_live_report(self):
        code, out, logs, posts = self.run_main([FakeResponse(200, ok_body(812345))])
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(len(posts), 1)
        snaps = self.snapshots()
        self.assertEqual([s.metric_value for s in snaps], [812345])
        self.assertEqual(self.runs(), [("SUCCESS", None)])
        html = self.report_html()
        self.assertIn("812,345", html)
        self.assertIn("class=\"badge badge-live\"", html)
        self.assertIn("Snapshot              SAVED", out)
        self.assertIn("Historical Snapshots  1", out)
        self.assertIn("Insufficient history", out)
        self.assertIn(str(self.reports / "latest" / "index.html"), out)
        self.assert_no_secrets_anywhere(out, logs)

    def test_trend_after_two_hourly_runs(self):
        self.run_main([FakeResponse(200, ok_body(800000))], at=RUN_TIME - timedelta(hours=25))
        code, out, _, _ = self.run_main([FakeResponse(200, ok_body(806000))])
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual([s.metric_value for s in self.snapshots()], [800000, 806000])
        self.assertIn("+6,000 (+0.75%)", out)
        self.assertIn("Daily Change          +6,000 (+0.75%)", out)

    def test_duplicate_slot_is_not_stored_and_not_overwritten(self):
        self.run_main([FakeResponse(200, ok_body(812345))])
        code, out, _, _ = self.run_main([FakeResponse(200, ok_body(812999))], at=RUN_TIME + timedelta(minutes=10))
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual([s.metric_value for s in self.snapshots()], [812345])
        self.assertIn("SKIPPED (DUPLICATE_SLOT", out)
        html = self.report_html()
        self.assertIn("812,999", html)
        self.assertNotIn("DUPLICATE_SLOT", html)
        self.assertIn("History: Already captured this hour", html)

    def test_failed_retrieval_creates_no_snapshot(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                code, _, logs, posts = self.run_main([FakeResponse(status, '{"error": "denied"}')])
                self.assertEqual(code, app.EXIT_FAILURE)
                self.assertEqual(len(posts), 1)
                self.assertEqual(self.snapshots(), [])
                html = self.report_html()
                self.assertIn("NOT AVAILABLE", html)
                self.assertNotIn("NO HISTORY", html)
                self.assertNotIn("class=\"badge badge-live\"", html)
                self.assert_no_secrets_anywhere(logs)
        self.assertTrue(all(status == "FAILED" for status, _ in self.runs()))

    def test_parse_failure_creates_no_snapshot(self):
        code, _, _, _ = self.run_main([FakeResponse(200, '{"values": [{"data": "oops"}, {"data": 1}]}')])
        self.assertEqual(code, app.EXIT_FAILURE)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.runs(), [("FAILED", "ResponseValidationError")])

    def test_failure_after_success_reports_stale(self):
        self.run_main([FakeResponse(200, ok_body(812345))])
        code, _, _, _ = self.run_main([FakeResponse(401, "{}")], at=RUN_TIME + timedelta(hours=2))
        self.assertEqual(code, app.EXIT_FAILURE)
        html = self.report_html()
        self.assertIn("class=\"badge badge-stale\"", html)
        self.assertNotIn("class=\"badge badge-live\"", html)
        self.assertIn("812,345", html)
        self.assertEqual(len(self.snapshots()), 1)

    def test_diagnostics_only_touches_no_history_or_report(self):
        code, _, _, posts = self.run_main([FakeResponse(200, ok_body(1))], diagnostics_only=True)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(posts, [])
        self.assertFalse(self.db.exists())
        self.assertFalse(self.reports.exists())


if __name__ == "__main__":
    unittest.main()
