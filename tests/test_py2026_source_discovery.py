"""PY2026 database source discovery on fake catalog metadata and aggregate rows."""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from comparison.currently_enrolled import ComparisonOutputError, guard_no_pii
from database.comparison.py2026_source_discovery import (
    AGGREGATE,
    CATALOG_SQL,
    EXACT_NAME,
    FULL_RECORD,
    INSUFFICIENT,
    LATEST_STATE,
    NAME_SIMILAR,
    SNAPSHOT,
    build_grain_sql,
    build_profile_sql,
    build_report,
    build_status_sql,
    classify,
    collect,
    map_concepts,
    parse_catalog,
    print_summary,
    recommend,
    render_html,
    write_report,
)
from database.models import QueryExecutionError, QueryResult
from database.query_runner import assert_read_only

SENTINEL_ID = "778899001122"
FULL_COLUMNS = (
    ("coverage_year", "int"), ("enrollment_id", "bigint"), ("enrollee_id", "bigint"),
    ("enrollment_status_description", "varchar"), ("enrollee_status_description", "varchar"),
    ("enrollment_create_date", "datetime"), ("enrollment_confirmation_date", "datetime"),
    ("enrollment_last_update_date", "datetime"), ("benefit_effective_date", "date"),
    ("benefit_end_date", "date"), ("hios_issuer_id", "varchar"), ("application_type", "varchar"),
    ("exchange_policy_num", "varchar"),
)
CATALOG = {
    "Enrollments_PY2026": FULL_COLUMNS,
    "Enrollments_TEST": FULL_COLUMNS,
    "Enrollments_PY2526_10312026": FULL_COLUMNS,
    "Enrollments_PY2526_DEC31": FULL_COLUMNS,
    "EnrollmentSummary": (("month_name", "varchar"), ("enrollment_count", "int")),
    "Enrollee_Contact": (("enrollee_id", "bigint"), ("plan_year", "int")),
    "Enrollments_834_Inbound": FULL_COLUMNS,
}
PROFILES = {
    "Enrollments_PY2026": {"rows_2026": 1000, "distinct_policies": 500, "distinct_persons": 900, "distinct_issuers": 9},
    "Enrollments_TEST": {"rows_2026": 10, "distinct_policies": 5, "distinct_persons": 9, "distinct_issuers": 1},
    "Enrollments_PY2526_10312026": {"rows_2026": 950, "distinct_policies": 480, "distinct_persons": 860},
}
GRAINS = {
    "Enrollments_PY2026": {"grain_keys": 900, "keys_with_repeated_rows": 0, "max_rows_per_key": 1,
                           "keys_with_multiple_statuses": 0, "keys_with_multiple_update_days": 0},
    "Enrollments_TEST": {"grain_keys": 9, "keys_with_repeated_rows": 0, "max_rows_per_key": 1,
                         "keys_with_multiple_statuses": 0, "keys_with_multiple_update_days": 0},
}


def result(columns, rows):
    rows = tuple(tuple(row) for row in rows)
    return QueryResult(columns=tuple(columns), rows=rows, row_count=len(rows), duration_seconds=0.0)


def source(name, columns=FULL_COLUMNS, schema="dbo"):
    return parse_catalog(result(
        ("table_schema", "table_name", "table_type", "column_name", "data_type"),
        [(schema, name, "BASE TABLE", column, dtype) for column, dtype in columns],
    ))[0]


class FakeClient:
    def __init__(self, catalog=CATALOG, temporal=(), fail=()):
        self.catalog = catalog
        self.temporal = temporal
        self.fail = fail
        self.statements = []

    def execute_query(self, sql, params=None):
        assert_read_only(sql)
        self.statements.append(sql)
        if "2027" in sql or (params and 2027 in params):
            raise AssertionError("PY2027 must not be queried in the database")
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return result(("table_schema", "table_name", "table_type", "column_name", "data_type"),
                          [("dbo", name, "BASE TABLE", column, dtype)
                           for name, columns in self.catalog.items() for column, dtype in columns])
        if "sys.tables" in sql:
            return result(("table_schema", "table_name", "temporal_type"), self.temporal)
        if "834" in sql or "Inbound" in sql:
            raise AssertionError("excluded object queried")
        self.assert_params(params)
        table = re.search(r"FROM \[dbo\]\.\[([A-Za-z0-9_]+)\]", sql).group(1)
        if table in self.fail:
            raise QueryExecutionError("Query timeout expired")
        if sql.startswith("WITH g AS"):
            data = GRAINS[table]
        elif "status_value" in sql:
            return result(("status_value", "rows_2026", "distinct_policies"),
                          [("Effectuated", 700, 350), ("Cancelled", 200, 100), ("Terminated", 100, 50)])
        else:
            data = PROFILES[table]
        return result(tuple(data), [tuple(data.values())])

    @staticmethod
    def assert_params(params):
        if tuple(params) != (2026,):
            raise AssertionError(params)


class SqlTests(unittest.TestCase):
    def test_catalog_sql_is_read_only_and_excludes_inbound_names(self):
        assert_read_only(CATALOG_SQL)
        for marker in ("834", "inbound", "rcni", "sftp"):
            self.assertIn(f"NOT LIKE '%{marker}%'", CATALOG_SQL)
        self.assertNotIn("2027", CATALOG_SQL)

    def test_excluded_names_are_dropped_even_if_returned(self):
        names = [item["table_name"] for item in parse_catalog(result(
            ("table_schema", "table_name", "table_type", "column_name", "data_type"),
            [("dbo", "Enrollments_834_Inbound", "BASE TABLE", "enrollment_id", "bigint"),
             ("dbo", "RCNI_Enrollments", "VIEW", "enrollment_id", "bigint"),
             ("dbo", "Enrollments_PY2026", "BASE TABLE", "enrollment_id", "bigint")],
        ))]
        self.assertEqual(names, ["Enrollments_PY2026"])

    def test_profile_sql_is_aggregate_only_and_py2026_only(self):
        item = source("Enrollments_PY2026")
        for sql, params in (build_profile_sql(item), build_grain_sql(item),
                            build_status_sql(item, "enrollment_status")):
            assert_read_only(sql)
            self.assertEqual(params, (2026,))
            self.assertNotIn("2027", sql)
            self.assertNotIn("SELECT *", sql.upper())
            self.assertNotRegex(sql, r"SELECT\s+\[enrollment_id\]")
            self.assertNotRegex(sql, r"SELECT\s+\[enrollee_id\]")
        status_sql, _ = build_status_sql(item, "enrollment_status")
        self.assertIn("GROUP BY [enrollment_status_description]", status_sql)

    def test_name_similar_columns_are_listed_but_never_queried(self):
        item = source("Enrollments_X", FULL_COLUMNS[1:] + (("plan_year", "int"),))
        concepts = map_concepts(item["columns"])
        self.assertEqual(concepts["coverage_year"], {"status": NAME_SIMILAR, "columns": ["plan_year"]})
        self.assertEqual(concepts["exchange_policy_identifier"]["status"], NAME_SIMILAR)
        self.assertEqual(concepts["policy_identifier"]["status"], EXACT_NAME)
        classification, evidence = classify(item, concepts, None, None, None)
        self.assertEqual(classification, INSUFFICIENT)
        self.assertIn("coverage_year", evidence[0])


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.item = source("Enrollments_PY2026")
        self.concepts = map_concepts(self.item["columns"])

    def grain(self, keys, repeated, statuses=0, days=0):
        return {"grain_keys": keys, "keys_with_repeated_rows": repeated,
                "keys_with_multiple_statuses": statuses, "keys_with_multiple_update_days": days}

    def test_one_row_per_policy_member_is_latest_state(self):
        self.assertEqual(classify(self.item, self.concepts, {"rows_2026": 10}, self.grain(900, 0), None)[0],
                         LATEST_STATE)

    def test_versions_per_policy_member_is_full_record(self):
        self.assertEqual(classify(self.item, self.concepts, {"rows_2026": 10}, self.grain(900, 200, 50, 150), None)[0],
                         FULL_RECORD)

    def test_system_versioned_table_is_full_record(self):
        self.assertEqual(classify(self.item, self.concepts, {"rows_2026": 10}, None,
                                  "SYSTEM_VERSIONED_TEMPORAL_TABLE")[0], FULL_RECORD)

    def test_unexplained_duplicates_are_insufficient(self):
        self.assertEqual(classify(self.item, self.concepts, {"rows_2026": 10}, self.grain(900, 200), None)[0],
                         INSUFFICIENT)

    def test_failed_profile_is_not_zero_rows(self):
        failed = classify(self.item, self.concepts, None, None, None)
        empty = classify(self.item, self.concepts, {"rows_2026": 0}, None, None)
        self.assertEqual((failed[0], empty[0]), (INSUFFICIENT, INSUFFICIENT))
        self.assertIn("N/A", failed[1][0])
        self.assertIn("No coverage_year = 2026 rows", empty[1][0])

    def test_policy_only_grain_is_insufficient(self):
        item = source("Enrollments_P", tuple(c for c in FULL_COLUMNS if c[0] != "enrollee_id"))
        self.assertEqual(classify(item, map_concepts(item["columns"]), {"rows_2026": 10},
                                  self.grain(900, 300, 0, 100), None)[0], INSUFFICIENT)

    def test_snapshot_and_aggregate(self):
        snap = source("Enrollments_PY2526_10312026")
        self.assertEqual(classify(snap, map_concepts(snap["columns"]), {"rows_2026": 10}, None, None)[0], SNAPSHOT)
        agg = source("EnrollmentSummary", CATALOG["EnrollmentSummary"])
        self.assertEqual(classify(agg, map_concepts(agg["columns"]), None, None, None)[0], AGGREGATE)


class RecommendationTests(unittest.TestCase):
    def row(self, name, classification, policies):
        return {"db_source": name, "classification": classification, "distinct_policies": policies,
                "policy_grain_available": True, "person_grain_available": True,
                "status_fields": ["enrollment_status_description"]}

    def test_full_record_preferred_and_small_sources_rejected(self):
        rec = recommend([self.row("dbo.A", LATEST_STATE, 900), self.row("dbo.B", FULL_RECORD, 500),
                         self.row("dbo.C", FULL_RECORD, 10)], {"distinct_policies": 480})
        self.assertEqual(rec["source"], "dbo.B")
        self.assertEqual(rec["alternatives"], ["dbo.A"])

    def test_tie_and_nothing_eligible_recommend_none(self):
        tie = recommend([self.row("dbo.A", LATEST_STATE, 500), self.row("dbo.B", LATEST_STATE, 500)], None)
        self.assertIsNone(tie["source"])
        none = recommend([self.row("dbo.A", SNAPSHOT, 500)], None)
        self.assertIsNone(none["source"])
        self.assertTrue(none["reason"].startswith("INSUFFICIENT_EVIDENCE"))


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, **kwargs):
        client = FakeClient(**kwargs)
        return client, build_report(collect(client))

    def test_enrollments_py2026_recommended_and_named_objects_reported(self):
        client, report = self.run_pipeline()
        by_name = {row["db_source"]: row for row in report["sources"]}
        self.assertNotIn("dbo.Enrollments_834_Inbound", by_name)
        self.assertFalse(any("834" in sql and "INFORMATION_SCHEMA" not in sql for sql in client.statements))
        self.assertEqual(by_name["dbo.Enrollments_PY2026"]["classification"], LATEST_STATE)
        self.assertEqual(by_name["dbo.Enrollments_PY2526_10312026"]["classification"], SNAPSHOT)
        self.assertEqual(by_name["dbo.Enrollments_PY2526_DEC31"]["classification"], SNAPSHOT)
        self.assertIsNone(by_name["dbo.Enrollments_PY2526_DEC31"]["row_count"])
        self.assertEqual(by_name["dbo.EnrollmentSummary"]["classification"], AGGREGATE)
        self.assertEqual(by_name["dbo.Enrollee_Contact"]["classification"], INSUFFICIENT)
        self.assertEqual(report["baseline_snapshot"]["distinct_policies"], 480)
        self.assertEqual(report["recommendation"]["source"], "dbo.Enrollments_PY2026")
        self.assertEqual([item["exists"] for item in report["focus_objects"]], [True, True])
        self.assertFalse(report["production_comparable"])

    def test_missing_test_table_and_failed_profile_stay_na(self):
        catalog = {name: cols for name, cols in CATALOG.items() if name != "Enrollments_TEST"}
        _, report = self.run_pipeline(catalog=catalog, fail=("Enrollments_PY2026",))
        self.assertEqual(report["focus_objects"][1], {"object": "Enrollments_TEST", "exists": False,
                                                      "found_as": [], "classification": None})
        row = next(item for item in report["sources"] if item["db_source"] == "dbo.Enrollments_PY2026")
        self.assertIsNone(row["py2026_present"])
        self.assertIsNone(row["row_count"])
        self.assertEqual(row["classification"], INSUFFICIENT)
        self.assertIsNone(report["recommendation"]["source"])
        self.assertTrue(report["failures"])
        self.assertIn("N/A", render_html(report))

    def test_temporal_history_table_wins(self):
        catalog = dict(CATALOG, Enrollments_History=FULL_COLUMNS)
        PROFILES["Enrollments_History"] = {"rows_2026": 3000, "distinct_policies": 500, "distinct_persons": 900}
        GRAINS["Enrollments_History"] = {"grain_keys": 900, "keys_with_repeated_rows": 800, "max_rows_per_key": 6,
                                         "keys_with_multiple_statuses": 300, "keys_with_multiple_update_days": 700}
        try:
            _, report = self.run_pipeline(catalog=catalog)
        finally:
            PROFILES.pop("Enrollments_History")
            GRAINS.pop("Enrollments_History")
        self.assertEqual(report["recommendation"]["source"], "dbo.Enrollments_History")
        self.assertEqual(report["recommendation"]["classification"], FULL_RECORD)

    def test_report_has_aggregates_only(self):
        _, report = self.run_pipeline()
        output = io.StringIO()
        with redirect_stdout(output):
            print_summary(report)
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(report, Path(tmp))
            written = json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")
        for text in (json.dumps(report, default=str), output.getvalue(), written):
            self.assertNotIn(SENTINEL_ID, text)
            self.assertNotIn("Enrollments_834_Inbound", text)
        with self.assertRaises(ComparisonOutputError):
            guard_no_pii({"sources": [{"enrollee_id": 1}]})


if __name__ == "__main__":
    unittest.main()
