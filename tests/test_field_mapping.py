"""Phase 5B PY2026 field mapping. Fake query results only — no database, no Sisense."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, interpret_payload
from comparison.daily import load_oe_anchor, oe_day
from comparison.models import DIAGNOSTIC, NOT_COMPARABLE, SemanticChecks, spec_by_id
from comparison.validator import ComparisonValidationError, assert_grains_compatible, assign_status
from database.comparison.activity import (
    build_daily_activity_sql,
    monthly_from_daily,
    parse_daily_rows,
    refuse_cross_snapshot_sum,
    series_document,
)
from database.comparison.field_mapping import (
    attach_activity_series,
    build_report,
    classify_identifier_column,
    collect_evidence,
    currently_enrolled_checklist,
    field_mapping_rows,
    oe_anchor_evidence,
    print_summary,
    select_event_date_candidate,
    write_report,
)
from database.models import QueryResult
from database.query_runner import assert_read_only

PROJECT = Path(__file__).resolve().parents[1]


def payload():
    return json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))


def definition():
    return interpret_payload(payload())


def result(columns, rows):
    materialized = tuple(tuple(row) for row in rows)
    return QueryResult(columns=tuple(columns), rows=materialized, row_count=len(materialized), duration_seconds=0.0)


COLUMNS = {
    "coverage_year": "int",
    "enrollee_id": "varchar",
    "enrollment_id": "varchar",
    "enrollment_status_description": "varchar",
    "enrollee_status_description": "varchar",
    "benefit_effective_date": "datetime2",
    "person_type_description": "varchar",
    "insurance_type_description": "varchar",
    "enrollment_confirmation_date": "datetime2",
    "enrollment_create_date": "datetime2",
    "enrollment_last_update_date": "datetime2",
    "last_update_timestamp": "datetime2",
    "exchg_indiv_identifier": "varchar",
}


class FakeClient:
    def __init__(self, tables, columns=None, overlap=3, cancelled_days=None, terminated_days=None,
                 status_values=None):
        self.tables = tables
        self.columns = columns or {table: dict(COLUMNS) for _, table, _ in tables}
        self.overlap = overlap
        self.cancelled_days = cancelled_days or [(date(2025, 11, 1), Decimal("2")), (date(2025, 11, 2), Decimal("3"))]
        self.terminated_days = terminated_days
        self.status_values = status_values or [("Enrolled", 5), ("Cancelled", 2), ("Terminated", 1)]
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
        if "AS overlapping_enrollment_ids" in sql:
            return result(("overlapping_enrollment_ids",), [(self.overlap,)])
        if "AS duplicate_enrollment_ids" in sql:
            return result(("row_count", "distinct_enrollment_ids", "duplicate_enrollment_ids"), [(10, 8, 2)])
        if "AS status_value" in sql:
            return result(("status_value", "row_count"), self.status_values)
        if "AS enrolled_cancelled_people" in sql:
            return result(
                ("enrolled_cancelled_people", "cancelled_not_enrolled_people",
                 "people_with_cancelled", "people_in_scope"),
                [(4, 3, 7, 20)],
            )
        if "AS distinct_enrollee_keys" in sql:
            return result(
                ("paired_rows", "distinct_enrollee_keys", "distinct_exchange_keys",
                 "null_enrollee_keys", "null_exchange_keys",
                 "enrollee_to_many_exchange", "exchange_to_many_enrollee"),
                [(20, 18, 18, 1, 2, 0, 0)],
            )
        if "AS calendar_date" in sql:
            if params and params[0] == "Cancelled":
                return result(("calendar_date", "value"), self.cancelled_days)
            if params and params[0] == "Term%":
                if self.terminated_days is None:
                    return result(("calendar_date", "value"), [])
                return result(("calendar_date", "value"), self.terminated_days)
        if "AS row_count" in sql:
            return result(("row_count", "null_enrollment_id", "min_last_update_timestamp", "max_last_update_timestamp"),
                          [(10, 0, date(2025, 10, 1), date(2025, 11, 30))])
        raise AssertionError(sql)


class MappingTests(unittest.TestCase):
    def test_last_update_date_is_not_assumed_equal_to_timestamp(self):
        columns = {
            "enrollment_id": {"name": "enrollment_id", "data_type": "bigint"},
            "enrollment_status_description": {"name": "enrollment_status_description", "data_type": "varchar"},
            "enrollment_last_update_date": {"name": "enrollment_last_update_date", "data_type": "datetime2"},
        }
        rows = field_mapping_rows(columns, ["Cancelled"])
        timestamp = [item for item in rows if item["py2027_field"] == "enrollment.last_update_timestamp"]
        names = {item["py2026_candidate"]: item["confidence"] for item in timestamp}
        self.assertEqual(names.get("enrollment_last_update_date"), "NAME_SIMILAR_UNPROVEN")
        self.assertNotIn("last_update_timestamp", names)
        self.assertTrue(all(item["py2027_field"] == "enrollment.last_update_timestamp" for item in rows))
        selected = select_event_date_candidate(columns)
        self.assertEqual(selected["py2026_candidate"], "enrollment_last_update_date")
        self.assertEqual(selected["confidence"], "NAME_SIMILAR_UNPROVEN")

    def test_enrollment_id_is_never_event_date_candidate(self):
        columns = {
            "enrollment_id": {"name": "enrollment_id", "data_type": "bigint"},
            "enrollment_status_description": {"name": "enrollment_status_description", "data_type": "varchar"},
            "enrollment_last_update_date": {"name": "enrollment_last_update_date", "data_type": "datetime2"},
        }
        selected = select_event_date_candidate(columns)
        self.assertNotEqual(selected["py2026_candidate"], "enrollment_id")
        rows = field_mapping_rows(columns, ["Cancelled"])
        self.assertFalse(any(item.get("py2026_candidate") == "enrollment_id" for item in rows))
        self.assertFalse(any(item.get("py2027_field") == "enrollment.id" for item in rows))

    def test_event_date_candidate_requires_datetime_type(self):
        columns = {
            "enrollment_id": {"name": "enrollment_id", "data_type": "varchar"},
            "last_update_timestamp": {"name": "last_update_timestamp", "data_type": "varchar"},
            "enrollment_last_update_date": {"name": "enrollment_last_update_date", "data_type": "varchar"},
        }
        selected = select_event_date_candidate(columns)
        self.assertIsNone(selected["py2026_candidate"])
        self.assertEqual(selected["confidence"], "ABSENT")
        missing_type = select_event_date_candidate({
            "enrollment_last_update_date": {"name": "enrollment_last_update_date"},
        })
        self.assertIsNone(missing_type["py2026_candidate"])

    def test_print_summary_does_not_select_enrollment_id_as_event_date(self):
        live_columns = {key: value for key, value in COLUMNS.items() if key != "last_update_timestamp"}
        client = FakeClient(
            [("dbo", "Enrollments_PY2526_NOV30", "BASE TABLE")],
            columns={"Enrollments_PY2526_NOV30": live_columns},
            overlap=0,
        )
        report = build_report(attach_activity_series(client, collect_evidence(client, definition())))
        self.assertEqual(report["event_date_candidate"]["py2026_candidate"], "enrollment_last_update_date")
        self.assertNotEqual(report["event_date_candidate"]["py2026_candidate"], "enrollment_id")
        buf = StringIO()
        with patch("sys.stdout", buf):
            print_summary(report)
        text = buf.getvalue()
        self.assertIn("enrollment_last_update_date", text)
        self.assertNotIn("event-date candidate: enrollment_id", text)

    def test_status_mapping_does_not_treat_canceled_as_cancelled(self):
        client = FakeClient(
            [("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")],
            overlap=0,
            status_values=[("Canceled", 2)],
        )
        report = build_report(attach_activity_series(client, collect_evidence(client, definition())))
        self.assertFalse(report["status_mapping"]["cancelled_observed"])
        self.assertTrue(report["status_mapping"]["canceled_variant_observed"])

    def test_identifier_classifier_skips_pii_names(self):
        self.assertIsNone(classify_identifier_column("email_address"))
        self.assertEqual(classify_identifier_column("enrollee_id"), "enrollee")
        self.assertEqual(classify_identifier_column("exchg_indiv_identifier"), "exchange_individual")

    def test_cardinality_is_one_to_one_without_raw_ids(self):
        client = FakeClient([("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")], overlap=0)
        evidence = collect_evidence(client, definition())
        evidence = attach_activity_series(client, evidence)
        report = build_report(evidence)
        card = report["profiles"][0]["cardinality"]
        self.assertEqual(card["mapping_cardinality"], "1:1")
        dumped = json.dumps(report)
        self.assertNotIn("secret-person", dumped)
        self._assert_keys(report)

    def test_repeated_snapshots_are_not_summed_as_events(self):
        client = FakeClient(
            [
                ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
                ("dbo", "Enrollments_PY2526_02272026", "BASE TABLE"),
            ],
            overlap=9,
        )
        evidence = collect_evidence(client, definition())
        evidence = attach_activity_series(client, evidence)
        report = build_report(evidence)
        self.assertEqual(report["snapshot_duplication_risk"]["overlapping_enrollment_ids"], 9)
        self.assertTrue(report["snapshot_duplication_risk"]["must_not_sum_across_snapshots"])
        daily_calls = [sql for sql, _params in client.calls if "AS calendar_date" in sql]
        self.assertEqual(len(daily_calls), 2)
        with self.assertRaises(ComparisonValidationError):
            refuse_cross_snapshot_sum()

    def test_failed_semantic_check_prevents_comparable(self):
        client = FakeClient([("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")], overlap=0)
        evidence = collect_evidence(client, definition())
        evidence = attach_activity_series(client, evidence)
        report = build_report(evidence)
        self.assertEqual(report["comparable"], [])
        cancelled = report["metric_classifications"]["cancelled_policies"]
        self.assertEqual(cancelled["comparison_status"], DIAGNOSTIC)
        currently = report["metric_classifications"]["currently_enrolled_people"]
        self.assertEqual(currently["comparison_status"], DIAGNOSTIC)
        self.assertTrue(currently["failed_or_unresolved_checks"])
        pending = report["metric_classifications"]["pending_people"]
        self.assertEqual(pending["comparison_status"], NOT_COMPARABLE)
        checks = SemanticChecks()
        self.assertEqual(assign_status(spec_by_id("cancelled_policies_daily"), checks), NOT_COMPARABLE)

    def test_person_and_policy_cancellation_cannot_be_mixed(self):
        with self.assertRaises(ComparisonValidationError):
            assert_grains_compatible(spec_by_id("enrolled_cancelled"), spec_by_id("cancelled_policies_daily"))

    def test_missing_terminated_history_is_not_zero(self):
        client = FakeClient(
            [("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")],
            overlap=0,
            terminated_days=None,
        )
        evidence = collect_evidence(client, definition())
        evidence = attach_activity_series(client, evidence)
        report = build_report(evidence)
        terminated = report["activity_series"].get("terminated_policies_daily")
        self.assertIsNotNone(terminated)
        self.assertEqual(terminated["daily"], [])
        self.assertIsNone(next((item["py2027_value"] for item in []), None))
        self.assertNotEqual(terminated["daily"], [{"value": 0}])
        self.assertEqual(report["activity_readiness"]["py2027_daily_history"], "PENDING")

    def test_monthly_activity_equals_daily_sum(self):
        points = parse_daily_rows(
            [(date(2025, 11, 1), 2), (date(2025, 11, 2), 3), (date(2025, 12, 1), 4)],
            "cancelled_policies_daily",
            "Enrollments_PY2526_01312026",
        )
        monthly = monthly_from_daily("cancelled_policies_daily", points, "Enrollments_PY2526_01312026")
        by_month = {item["year_month"]: item["value"] for item in monthly}
        self.assertEqual(by_month["2025-11"], 5)
        self.assertEqual(by_month["2025-12"], 4)
        self.assertTrue(all(item["daily_sum_matches"] for item in monthly))
        self.assertTrue(all(item["aggregation_method"] == "ACTIVITY_SUM" for item in monthly))

    def test_unresolved_oe_anchor_does_not_create_fake_oe_day(self):
        evidence = oe_anchor_evidence()
        self.assertEqual(evidence["py2026"]["classification"], "UNKNOWN")
        self.assertFalse(evidence["official_verified_found"])
        self.assertEqual(evidence["rejected_as_official_oe_start"][0]["classification"], "DISCOVERY_WINDOW")
        blank = load_oe_anchor(2026, environ={})
        self.assertIsNone(oe_day(date(2025, 11, 8), blank))
        client = FakeClient([("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")], overlap=0)
        collected = attach_activity_series(client, collect_evidence(client, definition()))
        series = collected["activity_series"]["cancelled_policies_daily"]
        self.assertTrue(all(item["oe_day"] is None for item in series["daily"]))

    def test_activity_sql_is_read_only_and_single_snapshot(self):
        columns = {
            "enrollment_id": {"name": "enrollment_id"},
            "enrollment_status_description": {"name": "enrollment_status_description"},
            "last_update_timestamp": {"name": "last_update_timestamp"},
            "coverage_year": {"name": "coverage_year"},
        }
        sql, params = build_daily_activity_sql("[dbo].[Enrollments_PY2526_01312026]", columns, cancelled=True)
        assert_read_only(sql)
        self.assertNotIn("UNION", sql.upper())
        self.assertIn(2026, params)
        self.assertNotIn(2027, params)
        self.assertEqual(params[0], "Cancelled")

    def test_currently_enrolled_checklist_does_not_promote(self):
        checks = currently_enrolled_checklist(
            definition(),
            {"coverage_year": {"name": "coverage_year"}},
            {"identity_status": "IDENTIFIER_EQUIVALENCE_UNPROVEN", "reason": "proxy"},
        )
        self.assertTrue(any(item["status"] != "PASS" for item in checks))

    def test_write_report_and_html_sections(self):
        client = FakeClient([("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")], overlap=0)
        evidence = attach_activity_series(client, collect_evidence(client, definition()))
        report = build_report(evidence)
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            page = html_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["report_kind"], "PY2026_FIELD_MAPPING")
        for heading in (
            "Enrollment identifier mapping",
            "Person identifier mapping",
            "Status mapping",
            "Event-date mapping",
            "Snapshot duplication risk",
            "Currently Enrolled semantic checklist",
            "People cancellation semantic checklist",
            "Lifecycle-supporting fields",
            "OE anchor evidence",
            "Remaining blockers",
        ):
            self.assertIn(heading, page)
        self.assertNotIn("<canvas", page)
        before = VERIFIED_PAYLOAD_FILE.read_bytes()
        self.assertEqual(VERIFIED_PAYLOAD_FILE.read_bytes(), before)

    def test_command_module_does_not_change_main_or_catalog(self):
        main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("database.comparison.field_mapping", main_source)
        self.assertNotIn("import comparison", main_source)

    def test_people_cancellation_query_is_person_grain(self):
        client = FakeClient([("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")], overlap=0)
        evidence = collect_evidence(client, definition())
        people = evidence["profiles"][0]["people_cancellation"]
        self.assertEqual(people["enrolled_cancelled_people"], 4)
        self.assertEqual(people["cancelled_not_enrolled_people"], 3)
        self.assertEqual(people["comparison_status"], DIAGNOSTIC)

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
