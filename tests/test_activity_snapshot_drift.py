"""PY2026 activity snapshot drift: transition SQL on synthetic rows (SQLite, no network, no SQL Server)."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import date
from pathlib import Path

from comparison.currently_enrolled import ComparisonOutputError, guard_no_pii
from database.comparison.activity_snapshot_drift import (
    COMPARABLE_CANDIDATE,
    DIAGNOSTIC_ONLY,
    IMMUTABLE_EVENT_LIKE,
    INSUFFICIENT_EVIDENCE,
    MIXED,
    MUTABLE_LATEST_STATE,
    NOT_RECONSTRUCTABLE,
    build_report,
    build_transition_sql,
    collect,
    consecutive_pairs,
    metric_decision,
    print_summary,
    render_html,
    summarize_transition,
    write_report,
)
from database.models import QueryResult
from database.query_runner import assert_read_only

A_TABLE = "Enrollments_PY2526_01312026"
B_TABLE = "Enrollments_PY2526_02272026"
ID_BASE = 987654300
WINDOW = (date(2025, 11, 2), date(2026, 1, 31))
COLUMN_TYPES = {
    "enrollment_id": "bigint",
    "enrollee_id": "bigint",
    "enrollment_status_description": "varchar",
    "enrollment_last_update_date": "date",
    "coverage_year": "int",
}
COLUMNS = {name: {"name": name, "data_type": dtype} for name, dtype in COLUMN_TYPES.items()}


def pid(n):
    return ID_BASE + n


# (policy, status, date, coverage_year) per snapshot. Policy 1 has two member rows.
A_ROWS = [
    *[(pid(n), "Cancelled", "2026-01-10", 2026) for n in range(1, 6)],
    (pid(1), "Cancelled", "2026-01-10", 2026),
    (pid(6), "Cancelled", "2026-01-11", 2026),
    (pid(7), "Cancelled", "2026-01-11", 2026),
    (pid(8), "Cancelled", "2026-01-12", 2026),
    (pid(9), "Cancelled", "2026-01-13", 2026),
    (pid(10), "Cancelled", "2026-01-14", 2026),
    (pid(11), "Cancelled", "2026-01-14", 2026),
    (pid(13), "Enrolled", "2026-01-05", 2026),
    (pid(14), "Cancelled", "2026-01-10", 2025),
]
B_ROWS = [
    *[(pid(n), "Cancelled", "2026-01-10", 2026) for n in range(1, 6)],
    (pid(1), "Cancelled", "2026-01-10", 2026),
    (pid(6), "Cancelled", "2026-02-20", 2026),
    (pid(7), "Cancelled", "2026-02-20", 2026),
    (pid(8), "Terminated", "2026-01-12", 2026),
    (pid(9), "Enrolled", "2026-02-25", 2026),
    (pid(12), "Cancelled", "2026-01-15", 2026),
    (pid(13), "Cancelled", "2026-01-16", 2026),
    (pid(14), "Cancelled", "2026-01-10", 2025),
]


def to_sqlite(sql: str) -> str:
    sql = re.sub(r"CAST\((\[[A-Za-z_]+\]) AS date\)", r"date(\1)", sql)
    return sql.replace("COUNT_BIG(", "COUNT(")


def database(tables):
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS dbo")
    for name, rows in tables.items():
        connection.execute(
            f"CREATE TABLE dbo.[{name}] (enrollment_id INTEGER, enrollee_id INTEGER, "
            "enrollment_status_description TEXT, enrollment_last_update_date TEXT, coverage_year INTEGER)"
        )
        connection.executemany(
            f"INSERT INTO dbo.[{name}] VALUES (?, ?, ?, ?, ?)",
            [(key, index, status, day, year) for index, (key, status, day, year) in enumerate(rows)],
        )
    return connection


def transition(connection, family, a=A_TABLE, b=B_TABLE):
    sql, params = build_transition_sql(f"[dbo].[{a}]", COLUMNS, f"[dbo].[{b}]", COLUMNS,
                                       family=family, window_start=WINDOW[0], window_end=WINDOW[1])
    cursor = connection.execute(to_sqlite(sql), params)
    names = [item[0] for item in cursor.description]
    return dict(zip(names, cursor.fetchone()))


class FakeClient:
    def __init__(self, connection, tables):
        self.connection = connection
        self.tables = tables

    def execute_query(self, sql, params=None):
        assert_read_only(sql)
        if "2027" in sql or (params and 2027 in params) or "UNION" in sql.upper():
            raise AssertionError(sql)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return self._result(("table_schema", "table_name", "table_type"),
                                [("dbo", name, "BASE TABLE") for name in self.tables])
        if "ORDINAL_POSITION" in sql:
            return self._result(("column_name", "data_type"), list(COLUMN_TYPES.items()))
        cursor = self.connection.execute(to_sqlite(sql), params or ())
        return self._result([item[0] for item in cursor.description], cursor.fetchall())

    @staticmethod
    def _result(columns, rows):
        rows = tuple(tuple(row) for row in rows)
        return QueryResult(columns=tuple(columns), rows=rows, row_count=len(rows), duration_seconds=0.0)


def counts(a, same, moved=0, status=0, both=0, gone=0, b=None, both_sets=None, b_only=0, b_new=0):
    return {
        "a_policies": a, "b_policies": a if b is None else b,
        "intersection_policies": same if both_sets is None else both_sets,
        "b_only_policies": b_only, "b_only_new_policies": b_new,
        "disappeared": gone, "same_date_status": same, "moved_date": moved,
        "changed_status": status, "both_changed": both, "moved_later": moved, "moved_earlier": 0,
        "left_family": status + both, "null_in_b": 0, "ambiguous_policies": 0,
    }


class TransitionSqlTests(unittest.TestCase):
    def test_sql_is_read_only_pairwise_and_py2026(self):
        sql, params = build_transition_sql("[dbo].[A]", COLUMNS, "[dbo].[B]", COLUMNS, family="Terminated",
                                           window_start=WINDOW[0], window_end=WINDOW[1])
        assert_read_only(sql)
        self.assertNotIn("UNION", sql.upper())
        self.assertNotIn("2027", sql)
        self.assertNotIn(2027, params)
        self.assertEqual(params, ("Term%", 2026, "Term%", 2026, "2025-11-02", "2026-01-31", "2025-11-02", "2026-01-31"))
        final_select = sql.rsplit("\nSELECT ", 1)[1]
        self.assertNotIn("k,", final_select)
        self.assertNotIn("enrollment_id", final_select)

    def test_transition_categories_on_synthetic_rows(self):
        with closing(database({A_TABLE: A_ROWS, B_TABLE: B_ROWS})) as connection:
            row = transition(connection, "Cancelled")
        self.assertEqual(row, {
            "a_policies": 11, "b_policies": 7, "intersection_policies": 5,
            "b_only_policies": 2, "b_only_new_policies": 1,
            "disappeared": 2, "same_date_status": 5, "moved_date": 2, "changed_status": 1,
            "both_changed": 1, "moved_later": 3, "moved_earlier": 0, "left_family": 2,
            "null_in_b": 0, "ambiguous_policies": 0,
        })
        summary = summarize_transition(row)
        self.assertEqual(summary["counts"]["a_only_policies"], 6)
        self.assertEqual(summary["counts"]["present_in_b"], 9)
        self.assertEqual(summary["counts"]["b_moved_in_policies"], 1)
        self.assertEqual(summary["shares"]["retention"], round(5 / 11, 6))
        self.assertEqual(summary["classification"], INSUFFICIENT_EVIDENCE)

    def test_immutable_history_keeps_every_policy_in_place(self):
        with closing(database({A_TABLE: A_ROWS, B_TABLE: A_ROWS})) as connection:
            row = transition(connection, "Cancelled")
        self.assertEqual((row["a_policies"], row["same_date_status"], row["intersection_policies"]), (11, 11, 11))
        self.assertEqual(row["disappeared"] + row["moved_date"] + row["changed_status"] + row["both_changed"], 0)

    def test_family_with_no_earlier_policies_is_not_zero_retention(self):
        with closing(database({A_TABLE: A_ROWS, B_TABLE: B_ROWS})) as connection:
            row = transition(connection, "Terminated")
        self.assertEqual((row["a_policies"], row["b_policies"], row["b_only_policies"], row["b_only_new_policies"]),
                         (0, 1, 1, 0))
        summary = summarize_transition(row)
        self.assertIsNone(summary["shares"]["retention"])
        self.assertEqual(summary["classification"], INSUFFICIENT_EVIDENCE)


class ClassificationTests(unittest.TestCase):
    def test_immutable_event_like(self):
        self.assertEqual(summarize_transition(counts(1000, 990, moved=10))["classification"], IMMUTABLE_EVENT_LIKE)

    def test_moved_date_is_mutable_state(self):
        summary = summarize_transition(counts(1000, 50, moved=950))
        self.assertEqual(summary["classification"], MUTABLE_LATEST_STATE)
        self.assertEqual(summary["dominant_change"], "moved_date")

    def test_changed_status_is_mutable_state(self):
        summary = summarize_transition(counts(1000, 100, status=900))
        self.assertEqual(summary["classification"], MUTABLE_LATEST_STATE)
        self.assertEqual(summary["dominant_change"], "changed_status")

    def test_both_changed_is_mutable_state(self):
        summary = summarize_transition(counts(1000, 0, both=1000))
        self.assertEqual(summary["classification"], MUTABLE_LATEST_STATE)
        self.assertEqual(summary["dominant_change"], "both_changed")

    def test_disappeared_policies_are_not_event_history(self):
        summary = summarize_transition(counts(1000, 1, gone=999))
        self.assertEqual(summary["classification"], MUTABLE_LATEST_STATE)
        self.assertEqual(summary["dominant_change"], "disappeared")

    def test_newly_appearing_backdated_policies_block_immutable(self):
        moved_in = counts(1000, 990, moved=10, b=1100, b_only=100, b_new=0)
        self.assertEqual(summarize_transition(moved_in)["classification"], MIXED)
        late_new = counts(1000, 990, moved=10, b=1100, b_only=100, b_new=100)
        self.assertEqual(summarize_transition(late_new)["classification"], IMMUTABLE_EVENT_LIKE)

    def test_partial_preservation_is_mixed(self):
        self.assertEqual(summarize_transition(counts(1000, 700, moved=300))["classification"], MIXED)

    def test_na_is_never_zero(self):
        missing = summarize_transition(None)
        self.assertEqual(missing["classification"], INSUFFICIENT_EVIDENCE)
        self.assertIsNone(missing["counts"]["a_policies"])
        self.assertIsNone(missing["shares"]["retention"])
        zero = summarize_transition(counts(0, 0))
        self.assertEqual(zero["counts"]["a_policies"], 0)
        self.assertIsNone(zero["shares"]["retention"])
        report = build_report({"rows": [{
            "earlier": A_TABLE, "later": B_TABLE, "earlier_date": "2026-01-31", "later_date": "2026-02-27",
            "lag_days": 27, "window_start": "2025-11-02", "window_end_exclusive": "2026-01-31",
            "status_family": "Terminated", "counts": None,
        }]})
        self.assertIn("N/A", render_html(report))
        self.assertIsNone(report["pairs"][0]["counts"]["a_policies"])

    def test_metric_decisions(self):
        def pair(label):
            return {"classification": label}
        self.assertEqual(metric_decision([pair(IMMUTABLE_EVENT_LIKE)] * 2)["decision"], COMPARABLE_CANDIDATE)
        self.assertEqual(metric_decision([pair(MUTABLE_LATEST_STATE)] * 2)["decision"], NOT_RECONSTRUCTABLE)
        self.assertEqual(metric_decision([pair(IMMUTABLE_EVENT_LIKE), pair(MUTABLE_LATEST_STATE)])["decision"],
                         DIAGNOSTIC_ONLY)
        self.assertEqual(metric_decision([pair(INSUFFICIENT_EVIDENCE)])["decision"], DIAGNOSTIC_ONLY)
        self.assertFalse(metric_decision([pair(IMMUTABLE_EVENT_LIKE)])["production_comparable"])


class PipelineTests(unittest.TestCase):
    def test_pairs_use_dated_snapshots_only(self):
        tables = [{"schema_name": "dbo", "table": name} for name in (
            "Enrollments_PY2526_02272026", "Enrollments_PY2526_NOV30", "Enrollments_PY2526_01312026",
            "Enrollments_PY2526_06312026", "Enrollments_PY2526_DEC31", "Enrollments_PY2526_03312026",
        )]
        pairs, skipped = consecutive_pairs(tables)
        self.assertEqual([(a["table"][-8:], b["table"][-8:]) for a, b in pairs],
                         [("01312026", "02272026"), ("02272026", "03312026")])
        self.assertEqual({item["table"] for item in skipped}, {
            "Enrollments_PY2526_NOV30", "Enrollments_PY2526_DEC31", "Enrollments_PY2526_06312026"})

    def test_report_holds_aggregates_only(self):
        names = [A_TABLE, B_TABLE, "Enrollments_PY2526_NOV30"]
        with closing(database({A_TABLE: A_ROWS, B_TABLE: B_ROWS, names[2]: A_ROWS})) as connection:
            evidence = collect(FakeClient(connection, names))
        report = build_report(evidence)
        self.assertEqual([item["status_family"] for item in report["pairs"]], ["Cancelled", "Terminated"])
        self.assertEqual(report["pairs"][0]["counts"]["a_policies"], 11)
        self.assertEqual(report["pairs"][0]["counts"]["disappeared"], 2)
        self.assertEqual(report["skipped_tables"][0]["table"], "Enrollments_PY2526_NOV30")
        self.assertFalse(report["metrics"]["Cancelled"]["production_comparable"])
        output = io.StringIO()
        with redirect_stdout(output):
            print_summary(report)
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(report, Path(tmp))
            written = json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")
        for text in (json.dumps(report), output.getvalue(), written):
            self.assertNotIn(str(ID_BASE)[:7], text)
            for n in range(1, 15):
                self.assertNotIn(str(pid(n)), text)

    def test_identifier_keys_are_refused(self):
        with self.assertRaises(ComparisonOutputError):
            guard_no_pii({"pairs": [{"enrollment_id": 1}]})


if __name__ == "__main__":
    unittest.main()
