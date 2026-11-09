"""PY2026 source equivalence on synthetic tables (in-memory SQLite, no SQL Server, no network)."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing, redirect_stdout
from pathlib import Path

from comparison.currently_enrolled import ComparisonOutputError, guard_no_pii
from database.comparison.py2026_source_equivalence import (
    COLUMNS_SQL,
    DATASETS_DIFFER,
    DIFFERENT,
    EXACT_MATCH,
    IDENTICAL,
    INSUFFICIENT,
    KEYS_MATCH_FIELDS_INCOMPLETE,
    MATCH_WITH_AMBIGUITY,
    OCT31,
    TABLES,
    TEMP,
    TEMP_CANDIDATE,
    TEST,
    TEST_CANDIDATE,
    build_pair_sql,
    build_report,
    collect,
    compare_schema,
    parse_columns,
    print_summary,
    render_html,
    write_report,
)
from database.models import QueryExecutionError, QueryResult
from database.query_runner import assert_read_only

ID_BASE = 551234500
COLUMNS = (
    ("enrollment_id", "INTEGER"), ("enrollee_id", "INTEGER"),
    ("enrollment_status_description", "TEXT"), ("enrollee_status_description", "TEXT"),
    ("enrollment_create_date", "TEXT"), ("enrollment_confirmation_date", "TEXT"),
    ("enrollment_last_update_date", "TEXT"), ("benefit_effective_date", "TEXT"),
    ("benefit_end_date", "TEXT"), ("hios_issuer_id", "TEXT"), ("application_type", "TEXT"),
    ("coverage_year", "INTEGER"),
)
NEEDS_FULL_JOIN = unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 39, 0), "FULL OUTER JOIN needs SQLite 3.39")


def row(n, member=1, status="Effectuated", updated="2026-03-01", year=2026, **changes):
    values = {
        "enrollment_id": ID_BASE + n, "enrollee_id": ID_BASE + 1000 + n * 10 + member,
        "enrollment_status_description": status, "enrollee_status_description": status,
        "enrollment_create_date": "2025-11-01", "enrollment_confirmation_date": "2025-11-02",
        "enrollment_last_update_date": updated, "benefit_effective_date": "2026-01-01",
        "benefit_end_date": "2026-12-31", "hios_issuer_id": "12345", "application_type": "New",
        "coverage_year": year,
    }
    values.update(changes)
    return tuple(values[name] for name, _ in COLUMNS)


BASE = [row(n) for n in range(1, 6)] + [row(1, member=2), row(9, year=2025)]


class _ChecksumAgg:
    def __init__(self):
        self.value = 0

    def step(self, value):
        self.value ^= int(value or 0)

    def finalize(self):
        return self.value


def database(tables, columns=None):
    connection = sqlite3.connect(":memory:")
    connection.create_function("BINARY_CHECKSUM", -1, lambda *args: zlib.crc32(repr(args).encode()))
    connection.create_aggregate("CHECKSUM_AGG", 1, _ChecksumAgg)
    connection.execute("ATTACH DATABASE ':memory:' AS dbo")
    for name, rows in tables.items():
        cols = (columns or {}).get(name, COLUMNS)
        connection.execute(f"CREATE TABLE dbo.[{name}] ({', '.join(f'{c} {t}' for c, t in cols)})")
        connection.executemany(f"INSERT INTO dbo.[{name}] VALUES ({', '.join('?' * len(cols))})",
                               [r[:len(cols)] for r in rows])
    return connection


def to_sqlite(sql: str) -> str:
    return sql.replace("COUNT_BIG(", "COUNT(")


class FakeClient:
    def __init__(self, connection, fail=()):
        self.connection = connection
        self.fail = fail
        self.statements = []

    def execute_query(self, sql, params=None):
        assert_read_only(sql)
        self.statements.append((sql, tuple(params or ())))
        if "2027" in sql or 2027 in (params or ()):
            raise AssertionError("PY2027 must not be queried")
        for name in re.findall(r"\[dbo\]\.\[([A-Za-z0-9_]+)\]", sql):
            if name not in TABLES:
                raise AssertionError(f"out-of-scope object {name}")
        if sql == COLUMNS_SQL:
            info = self.connection.execute(f"PRAGMA dbo.table_info([{params[1]}])").fetchall()
            return result(("column_name", "data_type", "max_length", "numeric_precision", "numeric_scale",
                           "is_nullable", "ordinal_position"),
                          [(c[1], c[2].lower(), None, None, None, "NO" if c[3] else "YES", c[0] + 1) for c in info])
        if any(step in sql for step in self.fail):
            raise QueryExecutionError("Query timeout expired")
        if set(params) != {2026}:
            raise AssertionError(params)
        cursor = self.connection.execute(to_sqlite(sql), params)
        return result([c[0] for c in cursor.description], cursor.fetchall())


def result(columns, rows):
    rows = tuple(tuple(r) for r in rows)
    return QueryResult(columns=tuple(columns), rows=rows, row_count=len(rows), duration_seconds=0.0)


def run(test_rows, temp_rows, oct_rows, fail=(), columns=None):
    tables = {name: rows for name, rows in ((TEST, test_rows), (TEMP, temp_rows), (OCT31, oct_rows))
              if rows is not None}
    with closing(database(tables, columns)) as connection:
        client = FakeClient(connection, fail)
        report = build_report(collect(client))
    return client, report


def pair(report, a, b):
    return report["pairs"][f"{a} vs {b}"]


@NEEDS_FULL_JOIN
class SetEquivalenceTests(unittest.TestCase):
    def test_identical_tables_and_oct31_exact_match(self):
        client, report = run(BASE, BASE, BASE)
        tt = pair(report, TEST, TEMP)
        self.assertEqual(tt["status"], EXACT_MATCH)
        self.assertEqual((tt["counts"]["keys_a"], tt["counts"]["keys_both"], tt["counts"]["rows_a"]), (6, 6, 6))
        self.assertEqual(tt["schema"]["status"], "SCHEMA_IDENTICAL")
        self.assertTrue(tt["aggregates"]["all_equal"])
        self.assertEqual(pair(report, TEST, OCT31)["status"], EXACT_MATCH)
        self.assertEqual(report["decision"]["decision"], IDENTICAL)
        self.assertIn("both match the Oct-31 snapshot", report["decision"]["reason"])
        self.assertFalse(report["production_comparable"])
        allowed = {(2026,), (2026, 2026)} | {("dbo", table) for table in TABLES}
        for _, params in client.statements:
            self.assertIn(params, allowed)

    def test_test_only_and_temp_only_keys(self):
        _, report = run(BASE + [row(7)], BASE + [row(8), row(10)], BASE)
        counts = pair(report, TEST, TEMP)["counts"]
        self.assertEqual((counts["a_only"], counts["b_only"], counts["keys_both"]), (1, 2, 6))
        self.assertEqual(pair(report, TEST, TEMP)["status"], DIFFERENT)

    def test_changed_status_and_changed_date_on_shared_keys(self):
        temp = [row(1, status="Cancelled"), row(2, updated="2026-04-15")] + BASE[2:]
        _, report = run(BASE, temp, BASE)
        tt = pair(report, TEST, TEMP)
        self.assertEqual(tt["status"], DIFFERENT)
        self.assertEqual(tt["counts"]["shared_any_field_differs"], 2)
        self.assertEqual(tt["field_differences"]["enrollment_status_description"], 1)
        self.assertEqual(tt["field_differences"]["enrollee_status_description"], 1)
        self.assertEqual(tt["field_differences"]["enrollment_last_update_date"], 1)
        self.assertEqual(tt["field_differences"]["benefit_effective_date"], 0)

    def test_ambiguous_duplicate_key_is_reported_not_collapsed(self):
        dup = BASE + [row(3, status="Cancelled")]
        _, same = run(dup, dup, BASE)
        tt = pair(same, TEST, TEMP)
        self.assertEqual(tt["status"], MATCH_WITH_AMBIGUITY)
        self.assertEqual((tt["counts"]["ambiguous_keys_a"], tt["counts"]["shared_ambiguous"],
                          tt["counts"]["shared_ambiguous_signature_equal"], tt["counts"]["shared_comparable"]),
                         (1, 1, 1, 5))
        self.assertEqual(same["decision"]["decision"], IDENTICAL)
        _, other = run(dup, BASE + [row(3, status="Terminated")], BASE)
        self.assertEqual(pair(other, TEST, TEMP)["status"], DIFFERENT)
        self.assertEqual(pair(other, TEST, TEMP)["counts"]["shared_any_field_differs"], 0)

    def test_exact_duplicate_rows_are_multiplicity_not_ambiguity(self):
        _, report = run(BASE + [row(4)], BASE, BASE)
        counts = pair(report, TEST, TEMP)["counts"]
        self.assertEqual((counts["repeated_keys_a"], counts["ambiguous_keys_a"],
                          counts["shared_multiplicity_differs"]), (1, 0, 1))
        self.assertEqual(pair(report, TEST, TEMP)["status"], DIFFERENT)

    def test_identical_aggregate_counts_but_different_sets(self):
        temp = BASE[:4] + [row(6)] + BASE[5:]
        _, report = run(BASE, temp, BASE)
        tt = pair(report, TEST, TEMP)
        self.assertEqual(report["tables"][TEST]["profile"]["rows_2026"], report["tables"][TEMP]["profile"]["rows_2026"])
        self.assertEqual(report["tables"][TEST]["profile"]["distinct_policies"],
                         report["tables"][TEMP]["profile"]["distinct_policies"])
        self.assertTrue(tt["aggregates"]["metrics"]["rows_2026"])
        self.assertEqual((tt["counts"]["a_only"], tt["counts"]["b_only"]), (1, 1))
        self.assertEqual(tt["status"], DIFFERENT)

    def test_oct31_same_counts_different_records(self):
        oct_rows = [row(1, updated="2026-10-30")] + BASE[1:]
        _, report = run(BASE, BASE, oct_rows)
        self.assertEqual(pair(report, TEST, OCT31)["status"], DIFFERENT)
        self.assertEqual(pair(report, TEST, OCT31)["counts"]["keys_a"], pair(report, TEST, OCT31)["counts"]["keys_b"])
        self.assertEqual(report["decision"]["decision"], IDENTICAL)
        self.assertIn(f"{DIFFERENT}/{DIFFERENT}", report["decision"]["reason"])

    def test_candidate_only_when_one_side_matches_oct31(self):
        temp = [row(1, status="Cancelled")] + BASE[1:]
        _, report = run(BASE, temp, BASE)
        self.assertEqual(report["decision"]["decision"], TEST_CANDIDATE)
        _, report = run(temp, BASE, BASE)
        self.assertEqual(report["decision"]["decision"], TEMP_CANDIDATE)
        _, report = run(BASE, temp, [row(2, status="Terminated")] + BASE)
        self.assertEqual(report["decision"]["decision"], DATASETS_DIFFER)

    def test_missing_business_field_is_incomplete(self):
        fewer = {TEMP: tuple(c for c in COLUMNS if c[0] != "application_type")}
        temp_rows = [tuple(v for (name, _), v in zip(COLUMNS, r) if name != "application_type") for r in BASE]
        _, report = run(BASE, temp_rows, BASE, columns=fewer)
        tt = pair(report, TEST, TEMP)
        self.assertEqual(tt["status"], KEYS_MATCH_FIELDS_INCOMPLETE)
        self.assertEqual(tt["fields_not_compared"], ["application_type"])
        self.assertEqual(tt["schema"]["only_in_a"], ["application_type"])
        self.assertEqual(report["decision"]["decision"], INSUFFICIENT)


@NEEDS_FULL_JOIN
class NotAvailableTests(unittest.TestCase):
    def test_failed_query_is_na_and_empty_tables_are_zero(self):
        _, failed = run(BASE, BASE, BASE, fail=("FULL OUTER JOIN",))
        tt = pair(failed, TEST, TEMP)
        self.assertEqual(tt["status"], INSUFFICIENT)
        self.assertIsNone(tt["counts"])
        self.assertEqual(failed["decision"]["decision"], INSUFFICIENT)
        self.assertIn("N/A", render_html(failed))
        _, empty = run([row(1, year=2025)], [row(1, year=2025)], BASE)
        tt = pair(empty, TEST, TEMP)
        self.assertEqual(tt["status"], INSUFFICIENT)
        self.assertEqual((tt["counts"]["keys_a"], tt["counts"]["keys_b"]), (0, 0))

    def test_missing_table_is_insufficient(self):
        client, report = run(BASE, None, BASE)
        self.assertFalse(report["tables"][TEMP]["exists"])
        self.assertEqual(pair(report, TEST, TEMP)["status"], INSUFFICIENT)
        self.assertEqual(report["decision"]["decision"], INSUFFICIENT)
        self.assertFalse(any(f"[{TEMP}]" in sql for sql, _ in client.statements))


class SchemaAndSafetyTests(unittest.TestCase):
    def test_schema_differences(self):
        meta = result(("column_name", "data_type", "max_length", "numeric_precision", "numeric_scale",
                       "is_nullable", "ordinal_position"),
                      [("enrollment_id", "bigint", None, None, None, "NO", 1),
                       ("hios_issuer_id", "varchar", 10, None, None, "YES", 2),
                       ("extra", "int", None, None, None, "YES", 3)])
        other = result(meta.columns, [("enrollment_id", "bigint", None, None, None, "YES", 2),
                                      ("hios_issuer_id", "varchar", 20, None, None, "YES", 1)])
        diff = compare_schema(parse_columns(meta), parse_columns(other))
        self.assertEqual(diff["status"], "SCHEMA_DIFFERS")
        self.assertEqual(diff["only_in_a"], ["extra"])
        self.assertEqual(diff["type_differences"], [{"column": "hios_issuer_id", "a": "varchar(10)", "b": "varchar(20)"}])
        self.assertEqual(len(diff["nullability_differences"]), 1)
        self.assertEqual(len(diff["ordinal_differences"]), 2)
        self.assertEqual(compare_schema(None, parse_columns(other))["status"], INSUFFICIENT)

    def test_pair_sql_returns_counts_only(self):
        cols = {name: {"name": name, "data_type": dtype} for name, dtype in COLUMNS}
        sql, params, fields = build_pair_sql(TEST, cols, TEMP, cols)
        assert_read_only(sql)
        self.assertEqual(params, (2026, 2026))
        self.assertEqual(len(fields), 9)
        final = sql.rsplit("\nSELECT ", 1)[1]
        self.assertNotIn("k1", final.replace("keys_", ""))
        self.assertNotIn("enrollment_id", final)
        self.assertNotIn("2027", sql)
        self.assertNotRegex(sql.lower(), r"834|inbound|rcni|sftp")

    @NEEDS_FULL_JOIN
    def test_no_raw_identifier_leakage(self):
        _, report = run(BASE + [row(7)], BASE, BASE)
        output = io.StringIO()
        with redirect_stdout(output):
            print_summary(report)
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(report, Path(tmp))
            written = json_path.read_text(encoding="utf-8") + html_path.read_text(encoding="utf-8")
        for text in (json.dumps(report, default=str), output.getvalue(), written):
            self.assertNotIn(str(ID_BASE)[:6], text)
        with self.assertRaises(ComparisonOutputError):
            guard_no_pii({"pairs": {"x": {"enrollment_id": 1}}})


if __name__ == "__main__":
    unittest.main()
