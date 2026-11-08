"""Lifecycle alignment. Fake catalog results and a local sqlite file only."""

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE
from comparison.lifecycle import (
    CATALOG_SQL,
    EXACT,
    NOT_AVAILABLE,
    NOT_PROVEN,
    PERSON_UNPROVEN,
    RECONSTRUCTABLE,
    SHIFTED,
    SNAPSHOT_NEAR,
    UNCERTAIN,
    build_report,
    classify_asof,
    collect_database_evidence,
    infer_filename_date,
    official_comparison,
    read_observation,
    shift_benefit_window,
    shift_one_calendar_year,
    snapshot_rows,
    write_report,
)
from database.models import QueryExecutionError, QueryResult
from database.query_runner import assert_read_only


def payload() -> dict:
    return json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))


def observation(day: str = "2026-11-08T15:04:05Z", value: int = 400000) -> dict:
    return {
        "source": "metric_snapshots.retrieved_at_utc",
        "retrieved_at_utc": day,
        "observation_date": day[:10],
        "metric_value": value,
        "run_id": "run-1",
        "snapshot_slot_utc": f"{day[:10]}T15:00:00Z",
        "run_started_at_utc": day,
        "run_finished_at_utc": day,
    }


def result(columns, rows):
    materialized = tuple(tuple(row) for row in rows)
    return QueryResult(columns=tuple(columns), rows=materialized, row_count=len(materialized), duration_seconds=0.0)


COLUMNS = (
    "coverage_year", "enrollee_id", "enrollment_id", "enrollment_status_description",
    "enrollee_status_description", "benefit_effective_date", "benefit_end_date",
    "enrollment_confirmation_date", "enrollment_create_date", "enrollment_last_update_date",
    "hios_issuer_id",
)


def catalog_rows(*tables):
    rows = []
    for table, extra in tables:
        for column in (*COLUMNS, *extra):
            rows.append(("dbo", table, "BASE TABLE", column, "varchar"))
    return rows


class FakeClient:
    def __init__(self, rows, fail_step=None):
        self.rows = rows
        self.fail_step = fail_step
        self.calls = []

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        assert_read_only(sql)
        if self.fail_step and self.fail_step in sql:
            raise QueryExecutionError("query timed out")
        if "INFORMATION_SCHEMA" in sql:
            return result(
                ("table_schema", "table_name", "table_type", "column_name", "data_type"),
                self.rows,
            )
        if "AS distinct_enrollee_id" in sql or "FROM pairs" in sql:
            if "FROM pairs" in sql:
                return result(
                    ("distinct_enrollee_id", "distinct_exchange_identifier", "a", "b", "c", "d"),
                    [(8, 8, 8, 0, 8, 0)],
                )
            return result(("distinct_enrollee_id",), [(12,)])
        if "AS distinct_dates" in sql:
            return result(("min_date", "max_date", "distinct_dates"), [(date(2025, 11, 8), date(2025, 11, 8), 1)])
        self.assert_year_probe(sql, params)
        return result(("coverage_year",), [(2026,)])

    def assert_year_probe(self, sql, params):
        if "2027" in sql:
            raise AssertionError(sql)
        if params is not None:
            if params != (2026,):
                raise AssertionError(params)
            if "2027" in params:
                raise AssertionError(params)


class AlignmentTests(unittest.TestCase):
    def test_observation_date_comes_from_stored_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE pipeline_runs (
                    run_id TEXT PRIMARY KEY, started_at_utc TEXT, finished_at_utc TEXT, status TEXT
                );
                CREATE TABLE metric_snapshots (
                    run_id TEXT, metric_id TEXT, metric_value NUMERIC, plan_year INTEGER,
                    retrieved_at_utc TEXT, snapshot_slot_utc TEXT, retrieval_status TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO pipeline_runs VALUES ('run-1', '2026-04-02T11:00:00Z', '2026-04-02T11:05:00Z', 'SUCCESS')")
            connection.execute(
                """INSERT INTO metric_snapshots VALUES
                   ('run-1', 'currently_enrolled_people', 321, 2027, '2026-04-02T11:04:00Z',
                    '2026-04-02T11:00:00Z', 'VALIDATED')""")
            connection.commit()
            connection.close()
            found = read_observation(path)
        self.assertEqual(found["observation_date"], "2026-04-02")
        self.assertEqual(found["source"], "metric_snapshots.retrieved_at_utc")
        self.assertEqual(found["metric_value"], 321)
        self.assertIsNone(read_observation(path.parent / "missing.sqlite3"))

    def test_leap_day_shifts_to_february_28(self):
        self.assertEqual(shift_one_calendar_year(date(2026, 4, 2)), date(2025, 4, 2))
        self.assertEqual(shift_one_calendar_year(date(2024, 2, 29)), date(2023, 2, 28))

    def test_benefit_window_shifts_only_when_the_pattern_matches(self):
        shifted = shift_benefit_window(2027, date(2026, 10, 1), date(2027, 12, 31))
        self.assertEqual(shifted["translation_status"], SHIFTED)
        self.assertEqual(shifted["from"], "2025-10-01")
        self.assertEqual(shifted["to"], "2026-12-31")
        other = shift_benefit_window(2027, date(2026, 10, 1), date(2027, 6, 30))
        self.assertEqual(other["translation_status"], UNCERTAIN)
        self.assertIsNone(other["from"])

    def test_missing_observation_does_not_invent_a_date(self):
        report = build_report(payload(), None)
        self.assertIsNone(report["py2027_observation_date"])
        self.assertIsNone(report["py2026_equivalent_lifecycle_date"])
        self.assertFalse(report["official_comparison"])
        self.assertIsNone(report["py2026_currently_enrolled_people"])
        self.assertIsNone(report["py2027_currently_enrolled_people"])


class SnapshotTests(unittest.TestCase):
    def _sources(self):
        tables = catalog_rows(
            ("Enrollments_PY2526_11082025", ()),
            ("Enrollments_PY2526_01312026", ()),
            ("Enrollments_PY2526_NOV30", ()),
            ("Enrollments_PY2526_11072025", ()),
        )
        client = FakeClient(tables)
        evidence = collect_database_evidence(client, date(2025, 11, 8))
        return client, evidence

    def test_filename_date_is_not_an_asof_and_nov30_is_not_chosen(self):
        client, evidence = self._sources()
        report = build_report(payload(), observation(), evidence["sources"], evidence["proven_dates"],
                               evidence["diagnostics"], evidence["failures"], raw_row_count=900000)
        self.assertEqual(report["asof_outcome"], NOT_PROVEN)
        self.assertEqual(report["py2026_source"]["table"], "Enrollments_PY2526_11082025")
        self.assertFalse(report["py2026_source"]["selected_for_official_comparison"])
        self.assertFalse(report["official_comparison"])
        self.assertIsNone(report["py2026_currently_enrolled_people"])
        self.assertIsNone(report["absolute_difference"])
        self.assertEqual(report["py2027_stored_observation_value"], 400000)
        self.assertIsNone(report["py2027_currently_enrolled_people"])
        self.assertIn("raw_row_count", report["refused_inputs"])
        self.assertNotIn("900000", json.dumps(report))
        by_name = {item["table"]: item for item in report["snapshots"]}
        self.assertEqual(by_name["Enrollments_PY2526_01312026"]["inferred_filename_date"], "2026-01-31")
        self.assertFalse(by_name["Enrollments_PY2526_01312026"]["date_independently_supported"])
        self.assertIsNone(by_name["Enrollments_PY2526_NOV30"]["inferred_filename_date"])
        self.assertEqual(report["person_identifier"]["identity_status"], PERSON_UNPROVEN)
        self.assertEqual(report["diagnostics"]["distinct_enrollee_id"], 12)
        self.assertNotEqual(report["diagnostics"]["distinct_enrollee_id"], report["py2026_currently_enrolled_people"])
        for sql, params in client.calls:
            if "INFORMATION_SCHEMA" not in sql:
                self.assertNotIn("2027", sql)
                if params is not None:
                    self.assertEqual(params, (2026,))
            self.assertNotIn("[dbo].[inbound_automation]", sql)

    def test_tied_distances_do_not_query_a_population(self):
        rows = catalog_rows(
            ("Enrollments_PY2526_11072025", ()),
            ("Enrollments_PY2526_11092025", ()),
        )
        client = FakeClient(rows)
        evidence = collect_database_evidence(client, date(2025, 11, 8))
        self.assertIsNone(evidence["diagnostics"])
        self.assertFalse(any("distinct_enrollee_id" in sql for sql, _params in client.calls))

    def test_exact_exchange_identifier_still_does_not_make_an_official_total(self):
        rows = catalog_rows(("Enrollments_PY2526_11082025", ("exchg_indiv_identifier", "person_type_description",
                                                             "insurance_type_description")))
        client = FakeClient(rows)
        evidence = collect_database_evidence(client, date(2025, 11, 8))
        report = build_report(payload(), observation(), evidence["sources"], evidence["proven_dates"],
                               evidence["diagnostics"])
        self.assertEqual(report["person_identifier"]["translation_status"], EXACT)
        self.assertEqual(report["person_identifier"]["py2026_db_column"], "exchg_indiv_identifier")
        self.assertEqual(report["diagnostics"]["one_to_one"]["person_keys_with_multiple_exchange_ids"], 0)
        self.assertFalse(report["official_comparison"])
        statuses = {row["sisense_concept"]: row["translation_status"] for row in report["filter_mapping"]}
        self.assertEqual(statuses["Plan year"], SHIFTED)
        self.assertEqual(statuses["Benefit effective date"], SHIFTED)
        self.assertEqual(statuses["Enrollment status"], UNCERTAIN)
        self.assertEqual(statuses["Person type"], EXACT)
        self.assertEqual(statuses["Insurance type"], EXACT)

    def test_proven_snapshot_date_on_the_lifecycle_date_is_outcome_b(self):
        rows = catalog_rows(("Enrollments_PY2526_01012020", ("snapshot_date",)))
        client = FakeClient(rows)
        evidence = collect_database_evidence(client, date(2025, 11, 8))
        report = build_report(payload(), observation(), evidence["sources"], evidence["proven_dates"],
                               evidence["diagnostics"])
        self.assertEqual(report["asof_outcome"], SNAPSHOT_NEAR)
        item = report["snapshots"][0]
        self.assertTrue(item["date_independently_supported"])
        self.assertEqual(item["proven_snapshot_date"], "2025-11-08")
        self.assertEqual(item["distance_days_from_proven_date"], 0)
        self.assertFalse(item["date_independently_supported"] and item["inferred_filename_date"] == "2025-11-08")
        self.assertFalse(report["official_comparison"])

    def test_missing_columns_are_not_available(self):
        rows = catalog_rows(("Enrollments_PY2526_11082025", ()))
        evidence = collect_database_evidence(FakeClient(rows), date(2025, 11, 8))
        report = build_report(payload(), observation(), evidence["sources"], evidence["proven_dates"])
        statuses = {row["sisense_concept"]: row["translation_status"] for row in report["filter_mapping"]}
        self.assertEqual(statuses["Person type"], NOT_AVAILABLE)
        self.assertEqual(statuses["Insurance type"], NOT_AVAILABLE)

    def test_invalid_filename_date_is_not_inferred(self):
        self.assertIsNone(infer_filename_date("Enrollments_PY2526_02312026"))
        self.assertIsNone(infer_filename_date("Enrollments_PY2526_NOV30"))


class GateTests(unittest.TestCase):
    def test_official_gate_requires_asof_person_and_filters(self):
        person = {"translation_status": EXACT}
        clear = [{"translation_status": SHIFTED}, {"translation_status": EXACT}]
        blocked = [{"translation_status": UNCERTAIN}]
        seen = observation()
        self.assertEqual(official_comparison(RECONSTRUCTABLE, person, clear, seen), (True, "HIGH", "EXACT_LIFECYCLE"))
        self.assertEqual(official_comparison(SNAPSHOT_NEAR, person, clear, seen), (True, "MEDIUM", "NEAREST_HISTORICAL_SNAPSHOT"))
        self.assertEqual(official_comparison(NOT_PROVEN, person, clear, seen)[0], False)
        self.assertEqual(official_comparison(RECONSTRUCTABLE, person, blocked, seen)[0], False)
        self.assertEqual(official_comparison(RECONSTRUCTABLE, {"translation_status": UNCERTAIN}, clear, seen)[1], "LOW")

    def test_catalog_queries_are_read_only(self):
        assert_read_only(CATALOG_SQL)
        self.assertNotIn("DELETE", CATALOG_SQL.upper())
        self.assertNotIn("SELECT *", CATALOG_SQL.upper())

    def test_output_has_no_individual_identifiers(self):
        report = build_report(payload(), observation())
        with tempfile.TemporaryDirectory() as directory:
            path = write_report(report, Path(directory))
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["comparison_confidence"], "LOW")
        self._keys(loaded)

    def test_command_module_does_not_change_sisense_runtime(self):
        source = Path(__file__).resolve().parents[1].joinpath("comparison", "lifecycle.py").read_text(encoding="utf-8")
        main_source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        self.assertNotIn("SisenseJaqlClient", source)
        self.assertNotIn("import comparison", main_source)
        self.assertNotIn("comparison.lifecycle", main_source)

    def test_changed_benefit_window_is_not_shifted(self):
        body = payload()
        for item in body["metadata"]:
            jaql = item.get("jaql") or {}
            if jaql.get("column") == "benefit_effective_date":
                jaql["filter"]["to"] = "2027-06-30"
        rows = catalog_rows(("Enrollments_PY2526_11082025", ()))
        evidence = collect_database_evidence(FakeClient(rows), date(2025, 11, 8))
        report = build_report(body, observation(), evidence["sources"])
        benefit = next(row for row in report["filter_mapping"] if row["sisense_concept"] == "Benefit effective date")
        self.assertEqual(benefit["translation_status"], UNCERTAIN)
        self.assertNotIn("2025-10-01", benefit["reason"])

    def _keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id", "exchg_indiv_identifier"})
                self._keys(item)
        elif isinstance(value, list):
            for item in value:
                self._keys(item)


class ClassificationUnitTests(unittest.TestCase):
    def test_filename_match_without_a_date_column_stays_unproven(self):
        snapshots = snapshot_rows([{
            "schema_name": "dbo",
            "table_name": "Enrollments_PY2526_11082025",
            "table_type": "BASE TABLE",
            "columns": {"coverage_year": {"name": "coverage_year"}},
        }], date(2025, 11, 8))
        self.assertEqual(snapshots[0]["distance_days_from_filename"], 0)
        self.assertFalse(snapshots[0]["date_independently_supported"])
        self.assertEqual(classify_asof(snapshots), NOT_PROVEN)
        versioned = snapshot_rows([{
            "schema_name": "dbo",
            "table_name": "Enrollments_PY2526_11072025",
            "table_type": "BASE TABLE",
            "columns": {"valid_from": {"name": "valid_from"}, "valid_to": {"name": "valid_to"}},
        }], date(2025, 11, 8))
        self.assertEqual(classify_asof(versioned), RECONSTRUCTABLE)


if __name__ == "__main__":
    unittest.main()
