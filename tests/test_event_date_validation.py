"""Event-date semantic validation. Fake query results only — no database, no Sisense."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.models import DIAGNOSTIC, NOT_COMPARABLE
from database.comparison.event_date_validation import (
    DIAGNOSTIC_LABEL,
    FIELD_EXISTS,
    FIELD_SEMANTICS_SUPPORTED,
    FULL_METRIC_COMPARABLE,
    NOT_SUPPORTED,
    SUPPORTED,
    UNRESOLVED,
    build_cross_snapshot_sql,
    build_daily_distribution_sql,
    build_identifier_date_cardinality_sql,
    build_report,
    build_snapshot_profile_sql,
    build_snapshot_relative_sql,
    build_status_family_sql,
    cancelled_policies_checks,
    collect_evidence,
    decide_event_date_semantics,
    snapshot_date_context,
    terminated_policies_checks,
    write_report,
)
from database.comparison.field_mapping import select_event_date_candidate
from database.models import QueryResult
from database.query_runner import assert_read_only

PROJECT = Path(__file__).resolve().parents[1]

LIVE_COLUMNS = {
    "coverage_year": "int",
    "enrollment_id": "bigint",
    "enrollment_status_description": "varchar",
    "enrollment_last_update_date": "datetime2",
    "ENROLLEE_ID_PROXY": "varchar",
}

TABLES = [
    ("dbo", "Enrollments_PY2526_NOV30", "BASE TABLE"),
    ("dbo", "Enrollments_PY2526_DEC31", "BASE TABLE"),
    ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
    ("dbo", "Enrollments_PY2526_02272026", "BASE TABLE"),
]


def result(columns, rows):
    materialized = tuple(tuple(row) for row in rows)
    return QueryResult(
        columns=tuple(columns), rows=materialized,
        row_count=len(materialized), duration_seconds=0.0,
    )


def columns_meta(extra=None):
    data = {
        "enrollment_id": {"name": "enrollment_id", "data_type": "bigint"},
        "enrollment_status_description": {
            "name": "enrollment_status_description", "data_type": "varchar",
        },
        "enrollment_last_update_date": {
            "name": "enrollment_last_update_date", "data_type": "datetime2",
        },
        "coverage_year": {"name": "coverage_year", "data_type": "int"},
    }
    if extra:
        data.update(extra)
    return data


class FakeClient:
    def __init__(self, tables=None, columns=None):
        self.tables = tables or list(TABLES)
        self.columns = columns or {table: dict(LIVE_COLUMNS) for _, table, _ in self.tables}
        self.calls = []

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        assert_read_only(sql)
        if "2027" in sql or (params and 2027 in params):
            raise AssertionError((sql, params))
        if "LIKE 'Enrollments[_]PY2526%'" in sql:
            return result(("table_schema", "table_name", "table_type"), self.tables)
        if "ORDINAL_POSITION" in sql:
            table = params[1]
            return result(("column_name", "data_type"), list(self.columns[table].items()))
        if "AS present_in_both" in sql:
            if params and "Cancelled" in params:
                return result(
                    ("present_in_both", "only_earlier_snapshot", "only_later_snapshot"),
                    [(Decimal("5"), Decimal("1"), Decimal("2"))],
                )
            if params and "Term%" in params:
                return result(
                    ("present_in_both", "only_earlier_snapshot", "only_later_snapshot"),
                    [(Decimal("3"), Decimal("0"), Decimal("1"))],
                )
            return result(
                ("present_in_both", "only_earlier_snapshot", "only_later_snapshot"),
                [(Decimal("8"), Decimal("2"), Decimal("4"))],
            )
        if "AS one_date_enrollment_keys" in sql:
            return result(
                ("one_date_enrollment_keys", "multi_date_enrollment_keys",
                 "no_date_enrollment_keys", "enrollment_keys_in_family"),
                [(Decimal("10"), Decimal("2"), Decimal("1"), Decimal("13"))],
            )
        if "AS event_dates_after_snapshot" in sql:
            return result(
                ("event_dates_after_snapshot", "event_dates_before_snapshot",
                 "event_dates_on_snapshot",
                 "event_dates_more_than_lookback_days_before_snapshot"),
                [(Decimal("0"), Decimal("4"), Decimal("1"), Decimal("2"))],
            )
        if "AS event_calendar_date" in sql:
            family = "Cancelled" if params and params[0] == "Cancelled" else "Terminated"
            return result(
                ("event_calendar_date", "status_family",
                 "diagnostic_row_count", "diagnostic_distinct_enrollment_keys"),
                [(date(2025, 11, 15), family, Decimal("3"), Decimal("2"))],
            )
        if "AS family_row_count" in sql:
            return result(
                ("family_row_count", "family_distinct_enrollment_keys",
                 "family_non_null_event_dates", "family_null_event_dates",
                 "family_min_event_date", "family_max_event_date",
                 "family_distinct_event_dates"),
                [(Decimal("6"), Decimal("5"), Decimal("5"), Decimal("1"),
                  date(2025, 10, 1), date(2025, 11, 30), Decimal("4"))],
            )
        if "AS total_rows" in sql and "AS non_null_event_dates" in sql:
            return result(
                ("total_rows", "non_null_enrollment_keys", "distinct_enrollment_keys",
                 "non_null_event_dates", "null_event_dates",
                 "min_event_date", "max_event_date"),
                [(Decimal("20"), Decimal("20"), Decimal("18"), Decimal("17"),
                  Decimal("3"), date(2025, 10, 1), date(2025, 11, 30))],
            )
        raise AssertionError(sql)


class EventDateValidationTests(unittest.TestCase):
    def test_identifier_is_refused_as_event_column(self):
        columns = columns_meta()
        with self.assertRaises(ValueError):
            build_snapshot_profile_sql(
                "[dbo].[Enrollments_PY2526_01312026]", columns,
                "enrollment_id", "datetime2",
            )
        selected = select_event_date_candidate(columns)
        self.assertNotEqual(selected["py2026_candidate"], "enrollment_id")

    def test_non_datetime_type_is_not_selected_or_queried(self):
        columns = columns_meta({
            "enrollment_last_update_date": {
                "name": "enrollment_last_update_date", "data_type": "varchar",
            },
        })
        selected = select_event_date_candidate(columns)
        self.assertIsNone(selected["py2026_candidate"])
        with self.assertRaises(ValueError):
            build_status_family_sql(
                "[dbo].[Enrollments_PY2526_01312026]", columns,
                "enrollment_last_update_date", "varchar", cancelled=True,
            )

    def test_sql_is_read_only_single_snapshot_and_py2026(self):
        columns = columns_meta()
        qualified = "[dbo].[Enrollments_PY2526_01312026]"
        builders = [
            build_snapshot_profile_sql(
                qualified, columns, "enrollment_last_update_date", "datetime2"),
            build_status_family_sql(
                qualified, columns, "enrollment_last_update_date", "datetime2",
                cancelled=True),
            build_status_family_sql(
                qualified, columns, "enrollment_last_update_date", "datetime2",
                cancelled=False),
            build_identifier_date_cardinality_sql(
                qualified, columns, "enrollment_last_update_date", "datetime2",
                cancelled=True),
            build_snapshot_relative_sql(
                qualified, columns, "enrollment_last_update_date", "datetime2",
                cancelled=True, snapshot_date="2026-01-31"),
            build_daily_distribution_sql(
                qualified, columns, "enrollment_last_update_date", "datetime2"),
            build_cross_snapshot_sql(
                qualified, "[dbo].[Enrollments_PY2526_02272026]", columns,
                "enrollment_last_update_date", "datetime2", mode="cancelled"),
        ]
        for sql, params in builders:
            assert_read_only(sql)
            self.assertNotIn("UNION", sql.upper())
            self.assertNotIn(2027, params)
            self.assertIn(2026, params)
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")))

    def test_month_day_token_does_not_invent_a_year(self):
        nov = snapshot_date_context("Enrollments_PY2526_NOV30")
        self.assertIsNone(nov["snapshot_date"])
        self.assertEqual(nov["relative_date_comparison"], "SKIPPED_NO_PROVEN_SNAPSHOT_DATE")
        jan = snapshot_date_context("Enrollments_PY2526_01312026")
        self.assertEqual(jan["snapshot_date"], "2026-01-31")

    def test_dates_do_not_auto_support_event_semantics(self):
        candidate = {
            "py2026_candidate": "enrollment_last_update_date",
            "sql_datatype": "datetime2",
            "datetime_compatible": True,
            "confidence": "NAME_SIMILAR_UNPROVEN",
        }
        evidence = decide_event_date_semantics(candidate, {
            "null_rate": 0.0,
            "date_range": {"min": "2025-10-01", "max": "2025-11-30"},
            "cancelled_coverage": 1.0,
            "terminated_coverage": 1.0,
            "multi_date_identifier_rate": 0.0,
            "future_date_anomaly_count": 0,
            "cross_snapshot_repeat_rate": 0.0,
        })
        self.assertEqual(evidence["decision"], UNRESOLVED)
        self.assertNotEqual(evidence["decision"], SUPPORTED)
        self.assertFalse(evidence["states"][FIELD_SEMANTICS_SUPPORTED])
        self.assertFalse(evidence["states"][FULL_METRIC_COMPARABLE])
        self.assertTrue(evidence["states"][FIELD_EXISTS])

    def test_absent_candidate_is_not_supported(self):
        evidence = decide_event_date_semantics(
            {"py2026_candidate": None, "sql_datatype": None, "datetime_compatible": False},
            {},
        )
        self.assertEqual(evidence["decision"], NOT_SUPPORTED)

    def test_collect_evidence_does_not_union_or_query_2027(self):
        client = FakeClient()
        evidence = collect_evidence(client)
        report = build_report(evidence)
        self.assertEqual(report["label"], DIAGNOSTIC_LABEL)
        self.assertTrue(report["not_official_py2026_cancellation_activity"])
        self.assertEqual(report["event_date_evidence"]["decision"], UNRESOLVED)
        self.assertFalse(report["states"][FULL_METRIC_COMPARABLE])
        self.assertEqual(report["comparable"], [])
        self.assertEqual(report["cancelled_policies"]["comparison_status"], DIAGNOSTIC)
        self.assertEqual(report["terminated_policies"]["comparison_status"], DIAGNOSTIC)
        self.assertFalse(report["cancelled_policies"]["full_metric_comparable"])
        self.assertEqual(report["out_of_scope_this_phase"]["currently_enrolled_people"], "unchanged")
        self.assertEqual(report["out_of_scope_this_phase"]["new_active_passive"], "unchanged")
        self.assertEqual(report["out_of_scope_this_phase"]["applications"], "unchanged")
        self.assertEqual(report["out_of_scope_this_phase"]["pending_people"], "unchanged")
        for sql, params in client.calls:
            self.assertNotIn("UNION", sql.upper())
            self.assertNotIn(2027, params or ())
        relative = [sql for sql, _params in client.calls if "AS event_dates_after_snapshot" in sql]
        self.assertTrue(relative)
        self.assertTrue(all("01312026" in sql or "02272026" in sql for sql in relative))
        self.assertFalse(any("NOV30" in sql or "DEC31" in sql for sql in relative))
        dumped = json.dumps(report)
        self.assertNotIn("secret-person", dumped)
        self._assert_keys(report)

    def test_cross_snapshot_is_not_activity(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        pair = next(
            item for item in report["cross_snapshot"]
            if item.get("earlier") == "Enrollments_PY2526_NOV30"
        )
        all_mode = next(item for item in pair["modes"] if item["mode"] == "all")
        self.assertEqual(all_mode["present_in_both"], 8)
        self.assertEqual(all_mode["only_earlier_snapshot"], 2)
        self.assertEqual(all_mode["only_later_snapshot"], 4)
        self.assertTrue(all_mode["not_activity"])
        self.assertTrue(report["activity_extractor"]["must_use_one_snapshot_unless_dedup_proven"])
        self.assertTrue(report["activity_extractor"]["enrollment_last_update_date_not_auto_promoted"])

    def test_daily_distribution_is_labeled_diagnostic(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        daily = report["daily_distribution"]
        self.assertEqual(daily["label"], DIAGNOSTIC_LABEL)
        self.assertTrue(daily["not_official_activity"])
        self.assertTrue(all(item["label"] == DIAGNOSTIC_LABEL for item in daily["rows"] if item.get("event_calendar_date")))

    def test_cancelled_and_terminated_checks_are_independent(self):
        cancelled = cancelled_policies_checks(
            identifier_present=True, cancelled_observed=True,
            event_decision=UNRESOLVED, coverage_year_present=True,
        )
        terminated = terminated_policies_checks(
            identifier_present=True, term_observed=False,
            event_decision=UNRESOLVED, coverage_year_present=True,
        )
        by_cancelled = {item["check"]: item["status"] for item in cancelled}
        by_terminated = {item["check"]: item["status"] for item in terminated}
        self.assertEqual(by_cancelled["status_cancelled"], "PASS")
        self.assertEqual(by_cancelled["event_date_semantics"], "UNRESOLVED")
        self.assertEqual(by_terminated["status_term_prefix"], "UNRESOLVED")
        self.assertNotEqual(by_cancelled["status_cancelled"], by_terminated["status_term_prefix"])
        self.assertTrue(all(item["status"] != "COMPARABLE" for item in cancelled + terminated))

    def test_supported_event_date_still_does_not_make_metric_comparable(self):
        cancelled = cancelled_policies_checks(
            identifier_present=True, cancelled_observed=True,
            event_decision=SUPPORTED, coverage_year_present=True,
        )
        event = next(item for item in cancelled if item["check"] == "event_date_semantics")
        self.assertEqual(event["status"], "PASS")
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertNotEqual(report["cancelled_policies"]["comparison_status"], "COMPARABLE")
        self.assertNotEqual(report["terminated_policies"]["comparison_status"], "COMPARABLE")
        self.assertFalse(report["states"][FULL_METRIC_COMPARABLE])

    def test_write_report_distinguishes_three_states(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            page = html_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["report_kind"], "PY2026_EVENT_DATE_VALIDATION")
        self.assertEqual(json_path.name, "py2026_event_date_validation.json")
        self.assertEqual(html_path.name, "py2026_event_date_validation.html")
        for heading in (
            "Three different states",
            FIELD_EXISTS,
            FIELD_SEMANTICS_SUPPORTED,
            FULL_METRIC_COMPARABLE,
            "Cancelled Policies checks",
            "Terminated Policies checks",
            DIAGNOSTIC_LABEL,
        ):
            self.assertIn(heading, page)
        self.assertNotIn("<canvas", page.lower())
        self.assertNotIn("chart", page.lower())

    def test_missing_identifier_is_not_comparable(self):
        checks = cancelled_policies_checks(
            identifier_present=False, cancelled_observed=False,
            event_decision=NOT_SUPPORTED, coverage_year_present=False,
        )
        by_name = {item["check"]: item["status"] for item in checks}
        self.assertEqual(by_name["enrollment_identifier"], "FAIL")
        self.assertEqual(by_name["event_date_semantics"], "FAIL")
        self.assertEqual(NOT_COMPARABLE, NOT_COMPARABLE)

    def test_command_module_does_not_change_main_or_catalog(self):
        main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("database.comparison.event_date_validation", main_source)
        self.assertNotIn("database.comparison.field_mapping", main_source)

    def _assert_keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id", "exchg_indiv_identifier"})
                self._assert_keys(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_keys(item)


if __name__ == "__main__":
    unittest.main()
