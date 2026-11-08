"""Diagnostic people counts. Fake catalog results and a local sqlite file only."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, interpret_payload
from comparison.diagnostic_numbers import (
    BLOCKED,
    CLOSEST,
    COMPLETED,
    ENROLLEE_PROXY,
    FILENAME_INFERRED,
    MISSING_OBSERVATION,
    NO_MATCHING,
    NOT_APPLIED,
    PERSON_UNPROVEN,
    SCENARIO_A,
    SCENARIO_B,
    SCENARIO_C,
    TIED,
    TABLES_SQL,
    build_count_sql,
    build_report,
    collect_evidence,
    find_exchange_column,
    print_table,
    select_diagnostic_snapshot,
    status_scenarios,
    write_report,
)
from comparison.lifecycle import official_comparison
from database.models import QueryResult
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


def snapshot(table: str, inferred: str | None = None) -> dict:
    return {
        "schema_name": "dbo",
        "table": table,
        "table_type": "BASE TABLE",
        "inferred_filename_date": inferred,
    }


COLUMNS = {
    "coverage_year": {"name": "coverage_year", "data_type": "int"},
    "enrollee_id": {"name": "enrollee_id", "data_type": "varchar"},
    "enrollment_status_description": {"name": "enrollment_status_description", "data_type": "varchar"},
    "enrollee_status_description": {"name": "enrollee_status_description", "data_type": "varchar"},
    "benefit_effective_date": {"name": "benefit_effective_date", "data_type": "datetime2"},
    "person_type_description": {"name": "person_type_description", "data_type": "varchar"},
    "insurance_type_description": {"name": "insurance_type_description", "data_type": "varchar"},
}


class FakeClient:
    def __init__(self, tables, columns, counts):
        self.tables = tables
        self.columns = columns
        self.counts = counts
        self.calls = []

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        assert_read_only(sql)
        self.assert_no_py2027(sql, params)
        if "LIKE 'Enrollments[_]PY2526%'" in sql:
            return result(("table_schema", "table_name", "table_type"), self.tables)
        if "INFORMATION_SCHEMA.COLUMNS" in sql and "IN (" in sql:
            rows = []
            for schema_name, table_name, _type in self.tables:
                for column in self.columns:
                    rows.append((schema_name, table_name, "BASE TABLE", column, "varchar"))
            return result(
                ("table_schema", "table_name", "table_type", "column_name", "data_type"),
                rows,
            )
        if "ORDINAL_POSITION" in sql:
            return result(("column_name", "data_type"), [
                (name, meta["data_type"]) for name, meta in self.columns.items()
            ])
        if "people_count" in sql:
            key = SCENARIO_A
            if params and "Terminated" not in params and "Pending" in params:
                key = SCENARIO_B
            elif params and "Pending" not in params and "Terminated" not in params:
                key = SCENARIO_C
            return result(("people_count",), [(self.counts[key],)])
        raise AssertionError(sql)

    def assert_no_py2027(self, sql, params):
        if "2027" in sql:
            raise AssertionError(sql)
        if params and 2027 in params:
            raise AssertionError(params)


class ObservationAndScenarioTests(unittest.TestCase):
    def test_missing_observation_stops_without_counts(self):
        report = build_report(payload(), None)
        self.assertEqual(report["diagnostic_comparison_status"], MISSING_OBSERVATION)
        self.assertEqual(report["official_comparison_status"], BLOCKED)
        self.assertEqual(report["status_scenarios"], [])
        self.assertIn("main.py", report["refresh_command"])

    def test_status_scenarios_follow_the_verified_members(self):
        scenarios = status_scenarios(interpret_payload(payload()))
        ids = [item["id"] for item in scenarios]
        self.assertEqual(ids, [SCENARIO_A, SCENARIO_B, SCENARIO_C])
        self.assertEqual(scenarios[0]["members"], ["Enrolled", "Pending", "Terminated"])
        self.assertEqual(scenarios[1]["members"], ["Enrolled", "Pending"])
        self.assertEqual(scenarios[2]["members"], ["Enrolled"])

    def test_exchange_column_uses_actual_schema_name(self):
        self.assertEqual(find_exchange_column({
            "exchg_individual_identifier": {"name": "Exchg_Individual_Identifier"},
        }), "Exchg_Individual_Identifier")
        self.assertIsNone(find_exchange_column({"enrollee_id": {"name": "enrollee_id"}}))


class SnapshotSelectionTests(unittest.TestCase):
    def test_closest_filename_date_is_labeled_inferred(self):
        snapshots = [
            snapshot("Enrollments_PY2526_01312026", "2026-01-31"),
            snapshot("Enrollments_PY2526_02272026", "2026-02-27"),
            snapshot("Enrollments_PY2526_NOV30", None),
        ]
        choice = select_diagnostic_snapshot(snapshots, date(2026, 2, 1))
        self.assertEqual(choice["match_status"], CLOSEST)
        self.assertEqual(choice["selected"]["table"], "Enrollments_PY2526_01312026")
        self.assertEqual(choice["selected"]["distance_days"], -1)
        self.assertEqual(choice["selected"]["position"], "before")

    def test_tie_is_not_chosen(self):
        snapshots = [
            snapshot("Enrollments_PY2526_11072025", "2025-11-07"),
            snapshot("Enrollments_PY2526_11092025", "2025-11-09"),
        ]
        choice = select_diagnostic_snapshot(snapshots, date(2025, 11, 8))
        self.assertEqual(choice["match_status"], TIED)
        self.assertIsNone(choice["selected"])
        self.assertEqual(len(choice["tied"]), 2)

    def test_lifecycle_before_earliest_snapshot_uses_earliest_as_diagnostic_only(self):
        snapshots = [
            snapshot("Enrollments_PY2526_01312026", "2026-01-31"),
            snapshot("Enrollments_PY2526_02272026", "2026-02-27"),
        ]
        choice = select_diagnostic_snapshot(snapshots, date(2025, 11, 8))
        self.assertEqual(choice["match_status"], NO_MATCHING)
        self.assertEqual(choice["selected"]["table"], "Enrollments_PY2526_01312026")
        self.assertEqual(choice["selected"]["distance_days"], 84)
        self.assertEqual(choice["selected"]["position"], "after")
        self.assertIn("not lifecycle-equivalent", choice["reason"])


class ReportTests(unittest.TestCase):
    def _columns(self, extra=None):
        columns = dict(COLUMNS)
        if extra:
            columns.update(extra)
        return columns

    def test_enrollee_id_proxy_and_official_remain_separate(self):
        snapshots = [snapshot("Enrollments_PY2526_01312026", "2026-01-31")]
        report = build_report(
            payload(), observation(),
            snapshots=snapshots,
            selected_columns=self._columns(),
            scenario_counts={SCENARIO_A: 100, SCENARIO_B: 80, SCENARIO_C: 50},
            official={"status": BLOCKED, "reason": "as-of not proven"},
        )
        self.assertEqual(report["comparison_kind"], "DIAGNOSTIC_COMPARISON")
        self.assertEqual(report["official_comparison_status"], BLOCKED)
        self.assertEqual(report["diagnostic_comparison_status"], COMPLETED)
        self.assertEqual(report["py2027_observation_date"], "2026-11-08")
        self.assertEqual(report["py2026_equivalent_lifecycle_date"], "2025-11-08")
        self.assertEqual(report["snapshot_selection"]["date_label"], FILENAME_INFERRED)
        self.assertEqual(report["snapshot_selection"]["match_status"], NO_MATCHING)
        self.assertEqual(report["person_identifier"]["label"], ENROLLEE_PROXY)
        self.assertEqual(report["person_identifier"]["identity_status"], PERSON_UNPROVEN)
        by_id = {item["id"]: item for item in report["status_scenarios"]}
        self.assertEqual(by_id[SCENARIO_A]["py2026_db_count"], 100)
        self.assertEqual(by_id[SCENARIO_B]["py2026_db_count"], 80)
        self.assertEqual(by_id[SCENARIO_C]["py2026_db_count"], 50)
        self.assertEqual(by_id[SCENARIO_A]["py2027_sisense_count"], 400000)
        self.assertEqual(by_id[SCENARIO_B]["py2027_sisense_count"], 400000)
        self.assertEqual(by_id[SCENARIO_A]["absolute_difference"], 399900)
        self.assertNotIn("enrollee_id", report["person_identifier"])
        applied = {item["sisense_concept"]: item for item in report["filters_applied"]}
        self.assertEqual(applied["Plan year"]["applied_value"], 2026)
        self.assertEqual(applied["Benefit effective date"]["applied_value"]["from"], "2025-10-01")
        self.assertEqual(applied["Benefit effective date"]["applied_value"]["to"], "2026-12-31")
        self.assertEqual(applied["Person type"]["applied_value"], ["Enrollee", "Subscriber"])
        self.assertEqual(applied["Insurance type"]["applied_value"], ["Health"])
        self.assertTrue(any("ENROLLEE_ID_PROXY" in item for item in report["assumptions"]))
        self.assertTrue(any("not an official" in item.lower() for item in report["assumptions"]))

    def test_missing_filter_column_is_marked_not_applied(self):
        columns = {key: value for key, value in COLUMNS.items()
                   if key not in {"person_type_description", "insurance_type_description"}}
        report = build_report(
            payload(), observation(),
            snapshots=[snapshot("Enrollments_PY2526_01312026", "2026-01-31")],
            selected_columns=columns,
            scenario_counts={SCENARIO_A: 10, SCENARIO_B: 9, SCENARIO_C: 8},
        )
        names = {item["sisense_concept"] for item in report["filters_unavailable"]}
        self.assertIn("Person type", names)
        self.assertIn("Insurance type", names)
        self.assertTrue(all(item["status"] == NOT_APPLIED for item in report["filters_unavailable"]))
        self.assertNotIn("Person type", {item["sisense_concept"] for item in report["filters_applied"]})

    def test_exchange_identifier_is_preferred(self):
        report = build_report(
            payload(), observation("2027-02-01T12:00:00Z", 200),
            snapshots=[snapshot("Enrollments_PY2526_01312026", "2026-01-31")],
            selected_columns=self._columns({
                "exchg_indiv_identifier": {"name": "exchg_indiv_identifier", "data_type": "varchar"},
            }),
            scenario_counts={SCENARIO_A: 180, SCENARIO_B: 150, SCENARIO_C: 100},
        )
        self.assertEqual(report["person_identifier"]["py2026_column"], "exchg_indiv_identifier")
        self.assertEqual(report["snapshot_selection"]["match_status"], CLOSEST)
        self.assertEqual(report["snapshot_selection"]["selected"]["position"], "before")

    def test_raw_row_count_is_not_a_people_count(self):
        report = build_report(
            payload(), observation(),
            snapshots=[snapshot("Enrollments_PY2526_01312026", "2026-01-31")],
            selected_columns=self._columns(),
            scenario_counts={SCENARIO_A: 12, SCENARIO_B: 11, SCENARIO_C: 10},
        )
        text = json.dumps(report)
        self.assertNotIn("900000", text)
        self.assertTrue(all(item["py2026_db_count"] != 900000 for item in report["status_scenarios"]))

    def test_zero_denominator_is_safe(self):
        report = build_report(
            payload(), observation(value=5),
            snapshots=[snapshot("Enrollments_PY2526_01312026", "2026-01-31")],
            selected_columns=self._columns(),
            scenario_counts={SCENARIO_A: 0, SCENARIO_B: 0, SCENARIO_C: 0},
        )
        self.assertEqual(report["status_scenarios"][0]["percent_status"], "ZERO_DENOMINATOR")
        self.assertIsNone(report["status_scenarios"][0]["percent_change"])
        self.assertEqual(report["status_scenarios"][0]["absolute_difference"], 5)


class QueryAndCollectTests(unittest.TestCase):
    def test_count_sql_is_parameterized_and_read_only(self):
        sql, params = build_count_sql(
            "[dbo].[Enrollments_PY2526_01312026]",
            "enrollee_id",
            [{
                "sql": "[coverage_year] = ?",
                "params": [2026],
            }],
            ["Enrolled", "Pending"],
            COLUMNS,
        )
        assert_read_only(sql)
        self.assertNotIn("SELECT *", sql.upper())
        self.assertIn("COUNT_BIG(DISTINCT [enrollee_id])", sql)
        self.assertEqual(sql.count("?"), 1 + 2 + 2)
        self.assertEqual(params[0], 2026)
        self.assertNotRegex(sql, r"=\s*2026")
        self.assertNotIn("Enrolled", sql)

    def test_collect_runs_three_status_scenarios(self):
        client = FakeClient(
            tables=[("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
                    ("dbo", "Enrollments_PY2526_02272026", "BASE TABLE")],
            columns=COLUMNS,
            counts={SCENARIO_A: Decimal("100"), SCENARIO_B: Decimal("80"), SCENARIO_C: Decimal("50")},
        )
        evidence = collect_evidence(client, payload(), observation(), date(2025, 11, 8))
        self.assertEqual(evidence["scenario_counts"][SCENARIO_A], 100)
        self.assertEqual(evidence["scenario_counts"][SCENARIO_B], 80)
        self.assertEqual(evidence["scenario_counts"][SCENARIO_C], 50)
        self.assertEqual(evidence["official"]["status"], BLOCKED)
        count_calls = [params for sql, params in client.calls if "people_count" in sql]
        self.assertEqual(len(count_calls), 3)
        self.assertTrue(any("Terminated" in params for params in count_calls))
        assert_read_only(TABLES_SQL)

    def test_output_round_trip_and_terminal_table(self):
        report = build_report(
            payload(), observation(),
            snapshots=[snapshot("Enrollments_PY2526_01312026", "2026-01-31")],
            selected_columns=COLUMNS,
            scenario_counts={SCENARIO_A: 100, SCENARIO_B: 80, SCENARIO_C: 50},
            official={"status": BLOCKED, "reason": "blocked"},
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_table(report)
        text = buffer.getvalue()
        self.assertIn("not an official YoY metric", text)
        self.assertIn("100", text)
        self.assertIn("400000", text)
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["diagnostic_comparison_status"], COMPLETED)
        self.assertIn("Diagnostic comparison", html)
        self.assertNotIn("<svg", html.lower())
        self._keys(loaded)

    def test_official_gate_is_unchanged(self):
        ready, _confidence, _basis = official_comparison(
            "HISTORICAL_ASOF_NOT_PROVEN",
            {"translation_status": "EXACT"},
            [{"translation_status": "EXACT"}],
            observation(),
        )
        self.assertFalse(ready)

    def test_command_module_does_not_send_jaql(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "comparison", "diagnostic_numbers.py").read_text(encoding="utf-8")
        main_source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        self.assertNotIn("SisenseJaqlClient", source)
        self.assertNotIn("import comparison", main_source)
        self.assertNotIn("comparison.diagnostic_numbers", main_source)

    def test_history_reader_still_uses_validated_snapshots_only(self):
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
                   ('run-1', 'currently_enrolled_people', 555, 2027, '2026-11-08T12:04:00Z',
                    '2026-11-08T12:00:00Z', 'VALIDATED')""")
            connection.commit()
            connection.close()
            found = read_observation(path)
        self.assertEqual(found["observation_date"], "2026-11-08")
        self.assertEqual(found["metric_value"], 555)

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
