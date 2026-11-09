"""How do Enrollments_TEST, Enrollments_TEST_TEMP and the Oct-31 snapshot relate?

Phase 5D, read-only, fixed scope: exactly three named tables, coverage_year = 2026.
No other object is searched. Set comparison runs inside SQL Server on the
(enrollment_id, enrollee_id) policy-member key; only aggregate counts return.

Per key and table, each business field is reduced to MIN, MAX and NULL count.
A key whose rows disagree on any field is AMBIGUOUS and is never collapsed to a
single value: it is reported separately and compared only by row multiplicity
and an order-independent row signature (CHECKSUM_AGG of BINARY_CHECKSUM), which
is evidence, not proof.

    python -m database.comparison.py2026_source_equivalence
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.lifecycle import PREVIOUS_PLAN_YEAR
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_source_equivalence.json"
HTML_NAME = "py2026_source_equivalence.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.py2026_source_equivalence"
QUERY_TIMEOUT_SECONDS = 900

SCHEMA = "dbo"
TEST = "Enrollments_TEST"
TEMP = "Enrollments_TEST_TEMP"
OCT31 = "Enrollments_PY2526_10312026"
TABLES = (TEST, TEMP, OCT31)
PAIRS = ((TEST, TEMP), (TEST, OCT31), (TEMP, OCT31))

YEAR = "coverage_year"
KEY = ("enrollment_id", "enrollee_id")
FIELDS = (
    "enrollment_status_description",
    "enrollee_status_description",
    "enrollment_create_date",
    "enrollment_confirmation_date",
    "enrollment_last_update_date",
    "benefit_effective_date",
    "benefit_end_date",
    "hios_issuer_id",
    "application_type",
)
DATE_FIELDS = FIELDS[2:7]
STATUS_FIELDS = FIELDS[:2]
ISSUER = "hios_issuer_id"

EXACT_MATCH = "EXACT_MATCH"
MATCH_WITH_AMBIGUITY = "MATCH_WITH_AMBIGUITY"
KEYS_MATCH_FIELDS_INCOMPLETE = "KEYS_MATCH_FIELDS_INCOMPLETE"
DIFFERENT = "DIFFERENT"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
MATCHES = (EXACT_MATCH, MATCH_WITH_AMBIGUITY)

TEST_CANDIDATE = "TEST_AUTHORITATIVE_CANDIDATE"
TEMP_CANDIDATE = "TEST_TEMP_AUTHORITATIVE_CANDIDATE"
IDENTICAL = "IDENTICAL_COPIES_AUTHORITY_UNPROVEN"
DATASETS_DIFFER = "DATASETS_DIFFER"

COLUMNS_SQL = (
    "SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,\n"
    "       CHARACTER_MAXIMUM_LENGTH AS max_length, NUMERIC_PRECISION AS numeric_precision,\n"
    "       NUMERIC_SCALE AS numeric_scale, IS_NULLABLE AS is_nullable, ORDINAL_POSITION AS ordinal_position\n"
    "FROM INFORMATION_SCHEMA.COLUMNS\n"
    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?\n"
    "ORDER BY ORDINAL_POSITION"
)
COUNT_FIELDS = (
    "keys_a", "keys_b", "keys_both", "a_only", "b_only", "rows_a", "rows_b",
    "repeated_keys_a", "repeated_keys_b", "ambiguous_keys_a", "ambiguous_keys_b",
    "shared_multiplicity_differs", "shared_ambiguous", "shared_ambiguous_signature_equal",
    "shared_comparable", "shared_any_field_differs",
)


def parse_columns(result) -> dict:
    """{lowercase name: metadata}. An empty result means the table does not exist."""
    columns = {}
    for row in result.rows:
        record = {str(column).lower(): value for column, value in zip(result.columns, row)}
        name = str(record["column_name"])
        dtype = str(record.get("data_type") or "")
        if record.get("max_length") is not None:
            dtype += "(max)" if record["max_length"] == -1 else f"({record['max_length']})"
        elif dtype.lower() in ("decimal", "numeric") and record.get("numeric_precision") is not None:
            dtype += f"({record['numeric_precision']},{record.get('numeric_scale') or 0})"
        columns[name.lower()] = {
            "name": name,
            "data_type": dtype,
            "is_nullable": str(record.get("is_nullable") or ""),
            "ordinal_position": _int(record.get("ordinal_position")),
        }
    return columns


def compare_schema(a: dict | None, b: dict | None) -> dict:
    if not a or not b:
        return {"status": INSUFFICIENT, "reason": "A table is missing or its metadata query failed."}
    shared = [name for name in a if name in b]
    type_diffs = [{"column": a[n]["name"], "a": a[n]["data_type"], "b": b[n]["data_type"]}
                  for n in shared if a[n]["data_type"].lower() != b[n]["data_type"].lower()]
    null_diffs = [{"column": a[n]["name"], "a": a[n]["is_nullable"], "b": b[n]["is_nullable"]}
                  for n in shared if a[n]["is_nullable"] != b[n]["is_nullable"]]
    ordinal_diffs = [{"column": a[n]["name"], "a": a[n]["ordinal_position"], "b": b[n]["ordinal_position"]}
                     for n in shared if a[n]["ordinal_position"] != b[n]["ordinal_position"]]
    only_a = [a[n]["name"] for n in a if n not in b]
    only_b = [b[n]["name"] for n in b if n not in a]
    identical = not (type_diffs or null_diffs or ordinal_diffs or only_a or only_b)
    return {
        "status": "SCHEMA_IDENTICAL" if identical else "SCHEMA_DIFFERS",
        "columns_a": len(a), "columns_b": len(b), "shared_columns": len(shared),
        "only_in_a": only_a, "only_in_b": only_b,
        "type_differences": type_diffs, "nullability_differences": null_diffs,
        "ordinal_differences": ordinal_diffs,
    }


def _q(columns: dict, name: str) -> str:
    return quote_identifier(columns[name]["name"])


def required_missing(columns: dict | None) -> list[str]:
    if not columns:
        return [YEAR, *KEY]
    return [name for name in (YEAR, *KEY) if name not in columns]


def build_profile_sql(table: str, columns: dict) -> tuple[str, tuple]:
    k1, k2 = _q(columns, KEY[0]), _q(columns, KEY[1])
    selects = [
        "COUNT_BIG(*) AS rows_2026",
        f"COUNT_BIG(DISTINCT {k1}) AS distinct_policies",
        f"COUNT_BIG(DISTINCT {k2}) AS distinct_persons",
        f"COUNT_BIG(CASE WHEN {k1} IS NULL OR {k2} IS NULL THEN 1 END) AS null_key_rows",
    ]
    if ISSUER in columns:
        selects.append(f"COUNT_BIG(DISTINCT {_q(columns, ISSUER)}) AS distinct_issuers")
    for name in DATE_FIELDS:
        if name in columns:
            selects.append(f"MIN({_q(columns, name)}) AS min_{name}")
            selects.append(f"MAX({_q(columns, name)}) AS max_{name}")
    sql = (f"SELECT {', '.join(selects)}\nFROM {qualified_name(SCHEMA, table)}\n"
           f"WHERE {_q(columns, YEAR)} = ?")
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR,)


def build_status_sql(table: str, columns: dict, field: str) -> tuple[str, tuple]:
    status = _q(columns, field)
    sql = (f"SELECT {status} AS status_value, COUNT_BIG(*) AS rows_2026\n"
           f"FROM {qualified_name(SCHEMA, table)}\nWHERE {_q(columns, YEAR)} = ?\n"
           f"GROUP BY {status}")
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR,)


def comparable_fields(a: dict, b: dict) -> list[str]:
    return [name for name in FIELDS if name in a and name in b]


def _per_key_cte(alias: str, table: str, columns: dict, fields: list[str]) -> str:
    k1, k2 = _q(columns, KEY[0]), _q(columns, KEY[1])
    parts = [f"{k1} AS k1", f"{k2} AS k2", "COUNT_BIG(*) AS n"]
    if fields:
        values = ", ".join(_q(columns, name) for name in fields)
        parts.append(f"CHECKSUM_AGG(BINARY_CHECKSUM({values})) AS sig")
    else:
        parts.append("0 AS sig")
    for index, name in enumerate(fields):
        column = _q(columns, name)
        parts += [f"MIN({column}) AS lo{index}", f"MAX({column}) AS hi{index}",
                  f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS nl{index}"]
    return (
        f"{alias} AS (\n"
        f"    SELECT {', '.join(parts)}\n"
        f"    FROM {qualified_name(SCHEMA, table)}\n"
        f"    WHERE {_q(columns, YEAR)} = ? AND {k1} IS NOT NULL AND {k2} IS NOT NULL\n"
        f"    GROUP BY {k1}, {k2}\n"
        ")"
    )


def _ambiguous(alias: str, count: int) -> str:
    if not count:
        return "0"
    tests = " OR ".join(f"{alias}.lo{i} <> {alias}.hi{i} OR ({alias}.nl{i} > 0 AND {alias}.nl{i} < {alias}.n)"
                        for i in range(count))
    return f"CASE WHEN {tests} THEN 1 ELSE 0 END"


def build_pair_sql(table_a: str, cols_a: dict, table_b: str, cols_b: dict) -> tuple[str, tuple, list[str]]:
    """One FULL OUTER JOIN of per-key summaries; each table is scanned once."""
    fields = comparable_fields(cols_a, cols_b)
    count = len(fields)
    diffs = [f"CASE WHEN a.lo{i} = b.lo{i} OR (a.lo{i} IS NULL AND b.lo{i} IS NULL) THEN 0 ELSE 1 END AS d{i}"
             for i in range(count)]
    joined = ",\n           ".join([
        "CASE WHEN a.k1 IS NOT NULL THEN 1 ELSE 0 END AS in_a",
        "CASE WHEN b.k1 IS NOT NULL THEN 1 ELSE 0 END AS in_b",
        "a.n AS an", "b.n AS bn",
        f"{_ambiguous('a', count)} AS aamb",
        f"{_ambiguous('b', count)} AS bamb",
        "CASE WHEN a.sig = b.sig THEN 1 ELSE 0 END AS sig_eq",
        *diffs,
    ])
    both = "in_a = 1 AND in_b = 1"
    clean = f"{both} AND aamb = 0 AND bamb = 0"
    any_diff = " OR ".join(f"d{i} = 1" for i in range(count)) or "1 = 0"
    outputs = [
        "COUNT_BIG(CASE WHEN in_a = 1 THEN 1 END) AS keys_a",
        "COUNT_BIG(CASE WHEN in_b = 1 THEN 1 END) AS keys_b",
        f"COUNT_BIG(CASE WHEN {both} THEN 1 END) AS keys_both",
        "COUNT_BIG(CASE WHEN in_a = 1 AND in_b = 0 THEN 1 END) AS a_only",
        "COUNT_BIG(CASE WHEN in_a = 0 AND in_b = 1 THEN 1 END) AS b_only",
        "COALESCE(SUM(an), 0) AS rows_a",
        "COALESCE(SUM(bn), 0) AS rows_b",
        "COUNT_BIG(CASE WHEN an > 1 THEN 1 END) AS repeated_keys_a",
        "COUNT_BIG(CASE WHEN bn > 1 THEN 1 END) AS repeated_keys_b",
        "COUNT_BIG(CASE WHEN aamb = 1 THEN 1 END) AS ambiguous_keys_a",
        "COUNT_BIG(CASE WHEN bamb = 1 THEN 1 END) AS ambiguous_keys_b",
        f"COUNT_BIG(CASE WHEN {both} AND an <> bn THEN 1 END) AS shared_multiplicity_differs",
        f"COUNT_BIG(CASE WHEN {both} AND (aamb = 1 OR bamb = 1) THEN 1 END) AS shared_ambiguous",
        f"COUNT_BIG(CASE WHEN {both} AND (aamb = 1 OR bamb = 1) AND an = bn AND sig_eq = 1 THEN 1 END) "
        "AS shared_ambiguous_signature_equal",
        f"COUNT_BIG(CASE WHEN {clean} THEN 1 END) AS shared_comparable",
        f"COUNT_BIG(CASE WHEN {clean} AND ({any_diff}) THEN 1 END) AS shared_any_field_differs",
        *[f"COUNT_BIG(CASE WHEN {clean} AND d{i} = 1 THEN 1 END) AS diff{i}" for i in range(count)],
    ]
    sql = (
        f"WITH {_per_key_cte('a', table_a, cols_a, fields)},\n{_per_key_cte('b', table_b, cols_b, fields)},\n"
        "j AS (\n"
        f"    SELECT {joined}\n"
        "    FROM a FULL OUTER JOIN b ON a.k1 = b.k1 AND a.k2 = b.k2\n"
        ")\n"
        f"SELECT {', '.join(outputs)}\nFROM j"
    )
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR, PREVIOUS_PLAN_YEAR), fields


def summarize_pair(counts: dict | None, fields: list[str]) -> dict:
    not_compared = [name for name in FIELDS if name not in fields]
    if counts is None:
        return {"status": INSUFFICIENT, "reason": "Set comparison did not run (N/A, not zero).",
                "counts": None, "field_differences": None, "fields_not_compared": not_compared}
    values = {name: _int(counts.get(name)) for name in COUNT_FIELDS}
    field_diffs = {name: _int(counts.get(f"diff{index}")) for index, name in enumerate(fields)}
    only = values["a_only"] + values["b_only"]
    ambiguous_unmatched = values["shared_ambiguous"] - values["shared_ambiguous_signature_equal"]
    differing = values["shared_any_field_differs"] + values["shared_multiplicity_differs"] + ambiguous_unmatched
    if values["keys_a"] == 0 and values["keys_b"] == 0:
        status = INSUFFICIENT
        reason = "Neither table has PY2026 policy-member keys (zero, not N/A); nothing to compare."
    elif only or differing:
        status = DIFFERENT
        reason = (f"{values['a_only']} keys only in A, {values['b_only']} only in B, "
                  f"{values['shared_any_field_differs']} shared keys with field differences, "
                  f"{values['shared_multiplicity_differs']} with different row counts, "
                  f"{ambiguous_unmatched} ambiguous shared keys whose rows do not match.")
    elif not_compared:
        status = KEYS_MATCH_FIELDS_INCOMPLETE
        reason = "Keys and compared fields match, but some business fields are missing from a table."
    elif values["shared_ambiguous"]:
        status = MATCH_WITH_AMBIGUITY
        reason = (f"All keys match; {values['shared_ambiguous']} ambiguous keys match only by row count and "
                  "a non-cryptographic row signature.")
    else:
        status = EXACT_MATCH
        reason = "Same policy-member keys, same row counts per key, same values in every compared field."
    return {"status": status, "reason": reason, "counts": values,
            "field_differences": field_diffs, "fields_not_compared": not_compared}


def compare_aggregates(a: dict | None, b: dict | None) -> dict | None:
    if a is None or b is None:
        return None
    metrics = {}
    for name in sorted(set(a["profile"] or {}) | set(b["profile"] or {})):
        left, right = (a["profile"] or {}).get(name), (b["profile"] or {}).get(name)
        metrics[name] = None if left is None or right is None else left == right
    for field in STATUS_FIELDS:
        left, right = (a["status"] or {}).get(field), (b["status"] or {}).get(field)
        metrics[f"status_distribution_{field}"] = None if left is None or right is None else left == right
    known = [value for value in metrics.values() if value is not None]
    return {"all_equal": (all(known) if known else None), "metrics": metrics}


def decide(pairs: dict) -> dict:
    tt = pairs[f"{TEST} vs {TEMP}"]["status"]
    t_oct = pairs[f"{TEST} vs {OCT31}"]["status"]
    m_oct = pairs[f"{TEMP} vs {OCT31}"]["status"]
    oct31 = {"test_vs_oct31": t_oct, "test_temp_vs_oct31": m_oct}
    if tt in MATCHES:
        note = ("TEST and TEST_TEMP hold the same PY2026 policy-member records"
                + (" and both match the Oct-31 snapshot." if t_oct in MATCHES and m_oct in MATCHES else
                   "; their relationship to the Oct-31 snapshot is " + f"{t_oct}/{m_oct}."))
        return {"decision": IDENTICAL, "reason": note + " Authority is not proven by data equality or by name.",
                "oct31_relationship": oct31}
    if tt == DIFFERENT:
        if t_oct in MATCHES and m_oct == DIFFERENT:
            return {"decision": TEST_CANDIDATE, "oct31_relationship": oct31, "reason": (
                "TEST and TEST_TEMP differ; only TEST matches the latest dated snapshot. Candidate only.")}
        if m_oct in MATCHES and t_oct == DIFFERENT:
            return {"decision": TEMP_CANDIDATE, "oct31_relationship": oct31, "reason": (
                "TEST and TEST_TEMP differ; only TEST_TEMP matches the latest dated snapshot. Candidate only.")}
        return {"decision": DATASETS_DIFFER, "oct31_relationship": oct31,
                "reason": "TEST and TEST_TEMP differ, and the Oct-31 snapshot does not single one out."}
    return {"decision": INSUFFICIENT, "oct31_relationship": oct31,
            "reason": f"TEST vs TEST_TEMP comparison is {tt}."}


def collect(client) -> dict:
    failures: list[dict] = []
    tables = {}
    for table in TABLES:
        result = _run(client, failures, f"columns {table}", COLUMNS_SQL, (SCHEMA, table))
        columns = None if result is None else parse_columns(result)
        entry = {"exists": None if result is None else bool(columns), "columns": columns,
                 "missing_required": required_missing(columns), "profile": None, "status": None}
        tables[table] = entry
        if entry["missing_required"]:
            continue
        result = _query(client, failures, f"profile {table}", lambda: build_profile_sql(table, columns))
        entry["profile"] = None if result is None or not result.rows else _clean(_row(result))
        entry["status"] = {}
        for field in STATUS_FIELDS:
            if field in columns:
                result = _query(client, failures, f"{field} {table}", lambda: build_status_sql(table, columns, field))
                entry["status"][field] = None if result is None else {
                    str(_clean(value)): _int(count) for value, count in result.rows}
    pairs = {}
    for left, right in PAIRS:
        a, b = tables[left], tables[right]
        fields, counts = [], None
        if not a["missing_required"] and not b["missing_required"]:
            fields = comparable_fields(a["columns"], b["columns"])
            try:
                sql, params, fields = build_pair_sql(left, a["columns"], right, b["columns"])
            except QueryExecutionError as exc:
                failures.append({"step": f"pair {left} vs {right}", "error": str(exc)})
            else:
                result = _run(client, failures, f"pair {left} vs {right}", sql, params)
                counts = None if result is None or not result.rows else _row(result)
        pairs[f"{left} vs {right}"] = {"a": left, "b": right, "fields": fields, "counts": counts}
    return {"tables": tables, "pairs": pairs, "failures": failures}


def build_report(evidence: dict) -> dict:
    tables = evidence.get("tables") or {}
    pairs = {}
    for name, pair in (evidence.get("pairs") or {}).items():
        a, b = tables.get(pair["a"]), tables.get(pair["b"])
        pairs[name] = {
            "a": pair["a"], "b": pair["b"],
            "schema": compare_schema((a or {}).get("columns"), (b or {}).get("columns")),
            "aggregates": compare_aggregates(a, b),
            **summarize_pair(pair.get("counts"), pair.get("fields") or []),
        }
    for left, right in PAIRS:
        pairs.setdefault(f"{left} vs {right}", {"a": left, "b": right, "status": INSUFFICIENT,
                                                "reason": "Not evaluated.", "counts": None})
    report = {
        "report_kind": "PY2026_SOURCE_EQUIVALENCE",
        "scope": [f"{SCHEMA}.{table}" for table in TABLES],
        "plan_year_filter": f"{YEAR} = {PREVIOUS_PLAN_YEAR}",
        "key": "policy-member (enrollment_id, enrollee_id); compared inside SQL Server",
        "fields": list(FIELDS),
        "tables": {
            table: {
                "exists": entry.get("exists"),
                "column_count": None if not entry.get("columns") else len(entry["columns"]),
                "missing_required": entry.get("missing_required"),
                "profile": entry.get("profile"),
                "status_distribution": entry.get("status"),
            }
            for table, entry in tables.items()
        },
        "pairs": pairs,
        "decision": decide(pairs),
        "definitions": {
            EXACT_MATCH: "Same keys, same rows per key, same value in every compared field.",
            MATCH_WITH_AMBIGUITY: "As above, except ambiguous keys match only by row count and row signature.",
            KEYS_MATCH_FIELDS_INCOMPLETE: "Keys match but some business fields exist in only one table.",
            DIFFERENT: "Any one-sided key, field difference, row-count difference or unmatched ambiguous key.",
            "ambiguous": "A key whose rows in one table disagree on a field. Never collapsed to one value.",
            "null": "N/A: the query did not run. Never zero.",
        },
        "production_comparable": False,
        "failures": evidence.get("failures") or [],
        "notes": [
            "Only the three named tables are queried. No discovery, no PY2027, no 834/inbound/RCNI/SFTP.",
            "Identifier values never leave SQL Server; only aggregate counts are returned.",
            "Equal data does not prove provenance or authority, and a table name is not evidence.",
        ],
        "command": COMMAND,
    }
    guard_no_pii(report)
    return report


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def _cell(value) -> str:
    return "N/A" if value is None else html.escape(str(value))


def render_html(report: dict) -> str:
    rows = []
    for name, pair in report["pairs"].items():
        counts = pair.get("counts") or {}
        rows.append(
            f"<tr><td>{_cell(name)}</td><td>{_cell((pair.get('schema') or {}).get('status'))}</td>"
            f"<td>{_cell((pair.get('aggregates') or {}).get('all_equal'))}</td>"
            + "".join(f"<td>{_cell(counts.get(field))}</td>" for field in (
                "keys_a", "keys_b", "keys_both", "a_only", "b_only", "shared_any_field_differs",
                "shared_multiplicity_differs", "shared_ambiguous"))
            + f"<td>{_cell(pair.get('status'))}</td></tr>")
    decision = report["decision"]
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>PY2026 source equivalence</title></head>"
        "<body><h1>PY2026 source equivalence</h1>"
        f"<p><b>{_cell(decision['decision'])}</b>: {_cell(decision['reason'])}</p>"
        "<table border='1' cellpadding='4'><tr><th>Pair</th><th>Schema</th><th>Aggregates equal</th>"
        "<th>Keys A</th><th>Keys B</th><th>Both</th><th>A only</th><th>B only</th><th>Field diffs</th>"
        "<th>Row-count diffs</th><th>Ambiguous shared</th><th>Status</th></tr>"
        f"{''.join(rows)}</table>"
        "<p>Aggregates only. N/A means the query did not run; it is never zero.</p></body></html>"
    )


def print_summary(report: dict) -> None:
    print("PY2026 SOURCE EQUIVALENCE (read-only, aggregates only)")
    for table, entry in report["tables"].items():
        profile = entry.get("profile") or {}
        print(f"{table}: exists={_text(entry.get('exists'))} rows={_text(profile.get('rows_2026'))} "
              f"policies={_text(profile.get('distinct_policies'))} persons={_text(profile.get('distinct_persons'))}")
    for name, pair in report["pairs"].items():
        counts = pair.get("counts") or {}
        print(f"{name}: schema={_text((pair.get('schema') or {}).get('status'))} "
              f"aggregates_equal={_text((pair.get('aggregates') or {}).get('all_equal'))} "
              f"keys={_text(counts.get('keys_a'))}/{_text(counts.get('keys_b'))} both={_text(counts.get('keys_both'))} "
              f"a_only={_text(counts.get('a_only'))} b_only={_text(counts.get('b_only'))} "
              f"field_diffs={_text(counts.get('shared_any_field_differs'))} "
              f"row_count_diffs={_text(counts.get('shared_multiplicity_differs'))} "
              f"ambiguous={_text(counts.get('shared_ambiguous'))} -> {pair.get('status')}")
        if pair.get("field_differences"):
            print(f"  field differences: {pair['field_differences']}")
    decision = report["decision"]
    print(f"Decision: {decision['decision']} - {decision['reason']}")
    for failure in report["failures"]:
        print(f"Failure: {failure['step']}: {failure['error']}")


def _run(client, failures, step, sql, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


def _query(client, failures, step, builder):
    try:
        sql, params = builder()
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None
    return _run(client, failures, step, sql, params)


def _row(result) -> dict:
    return {str(column): value for column, value in zip(result.columns, result.rows[0])}


def _int(value):
    return None if value is None else int(value)


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _text(value) -> str:
    return "N/A" if value is None else str(value)


def main() -> int:
    evidence = {"tables": {}, "pairs": {}, "failures": []}
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect(DatabaseClient(connection))
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(evidence)
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
