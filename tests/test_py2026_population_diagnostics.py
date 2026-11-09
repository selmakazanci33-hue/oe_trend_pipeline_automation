"""Phase 6B population diagnostics on synthetic data (in-memory SQLite, no SQL Server, no network)."""

from __future__ import annotations

import inspect
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from comparison.currently_enrolled import ComparisonOutputError, guard_no_pii
from comparison.py2026_py2027_metric_bridge import (
    BLOCKED_EVENT_HISTORY,
    BLOCKED_IDENTIFIER,
    BLOCKED_LIFECYCLE,
    DIAGNOSTIC,
    WRONG_SOURCE_FAMILY,
    condition,
    currently_enrolled_candidates,
    load_definition,
)
from database.comparison import py2026_population_diagnostics as module
from database.comparison.py2026_population_diagnostics import (
    COLUMNS_SQL,
    EXCHANGE_NOT_AVAILABLE,
    MANY_PROXY_TO_ONE,
    MONTHLY_STATEMENT,
    OE_ACTIVITY_VIEW,
    OE_ALIGNMENT_STATEMENT,
    OE_ANCHOR_UNPROVEN,
    OE_LIFECYCLE_LABEL,
    ONE_PROXY_TO_MANY,
    ONE_TO_ONE,
    PERSON_PROXY,
    POPULATION_STATE_VIEW,
    build_report,
    collect,
    print_summary,
    render_html,
    where_from_conditions,
    write_report,
)
from database.models import QueryExecutionError, QueryResult
from database.query_runner import assert_read_only

ID_BASE = 771234500
BASE_COLUMNS = (
    ("coverage_year", "INTEGER"), ("enrollment_id", "INTEGER"), ("enrollee_id", "INTEGER"),
    ("enrollment_status_description", "TEXT"), ("enrollee_status_description", "TEXT"),
    ("benefit_effective_date", "TEXT"), ("enrollment_last_update_date", "TEXT"),
)
EXCHANGE_COLUMN = ("exchg_indiv_identifier", "TEXT")
FILTER_COLUMNS = (("person_type_description", "TEXT"), ("insurance_type_description", "TEXT"))
MISSING_FILTERS = ["person_type_description", "insurance_type_description"]


def record(year, policy, person, enrollment, enrollee, benefit, updated, exchange):
    return {
        "coverage_year": year, "enrollment_id": ID_BASE + policy, "enrollee_id": ID_BASE + 100 + person,
        "enrollment_status_description": enrollment, "enrollee_status_description": enrollee,
        "benefit_effective_date": benefit, "enrollment_last_update_date": updated,
        "exchg_indiv_identifier": f"XCHG{exchange:07d}", "person_type_description": "Enrollee",
        "insurance_type_description": "Health",
    }


def base_rows(exchange_for_r2=2, exchange_for_r6=1):
    return [
        record(2026, 1, 1, "Enrolled", "Enrolled", "2026-01-01", "2025-11-15", 1),
        record(2026, 1, 2, "Enrolled", "Enrolled", "2026-01-01", "2025-12-02", exchange_for_r2),
        record(2026, 2, 3, "Pending", "Pending", "2026-02-01", "2026-01-10", 3),
        record(2026, 3, 4, "Terminated", "Terminated", "2026-01-01", "2026-06-30", 4),
        record(2026, 4, 5, "Cancelled", "Cancelled", "2026-01-01", "2025-12-20", 5),
        record(2026, 5, 1, "Enrolled", "Enrolled", "2026-12-31 08:00:00", "2026-06-01", exchange_for_r6),
        record(2026, 6, 6, "Enrolled", "Enrolled", "2027-01-01", "2026-02-01", 6),
        record(2026, 8, 8, "Terminated by Issuer", "Terminated", "2026-01-01", "2026-06-15", 8),
        record(2025, 9, 9, "Enrolled", "Enrolled", "2025-01-01", "2025-01-05", 9),
    ]


def database(rows, columns):
    connection = sqlite3.connect(":memory:")
    connection.create_function("YEAR", 1, lambda value: None if value is None else int(str(value)[:4]))
    connection.create_function("MONTH", 1, lambda value: None if value is None else int(str(value)[5:7]))
    connection.execute("ATTACH DATABASE ':memory:' AS dbo")
    connection.execute(f"CREATE TABLE dbo.[Enrollments_TEST] ({', '.join(f'{c} {t}' for c, t in columns)})")
    connection.executemany(f"INSERT INTO dbo.[Enrollments_TEST] VALUES ({', '.join('?' * len(columns))})",
                           [tuple(r[c] for c, _ in columns) for r in rows])
    return connection


def result(columns, rows):
    rows = tuple(tuple(r) for r in rows)
    return QueryResult(columns=tuple(columns), rows=rows, row_count=len(rows), duration_seconds=0.0)


class FakeClient:
    def __init__(self, connection, fail=()):
        self.connection = connection
        self.fail = fail
        self.statements = []

    def execute_query(self, sql, params=None):
        assert_read_only(sql)
        params = tuple(params or ())
        self.statements.append((sql, params))
        if "2027" in sql or 2027 in params:
            raise AssertionError("PY2027 must not be queried")
        for name in re.findall(r"\[dbo\]\.\[([A-Za-z0-9_]+)\]", sql):
            if name != "Enrollments_TEST":
                raise AssertionError(f"out-of-scope object {name}")
        if sql == COLUMNS_SQL:
            if params != ("dbo", "Enrollments_TEST"):
                raise AssertionError(params)
            info = self.connection.execute("PRAGMA dbo.table_info([Enrollments_TEST])").fetchall()
            return result(("column_name", "data_type"), [(c[1], c[2].lower()) for c in info])
        if "[coverage_year] = ?" not in sql or 2026 not in params:
            raise AssertionError("every data query must bind coverage_year = 2026")
        if any(step in sql for step in self.fail):
            raise QueryExecutionError("Query timeout expired")
        cursor = self.connection.execute(sql.replace("COUNT_BIG(", "COUNT("), params)
        return result([c[0] for c in cursor.description], cursor.fetchall())


def run(rows=None, columns=BASE_COLUMNS + (EXCHANGE_COLUMN,), fail=(), definition="real"):
    definition = load_definition() if definition == "real" else definition
    with closing(database(base_rows() if rows is None else rows, columns)) as connection:
        client = FakeClient(connection, fail)
        report = build_report(collect(client, definition))
    return report, client


def candidate(report, cid):
    return next(item for item in report["currently_enrolled_candidates"] if item["candidate_id"] == cid)


def walk(value, key):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, key)


class ColumnAvailabilityTests(unittest.TestCase):
    def test_missing_optional_sisense_fields(self):
        report, _ = run(columns=BASE_COLUMNS)
        exists = {item["column"]: item["exists"] for item in report["column_availability"]}
        self.assertFalse(exists["person_type_description"])
        self.assertFalse(exists["insurance_type_description"])
        self.assertFalse(exists["exchg_indiv_identifier"])
        self.assertTrue(all(exists[name] for name in module.PROVEN_COLUMNS))
        for item in report["currently_enrolled_candidates"]:
            self.assertEqual(item["filters_unavailable"], MISSING_FILTERS)
            self.assertEqual(item["percent_effectuated"]["filters_unavailable"], MISSING_FILTERS)

    def test_present_filter_fields_are_reported_not_applied(self):
        report, client = run(columns=BASE_COLUMNS + (EXCHANGE_COLUMN,) + FILTER_COLUMNS)
        for item in report["currently_enrolled_candidates"]:
            self.assertEqual(item["filters_unavailable"], [])
            self.assertEqual(item["filters_present_not_applied"], MISSING_FILTERS)
        self.assertFalse(any("person_type_description" in sql for sql, _ in client.statements if sql != COLUMNS_SQL))

    def test_no_similar_name_substitution(self):
        columns = BASE_COLUMNS + (("exchg_indiv_id", "TEXT"), ("person_type", "TEXT"))
        rows = [dict(r, exchg_indiv_id="x", person_type="y") for r in base_rows()]
        report, _ = run(rows=rows, columns=columns)
        exists = {item["column"]: item["exists"] for item in report["column_availability"]}
        self.assertFalse(exists["exchg_indiv_identifier"])
        self.assertFalse(exists["person_type_description"])
        self.assertEqual(report["identifier_behavior"]["exchange_person_identifier"]["status"],
                         EXCHANGE_NOT_AVAILABLE)


class IdentifierTests(unittest.TestCase):
    def test_identity_profile_and_fanout(self):
        report, _ = run()
        ident = report["identifier_behavior"]
        self.assertEqual(ident["person_identifier"], PERSON_PROXY)
        self.assertEqual((ident["row_count"], ident["distinct_people"], ident["distinct_policies"]), (8, 7, 7))
        self.assertEqual((ident["null_person_rows"], ident["null_policy_rows"]), (0, 0))
        self.assertEqual(ident["distinct_policy_person_pairs"], 8)
        self.assertEqual(ident["policies_per_person"]["buckets"], {"1": 6, "2": 1})
        self.assertEqual(ident["policies_per_person"]["max"], 2)
        self.assertEqual(ident["persons_per_policy"]["buckets"], {"1": 6, "2": 1})

    def test_exchange_identifier_absent(self):
        report, client = run(columns=BASE_COLUMNS)
        self.assertEqual(report["identifier_behavior"]["exchange_person_identifier"]["status"],
                         EXCHANGE_NOT_AVAILABLE)
        self.assertFalse(any("exchg_indiv_identifier" in sql for sql, _ in client.statements))
        decision = report["phase_6b_decision"]["currently_enrolled"]
        self.assertEqual(decision["classification"], BLOCKED_IDENTIFIER)
        self.assertEqual(decision["identifier_evidence"], EXCHANGE_NOT_AVAILABLE)

    def test_exchange_identifier_one_to_one(self):
        report, _ = run()
        exchange = report["identifier_behavior"]["exchange_person_identifier"]
        self.assertEqual(exchange["status"], ONE_TO_ONE)
        self.assertEqual(exchange["mapping"]["counts"]["one_to_one_pairs"], 7)
        decision = report["phase_6b_decision"]["currently_enrolled"]
        self.assertEqual(decision["classification"], DIAGNOSTIC)
        self.assertIn("not proven", decision["identifier_evidence"])
        self.assertTrue(decision["remaining_blockers"])
        self.assertFalse(decision["production_comparable"])

    def test_exchange_identifier_not_one_to_one(self):
        report, _ = run(rows=base_rows(exchange_for_r2=1))
        self.assertEqual(report["identifier_behavior"]["exchange_person_identifier"]["status"], MANY_PROXY_TO_ONE)
        self.assertEqual(report["phase_6b_decision"]["currently_enrolled"]["classification"], BLOCKED_IDENTIFIER)
        report, _ = run(rows=base_rows(exchange_for_r6=7))
        self.assertEqual(report["identifier_behavior"]["exchange_person_identifier"]["status"], ONE_PROXY_TO_MANY)
        self.assertEqual(report["phase_6b_decision"]["currently_enrolled"]["classification"], BLOCKED_IDENTIFIER)

    def test_null_exchange_values_block_promotion(self):
        rows = base_rows()
        rows[2]["exchg_indiv_identifier"] = None
        report, _ = run(rows=rows)
        self.assertNotEqual(report["identifier_behavior"]["exchange_person_identifier"]["status"], ONE_TO_ONE)
        self.assertEqual(report["phase_6b_decision"]["currently_enrolled"]["classification"], BLOCKED_IDENTIFIER)


class CandidateTests(unittest.TestCase):
    def test_candidates_remain_separate(self):
        report, _ = run()
        got = {c["candidate_id"]: (c["distinct_people"], c["distinct_policies"], c["row_count"])
               for c in report["currently_enrolled_candidates"]}
        self.assertEqual(got, {"CE1": (6, 6, 7), "CE2": (3, 3, 4), "CE3": (5, 5, 6), "CE4": (3, 3, 4)})
        for item in report["currently_enrolled_candidates"]:
            self.assertEqual(item["label"], "DIAGNOSTIC_ONLY")
            self.assertNotIn("rank", json.dumps(item).lower())
        self.assertNotIn("selected_candidate", report)

    def test_phase_6a_definitions_consumed(self):
        report, _ = run()
        expected = currently_enrolled_candidates(load_definition())
        self.assertEqual([c["formula"] for c in report["currently_enrolled_candidates"]],
                         [c["formula"] for c in expected])
        self.assertTrue(all("PLAN_YEAR_SHIFT_ASSUMPTION" in " ".join(c["filters_applied"])
                            for c in report["currently_enrolled_candidates"]))

    def test_queries_follow_bridge_not_local_copy(self):
        custom = [{
            "candidate_id": "CEX", "description": "sentinel", "formula": "sentinel formula",
            "conditions": [condition("coverage_year", "=", [2026]),
                           condition("enrollee_status_description", "in", ["Pending"])],
            "unavailable_filter_fields": [], "assumptions": [],
        }]
        with mock.patch.object(module, "currently_enrolled_candidates", return_value=custom):
            report, client = run()
        self.assertEqual(candidate(report, "CEX")["distinct_people"], 1)
        self.assertTrue(any(params[-1:] == ("Pending",) for _, params in client.statements))
        source = inspect.getsource(module)
        for literal in ("'Enrolled'", '"Enrolled"', "'Pending'", '"Pending"', "'Cancelled'", '"Cancelled"',
                        "'Term%'", '"Term%"', "2026-12-31"):
            self.assertNotIn(literal, source)

    def test_day_range_includes_end_day(self):
        columns = {name: {"name": name} for name, _ in BASE_COLUMNS}
        where, params = where_from_conditions(
            [condition("coverage_year", "=", [2026]),
             condition("benefit_effective_date", "day_range", ["2025-10-01", "2026-12-31"])], columns)
        self.assertIn("[benefit_effective_date] >= ? AND [benefit_effective_date] < ?", where)
        self.assertEqual(params, (2026, "2025-10-01", "2027-01-01"))
        with self.assertRaises(ValueError):
            where_from_conditions([condition("enrollee_status_description", "in", ["X"])], columns)


class EffectuatedTests(unittest.TestCase):
    def test_numerator_denominator_semantics(self):
        report, _ = run()
        ce1 = candidate(report, "CE1")["percent_effectuated"]
        self.assertEqual(ce1["numerator"], "enrollment_status_description IN ['Enrolled', 'Terminated']")
        self.assertEqual(ce1["denominator"], "enrollment_status_description IN ['Enrolled', 'Pending', 'Terminated']")
        self.assertEqual((ce1["numerator_people"], ce1["denominator_people"], ce1["ratio"]), (3, 4, 0.75))
        ce2 = candidate(report, "CE2")["percent_effectuated"]
        self.assertEqual((ce2["numerator_people"], ce2["denominator_people"]), (2, 3))
        for item in report["percent_effectuated"]["per_candidate"]:
            self.assertEqual(item["label"], "DIAGNOSTIC_ONLY")
            self.assertEqual(item["person_identifier"], PERSON_PROXY)
            self.assertIn("not the official", item["name"])

    def test_zero_denominator_is_na_not_zero(self):
        rows = [r for r in base_rows() if r["enrollment_status_description"] == "Cancelled"]
        report, _ = run(rows=rows)
        ce1 = candidate(report, "CE1")
        self.assertEqual(ce1["distinct_people"], 1)
        self.assertEqual(ce1["percent_effectuated"]["denominator_people"], 0)
        self.assertIsNone(ce1["percent_effectuated"]["ratio"])


class PopulationAndTemporalTests(unittest.TestCase):
    def test_populations_separate_from_sisense_activity(self):
        report, _ = run()
        pops = {p["label"]: p for p in report["population_diagnostics"]}
        cancelled = pops["PY2026_DB_CANCELLED_POPULATION_DIAGNOSTIC"]
        terminated = pops["PY2026_DB_TERMINATED_POPULATION_DIAGNOSTIC"]
        self.assertEqual((cancelled["distinct_policies"], cancelled["distinct_people"]), (1, 1))
        self.assertEqual((terminated["distinct_policies"], terminated["distinct_people"]), (2, 2))
        self.assertEqual(sorted(terminated["status_values_matched"]), ["Terminated", "Terminated by Issuer"])
        self.assertEqual(cancelled["status_values_matched"], ["Cancelled"])
        for label in walk(report, "label"):
            self.assertNotIn(label, ("Cancelled Policies", "Terminated Policies"))
        decision = report["phase_6b_decision"]
        self.assertEqual(decision["cancelled_policies_daily"]["classification"], BLOCKED_EVENT_HISTORY)
        self.assertEqual(decision["terminated_policies_daily"]["classification"], BLOCKED_EVENT_HISTORY)

    def test_monthly_distribution_is_diagnostic(self):
        report, _ = run()
        temporal = report["temporal_diagnostics"]
        self.assertEqual(temporal["statement"], MONTHLY_STATEMENT)
        self.assertEqual([(m["month"], m["row_count"]) for m in temporal["last_update_by_month"]],
                         [("2025-11", 1), ("2025-12", 2), ("2026-01", 1), ("2026-02", 1), ("2026-06", 3)])
        self.assertEqual(temporal["bounds"]["max_benefit_effective_date"], "2027-01-01")
        self.assertIn(MONTHLY_STATEMENT, render_html(report))

    def test_out_of_scope_metrics_not_queried(self):
        report, client = run()
        decision = report["phase_6b_decision"]
        self.assertEqual(decision["application_metrics"]["classification"], WRONG_SOURCE_FAMILY)
        self.assertEqual(decision["new_active_passive"]["classification"], BLOCKED_LIFECYCLE)
        self.assertFalse(any(re.search(r"ssap|application", sql, re.I) for sql, _ in client.statements))


CREATE, CONFIRM, LAST_UPDATE = "enrollment_create_date", "enrollment_confirmation_date", "enrollment_last_update_date"
OE_COLUMNS = BASE_COLUMNS + (EXCHANGE_COLUMN, (CREATE, "TEXT"), (CONFIRM, "TEXT"))


def oe_record(policy, person, created, confirmed, status="Enrolled", year=2026):
    row = record(year, policy, person, status, status, "2026-01-01", "2026-10-15", person)
    row.update({CREATE: created, CONFIRM: confirmed})
    return row


def oe_rows():
    return [
        oe_record(1, 1, "2025-10-20", "2025-10-21"),
        oe_record(2, 2, "2025-11-01", "2025-11-08 23:59:00"),
        oe_record(3, 3, "2025-11-08 15:00:00", "2025-11-16"),
        oe_record(4, 1, "2025-11-15", None),
        oe_record(5, 5, "2025-11-29", "2025-12-01", status="Cancelled"),
        oe_record(6, 6, "2025-12-10", "2025-12-15"),
        oe_record(7, 7, "2025-12-31 22:00:00", "2026-01-02"),
        oe_record(8, 8, "2025-09-15", "2025-09-20"),
        oe_record(9, 9, "2025-11-02", "2025-11-03", year=2025),
    ]


def run_oe():
    return run(rows=oe_rows(), columns=OE_COLUMNS)


def counts(items):
    return {item.get("through") or item["month"]: (item["row_count"], item["distinct_people"],
                                                   item["distinct_policies"]) for item in items}


class OeTemporalTests(unittest.TestCase):
    def test_november_retained_and_october_context_visible(self):
        report, _ = run_oe()
        created = report["oe_aligned_temporal_diagnostics"]["fields"][CREATE]
        self.assertEqual(counts(created["monthly"]),
                         {"2025-10": (1, 1, 1), "2025-11": (4, 4, 4), "2025-12": (2, 2, 2)})
        self.assertEqual(created["context"]["rows_before_window"], 1)
        self.assertTrue(report["oe_lifecycle_alignment"]["november_2025_activity_observed"])
        self.assertTrue(report["oe_lifecycle_alignment"]["preserve_for_next_phase"])

    def test_cumulative_cutoffs(self):
        report, _ = run_oe()
        fields = report["oe_aligned_temporal_diagnostics"]["fields"]
        self.assertEqual(counts(fields[CREATE]["cumulative"]), {
            "2025-11-08": (4, 4, 4), "2025-11-15": (5, 4, 5), "2025-11-30": (6, 5, 6),
            "2025-12-15": (7, 6, 7), "2025-12-31": (8, 7, 8)})
        self.assertEqual(counts(fields[CONFIRM]["cumulative"]), {
            "2025-11-08": (3, 3, 3), "2025-11-15": (3, 3, 3), "2025-11-30": (4, 4, 4),
            "2025-12-15": (6, 6, 6), "2025-12-31": (6, 6, 6)})
        self.assertIsNone(fields[LAST_UPDATE]["cumulative"])

    def test_create_and_confirmation_separate(self):
        report, client = run_oe()
        fields = report["oe_aligned_temporal_diagnostics"]["fields"]
        self.assertEqual(counts(fields[CONFIRM]["monthly"]),
                         {"2025-10": (1, 1, 1), "2025-11": (2, 2, 2), "2025-12": (2, 2, 2), "2026-01": (1, 1, 1)})
        self.assertEqual(fields[CONFIRM]["context"]["null_rows"], 1)
        self.assertNotEqual(fields[CREATE]["cumulative"], fields[CONFIRM]["cumulative"])
        for sql, _ in client.statements:
            if "rows_0" in sql:
                self.assertEqual((CREATE in sql) + (CONFIRM in sql), 1)

    def test_never_labelled_currently_enrolled(self):
        report, _ = run_oe()
        temporal = report["oe_aligned_temporal_diagnostics"]
        self.assertNotIn("currently enrolled", json.dumps(temporal).lower())
        self.assertEqual({temporal["label"], *[f["label"] for f in temporal["fields"].values()]},
                         {OE_LIFECYCLE_LABEL})
        self.assertEqual(temporal["view"], OE_ACTIVITY_VIEW)
        for item in report["currently_enrolled_candidates"]:
            self.assertEqual(item["view"], POPULATION_STATE_VIEW)
            self.assertEqual(item["alignment_assumption"], "PLAN_YEAR_SHIFT_ASSUMPTION")
            self.assertEqual(item["not_the_same_as"], OE_LIFECYCLE_LABEL)

    def test_latest_state_not_used_for_historical_status(self):
        report, client = run_oe()
        oe_sql = [sql for sql, _ in client.statements if "activity_year" in sql or "rows_0" in sql]
        self.assertTrue(oe_sql)
        for sql in oe_sql:
            self.assertNotIn("status_description", sql)
        for sql, _ in client.statements:
            self.assertIsNone(re.search(r"\[enrollment_last_update_date\]\s*(<|<=)", sql))
        cumulative = report["oe_aligned_temporal_diagnostics"]["fields"][CREATE]["cumulative"]
        self.assertEqual(cumulative[-1]["row_count"], 8)
        self.assertFalse(report["oe_aligned_temporal_diagnostics"]["status_filter_applied"])
        flags = list(walk(report, "historical_state_reconstructable"))
        self.assertTrue(flags and all(flag is False for flag in flags))
        with self.assertRaises(ValueError):
            module.build_oe_cumulative_sql({n: {"name": n} for n, _ in OE_COLUMNS}, LAST_UPDATE)

    def test_no_invented_november_first_anchor(self):
        report, _ = run_oe()
        alignment = report["oe_lifecycle_alignment"]
        self.assertEqual(alignment["oe_alignment_status"], OE_ANCHOR_UNPROVEN)
        self.assertEqual(alignment["oe_start_anchor"], {"PY2026": None, "PY2027": None})
        self.assertEqual(alignment["statement"], OE_ALIGNMENT_STATEMENT)
        self.assertNotIn("2025-11-01", json.dumps(alignment))
        source = inspect.getsource(module)
        self.assertNotIn("2025-11-01", source)
        self.assertNotIn("2026-11-01", source)

    def test_anchor_unproven_preserved_without_evidence(self):
        for report, _ in (run_oe(), run(columns=BASE_COLUMNS), run(definition=None)):
            self.assertEqual(report["oe_lifecycle_alignment"]["oe_alignment_status"], OE_ANCHOR_UNPROVEN)
            self.assertEqual(report["phase_6b_decision"]["oe_temporal_activity"]["oe_alignment_status"],
                             OE_ANCHOR_UNPROVEN)

    def test_missing_oe_columns_are_na(self):
        report, _ = run(columns=BASE_COLUMNS)
        fields = report["oe_aligned_temporal_diagnostics"]["fields"]
        self.assertIsNone(fields[CREATE]["monthly"])
        self.assertIsNone(fields[CREATE]["cumulative"])
        self.assertIsNone(report["oe_lifecycle_alignment"]["november_2025_activity_observed"])
        self.assertFalse(report["oe_lifecycle_alignment"]["preserve_for_next_phase"])

    def test_oe_production_comparable_false(self):
        report, _ = run_oe()
        flags = list(walk(report, "production_comparable"))
        self.assertTrue(all(flag is False for flag in flags))
        self.assertFalse(report["oe_lifecycle_alignment"]["production_comparable"])
        self.assertFalse(report["oe_aligned_temporal_diagnostics"]["production_comparable"])


class SafetyTests(unittest.TestCase):
    def test_failure_is_na_not_zero(self):
        report, _ = run(fail=("effectuated_numerator_people",))
        for item in report["currently_enrolled_candidates"]:
            self.assertIsNone(item["distinct_people"])
            self.assertIsNone(item["percent_effectuated"]["ratio"])
        self.assertTrue(report["failures"])
        self.assertIsNotNone(report["identifier_behavior"]["row_count"])

    def test_metadata_failure_everything_na(self):
        with closing(database(base_rows(), BASE_COLUMNS)) as connection:
            client = FakeClient(connection)
            client.execute_query = mock.Mock(side_effect=QueryExecutionError("Login timeout"))
            report = build_report(collect(client, load_definition()))
        self.assertIsNone(report["identifier_behavior"]["row_count"])
        self.assertIsNone(report["phase_6b_decision"]["currently_enrolled"]["classification"])
        self.assertTrue(all(item["exists"] is None for item in report["column_availability"]))

    def test_missing_payload(self):
        report, _ = run(definition=None)
        self.assertEqual(report["currently_enrolled_candidates"], [])
        self.assertTrue(any("payload not loaded" in gap for gap in report["remaining_semantic_gaps"]))

    def test_no_identifier_leakage(self):
        report, _ = run()
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(report, Path(tmp))
            text = json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_summary(report)
        text += buffer.getvalue()
        for value in [ID_BASE + n for n in range(1, 10)] + [ID_BASE + 100 + n for n in range(1, 10)]:
            self.assertNotIn(str(value), text)
        self.assertNotIn("XCHG", text)
        with self.assertRaises(ComparisonOutputError):
            guard_no_pii({"enrollee_id": 1})

    def test_production_comparable_false_everywhere(self):
        report, _ = run()
        flags = list(walk(report, "production_comparable"))
        self.assertGreater(len(flags), 15)
        self.assertTrue(all(flag is False for flag in flags))

    def test_report_sections(self):
        report, _ = run()
        page = render_html(report)
        for section in ("Source", "Column Availability", "Status Distributions", "Identifier Behavior",
                        "Currently Enrolled Candidates", "Percent Effectuated Diagnostic",
                        "Cancellation / Termination Population Diagnostics", "Temporal Diagnostics",
                        "Remaining Semantic Gaps", "Phase 6B Decision", "OE-Aligned Temporal Diagnostics",
                        "OE Lifecycle Alignment"):
            self.assertIn(f"<h2>{section}</h2>", page)
        self.assertIn("PY2026 Oct-31-equivalent latest-state dataset", report["source"]["description"])
        self.assertNotIn("after the plan year", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
