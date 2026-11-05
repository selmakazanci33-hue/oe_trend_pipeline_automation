import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from history.store import (
    RUN_FAILED,
    RUN_SUCCESS,
    WRITE_DUPLICATE_RUN,
    WRITE_DUPLICATE_SLOT,
    WRITE_SAVED,
    SnapshotRejected,
    SnapshotStore,
    snapshot_slot,
)
from tests.reporting_fixtures import METRIC_ID, T0, result

EXPECTED_SNAPSHOT_COLUMNS = {
    "id", "run_id", "metric_id", "metric_value", "plan_year", "retrieved_at_utc", "snapshot_slot_utc",
    "source", "dashboard", "retrieval_status", "created_at_utc",
}
EXPECTED_RUN_COLUMNS = {"run_id", "started_at_utc", "finished_at_utc", "status", "failure_type"}


class StoreTestCase(unittest.TestCase):
    granularity = "hour"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "h" / "oe.sqlite3"
        self.store = SnapshotStore(self.path, self.granularity, clock=lambda: T0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_with(self, run_id, res):
        self.store.start_run(run_id)
        return self.store.record_snapshot(run_id, res)


class SnapshotCreationTests(StoreTestCase):
    def test_saved_snapshot_fields(self):
        write = self.run_with("r1", result(812_000))
        self.assertEqual(write.status, WRITE_SAVED)
        s = write.snapshot
        self.assertEqual((s.metric_id, s.metric_value, s.plan_year), (METRIC_ID, 812_000, 2027))
        self.assertEqual(s.retrieved_at_utc, T0)
        self.assertEqual(s.snapshot_slot_utc, "2026-11-10T14:00:00Z")
        self.assertEqual((s.source, s.retrieval_status), ("Sisense MAIN", "VALIDATED"))
        self.assertEqual(self.store.snapshot_count(METRIC_ID, 2027), 1)

    def test_invalid_results_rejected(self):
        bad = [
            {"value": -1}, {"value": True}, {"value": float("nan")}, {"value": 12.5}, {"value": "812000"},
            {"metric": "total_applicants"}, {"plan_year": 2026},
        ]
        self.store.start_run("r1")
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(SnapshotRejected):
                self.store.record_snapshot("r1", replace(result(), **overrides))
        with self.assertRaises(SnapshotRejected):
            self.store.record_snapshot("r1", {"metric": METRIC_ID, "value": 1})
        self.assertEqual(self.store.snapshot_count(METRIC_ID, 2027), 0)

    def test_requires_active_run(self):
        with self.assertRaises(SnapshotRejected):
            self.store.record_snapshot("never-started", result())
        self.store.start_run("r1")
        self.store.finish_run("r1", RUN_FAILED, "AuthenticationError")
        with self.assertRaises(SnapshotRejected):
            self.store.record_snapshot("r1", result())
        self.assertEqual(self.store.snapshot_count(METRIC_ID, 2027), 0)

    def test_run_lifecycle(self):
        self.store.start_run("r1")
        self.store.finish_run("r1", RUN_SUCCESS)
        run = self.store.get_run("r1")
        self.assertEqual(run.status, RUN_SUCCESS)
        with self.assertRaises(ValueError):
            self.store.finish_run("r1", RUN_FAILED)

    def test_schema_has_no_authentication_columns(self):
        conn = sqlite3.connect(self.path)
        try:
            snap_cols = {row[1] for row in conn.execute("PRAGMA table_info(metric_snapshots)")}
            run_cols = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)")}
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(metric_snapshots)")}
        finally:
            conn.close()
        self.assertEqual(snap_cols, EXPECTED_SNAPSHOT_COLUMNS)
        self.assertEqual(run_cols, EXPECTED_RUN_COLUMNS)
        self.assertIn("idx_snapshots_metric_time", indexes)

    def test_retrieval_status_constraint(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("INSERT INTO pipeline_runs (run_id, started_at_utc, status) VALUES ('x', 't', 'RUNNING')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO metric_snapshots (run_id, metric_id, metric_value, plan_year, retrieved_at_utc, "
                    "snapshot_slot_utc, source, dashboard, retrieval_status, created_at_utc) "
                    "VALUES ('x', 'm', 1, 2027, 't', 's', 'src', 'd', 'FAILED', 't')"
                )
        finally:
            conn.close()


class DuplicateProtectionTests(StoreTestCase):
    def test_same_run_twice(self):
        self.run_with("r1", result(812_000))
        again = self.store.record_snapshot("r1", result(999_999, at=T0 + timedelta(hours=3)))
        self.assertEqual(again.status, WRITE_DUPLICATE_RUN)
        self.assertEqual(again.snapshot.metric_value, 812_000)
        self.assertEqual(self.store.snapshot_count(METRIC_ID, 2027), 1)

    def test_same_slot_keeps_first_value(self):
        self.run_with("r1", result(812_000, at=T0))
        write = self.run_with("r2", result(812_500, at=T0 + timedelta(minutes=20)))
        self.assertEqual(write.status, WRITE_DUPLICATE_SLOT)
        self.assertEqual(write.snapshot.metric_value, 812_000)
        values = [s.metric_value for s in self.store.snapshots(METRIC_ID, 2027)]
        self.assertEqual(values, [812_000])

    def test_next_slot_saved(self):
        self.run_with("r1", result(812_000, at=T0))
        write = self.run_with("r2", result(812_500, at=T0 + timedelta(hours=1)))
        self.assertEqual(write.status, WRITE_SAVED)
        self.assertEqual([s.metric_value for s in self.store.snapshots(METRIC_ID, 2027)], [812_000, 812_500])

    def test_reopen_preserves_history(self):
        self.run_with("r1", result(812_000))
        self.store.close()
        self.store = SnapshotStore(self.path, "hour", clock=lambda: T0)
        self.assertEqual(self.store.snapshot_count(METRIC_ID, 2027), 1)


class DailyGranularityTests(StoreTestCase):
    granularity = "day"

    def test_same_day_is_duplicate(self):
        self.run_with("r1", result(812_000, at=T0))
        self.assertEqual(self.run_with("r2", result(1, at=T0 + timedelta(hours=5))).status, WRITE_DUPLICATE_SLOT)
        self.assertEqual(self.run_with("r3", result(2, at=T0 + timedelta(days=1))).status, WRITE_SAVED)


class SlotTests(unittest.TestCase):
    def test_slots(self):
        self.assertEqual(snapshot_slot(T0, "hour"), "2026-11-10T14:00:00Z")
        self.assertEqual(snapshot_slot(T0, "day"), "2026-11-10T00:00:00Z")
        with self.assertRaises(ValueError):
            snapshot_slot(T0, "minute")


if __name__ == "__main__":
    unittest.main()
