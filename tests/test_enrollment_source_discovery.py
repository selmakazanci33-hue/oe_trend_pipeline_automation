"""Enrollment source discovery. Fake query results only — no database and no Sisense."""

import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from database.connection import DatabaseConnection
from database.discovery.enrollment_source import (
    TARGET_COLUMNS,
    DiscoveryOutputError,
    build_confirmation_month_sql,
    build_first_confirmation_sql,
    build_grouped_profile_sql,
    build_year_profile_sql,
    choose_source,
    guard_no_pii,
    parse_catalog,
    quote_identifier,
    run_discovery,
    write_discovery_report,
)
from database.models import QueryExecutionError, QueryResult
from database.query_runner import ReadOnlyQueryError, assert_read_only, load_query_file


def result(columns, rows):
    materialized = tuple(tuple(row) for row in rows)
    return QueryResult(columns=tuple(columns), rows=materialized, row_count=len(materialized), duration_seconds=0.0)


CATALOG_COLUMNS = (
    "table_schema", "table_name", "table_type", "column_name", "data_type",
    "character_maximum_length", "numeric_precision", "numeric_scale", "datetime_precision",
)


def catalog_row(table, column, data_type, table_type="BASE TABLE", length=None, precision=None, scale=None, dt_precision=None):
    return ("dbo", table, table_type, column, data_type, length, precision, scale, dt_precision)


def full_catalog_rows(table, table_type="BASE TABLE"):
    rows = []
    for column in TARGET_COLUMNS:
        if column.endswith("_date"):
            rows.append(catalog_row(table, column, "datetime2", table_type, dt_precision=7))
        elif column in {"enrollment_status_description", "enrollee_status_description", "application_type"}:
            rows.append(catalog_row(table, column, "varchar", table_type, length=50))
        else:
            rows.append(catalog_row(table, column, "int", table_type))
    return rows


class FakeClient:
    def __init__(self, fail_step=None):
        self.calls = []
        self.fail_step = fail_step

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        assert_read_only(sql)
        if self.fail_step and self.fail_step in sql:
            raise QueryExecutionError("query timed out (40532)")
        if "INFORMATION_SCHEMA" in sql:
            rows = []
            rows.extend(full_catalog_rows("BothYears"))
            rows.extend(full_catalog_rows("Enrollments_TEST"))
            rows.extend([
                catalog_row("AaaBoth", "coverage_year", "int", "VIEW"),
                catalog_row("AaaBoth", "enrollment_id", "int", "VIEW"),
                catalog_row("AaaBoth", "enrollee_id", "int", "VIEW"),
                catalog_row("Only2026", "coverage_year", "int"),
                catalog_row("Only2026", "enrollment_id", "int"),
                catalog_row("inbound_automation", "coverage_year", "int"),
                catalog_row("inbound_automation", "enrollee_id", "varchar", length=40),
            ])
            return result(CATALOG_COLUMNS, rows)
        if "[dbo].[inbound_automation]" in sql:
            raise AssertionError("inbound rows must not be queried")
        if "AS overlap_count" in sql:
            value = 4 if "[enrollment_id]" in sql.split("INTERSECT")[0] else 9
            return result(("overlap_count",), [(value,)])
        if "AS person_count" in sql:
            return result(
                ("coverage_year", "confirmation_calendar_year", "confirmation_calendar_month", "person_count"),
                [(2026, 2025, 11, 15)],
            )
        if "AS enrollment_count" in sql:
            return result(
                ("coverage_year", "confirmation_calendar_year", "confirmation_calendar_month", "enrollment_count"),
                [(2026, 2025, 11, 12)],
            )
        if "confirmation_calendar_year" in sql:
            return result(
                ("coverage_year", "confirmation_calendar_year", "confirmation_calendar_month",
                 "raw_rows", "distinct_enrollment_id", "distinct_enrollee_id"),
                [(2026, 2025, 11, 20, 12, 15)],
            )
        if "AS enrollment_status_description" in sql:
            return result(
                ("coverage_year", "enrollment_status_description", "raw_rows",
                 "distinct_enrollment_id", "distinct_enrollee_id"),
                [(2026, "Enrolled", 8, 5, 6), (2027, "Pending", 3, 2, 2)],
            )
        if "AS enrollee_status_description" in sql:
            return result(
                ("coverage_year", "enrollee_status_description", "raw_rows",
                 "distinct_enrollment_id", "distinct_enrollee_id"),
                [(2026, "Enrolled", 7, 4, 5)],
            )
        if "AS application_type" in sql:
            return result(
                ("coverage_year", "application_type", "raw_rows", "distinct_enrollment_id", "distinct_enrollee_id"),
                [(2026, "OE", 6, 4, 4), (2027, "OE", 2, 2, 2)],
            )
        if "AS hios_issuer_id" in sql:
            return result(
                ("coverage_year", "hios_issuer_id", "raw_rows", "distinct_enrollment_id", "distinct_enrollee_id"),
                [(2026, "15105", 6, 4, 4), (2027, "15105", 2, 2, 2)],
            )
        if "MIN(" in sql:
            return result(
                ("coverage_year", "raw_rows", "distinct_enrollment_id", "distinct_enrollee_id",
                 "distinct_issuer_count", "min_enrollment_confirmation_date", "max_enrollment_confirmation_date",
                 "null_enrollment_id", "null_enrollee_id"),
                [(
                    2026, Decimal("100"), Decimal("80"), Decimal("70"), Decimal("4"),
                    datetime(2025, 11, 1, 8, 0, 0), datetime(2026, 1, 15, 9, 0, 0),
                    Decimal("1"), Decimal("0"),
                )],
            )
        if "[dbo].[BothYears]" in sql or "[dbo].[AaaBoth]" in sql:
            if "[dbo].[AaaBoth]" in sql:
                return result(("coverage_year", "raw_rows"), [(2026, 5), (2027, 1)])
            return result(("coverage_year", "raw_rows"), [(2026, 100), (2027, 40)])
        return result(("coverage_year", "raw_rows"), [(2026, 10)])


class CatalogAndSelectionTests(unittest.TestCase):
    def test_catalog_groups_columns_and_formats_types(self):
        rows = full_catalog_rows("BothYears")
        sources = parse_catalog(result(CATALOG_COLUMNS, rows))
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["table_type"], "BASE TABLE")
        self.assertEqual(source["columns"]["application_type"]["data_type"], "varchar(50)")
        self.assertEqual(source["columns"]["coverage_year"]["data_type"], "int")
        self.assertEqual(source["columns"]["enrollment_confirmation_date"]["data_type"], "datetime2(7)")

    def test_historical_name_does_not_win_over_both_years(self):
        chosen = choose_source([
            {
                "schema_name": "dbo", "table_name": "Enrollments_TEST", "table_type": "BASE TABLE",
                "target_column_count": 12, "py2026": True, "py2027": False,
            },
            {
                "schema_name": "dbo", "table_name": "BothYears", "table_type": "BASE TABLE",
                "target_column_count": 12, "py2026": True, "py2027": True,
            },
            {
                "schema_name": "dbo", "table_name": "AaaBoth", "table_type": "VIEW",
                "target_column_count": 3, "py2026": True, "py2027": True,
            },
        ])
        self.assertEqual(chosen["table_name"], "BothYears")
        self.assertIn("not treated as authority", chosen["selection_reason"])

    def test_equal_coverage_prefers_base_table_not_a_familiar_name(self):
        chosen = choose_source([
            {
                "schema_name": "dbo", "table_name": "Enrollments_TEST", "table_type": "VIEW",
                "target_column_count": 4, "py2026": True, "py2027": True,
            },
            {
                "schema_name": "dbo", "table_name": "AaaBoth", "table_type": "BASE TABLE",
                "target_column_count": 4, "py2026": True, "py2027": True,
            },
        ])
        self.assertEqual(chosen["table_name"], "AaaBoth")

    def test_unsafe_identifier_is_rejected(self):
        with self.assertRaises(QueryExecutionError):
            quote_identifier("dbo]; DROP TABLE t;--")


class QueryShapeTests(unittest.TestCase):
    def setUp(self):
        self.columns = {name: name for name in TARGET_COLUMNS}
        self.qualified = "{{qualified_object}}"

    def test_saved_profiles_match_the_builder_and_are_parameterized(self):
        expected = {
            "discovery/enrollment_year_profile.sql": build_year_profile_sql(self.qualified, self.columns),
            "discovery/enrollment_application_type_profile.sql": build_grouped_profile_sql(
                self.qualified, self.columns, "application_type"),
            "discovery/enrollment_issuer_profile.sql": build_grouped_profile_sql(
                self.qualified, self.columns, "hios_issuer_id"),
        }
        status = load_query_file("discovery/enrollment_status_profile.sql")
        status_parts = [part.strip() for part in status.split("\n\n") if part.strip()]
        self.assertEqual(status_parts[0], build_grouped_profile_sql(
            self.qualified, self.columns, "enrollment_status_description"))
        self.assertEqual(status_parts[1], build_grouped_profile_sql(
            self.qualified, self.columns, "enrollee_status_description"))
        month = load_query_file("discovery/enrollment_confirmation_month_profile.sql")
        month_parts = [part.strip() for part in month.split("\n\n") if part.strip()]
        self.assertEqual(month_parts[0], build_confirmation_month_sql(self.qualified, self.columns))
        self.assertEqual(month_parts[1], build_first_confirmation_sql(
            self.qualified, self.columns, "enrollee_id", "person_count"))
        self.assertEqual(month_parts[2], build_first_confirmation_sql(
            self.qualified, self.columns, "enrollment_id", "enrollment_count"))
        for relative, built in expected.items():
            self.assertEqual(load_query_file(relative).strip(), built)
        for sql in (built, *status_parts, *month_parts):
            rendered = sql.replace("{{qualified_object}}", "[dbo].[Example]")
            assert_read_only(rendered)
            self.assertNotIn("2026", rendered)
            self.assertNotIn("2027", rendered)
            self.assertNotIn("PWD", rendered)
            self.assertNotIn("SELECT *", rendered.upper())
            self.assertNotIn("Enrollments_TEST", rendered)
            self.assertNotIn("Enrollments_PY2526_NOV30", rendered)
            self.assertNotIn("Enrollments_PY2026", rendered)

    def test_missing_columns_are_omitted(self):
        sql = build_year_profile_sql("[dbo].[Example]", {
            "coverage_year": "coverage_year",
            "enrollment_id": "enrollment_id",
        })
        assert_read_only(sql)
        self.assertIn("COUNT_BIG(DISTINCT [enrollment_id])", sql)
        self.assertNotIn("enrollee_id", sql)
        self.assertNotIn("benefit_effective_date", sql)
        self.assertNotIn("application_type", sql)
        self.assertIn("IN (?, ?)", sql)

    def test_writes_are_rejected(self):
        with self.assertRaises(ReadOnlyQueryError):
            assert_read_only("DELETE FROM Example")


class DiscoveryRunTests(unittest.TestCase):
    def test_profiles_the_both_year_source_without_persisting_ids(self):
        client = FakeClient()
        payload = run_discovery(client)
        self.assertEqual(payload["selected_source"]["table_name"], "BothYears")
        self.assertTrue(payload["year_matrix"])
        both = next(item for item in payload["year_matrix"] if item["table_name"] == "BothYears")
        only = next(item for item in payload["year_matrix"] if item["table_name"] == "Enrollments_TEST")
        self.assertTrue(both["py2026"] and both["py2027"])
        self.assertTrue(only["py2026"])
        self.assertFalse(only["py2027"])
        self.assertEqual(payload["year_profile"][0]["raw_rows"], 100)
        self.assertEqual(payload["year_profile"][0]["min_enrollment_confirmation_date"], "2025-11-01 08:00:00")
        self.assertEqual(payload["nulls"][0]["null_enrollment_id"], 1)
        self.assertEqual(payload["status_profile"]["enrollment_status"][0]["enrollment_status_description"], "Enrolled")
        self.assertEqual(payload["status_profile"]["enrollee_status"][0]["raw_rows"], 7)
        self.assertEqual(payload["application_type_profile"][0]["application_type"], "OE")
        self.assertEqual(payload["issuer_profile"][0]["hios_issuer_id"], "15105")
        self.assertEqual(payload["confirmation_month_profile"]["row_level"][0]["confirmation_calendar_month"], 11)
        self.assertEqual(payload["confirmation_month_profile"]["person_first"][0]["person_count"], 15)
        self.assertEqual(payload["confirmation_month_profile"]["enrollment_first"][0]["enrollment_count"], 12)
        self.assertEqual(payload["overlap"]["enrollee_id_in_both_years"], 9)
        self.assertEqual(payload["overlap"]["enrollment_id_in_both_years"], 4)
        self.assertTrue(any(item["table_name"] == "inbound_automation" for item in payload["excluded_inbound_objects"]))
        for sql, params in client.calls:
            if "INFORMATION_SCHEMA" in sql:
                self.assertIsNone(params)
            else:
                self.assertEqual(params, (2026, 2027))
                self.assertEqual(sql.count("?"), 2)
                self.assertNotRegex(sql, r"\(\s*2026")
                self.assertNotRegex(sql, r"\(\s*2027")
            self.assertNotIn("[dbo].[inbound_automation]", sql)
        guard_no_pii(payload)
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_discovery_report(payload, Path(directory))
            text = json_path.read_text(encoding="utf-8")
            loaded = json.loads(text)
            self.assertNotIn("E12345", text)
            self._assert_no_id_keys(loaded)
            self.assertIn("BothYears", html_path.read_text(encoding="utf-8"))
            self.assertNotIn("E12345", html_path.read_text(encoding="utf-8"))

    def test_refuses_an_individual_identifier_field(self):
        with self.assertRaises(DiscoveryOutputError):
            guard_no_pii({"rows": [{"enrollee_id": "hidden-person"}]})
        guard_no_pii({"distinct_enrollee_id": 3, "null_enrollment_id": 0})

    def test_failed_probe_is_not_retried(self):
        client = FakeClient(fail_step="COUNT_BIG(*) AS raw_rows")
        payload = run_discovery(client)
        presence_calls = [sql for sql, _params in client.calls if "COUNT_BIG(*) AS raw_rows" in sql and "MIN(" not in sql]
        self.assertEqual(len(presence_calls), len(set(presence_calls)))
        self.assertTrue(payload["failures"])
        self.assertIsNone(payload["selected_source"])
        self.assertIn("40532", payload["failures"][0]["error"])

    def test_query_timeout_can_be_set_on_the_connection(self):
        class Raw:
            timeout = 0

            def close(self):
                self.closed = True

        raw = Raw()
        connection = DatabaseConnection(raw, redact=lambda message: message)
        connection.set_query_timeout(180)
        self.assertEqual(raw.timeout, 180)
        connection.close()

    def _assert_no_id_keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id"})
                self._assert_no_id_keys(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_no_id_keys(item)


if __name__ == "__main__":
    unittest.main()
