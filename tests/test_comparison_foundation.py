"""Comparison foundation. Injected values and local fixtures only. No Sisense, no database."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from comparison.comparability import (
    build_foundation_report,
    foundation_checks,
    read_validated_snapshots,
    render_html,
    write_report,
)
from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, interpret_payload
from comparison.daily import align_by_oe_day, daily_point, load_oe_anchor, oe_day
from comparison.models import (
    AGG_ACTIVITY_SUM,
    AGG_LIFECYCLE,
    AGG_SNAPSHOT,
    COMPARABLE,
    DIAGNOSTIC,
    FAMILY_ACTIVITY,
    FAMILY_POPULATION,
    FOUNDATION_METRICS,
    GRAIN_PERSON,
    GRAIN_POLICY,
    NOT_COMPARABLE,
    PENDING_UNAVAILABLE,
    DailyObservation,
    SemanticChecks,
    spec_by_id,
)
from comparison.monthly import monthly_aggregate, refuse_snapshot_sum, snapshot_month_value, sum_activity_month
from comparison.validator import (
    ComparisonValidationError,
    assert_grains_compatible,
    assign_status,
    build_row,
    differences,
    refuse_null_as_zero,
    require_comparable,
)
from database.comparison.discovery import evaluate_schema, requirements_document

PROJECT = Path(__file__).resolve().parents[1]


def payload() -> dict:
    return json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))


def ready_checks(**overrides) -> SemanticChecks:
    base = SemanticChecks(
        identifier_equivalence_proven=True,
        grain_match=True,
        family_match=True,
        date_semantics_match=True,
        py2026_definition_verified=True,
        py2027_definition_verified=True,
        filter_mapping_proven=True,
        both_values_present=True,
        pending_has_verified_definition=True,
        people_policy_not_mixed=True,
        snapshot_not_summed=True,
        py2026_sql_not_invented=True,
    )
    return SemanticChecks(**{**base.as_dict(), **overrides})


class StatusGateTests(unittest.TestCase):
    def test_comparable_requires_every_semantic_check(self):
        spec = spec_by_id("currently_enrolled_people")
        self.assertEqual(assign_status(spec, SemanticChecks()), DIAGNOSTIC)
        with self.assertRaises(ComparisonValidationError):
            require_comparable(SemanticChecks(both_values_present=True))
        self.assertEqual(assign_status(spec, ready_checks()), COMPARABLE)

    def test_pending_stays_unavailable_even_with_ready_checks(self):
        spec = spec_by_id("pending_people")
        row = build_row(spec, py2026_value=10, py2027_value=20, checks=ready_checks())
        self.assertEqual(row.comparison_status, NOT_COMPARABLE)
        self.assertIn(PENDING_UNAVAILABLE, row.reason)
        self.assertIsNone(row.percent_difference)
        self.assertIsNone(row.absolute_difference)

    def test_null_is_not_zero_and_percent_is_skipped(self):
        self.assertIsNone(refuse_null_as_zero(None))
        spec = spec_by_id("currently_enrolled_people")
        row = build_row(spec, py2026_value=None, py2027_value=100, checks=ready_checks())
        self.assertIsNone(row.py2026_value)
        self.assertEqual(row.py2027_value, 100)
        self.assertIsNone(row.absolute_difference)
        self.assertIsNone(row.percent_difference)
        self.assertNotEqual(row.comparison_status, COMPARABLE)

    def test_percent_only_when_comparable(self):
        missing = differences(NOT_COMPARABLE, 100, 80)
        self.assertIsNone(missing["percent_difference"])
        self.assertIsNone(missing["absolute_difference"])
        diagnostic = differences(DIAGNOSTIC, 100, 80)
        self.assertEqual(diagnostic["absolute_difference"], -20)
        self.assertIsNone(diagnostic["percent_difference"])
        comparable = differences(COMPARABLE, 100, 80)
        self.assertEqual(comparable["absolute_difference"], -20)
        self.assertEqual(comparable["percent_difference"], -20)

    def test_people_metrics_cannot_combine_with_policy_metrics(self):
        with self.assertRaises(ComparisonValidationError):
            assert_grains_compatible(spec_by_id("enrolled_cancelled"), spec_by_id("cancelled_policies_daily"))
        with self.assertRaises(ComparisonValidationError):
            assert_grains_compatible(spec_by_id("currently_enrolled_people"), spec_by_id("terminated_policies_daily"))
        assert_grains_compatible(spec_by_id("new_enrolled"), spec_by_id("active_enrolled"))


class DailyModelTests(unittest.TestCase):
    def test_snapshot_cannot_enter_daily_activity(self):
        anchor = load_oe_anchor(2026, explicit=date(2025, 11, 1))
        with self.assertRaises(ComparisonValidationError):
            daily_point(
                spec_by_id("currently_enrolled_people"),
                plan_year=2026,
                calendar_day=date(2025, 11, 8),
                value=1,
                source="test",
                anchor=anchor,
            )

    def test_oe_day_alignment_and_unaligned_year(self):
        aligned = load_oe_anchor(2026, explicit=date(2025, 11, 1))
        self.assertEqual(oe_day(date(2025, 11, 1), aligned), 1)
        self.assertEqual(oe_day(date(2025, 11, 8), aligned), 8)
        blank = load_oe_anchor(2027, environ={})
        self.assertEqual(blank.alignment, "UNALIGNED")
        self.assertIsNone(oe_day(date(2026, 11, 8), blank))
        spec = spec_by_id("cancelled_policies_daily")
        left = [daily_point(spec, plan_year=2026, calendar_day=date(2025, 11, 8), value=3,
                            source="test", anchor=aligned)]
        right = [daily_point(spec, plan_year=2027, calendar_day=date(2026, 11, 8), value=5,
                             source="test", anchor=load_oe_anchor(2027, explicit=date(2026, 11, 1)))]
        paired = align_by_oe_day(left, right)
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["oe_day"], 8)
        self.assertEqual(align_by_oe_day(left, [
            daily_point(spec, plan_year=2027, calendar_day=date(2026, 11, 8), value=5,
                        source="test", anchor=load_oe_anchor(2027, environ={}))
        ]), [])


class MonthlyModelTests(unittest.TestCase):
    def test_activity_sum_and_snapshot_month_end(self):
        spec = spec_by_id("cancelled_policies_daily")
        anchor = load_oe_anchor(2026, explicit=date(2025, 11, 1))
        points = [
            daily_point(spec, plan_year=2026, calendar_day=date(2025, 11, 1), value=2, source="t", anchor=anchor),
            daily_point(spec, plan_year=2026, calendar_day=date(2025, 11, 2), value=3, source="t", anchor=anchor),
        ]
        month = monthly_aggregate(
            spec, points, plan_year=2026, year=2025, month=11, source="t", comparison_status=NOT_COMPARABLE
        )
        self.assertEqual(month.aggregation_method, AGG_ACTIVITY_SUM)
        self.assertEqual(month.value, 5)
        self.assertEqual(sum_activity_month(points, 2025, 12), None)

        population = spec_by_id("currently_enrolled_people")
        snaps = [
            DailyObservation(
                metric_name=population.metric_name,
                plan_year=2026,
                calendar_date=day,
                oe_day=None,
                value=value,
                grain=population.grain,
                date_semantics=population.date_semantics,
                source="t",
                comparison_status=DIAGNOSTIC,
                metric_family=FAMILY_POPULATION,
            )
            for day, value in ((date(2025, 11, 15), 100), (date(2025, 11, 30), 110))
        ]
        self.assertEqual(snapshot_month_value(snaps, 2025, 11), 110)
        pop_month = monthly_aggregate(
            population, snaps, plan_year=2026, year=2025, month=11, source="t", comparison_status=DIAGNOSTIC
        )
        self.assertEqual(pop_month.aggregation_method, AGG_SNAPSHOT)
        self.assertEqual(pop_month.value, 110)
        self.assertNotEqual(pop_month.value, 210)
        lifecycle = spec_by_id("new_enrolled")
        life_points = [
            DailyObservation(
                metric_name=lifecycle.metric_name,
                plan_year=2026,
                calendar_date=day,
                oe_day=None,
                value=value,
                grain=lifecycle.grain,
                date_semantics=lifecycle.date_semantics,
                source="t",
                comparison_status=NOT_COMPARABLE,
                metric_family=lifecycle.metric_family,
            )
            for day, value in ((date(2025, 11, 1), 10), (date(2025, 11, 30), 12))
        ]
        life_month = monthly_aggregate(
            lifecycle, life_points, plan_year=2026, year=2025, month=11,
            source="t", comparison_status=NOT_COMPARABLE,
        )
        self.assertEqual(life_month.aggregation_method, AGG_LIFECYCLE)
        self.assertEqual(life_month.value, 12)
        self.assertNotEqual(life_month.value, 22)

    def test_null_day_keeps_activity_month_null(self):
        spec = spec_by_id("cancelled_policies_daily")
        anchor = load_oe_anchor(2026, explicit=date(2025, 11, 1))
        points = [
            daily_point(spec, plan_year=2026, calendar_day=date(2025, 11, 1), value=2, source="t", anchor=anchor),
            daily_point(spec, plan_year=2026, calendar_day=date(2025, 11, 2), value=None, source="t", anchor=anchor),
        ]
        self.assertIsNone(sum_activity_month(points, 2025, 11))

    def test_snapshots_cannot_be_summed(self):
        with self.assertRaises(ComparisonValidationError):
            refuse_snapshot_sum([])


class FoundationReportTests(unittest.TestCase):
    def test_default_registry_has_no_comparable_metrics(self):
        report = build_foundation_report(definition=interpret_payload(payload()))
        self.assertEqual(report["comparable"], [])
        self.assertEqual(report["diagnostic"], ["Currently Enrolled People"])
        self.assertIn("Pending People", report["not_comparable"])
        self.assertIn("New Enrolled People", report["not_comparable"])
        self.assertIn("Cancelled Policies", report["not_comparable"])
        self.assertFalse(report["comparison_executed"])
        self.assertFalse(report["py2026_equivalents_manufactured"])
        pending = next(item for item in report["metrics"] if item["metric_id"] == "pending_people")
        self.assertEqual(pending["comparison_status"], NOT_COMPARABLE)
        self.assertIsNone(pending["py2026_value"])
        self.assertIsNone(pending["py2027_value"])

    def test_injected_py2026_value_does_not_become_comparable(self):
        report = build_foundation_report(
            definition=interpret_payload(payload()),
            snapshots={"currently_enrolled_people": {"metric_value": 797301, "observation_date": "2026-11-08"}},
            py2026_values={"currently_enrolled_people": 500000},
        )
        row = next(item for item in report["metrics"] if item["metric_id"] == "currently_enrolled_people")
        self.assertEqual(row["py2027_value"], 797301)
        self.assertEqual(row["py2026_value"], 500000)
        self.assertEqual(row["comparison_status"], DIAGNOSTIC)
        self.assertEqual(row["absolute_difference"], 297301)
        self.assertIsNone(row["percent_difference"])
        self.assertEqual(row["as_of_date_py2027"], "2026-11-08")

    def test_sqlite_value_is_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE metric_snapshots (
                    run_id TEXT, metric_id TEXT, metric_value NUMERIC, plan_year INTEGER,
                    retrieved_at_utc TEXT, snapshot_slot_utc TEXT, retrieval_status TEXT
                )
                """
            )
            connection.execute(
                """INSERT INTO metric_snapshots VALUES
                   ('run-1', 'currently_enrolled_people', 424242, 2027, '2026-11-08T15:04:05Z',
                    '2026-11-08T15:00:00Z', 'VALIDATED')"""
            )
            connection.commit()
            connection.close()
            snapshots = read_validated_snapshots(path)
        self.assertEqual(snapshots["currently_enrolled_people"]["metric_value"], 424242)

    def test_html_sections_and_no_charts(self):
        page = render_html(build_foundation_report(definition=interpret_payload(payload())))
        for heading in (
            "Enrollment Population",
            "Enrollment Lifecycle",
            "Cancellation / Termination Activity",
            "Applications",
            "Unresolved Metrics",
        ):
            self.assertIn(heading, page)
        self.assertNotIn("<canvas", page)
        self.assertNotIn("<svg", page)

    def test_write_report_round_trip_and_payload_untouched(self):
        before = VERIFIED_PAYLOAD_FILE.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(
                build_foundation_report(definition=interpret_payload(payload())),
                Path(directory),
            )
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["report_kind"], "PY2026_VS_PY2027_COMPARISON_FOUNDATION")
            self.assertTrue(html_path.is_file())
        self.assertEqual(VERIFIED_PAYLOAD_FILE.read_bytes(), before)

    def test_command_module_does_not_open_sisense_or_the_database(self):
        source = (PROJECT / "comparison" / "comparability.py").read_text(encoding="utf-8")
        main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
        for token in ("pyodbc", "playwright", "SisenseJaqlClient", "execute_query", "database_connection"):
            self.assertNotIn(token, source)
        self.assertNotIn("comparison.comparability", main_source)
        self.assertNotIn("import comparison", main_source)

    def test_output_has_no_identifier_keys(self):
        report = build_foundation_report(definition=interpret_payload(payload()))
        self._assert_keys(report)

    def _assert_keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id", "exchg_indiv_identifier"})
                self._assert_keys(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_keys(item)


class SchemaDiscoveryTests(unittest.TestCase):
    def test_schema_evaluator_does_not_invent_columns(self):
        result = evaluate_schema({"coverage_year", "enrollee_id"}, "currently_enrolled_people")
        self.assertEqual(result["status"], "COLUMNS_INCOMPLETE")
        self.assertIn("benefit_effective_date", result["missing"])
        unread = evaluate_schema(None, "currently_enrolled_people")
        self.assertEqual(unread["status"], "SCHEMA_NOT_READ")

    def test_requirements_document_does_not_write_sql(self):
        document = requirements_document()
        self.assertFalse(document["queries_py2027_enrollment"])
        self.assertFalse(document["production_sql_written"])
        self.assertTrue(all(item["py2026_sql_status"] == "NOT_WRITTEN" for item in document["metrics"]))

    def test_lifecycle_and_activity_families_are_distinct(self):
        self.assertEqual(spec_by_id("new_enrolled").metric_family, "LIFECYCLE_CLASSIFICATION")
        self.assertEqual(spec_by_id("cancelled_policies_daily").metric_family, FAMILY_ACTIVITY)
        self.assertEqual(spec_by_id("currently_enrolled_people").metric_family, FAMILY_POPULATION)
        self.assertEqual(spec_by_id("currently_enrolled_people").grain, GRAIN_PERSON)
        self.assertEqual(spec_by_id("cancelled_policies_daily").grain, GRAIN_POLICY)
        self.assertEqual(len(FOUNDATION_METRICS), 12)


class ForcedComparableTests(unittest.TestCase):
    def test_comparable_row_computes_percent(self):
        spec = spec_by_id("currently_enrolled_people")
        row = build_row(spec, py2026_value=50, py2027_value=40, checks=ready_checks())
        self.assertEqual(row.comparison_status, COMPARABLE)
        self.assertEqual(row.absolute_difference, -10)
        self.assertEqual(row.percent_difference, -20)

    def test_foundation_checks_never_mark_ready(self):
        spec = spec_by_id("currently_enrolled_people")
        checks = foundation_checks(spec, py2026=1, py2027=2)
        self.assertFalse(checks.comparable_ready())


if __name__ == "__main__":
    unittest.main()
