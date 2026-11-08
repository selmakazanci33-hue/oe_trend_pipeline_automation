"""PY2026 monthly snapshot population. Fake catalog results and a local sqlite file only."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE
from comparison.lifecycle import infer_filename_date, official_comparison, read_observation
from comparison.monthly_population import (
    ACTIVITY_LABEL,
    FILENAME_INFERRED,
    PY2027_NOT_VERIFIED,
    build_activity_sql,
    build_report,
    build_snapshot_sql,
    categorize_status,
    collapse_categories,
    collect_evidence,
    dated_snapshots,
    latest_summary,
    parse_profile_rows,
    print_table,
    write_report,
)
from database.models import QueryExecutionError, QueryResult
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
    "enrollment_status_description": "varchar",
    "enrollee_status_description": "varchar",
    "benefit_effective_date": "datetime2",
    "person_type_description": "varchar",
    "insurance_type_description": "varchar",
    "enrollment_confirmation_date": "datetime2",
}

JAN = [
    ("total", None, Decimal("100")),
    ("enrollment_status", "Enrolled", Decimal("80")),
    ("enrollment_status", "Pending", Decimal("15")),
    ("enrollment_status", "Terminated", Decimal("10")),
    ("enrollment_status", "Cancelled", Decimal("5")),
    ("enrollment_status", "UnknownX", Decimal("4")),
    ("enrollment_multi", None, Decimal("8")),
    ("enrollee_status", "Enrolled", Decimal("70")),
    ("enrollee_multi", None, Decimal("2")),
]
FEB = [
    ("total", None, Decimal("120")),
    ("enrollment_status", "Enrolled", Decimal("90")),
    ("enrollment_status", "Pending", Decimal("20")),
    ("enrollment_status", "Terminated", Decimal("10")),
    ("enrollment_multi", None, Decimal("0")),
    ("enrollee_status", "Enrolled", Decimal("90")),
    ("enrollee_multi", None, Decimal("0")),
]


class FakeClient:
    def __init__(self, tables, fail_tables=()):
        self.tables = tables
        self.fail_tables = set(fail_tables)
        self.calls = []
        self.column_sets = {table: dict(COLUMNS) for _, table, _ in tables}

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        assert_read_only(sql)
        if "2027" in sql:
            raise AssertionError(sql)
        if params and 2027 in params:
            raise AssertionError(params)
        if "LIKE 'Enrollments[_]PY2526%'" in sql:
            return result(("table_schema", "table_name", "table_type"), self.tables)
        if "ORDINAL_POSITION" in sql:
            table = params[1]
            cols = self.column_sets[table]
            return result(("column_name", "data_type"), list(cols.items()))
        if "newly_confirmed_people" in sql:
            return result(("activity_year", "activity_month", "newly_confirmed_people"), [
                (2025, 11, Decimal("40")),
                (2025, 12, Decimal("30")),
                (2026, 1, Decimal("20")),
            ])
        if "AS kind" in sql:
            table = "Enrollments_PY2526_01312026"
            if "02272026" in sql:
                table = "Enrollments_PY2526_02272026"
            if table in self.fail_tables:
                raise QueryExecutionError("query timed out")
            rows = FEB if "02272026" in sql else JAN
            return result(("kind", "status_value", "people_count"), rows)
        raise AssertionError(sql)


class ParsingTests(unittest.TestCase):
    def test_filename_dates_and_ordering(self):
        dated, undated, excluded = dated_snapshots([
            {"schema_name": "dbo", "table": "Enrollments_PY2526_02272026"},
            {"schema_name": "dbo", "table": "Enrollments_PY2526_NOV30"},
            {"schema_name": "dbo", "table": "Enrollments_PY2526_01312026"},
            {"schema_name": "dbo", "table": "Enrollments_PY2526_inbound_01312026"},
        ])
        self.assertEqual([item["table"] for item in dated], [
            "Enrollments_PY2526_01312026", "Enrollments_PY2526_02272026",
        ])
        self.assertEqual(dated[0]["inferred_filename_date"], "2026-01-31")
        self.assertEqual(dated[0]["snapshot_date_evidence"], FILENAME_INFERRED)
        self.assertEqual(undated[0]["table"], "Enrollments_PY2526_NOV30")
        self.assertEqual(excluded[0]["table"], "Enrollments_PY2526_inbound_01312026")
        self.assertIsNone(infer_filename_date("Enrollments_PY2526_NOV30"))

    def test_duplicate_inferred_dates_are_flagged_and_not_chosen_as_latest(self):
        dated, _undated, _excluded = dated_snapshots([
            {"schema_name": "dbo", "table": "Enrollments_PY2526_01312026"},
            {"schema_name": "archive", "table": "Enrollments_PY2526_01312026"},
        ])
        self.assertTrue(all(item["duplicate_inferred_date"] for item in dated))
        series = [
            {"snapshot_date": "2026-01-31", "source_table": "dbo.a", "total_people": 10,
             "enrolled_people": 1, "pending_people": 0, "cancelled_people": 0,
             "terminated_people": 0, "other_people": 0},
            {"snapshot_date": "2026-01-31", "source_table": "archive.a", "total_people": 11,
             "enrolled_people": 1, "pending_people": 0, "cancelled_people": 0,
             "terminated_people": 0, "other_people": 0},
        ]
        summary = latest_summary(series)
        self.assertIsNone(summary["selected"])
        self.assertEqual(summary["tied_tables"], ["dbo.a", "archive.a"])

    def test_status_mapping_keeps_unknown_labels(self):
        self.assertEqual(categorize_status("Enrolled"), "ENROLLED")
        self.assertEqual(categorize_status("Canceled"), "CANCELLED")
        self.assertEqual(categorize_status("Cancelled"), "CANCELLED")
        self.assertEqual(categorize_status("Hold"), "OTHER")
        collapsed = collapse_categories([
            {"status_value": "Enrolled", "people_count": 80},
            {"status_value": "Hold", "people_count": 4},
            {"status_value": None, "people_count": 1},
        ])
        self.assertEqual(collapsed["enrolled_people"], 80)
        self.assertEqual(collapsed["other_people"], 5)
        self.assertEqual(collapsed["unknown_status_values"][0]["status_value"], "Hold")

    def test_overlap_prevents_additive_claim(self):
        parsed = parse_profile_rows(JAN, {
            "enrollment_status_description": {"name": "enrollment_status_description"},
            "enrollee_status_description": {"name": "enrollee_status_description"},
        })
        self.assertEqual(parsed["total_people"], 100)
        self.assertEqual(parsed["categories"]["enrolled_people"], 80)
        self.assertEqual(parsed["categories"]["cancelled_people"], 5)
        self.assertEqual(parsed["categories"]["other_people"], 4)
        self.assertFalse(parsed["status_overlap"]["status_counts_additive"])
        self.assertEqual(parsed["status_overlap"]["people_with_multiple_enrollment_statuses"], 8)
        self.assertNotEqual(
            parsed["categories"]["enrolled_people"]
            + parsed["categories"]["pending_people"]
            + parsed["categories"]["cancelled_people"]
            + parsed["categories"]["terminated_people"],
            parsed["total_people"],
        )


class IdentifierAndFilterTests(unittest.TestCase):
    def test_exchange_identifier_is_preferred_over_enrollee_id(self):
        from comparison.diagnostic_numbers import choose_person_column
        person = choose_person_column({
            "enrollee_id": {"name": "enrollee_id"},
            "exchg_indiv_identifier": {"name": "exchg_indiv_identifier"},
        })
        self.assertEqual(person["column"], "exchg_indiv_identifier")
        fallback = choose_person_column({"enrollee_id": {"name": "enrollee_id"}})
        self.assertEqual(fallback["label"], "ENROLLEE_ID_PROXY")

    def test_snapshot_sql_is_read_only_and_omits_missing_filters(self):
        sql, params = build_snapshot_sql(
            "[dbo].[Enrollments_PY2526_01312026]",
            "enrollee_id",
            [{"sql": "[coverage_year] = ?", "params": [2026]}],
            {"enrollment_status_description": {"name": "enrollment_status_description"}},
        )
        assert_read_only(sql)
        self.assertNotIn("SELECT *", sql.upper())
        self.assertIn("COUNT_BIG(DISTINCT person_key)", sql)
        self.assertIn("[enrollee_id] AS person_key", sql)
        self.assertNotIn("enrollment_id", sql)
        self.assertEqual(params, (2026,))
        self.assertNotIn("Enrolled", sql)
        activity_sql, activity_params = build_activity_sql(
            "[dbo].[Enrollments_PY2526_01312026]", "enrollee_id",
            "enrollment_confirmation_date",
            [{"sql": "[coverage_year] = ?", "params": [2026]}],
        )
        assert_read_only(activity_sql)
        self.assertIn("MIN([enrollment_confirmation_date])", activity_sql)
        self.assertNotIn("person_key", activity_sql.split("FROM first_confirmation", 1)[1])
        self.assertEqual(activity_params, (2026,))


class ReportTests(unittest.TestCase):
    def _series(self):
        return [
            {
                "snapshot_date": "2026-01-31",
                "snapshot_date_evidence": FILENAME_INFERRED,
                "source_table": "Enrollments_PY2526_01312026",
                "schema_name": "dbo",
                "total_people": 100,
                "enrolled_people": 80,
                "pending_people": 15,
                "cancelled_people": 5,
                "terminated_people": 10,
                "other_people": 4,
                "person_identifier": "ENROLLEE_ID_PROXY",
                "person_identifier_column": "enrollee_id",
                "filter_notes": ["Plan year"],
                "unknown_status_values": [{"status_value": "UnknownX", "people_count": 4}],
            },
            {
                "snapshot_date": "2026-02-27",
                "snapshot_date_evidence": FILENAME_INFERRED,
                "source_table": "Enrollments_PY2526_02272026",
                "schema_name": "dbo",
                "total_people": 120,
                "enrolled_people": 90,
                "pending_people": 20,
                "cancelled_people": 0,
                "terminated_people": 10,
                "other_people": 0,
                "person_identifier": "ENROLLEE_ID_PROXY",
                "filter_notes": ["Plan year"],
            },
        ]

    def test_report_keeps_py2027_reference_separate_from_snapshots(self):
        report = build_report(
            payload(), observation(),
            series=self._series(),
            activity=[{
                "activity_month": "2025-11",
                "newly_confirmed_people": 40,
                "person_identifier": "ENROLLEE_ID_PROXY",
                "date_field": "enrollment_confirmation_date",
                "definition": ACTIVITY_LABEL,
            }],
        )
        self.assertFalse(report["lifecycle_equivalent"])
        self.assertEqual(report["py2026_equivalent_lifecycle_date"], "2025-11-08")
        self.assertEqual(report["dataset_py2027_current_reference"]["currently_enrolled_people"], 797301)
        self.assertEqual(report["dataset_py2027_current_reference"]["observation_date"], "2026-11-08")
        self.assertEqual(report["py2027_cancelled_metric_status"], PY2027_NOT_VERIFIED)
        self.assertEqual(report["py2027_terminated_metric_status"], PY2027_NOT_VERIFIED)
        self.assertIsNone(report["dataset_py2027_current_reference"].get("cancelled_people"))
        latest = report["py2026_latest_snapshot_summary"]
        self.assertEqual(latest["snapshot_date"], "2026-02-27")
        self.assertEqual(latest["total_people"], 120)
        self.assertEqual(latest["historical_minimum_total"]["snapshot_date"], "2026-01-31")
        self.assertEqual(latest["historical_maximum_total"]["total_people"], 120)
        self.assertNotIn("person_identifier_column", report["dataset_snapshot_population"][0])
        self.assertEqual(report["dataset_monthly_enrollment_activity"][0]["definition"], ACTIVITY_LABEL)

    def test_missing_observation_does_not_invent_py2027_status_metrics(self):
        report = build_report(payload(), None, series=self._series())
        self.assertIsNone(report["dataset_py2027_current_reference"]["currently_enrolled_people"])
        self.assertEqual(report["py2027_cancelled_metric_status"], PY2027_NOT_VERIFIED)
        self.assertEqual(report["py2027_terminated_metric_status"], PY2027_NOT_VERIFIED)

    def test_serialization_and_terminal_table(self):
        report = build_report(payload(), observation(), series=self._series())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_table(report)
        text = buffer.getvalue()
        self.assertIn("Jan 2026", text)
        self.assertIn("797301", text)
        self.assertIn("NOT_VERIFIED", text)
        self.assertIn("not lifecycle-aligned", text.lower().replace("—", " "))
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["py2027_cancelled_metric_status"], PY2027_NOT_VERIFIED)
        self.assertNotIn("<svg", html.lower())
        self.assertNotIn("plotly", html.lower())
        self._keys(loaded)

    def _keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id", "exchg_indiv_identifier"})
                self._keys(item)
        elif isinstance(value, list):
            for item in value:
                self._keys(item)


class CollectTests(unittest.TestCase):
    def test_collects_ordered_snapshots_and_activity(self):
        client = FakeClient([
            ("dbo", "Enrollments_PY2526_02272026", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_NOV30", "BASE TABLE"),
        ])
        evidence = collect_evidence(client, payload())
        self.assertEqual([row["snapshot_date"] for row in evidence["series"]], ["2026-01-31", "2026-02-27"])
        self.assertEqual(evidence["series"][0]["total_people"], 100)
        self.assertEqual(evidence["series"][0]["enrolled_people"], 80)
        self.assertEqual(evidence["series"][0]["cancelled_people"], 5)
        self.assertEqual(evidence["series"][0]["unknown_status_values"][0]["status_value"], "UnknownX")
        self.assertEqual(evidence["series"][1]["total_people"], 120)
        self.assertEqual(evidence["activity"][0]["activity_month"], "2025-11")
        self.assertEqual(evidence["activity"][0]["definition"], ACTIVITY_LABEL)
        self.assertEqual(evidence["activity_source"]["table"], "Enrollments_PY2526_02272026")
        self.assertIn("Plan year", evidence["series"][0]["filters_applied"])
        notes = evidence["series"][0]["filter_notes"]
        self.assertTrue(any("Plan year" in item or item == "Plan year" for item in notes))
        for sql, params in client.calls:
            if params is not None and "ORDINAL_POSITION" not in sql:
                self.assertNotIn(2027, params)

    def test_missing_filter_column_is_recorded(self):
        client = FakeClient([("dbo", "Enrollments_PY2526_01312026", "BASE TABLE")])
        del client.column_sets["Enrollments_PY2526_01312026"]["person_type_description"]
        evidence = collect_evidence(client, payload())
        unavailable = {item["sisense_concept"] for item in evidence["series"][0]["filters_unavailable"]}
        self.assertIn("Person type", unavailable)
        self.assertTrue(any("NOT_APPLIED_COLUMN_UNAVAILABLE" in item
                            for item in evidence["series"][0]["filter_notes"]))

    def test_partial_snapshot_failure_keeps_other_results(self):
        client = FakeClient([
            ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
            ("dbo", "Enrollments_PY2526_02272026", "BASE TABLE"),
        ], fail_tables={"Enrollments_PY2526_01312026"})
        evidence = collect_evidence(client, payload())
        self.assertEqual(evidence["series"][0]["total_people"], None)
        self.assertEqual(evidence["series"][1]["total_people"], 120)
        self.assertTrue(any("01312026" in item["step"] for item in evidence["failures"]))

    def test_py2027_reference_comes_from_stored_history(self):
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
        self.assertEqual(found["metric_value"], 797301)
        self.assertEqual(found["observation_date"], "2026-11-08")
        source = Path(__file__).resolve().parents[1].joinpath(
            "comparison", "monthly_population.py").read_text(encoding="utf-8")
        self.assertNotIn("797301", source)
        self.assertNotIn("SisenseJaqlClient", source)
        main_source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        self.assertNotIn("comparison.monthly_population", main_source)
        ready, _confidence, _basis = official_comparison(
            "HISTORICAL_ASOF_NOT_PROVEN",
            {"translation_status": "EXACT"},
            [{"translation_status": "EXACT"}],
            observation(),
        )
        self.assertFalse(ready)


if __name__ == "__main__":
    unittest.main()
