"""Activity semantic bridge. Fake query results only — no database, no Sisense."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.models import DIAGNOSTIC, NOT_COMPARABLE
from database.comparison.activity_semantic_bridge import (
    DIAGNOSTIC_LABEL,
    NOT_SUPPORTED,
    PLUS_ONE_LABEL,
    SUPPORTED,
    UNRESOLVED,
    WIDGET_OWN,
    WIDGET_TITLE,
    build_count_daily_sql,
    build_count_family_sql,
    build_coverage_year_sql,
    build_plus_one_sql,
    build_report,
    build_status_value_sql,
    cancelled_policies_checks,
    classify_activity_filters,
    collect_evidence,
    decide_coverage_year,
    duplicate_factor,
    evaluate_strategies,
    label_plus_one_pattern,
    metric_from_checks,
    write_report,
)
from database.models import QueryResult
from database.query_runner import assert_read_only

PROJECT = Path(__file__).resolve().parents[1]

LIVE_COLUMNS = {
    "coverage_year": "int",
    "enrollment_id": "bigint",
    "enrollment_status_description": "varchar",
    "enrollment_last_update_date": "date",
    "enrollment_create_date": "date",
    "enrollment_confirmation_date": "date",
    "benefit_effective_date": "date",
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


def columns_meta():
    return {
        "enrollment_id": {"name": "enrollment_id", "data_type": "bigint"},
        "enrollment_status_description": {
            "name": "enrollment_status_description", "data_type": "varchar",
        },
        "enrollment_last_update_date": {
            "name": "enrollment_last_update_date", "data_type": "date",
        },
        "coverage_year": {"name": "coverage_year", "data_type": "int"},
    }


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
            return result(("column_name", "data_type"), list(self.columns[params[1]].items()))
        if "AS event_calendar_date" in sql:
            return result(
                ("event_calendar_date", "count_star", "count_enrollment_ids",
                 "count_distinct_enrollment_keys"),
                [(date(2025, 11, 15), Decimal("3"), Decimal("3"), Decimal("3"))],
            )
        if "AS count_star" in sql:
            return result(
                ("count_star", "count_enrollment_ids", "count_distinct_enrollment_keys"),
                [(Decimal("6"), Decimal("6"), Decimal("5"))],
            )
        if "AS observed_status_value" in sql:
            return result(
                ("observed_status_value", "status_row_count", "status_distinct_enrollment_keys"),
                [("Cancelled", Decimal("4"), Decimal("4")),
                 ("Terminated", Decimal("2"), Decimal("2")),
                 ("Termination Pending", Decimal("1"), Decimal("1"))],
            )
        if "AS observed_coverage_year" in sql:
            return result(
                ("observed_coverage_year", "coverage_year_row_count",
                 "cancelled_row_count", "terminated_row_count"),
                [(Decimal("2026"), Decimal("20"), Decimal("6"), Decimal("3"))],
            )
        if "AS event_on_snapshot_plus_one" in sql:
            return result(
                ("event_before_snapshot", "event_on_snapshot",
                 "event_on_snapshot_plus_one", "event_after_snapshot_plus_one"),
                [(Decimal("2"), Decimal("4"), Decimal("1"), Decimal("0"))],
            )
        if "AS date_field_min" in sql:
            return result(
                ("date_field_min", "date_field_max", "date_field_nulls",
                 "date_field_rows", "date_field_distinct_dates"),
                [(date(2025, 10, 1), date(2026, 2, 1), Decimal("0"), Decimal("6"), Decimal("5"))],
            )
        if "AS same_calendar_date_count" in sql:
            return result(
                ("same_calendar_date_count", "candidate_after_other",
                 "candidate_before_other", "other_null_when_candidate_present"),
                [(Decimal("1"), Decimal("4"), Decimal("1"), Decimal("0"))],
            )
        raise AssertionError(sql)


class SemanticBridgeTests(unittest.TestCase):
    def test_count_sql_is_not_distinct_and_not_unioned(self):
        columns = columns_meta()
        qualified = "[dbo].[Enrollments_PY2526_01312026]"
        family_sql, family_params = build_count_family_sql(
            qualified, columns, "enrollment_last_update_date", "date", cancelled=True)
        daily_sql, daily_params = build_count_daily_sql(
            qualified, columns, "enrollment_last_update_date", "date", cancelled=False)
        for sql, params in ((family_sql, family_params), (daily_sql, daily_params)):
            assert_read_only(sql)
            self.assertIn("COUNT(", sql)
            self.assertIn("COUNT(DISTINCT", sql)
            self.assertIn("COUNT_BIG(*)", sql)
            self.assertNotIn("UNION", sql.upper())
            self.assertIn(2026, params)
            self.assertNotIn(2027, params)
        self.assertEqual(family_params[1] if len(family_params) > 1 else family_params[0], "Cancelled")
        self.assertEqual(daily_params[-1] if "Term%" in daily_params else daily_params[0], "Term%")

    def test_duplicate_factor_does_not_silently_deduplicate(self):
        self.assertEqual(duplicate_factor(6, 5), 1.2)
        self.assertIsNone(duplicate_factor(6, 0))
        self.assertEqual(duplicate_factor(4, 4), 1)

    def test_status_sql_lists_term_prefix_not_only_terminated(self):
        sql, params = build_status_value_sql(
            "[dbo].[Enrollments_PY2526_01312026]", columns_meta())
        assert_read_only(sql)
        self.assertIn("Term%", params)
        self.assertIn("Cancelled", params)
        self.assertNotIn("UNION", sql.upper())
        self.assertNotIn(2027, params)

    def test_coverage_year_is_not_equivalent_by_name(self):
        decision = decide_coverage_year([
            {"table": "Enrollments_PY2526_01312026", "observed_coverage_year": 2026},
        ])
        self.assertEqual(decision["decision"], UNRESOLVED)
        self.assertIn("not semantic equivalence", decision["reason"])
        missing = decide_coverage_year([
            {"table": "Enrollments_PY2526_01312026", "observed_coverage_year": 2025},
        ])
        self.assertEqual(missing["decision"], NOT_SUPPORTED)

    def test_currently_enrolled_filters_are_not_copied(self):
        inventory = classify_activity_filters(None, columns_meta())
        roles = {item["role"]: item for item in inventory["inventory"]}
        self.assertEqual(roles["measure_identifier"]["classification"], WIDGET_OWN)
        self.assertEqual(roles["cancelled_measure_filter"]["classification"], WIDGET_OWN)
        self.assertIn("person_type_description", inventory["currently_enrolled_filters_not_copied"])
        self.assertIn("benefit_effective_date", inventory["currently_enrolled_filters_not_copied"])
        self.assertTrue(all(
            item.get("classification") == UNRESOLVED
            for item in inventory["dashboard_filters_unresolved_without_activity_jaql"]
        ))
        self.assertFalse(any(
            "benefit_effective_date" in str(item.get("sisense_field"))
            and item.get("classification") == WIDGET_OWN
            for item in inventory["inventory"]
        ))

    def test_plus_one_pattern_is_not_timezone_proof(self):
        sql, params = build_plus_one_sql(
            "[dbo].[Enrollments_PY2526_01312026]", columns_meta(),
            "enrollment_last_update_date", "date",
            mode="cancelled", snapshot_date="2026-01-31",
        )
        assert_read_only(sql)
        self.assertIn("DATEADD", sql.upper())
        self.assertNotIn("UNION", sql.upper())
        self.assertNotIn(2027, params)
        label = label_plus_one_pattern([
            {"slice": "ALL", "event_on_snapshot_plus_one": 2, "event_after_snapshot_plus_one": 0},
            {"slice": "ALL", "event_on_snapshot_plus_one": 1, "event_after_snapshot_plus_one": 0},
        ])
        self.assertEqual(label, PLUS_ONE_LABEL)
        self.assertIsNone(label_plus_one_pattern([
            {"slice": "ALL", "event_on_snapshot_plus_one": 2, "event_after_snapshot_plus_one": 3},
        ]))

    def test_strategies_are_not_activated(self):
        strategies = evaluate_strategies(
            [{"grain": "family", "duplicate_factor": 1.2}],
            [{"slice": "ALL", "event_on_snapshot_plus_one": 1, "event_after_snapshot_plus_one": 0}],
            {"decision": UNRESOLVED},
        )
        by_id = {item.get("strategy"): item for item in strategies if item.get("strategy")}
        self.assertEqual(by_id["A"]["decision"], UNRESOLVED)
        self.assertEqual(by_id["B"]["decision"], NOT_SUPPORTED)
        self.assertEqual(by_id["C"]["decision"], NOT_SUPPORTED)
        self.assertFalse(by_id["A"]["can_reproduce_sisense_daily_count"])
        self.assertIsNone(next(item.get("activated_strategy") for item in strategies if "activated_strategy" in item))

    def test_event_date_is_not_auto_supported(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertEqual(report["event_date_decision"]["decision"], UNRESOLVED)
        self.assertNotEqual(report["event_date_decision"]["decision"], SUPPORTED)
        self.assertEqual(report["coverage_year"]["decision"], UNRESOLVED)

    def test_term_values_are_all_listed(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertIn("Terminated", report["term_status_values_observed"])
        self.assertIn("Termination Pending", report["term_status_values_observed"])
        factor = next(
            item["duplicate_factor"] for item in report["count_semantics"]
            if item["grain"] == "family" and item["status_family"] == "Cancelled"
        )
        self.assertEqual(factor, 1.2)

    def test_comparable_requires_every_check(self):
        unresolved = cancelled_policies_checks(
            identifier_present=True, count_ready=True, count_factor=1,
            status_observed=True, event_decision=UNRESOLVED,
            coverage_decision=UNRESOLVED, filter_decision=UNRESOLVED,
            strategy_decision=UNRESOLVED, term_values=["Terminated"],
        )
        metric = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", unresolved)
        self.assertEqual(metric["comparison_status"], DIAGNOSTIC)
        self.assertFalse(metric["full_metric_comparable"])
        passing = cancelled_policies_checks(
            identifier_present=True, count_ready=True, count_factor=1,
            status_observed=True, event_decision=SUPPORTED,
            coverage_decision=SUPPORTED, filter_decision=SUPPORTED,
            strategy_decision=SUPPORTED, term_values=["Terminated"],
        )
        for item in passing:
            if item["check"] == "daily_aggregation_semantics":
                item["status"] = "PASS"
        comparable = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", passing)
        self.assertEqual(comparable["comparison_status"], "COMPARABLE")
        missing = cancelled_policies_checks(
            identifier_present=False, count_ready=False, status_observed=False,
            event_decision=NOT_SUPPORTED, coverage_decision=NOT_SUPPORTED,
            filter_decision=UNRESOLVED, strategy_decision=NOT_SUPPORTED,
            term_values=[],
        )
        blocked = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", missing)
        self.assertEqual(blocked["comparison_status"], NOT_COMPARABLE)

    def test_collect_evidence_does_not_union_or_query_2027(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertEqual(report["label"], DIAGNOSTIC_LABEL)
        self.assertEqual(report["py2027_widget"], WIDGET_TITLE)
        self.assertTrue(report["not_official_activity"])
        self.assertEqual(report["comparable"], [])
        self.assertEqual(report["cancelled_policies"]["comparison_status"], DIAGNOSTIC)
        self.assertEqual(report["terminated_policies"]["comparison_status"], DIAGNOSTIC)
        self.assertEqual(report["out_of_scope_this_phase"]["currently_enrolled_people"], "unchanged")
        self.assertEqual(report["plus_one_pattern"]["label"], PLUS_ONE_LABEL)
        self.assertIsNone(next(
            item.get("activated_strategy")
            for item in report["extraction_strategies"] if "activated_strategy" in item
        ))
        for sql, params in client.calls:
            self.assertNotIn("UNION", sql.upper())
            self.assertNotIn(2027, params or ())
        relative = [sql for sql, _params in client.calls if "AS event_on_snapshot_plus_one" in sql]
        self.assertTrue(relative)
        self.assertTrue(all("01312026" in sql or "02272026" in sql for sql in relative))
        self._assert_keys(report)

    def test_coverage_year_sql_has_no_year_parameter(self):
        sql, params = build_coverage_year_sql(
            "[dbo].[Enrollments_PY2526_01312026]", columns_meta())
        assert_read_only(sql)
        self.assertNotIn(2026, params)
        self.assertNotIn(2027, params)
        self.assertEqual(params, ("Cancelled", "Term%"))

    def test_write_report_and_html_sections(self):
        report = build_report(collect_evidence(FakeClient()))
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            page = html_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["report_kind"], "PY2026_ACTIVITY_SEMANTIC_BRIDGE")
        self.assertEqual(json_path.name, "py2026_activity_semantic_bridge.json")
        self.assertEqual(html_path.name, "py2026_activity_semantic_bridge.html")
        for heading in (
            "COUNT semantics", "Status values", "Coverage year",
            "Activity widget filters", "Event-date field comparison",
            "+1 day pattern", "Extraction strategies",
            "Cancelled Policies checks", "Terminated Policies checks",
        ):
            self.assertIn(heading, page)
        self.assertNotIn("<canvas", page.lower())
        self.assertNotIn("chart", page.lower())

    def test_command_module_does_not_change_main_or_catalog(self):
        main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
        catalog = (PROJECT / "metrics" / "catalog.py").read_text(encoding="utf-8")
        self.assertNotIn("database.comparison.activity_semantic_bridge", main_source)
        self.assertNotIn("activity_semantic_bridge", catalog)

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
