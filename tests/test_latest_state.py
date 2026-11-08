"""Latest-state diagnostic. Fake catalog results and a local sqlite file only."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE
from comparison.latest_state import (
    FILENAME_INFERRED,
    INVALID_FILENAME,
    LATEST_STATE,
    MONTH_DAY_TOKEN,
    NOT_RECONSTRUCTABLE,
    SEMANTIC,
    UNRESOLVED,
    build_evidence_sql,
    build_latest_state_sql,
    build_report,
    choose_ordering_fields,
    classify_snapshot_name,
    collect_evidence,
    interpret_semantic_date,
    lifecycle_distance,
    parse_latest_rows,
    print_table,
    write_report,
)
from comparison.lifecycle import infer_filename_date, official_comparison
from database.models import QueryResult
from database.query_runner import assert_read_only


def payload() -> dict:
    return json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))


def observation(day: str = "2026-11-08T15:04:05Z", value: int = 797301) -> dict:
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


COLUMNS = {
    "coverage_year": "int",
    "enrollee_id": "varchar",
    "enrollment_id": "varchar",
    "enrollment_status_description": "varchar",
    "enrollee_status_description": "varchar",
    "benefit_effective_date": "datetime2",
    "benefit_end_date": "datetime2",
    "person_type_description": "varchar",
    "insurance_type_description": "varchar",
    "enrollment_confirmation_date": "datetime2",
    "enrollment_create_date": "datetime2",
    "enrollment_last_update_date": "datetime2",
}


class FakeClient:
    def __init__(self, tables, columns=None, fail_latest=()):
        self.tables = tables
        self.columns = columns or {table: dict(COLUMNS) for _, table, _ in tables}
        self.fail_latest = set(fail_latest)
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
        if "AS people_2026" in sql or "AS rows_2026" in sql:
            table = "NOV30" if "NOV30" in sql else "DEC31"
            if table == "NOV30":
                return result(
                    ("raw_rows", "rows_2025", "rows_2026", "people_2025", "people_2026",
                     "min_enrollment_last_update_date", "max_enrollment_last_update_date",
                     "min_enrollment_confirmation_date", "max_enrollment_confirmation_date"),
                    [(10, 2, 8, 2, 7, date(2025, 10, 1), date(2025, 11, 30),
                      date(2025, 10, 15), date(2025, 11, 29))],
                )
            return result(
                ("raw_rows", "rows_2025", "rows_2026", "people_2025", "people_2026",
                 "min_enrollment_last_update_date", "max_enrollment_last_update_date"),
                [(12, 1, 11, 1, 9, date(2025, 10, 1), date(2025, 12, 31))],
            )
        if "ROW_NUMBER()" in sql:
            if any(name in sql for name in self.fail_latest):
                from database.models import QueryExecutionError
                raise QueryExecutionError("query timed out")
            if "NOV30" in sql:
                rows = [("status", "Enrolled", Decimal("50")), ("status", "Pending", Decimal("10")),
                        ("status", "Terminated", Decimal("5")), ("total", None, Decimal("65"))]
            elif "DEC31" in sql:
                rows = [("status", "Enrolled", Decimal("55")), ("status", "Cancelled", Decimal("8")),
                        ("status", "Terminated", Decimal("7")), ("total", None, Decimal("70"))]
            else:
                rows = [("status", "Enrolled", Decimal("80")), ("status", "Pending", Decimal("15")),
                        ("status", "Hold", Decimal("5")), ("total", None, Decimal("100"))]
            return result(("kind", "status_value", "people_count"), rows)
        raise AssertionError(sql)


class NameAndDateTests(unittest.TestCase):
    def test_nov30_and_dec31_are_month_day_tokens_not_inferred(self):
        nov = classify_snapshot_name("Enrollments_PY2526_NOV30")
        dec = classify_snapshot_name("Enrollments_PY2526_DEC31")
        self.assertEqual(nov["filename_status"], MONTH_DAY_TOKEN)
        self.assertEqual(nov["month_day"], {"month": 11, "day": 30})
        self.assertIsNone(nov["inferred_date"])
        self.assertEqual(dec["month_day"], {"month": 12, "day": 31})
        self.assertIsNone(infer_filename_date("Enrollments_PY2526_NOV30"))
        self.assertIsNone(infer_filename_date("Enrollments_PY2526_DEC31"))

    def test_invalid_june_31_is_not_a_snapshot_date(self):
        parsed = classify_snapshot_name("Enrollments_PY2526_06312026")
        self.assertEqual(parsed["filename_status"], INVALID_FILENAME)
        self.assertIsNone(parsed["inferred_date"])
        self.assertIsNone(infer_filename_date("Enrollments_PY2526_06312026"))
        june = classify_snapshot_name("Enrollments_PY2526_06302026")
        self.assertEqual(june["filename_status"], FILENAME_INFERRED)
        self.assertEqual(june["inferred_date"], "2026-06-30")

    def test_semantic_date_requires_matching_timestamp_max(self):
        classification = classify_snapshot_name("Enrollments_PY2526_NOV30")
        supported = interpret_semantic_date(classification, {
            "people_coverage_year_2026": 7,
            "extrema": {"max_enrollment_last_update_date": "2025-11-30"},
        })
        self.assertEqual(supported["status"], SEMANTIC)
        self.assertEqual(supported["snapshot_date"], "2025-11-30")
        unresolved = interpret_semantic_date(classification, {
            "people_coverage_year_2026": 7,
            "extrema": {"max_enrollment_last_update_date": "2026-01-31"},
        })
        self.assertEqual(unresolved["status"], UNRESOLVED)
        self.assertIsNone(unresolved["snapshot_date"])
        no_year = interpret_semantic_date(classification, {
            "people_coverage_year_2026": 0,
            "extrema": {"max_enrollment_last_update_date": "2025-11-30"},
        })
        self.assertEqual(no_year["status"], UNRESOLVED)

    def test_lifecycle_distance_is_computed(self):
        prox = lifecycle_distance("2025-11-30", date(2025, 11, 8))
        self.assertEqual(prox["distance_days"], 22)
        self.assertEqual(prox["position"], "after")
        self.assertEqual(lifecycle_distance("2026-01-31", date(2025, 11, 8))["distance_days"], 84)


class OrderingTests(unittest.TestCase):
    def test_last_update_is_preferred_and_enrollment_id_is_tiebreaker(self):
        ordering = choose_ordering_fields({
            "enrollment_last_update_date": {"name": "enrollment_last_update_date"},
            "enrollment_create_date": {"name": "enrollment_create_date"},
            "enrollment_id": {"name": "enrollment_id"},
            "enrollment_confirmation_date": {"name": "enrollment_confirmation_date"},
        })
        self.assertEqual(ordering["primary"], "enrollment_last_update_date")
        self.assertEqual(ordering["tiebreakers"], ["enrollment_create_date", "enrollment_id"])
        self.assertTrue(ordering["reconstructable"])
        sql, params = build_latest_state_sql(
            "[dbo].[Enrollments_PY2526_NOV30]", "enrollee_id",
            "enrollment_status_description",
            [{"sql": "[coverage_year] = ?", "params": [2026]}],
            ordering,
            {
                "enrollment_last_update_date": {"name": "enrollment_last_update_date"},
                "enrollment_create_date": {"name": "enrollment_create_date"},
                "enrollment_id": {"name": "enrollment_id"},
                "enrollment_status_description": {"name": "enrollment_status_description"},
            },
        )
        assert_read_only(sql)
        self.assertIn("ROW_NUMBER()", sql)
        self.assertIn("PARTITION BY person_key", sql)
        self.assertIn("order_ts DESC", sql)
        self.assertIn("tie_id DESC", sql)
        self.assertNotIn("SELECT *", sql.upper())
        self.assertEqual(params, (2026,))
        self.assertIn("[enrollee_id] AS person_key", sql)

    def test_missing_ordering_field_is_not_reconstructable(self):
        ordering = choose_ordering_fields({
            "enrollment_confirmation_date": {"name": "enrollment_confirmation_date"},
            "benefit_effective_date": {"name": "benefit_effective_date"},
            "enrollee_id": {"name": "enrollee_id"},
        })
        self.assertFalse(ordering["reconstructable"])
        self.assertIsNone(ordering["primary"])

    def test_categories_reconcile_when_each_person_has_one_latest_status(self):
        parsed = parse_latest_rows([
            ("status", "Enrolled", 50),
            ("status", "Pending", 10),
            ("status", "Cancelled", 5),
            ("status", "Terminated", 5),
            ("total", None, 70),
        ])
        self.assertTrue(parsed["reconciles"])
        self.assertEqual(parsed["discrepancy"], 0)
        self.assertEqual(parsed["latest_state_total_people"], 70)
        broken = parse_latest_rows([
            ("status", "Enrolled", 50),
            ("status", "Pending", 10),
            ("total", None, 70),
        ])
        self.assertFalse(broken["reconciles"])
        self.assertEqual(broken["discrepancy"], -10)


class CollectTests(unittest.TestCase):
    def test_priority_snapshots_and_invalid_june_are_handled(self):
        client = FakeClient([
            ("dbo", "Enrollments_PY2526_NOV30", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_DEC31", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_06312026", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_06302026", "BASE TABLE"),
        ])
        evidence = collect_evidence(client, payload(), observation())
        tables = [item["table"] for item in evidence["latest_state"]]
        self.assertEqual(tables[0], "Enrollments_PY2526_NOV30")
        self.assertEqual(tables[1], "Enrollments_PY2526_DEC31")
        self.assertEqual(tables[2], "Enrollments_PY2526_01312026")
        self.assertIn("Enrollments_PY2526_06302026", tables)
        self.assertNotIn("Enrollments_PY2526_06312026", tables)
        self.assertEqual(evidence["invalid_filename_dates"][0]["filename_status"], INVALID_FILENAME)
        nov = evidence["date_evidence"][0]
        self.assertEqual(nov["semantic_status"], SEMANTIC)
        self.assertEqual(nov["snapshot_date"], "2025-11-30")
        self.assertEqual(nov["coverage_year_distribution"]["people_2026"], 7)
        dec = evidence["date_evidence"][1]
        self.assertEqual(dec["snapshot_date"], "2025-12-31")
        by_table = {item["table"]: item for item in evidence["latest_state"]}
        self.assertEqual(by_table["Enrollments_PY2526_NOV30"]["latest_state_total_people"], 65)
        self.assertEqual(by_table["Enrollments_PY2526_NOV30"]["enrolled_people"], 50)
        self.assertTrue(by_table["Enrollments_PY2526_NOV30"]["reconciles"])
        self.assertEqual(by_table["Enrollments_PY2526_DEC31"]["latest_state_total_people"], 70)
        self.assertEqual(by_table["Enrollments_PY2526_01312026"]["latest_state_total_people"], 100)
        self.assertEqual(by_table["Enrollments_PY2526_01312026"]["other_people"], 5)
        self.assertEqual(by_table["Enrollments_PY2526_NOV30"]["lifecycle_proximity"]["distance_days"], 22)
        self.assertEqual(by_table["Enrollments_PY2526_NOV30"]["person_identifier"], "ENROLLEE_ID_PROXY")
        self.assertEqual(evidence["reference"]["currently_enrolled_people"], 797301)
        self.assertNotIn("06312026", "".join(sql for sql, _ in client.calls if "ROW_NUMBER()" in sql))

    def test_partial_failure_keeps_other_snapshots(self):
        client = FakeClient([
            ("dbo", "Enrollments_PY2526_NOV30", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
        ], fail_latest={"01312026"})
        evidence = collect_evidence(client, payload(), observation())
        by_table = {item["table"]: item for item in evidence["latest_state"]}
        self.assertEqual(by_table["Enrollments_PY2526_NOV30"]["latest_state_total_people"], 65)
        self.assertEqual(by_table["Enrollments_PY2526_01312026"]["latest_state_status"], NOT_RECONSTRUCTABLE)

    def test_enrollee_id_fallback_and_exchange_preference(self):
        from comparison.diagnostic_numbers import choose_person_column
        self.assertEqual(choose_person_column({
            "exchg_indiv_identifier": {"name": "exchg_indiv_identifier"},
            "enrollee_id": {"name": "enrollee_id"},
        })["column"], "exchg_indiv_identifier")
        self.assertEqual(choose_person_column({
            "enrollee_id": {"name": "enrollee_id"},
        })["label"], "ENROLLEE_ID_PROXY")


class ReportTests(unittest.TestCase):
    def test_report_keeps_concepts_separate_and_loads_reference(self):
        report = build_report(payload(), observation(), latest_state=[{
            "table": "Enrollments_PY2526_NOV30",
            "latest_state_total_people": 65,
            "enrolled_people": 50,
            "pending_people": 10,
            "cancelled_people": 0,
            "terminated_people": 5,
            "other_people": 0,
            "lifecycle_proximity": {"distance_days_absolute": 22},
        }])
        self.assertEqual(report["report_kind"], LATEST_STATE)
        self.assertIn("SNAPSHOT POPULATION", report["concepts_preserved"])
        self.assertIn("MONTHLY ENROLLMENT ACTIVITY", report["concepts_preserved"])
        self.assertIn("LATEST STATE POPULATION", report["concepts_preserved"])
        self.assertFalse(report["lifecycle_equivalent"])
        self.assertEqual(report["dataset_py2027_current_reference"]["currently_enrolled_people"], 797301)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_table(report)
        text = buffer.getvalue()
        self.assertIn("65", text)
        self.assertIn("797301", text)
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
        self.assertNotIn("<svg", html.lower())
        self._keys(loaded)
        source = Path(__file__).resolve().parents[1].joinpath("comparison", "latest_state.py").read_text(encoding="utf-8")
        self.assertNotIn("797301", source)
        self.assertNotIn("SisenseJaqlClient", source)
        main_source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        self.assertNotIn("comparison.latest_state", main_source)
        self.assertFalse(official_comparison(
            "HISTORICAL_ASOF_NOT_PROVEN", {"translation_status": "EXACT"},
            [{"translation_status": "EXACT"}], observation(),
        )[0])

    def test_py2027_reference_from_sqlite(self):
        from comparison.lifecycle import read_observation
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
                "INSERT INTO pipeline_runs VALUES ('run-1', '2026-11-08T12:00:00Z', '2026-11-08T12:05:00Z', 'SUCCESS')")
            connection.execute(
                """INSERT INTO metric_snapshots VALUES
                   ('run-1', 'currently_enrolled_people', 797301, 2027, '2026-11-08T12:04:00Z',
                    '2026-11-08T12:00:00Z', 'VALIDATED')""")
            connection.commit()
            connection.close()
            found = read_observation(path)
        self.assertEqual(found["observation_date"], "2026-11-08")
        self.assertEqual(found["metric_value"], 797301)

    def test_evidence_sql_is_parameterized(self):
        sql, params = build_evidence_sql(
            "[dbo].[Enrollments_PY2526_NOV30]",
            {
                "coverage_year": {"name": "coverage_year"},
                "enrollee_id": {"name": "enrollee_id"},
                "enrollment_last_update_date": {"name": "enrollment_last_update_date"},
            },
            "enrollee_id",
        )
        assert_read_only(sql)
        self.assertEqual(params.count(2025) + params.count(2026), len(params))
        self.assertNotIn("2025-11-30", sql)

    def _keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id", "exchg_indiv_identifier"})
                self._keys(item)
        elif isinstance(value, list):
            for item in value:
                self._keys(item)


if __name__ == "__main__":
    unittest.main()
