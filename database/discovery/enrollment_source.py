"""Find an enrollment source that contains coverage years 2026 and 2027.

Explicit command (not run by main.py)::

    python -m database.discovery.enrollment_source

Reads catalog metadata, then aggregate counts only. Individual enrollee and
enrollment ids are used inside SQL for DISTINCT and are not returned.
Plan year is ``coverage_year``. Calendar year is not treated as plan year.
No production monthly definition is chosen here.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from database.config import PROJECT_ROOT, load_database_config
from database.connection import database_connection, sanitize_database_message
from database.models import DatabaseError, QueryResult
from database.query_runner import DatabaseClient, QueryExecutionError, assert_read_only, load_query_file

PLAN_YEARS = (2026, 2027)
QUERY_TIMEOUT_SECONDS = 180
MAX_YEAR_PROBES = 20
OUTPUT_DIR = PROJECT_ROOT / "reports" / "discovery"
JSON_NAME = "enrollment_py26_py27_source_discovery.json"
HTML_NAME = "enrollment_py26_py27_source_discovery.html"

TARGET_COLUMNS = (
    "coverage_year",
    "enrollment_id",
    "enrollee_id",
    "hios_issuer_id",
    "enrollment_status_description",
    "enrollee_status_description",
    "application_type",
    "enrollment_confirmation_date",
    "enrollment_create_date",
    "enrollment_last_update_date",
    "benefit_effective_date",
    "benefit_end_date",
)
NULL_COLUMNS = (
    "enrollment_id",
    "enrollee_id",
    "hios_issuer_id",
    "enrollment_confirmation_date",
    "enrollment_status_description",
    "enrollee_status_description",
    "application_type",
)
DATE_RANGES = (
    "enrollment_confirmation_date",
    "enrollment_create_date",
    "benefit_effective_date",
)
DISTINCT_COLUMNS = (
    ("enrollment_id", "distinct_enrollment_id"),
    ("enrollee_id", "distinct_enrollee_id"),
    ("hios_issuer_id", "distinct_issuer_count"),
)
INBOUND_NAME_MARKERS = ("834", "inbound", "rcni", "sftp")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROHIBITED_OUTPUT_KEYS = frozenset({
    "enrollee_id",
    "enrollment_id",
    "ssn",
    "social_security_number",
    "email",
    "email_address",
    "birth_date",
    "dob",
    "date_of_birth",
    "first_name",
    "last_name",
    "enrollee_first_name",
    "enrollee_last_name",
    "address",
    "street_address",
})

DISCOVERY_NOTES = (
    "Discovery only. These counts are not production Enrollment Count or Enrollee Count definitions.",
    "Plan year is coverage_year. Calendar year, table name, snapshot date, and confirmation year are not plan year.",
    "The historical October 1 confirmation floor is not applied.",
    "Month 1 of the enrollment season is not defined.",
    "834 and inbound objects are listed and not aggregated.",
    "Historical table names are not treated as the authoritative source.",
)


class DiscoveryOutputError(Exception):
    """The discovery artifact would contain a prohibited field."""


def quote_identifier(name: str) -> str:
    """Bracket-quote a catalog identifier. Reject anything that is not a plain name."""
    if not isinstance(name, str) or IDENTIFIER.fullmatch(name) is None:
        raise QueryExecutionError("Unsafe SQL identifier")
    return f"[{name}]"


def qualified_name(schema_name: str, table_name: str) -> str:
    return f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"


def parse_catalog(result: QueryResult) -> list[dict]:
    """Group metadata rows into one object per table or view."""
    grouped: dict[tuple[str, str], dict] = {}
    for row in result.rows:
        record = {column.lower(): value for column, value in zip(result.columns, row)}
        schema_name = str(record.get("table_schema") or "")
        table_name = str(record.get("table_name") or "")
        column_name = str(record.get("column_name") or "")
        key = (schema_name, table_name)
        source = grouped.get(key)
        if source is None:
            source = {
                "schema_name": schema_name,
                "table_name": table_name,
                "table_type": str(record.get("table_type") or ""),
                "columns": {},
            }
            grouped[key] = source
        lowered = column_name.lower()
        if lowered not in TARGET_COLUMNS:
            continue
        source["columns"][lowered] = {
            "name": column_name,
            "data_type": format_data_type(record),
        }
    return sorted(grouped.values(), key=lambda item: (item["schema_name"].lower(), item["table_name"].lower()))


def format_data_type(record: Mapping) -> str:
    dtype = str(record.get("data_type") or "")
    length = record.get("character_maximum_length")
    if dtype.lower() in {"varchar", "nvarchar", "char", "nchar", "varbinary", "binary"}:
        if length == -1:
            return f"{dtype}(max)"
        if length is not None:
            return f"{dtype}({length})"
    precision = record.get("numeric_precision")
    if dtype.lower() in {"decimal", "numeric"} and precision is not None:
        scale = record.get("numeric_scale")
        scale_text = 0 if scale is None else scale
        return f"{dtype}({precision},{scale_text})"
    datetime_precision = record.get("datetime_precision")
    if dtype.lower() in {"datetime2", "datetimeoffset", "time"} and datetime_precision is not None:
        return f"{dtype}({datetime_precision})"
    return dtype


def is_inbound_object(table_name: str) -> bool:
    lowered = table_name.lower()
    return any(marker in lowered for marker in INBOUND_NAME_MARKERS)


def split_sources(sources: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Return excluded inbound objects, probe candidates, and objects not probed."""
    excluded = []
    candidates = []
    not_probed = []
    for source in sources:
        columns = source["columns"]
        if is_inbound_object(source["table_name"]):
            excluded.append(_public_source(source, reason="834/inbound name; rows were not read"))
            continue
        has_year = "coverage_year" in columns
        has_person_or_policy = "enrollment_id" in columns or "enrollee_id" in columns
        if has_year and has_person_or_policy:
            candidates.append(source)
        else:
            not_probed.append(_public_source(
                source,
                reason="missing coverage_year or both enrollment_id and enrollee_id",
            ))
    candidates.sort(key=lambda item: (-len(item["columns"]), item["schema_name"].lower(), item["table_name"].lower()))
    return excluded, candidates, not_probed


def choose_source(matrix: list[dict]) -> dict | None:
    """Pick the source that has both plan years and the most requested columns.

    The table name is not a ranking factor except as a stable final tie-break.
    """
    both = [item for item in matrix if item["py2026"] and item["py2027"]]
    if not both:
        return None
    ranked = sorted(both, key=_selection_key)
    selected = dict(ranked[0])
    selected["selection_reason"] = (
        "Both coverage_year 2026 and coverage_year 2027 are present, "
        "and this object has the most requested enrollment columns. "
        "When column coverage is equal, a base table is preferred over a view. "
        "The table name was not treated as authority."
    )
    if len(ranked) > 1 and _selection_key(ranked[0])[:2] == _selection_key(ranked[1])[:2]:
        selected["tie_break"] = "Base tables are preferred over views. Remaining ties use schema and table name order."
    return selected


def column_map(source: dict) -> dict[str, str]:
    return {name: info["name"] for name, info in source["columns"].items()}


def schema_report(source: dict) -> list[dict]:
    columns = source["columns"]
    report = []
    for name in TARGET_COLUMNS:
        info = columns.get(name)
        report.append({
            "column": name,
            "exists": info is not None,
            "data_type": None if info is None else info["data_type"],
        })
    return report


def build_year_presence_sql(source: dict) -> str:
    qualified = qualified_name(source["schema_name"], source["table_name"])
    year_column = quote_identifier(source["columns"]["coverage_year"]["name"])
    return (
        "SELECT\n"
        f"    {year_column} AS coverage_year,\n"
        "    COUNT_BIG(*) AS raw_rows\n"
        f"FROM {qualified}\n"
        f"WHERE {year_column} IN (?, ?)\n"
        f"GROUP BY {year_column}\n"
        f"ORDER BY {year_column}"
    )


def build_year_profile_sql(qualified: str, columns: Mapping[str, str]) -> str:
    year_column = quote_identifier(columns["coverage_year"])
    selects = [
        f"{year_column} AS coverage_year",
        "COUNT_BIG(*) AS raw_rows",
    ]
    for name, alias in DISTINCT_COLUMNS:
        actual = columns.get(name)
        if actual:
            selects.append(f"COUNT_BIG(DISTINCT {quote_identifier(actual)}) AS {alias}")
    for name in DATE_RANGES:
        actual = columns.get(name)
        if actual:
            quoted = quote_identifier(actual)
            selects.append(f"MIN({quoted}) AS min_{name}")
            selects.append(f"MAX({quoted}) AS max_{name}")
    for name in NULL_COLUMNS:
        actual = columns.get(name)
        if actual:
            quoted = quote_identifier(actual)
            selects.append(
                f"SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END) AS null_{name}"
            )
    body = ",\n    ".join(selects)
    return (
        "SELECT\n"
        f"    {body}\n"
        f"FROM {qualified}\n"
        f"WHERE {year_column} IN (?, ?)\n"
        f"GROUP BY {year_column}\n"
        f"ORDER BY {year_column}"
    )


def build_grouped_profile_sql(
    qualified: str,
    columns: Mapping[str, str],
    group_column: str,
) -> str:
    year_column = quote_identifier(columns["coverage_year"])
    grouped = quote_identifier(columns[group_column])
    selects = [
        f"{year_column} AS coverage_year",
        f"{grouped} AS {group_column}",
        "COUNT_BIG(*) AS raw_rows",
    ]
    for name, alias in DISTINCT_COLUMNS[:2]:
        actual = columns.get(name)
        if actual:
            selects.append(f"COUNT_BIG(DISTINCT {quote_identifier(actual)}) AS {alias}")
    body = ",\n    ".join(selects)
    return (
        "SELECT\n"
        f"    {body}\n"
        f"FROM {qualified}\n"
        f"WHERE {year_column} IN (?, ?)\n"
        f"GROUP BY {year_column}, {grouped}\n"
        f"ORDER BY {year_column}, {grouped}"
    )


def build_confirmation_month_sql(qualified: str, columns: Mapping[str, str]) -> str:
    year_column = quote_identifier(columns["coverage_year"])
    confirmed = quote_identifier(columns["enrollment_confirmation_date"])
    selects = [
        f"{year_column} AS coverage_year",
        f"YEAR({confirmed}) AS confirmation_calendar_year",
        f"MONTH({confirmed}) AS confirmation_calendar_month",
        "COUNT_BIG(*) AS raw_rows",
    ]
    for name, alias in DISTINCT_COLUMNS[:2]:
        actual = columns.get(name)
        if actual:
            selects.append(f"COUNT_BIG(DISTINCT {quote_identifier(actual)}) AS {alias}")
    body = ",\n    ".join(selects)
    return (
        "SELECT\n"
        f"    {body}\n"
        f"FROM {qualified}\n"
        f"WHERE {year_column} IN (?, ?)\n"
        f"  AND {confirmed} IS NOT NULL\n"
        f"GROUP BY {year_column}, YEAR({confirmed}), MONTH({confirmed})\n"
        f"ORDER BY {year_column}, confirmation_calendar_year, confirmation_calendar_month"
    )


def build_first_confirmation_sql(
    qualified: str,
    columns: Mapping[str, str],
    identity_column: str,
    count_alias: str,
) -> str:
    year_column = quote_identifier(columns["coverage_year"])
    identity = quote_identifier(columns[identity_column])
    confirmed = quote_identifier(columns["enrollment_confirmation_date"])
    return (
        "WITH first_confirmation AS (\n"
        "    SELECT\n"
        f"        {year_column} AS coverage_year,\n"
        f"        {identity} AS identity_key,\n"
        f"        MIN({confirmed}) AS first_confirmation_date\n"
        f"    FROM {qualified}\n"
        f"    WHERE {year_column} IN (?, ?)\n"
        f"      AND {confirmed} IS NOT NULL\n"
        f"      AND {identity} IS NOT NULL\n"
        f"    GROUP BY {year_column}, {identity}\n"
        ")\n"
        "SELECT\n"
        "    coverage_year,\n"
        "    YEAR(first_confirmation_date) AS confirmation_calendar_year,\n"
        "    MONTH(first_confirmation_date) AS confirmation_calendar_month,\n"
        f"    COUNT_BIG(*) AS {count_alias}\n"
        "FROM first_confirmation\n"
        "GROUP BY coverage_year, YEAR(first_confirmation_date), MONTH(first_confirmation_date)\n"
        "ORDER BY coverage_year, confirmation_calendar_year, confirmation_calendar_month"
    )


def build_overlap_sql(qualified: str, columns: Mapping[str, str], identity_column: str) -> str:
    year_column = quote_identifier(columns["coverage_year"])
    identity = quote_identifier(columns[identity_column])
    return (
        "SELECT COUNT_BIG(*) AS overlap_count\n"
        "FROM (\n"
        f"    SELECT DISTINCT {identity} AS identity_key\n"
        f"    FROM {qualified}\n"
        f"    WHERE {year_column} = ?\n"
        f"      AND {identity} IS NOT NULL\n"
        "    INTERSECT\n"
        f"    SELECT DISTINCT {identity} AS identity_key\n"
        f"    FROM {qualified}\n"
        f"    WHERE {year_column} = ?\n"
        f"      AND {identity} IS NOT NULL\n"
        ") AS overlap_keys"
    )


def rows_to_records(result: QueryResult) -> list[dict]:
    records = []
    for row in result.rows:
        record = {}
        for column, value in zip(result.columns, row):
            record[column] = _json_value(value)
        records.append(record)
    return records


def parse_year_matrix_row(source: dict, result: QueryResult) -> dict:
    counts = {2026: 0, 2027: 0}
    for record in rows_to_records(result):
        year = _year_value(record.get("coverage_year"))
        if year in counts:
            counts[year] = int(record.get("raw_rows") or 0)
    return {
        "schema_name": source["schema_name"],
        "table_name": source["table_name"],
        "table_type": source["table_type"],
        "target_column_count": len(source["columns"]),
        "py2026": counts[2026] > 0,
        "py2027": counts[2027] > 0,
        "py2026_raw_rows": counts[2026],
        "py2027_raw_rows": counts[2027],
    }


def guard_no_pii(value, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in PROHIBITED_OUTPUT_KEYS:
                raise DiscoveryOutputError("Discovery output refused a prohibited field")
            guard_no_pii(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            guard_no_pii(item, path)
        return
    if isinstance(value, (bytes, bytearray)):
        raise DiscoveryOutputError("Discovery output refused a binary value")


def write_discovery_report(payload: dict, directory: Path | None = None) -> tuple[Path, Path]:
    guard_no_pii(payload)
    target = directory or OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / JSON_NAME
    html_path = target / HTML_NAME
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    return json_path, html_path


def render_html(payload: dict) -> str:
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<title>Enrollment source discovery</title>",
        "<style>body{font-family:sans-serif;margin:2rem;} table{border-collapse:collapse;margin:1rem 0;}",
        "td,th{border:1px solid #ccc;padding:.3rem .6rem;text-align:left;} .note{max-width:50rem;}</style>",
        "</head><body>",
        "<h1>Enrollment source discovery</h1>",
        "<p class=\"note\">Aggregate discovery only. Not a production PY2026 vs PY2027 comparison. "
        "Plan year is coverage_year. Month 1 is not defined.</p>",
    ]
    parts.append("<h2>Notes</h2><ul>")
    for note in payload.get("notes", []):
        parts.append(f"<li>{html.escape(str(note))}</li>")
    parts.append("</ul>")
    _html_table(parts, "Year coverage", payload.get("year_matrix", []))
    selected = payload.get("selected_source")
    parts.append("<h2>Selected source</h2>")
    if selected:
        _html_table(parts, None, [selected])
    else:
        parts.append("<p>No probed source contains both coverage_year 2026 and 2027.</p>")
    _html_table(parts, "Schema", payload.get("schema", []))
    _html_table(parts, "Year profile", payload.get("year_profile", []))
    _html_table(parts, "Nulls", payload.get("nulls", []))
    status = payload.get("status_profile", {})
    _html_table(parts, "Enrollment status", status.get("enrollment_status", []))
    _html_table(parts, "Enrollee status", status.get("enrollee_status", []))
    _html_table(parts, "Application type", payload.get("application_type_profile", []))
    _html_table(parts, "Issuer", payload.get("issuer_profile", []))
    months = payload.get("confirmation_month_profile", {})
    _html_table(parts, "Confirmation month, row level", months.get("row_level", []))
    _html_table(parts, "Person first confirmation", months.get("person_first", []))
    _html_table(parts, "Enrollment first confirmation", months.get("enrollment_first", []))
    _html_table(parts, "Cross-year overlap", [payload.get("overlap", {})])
    _html_table(parts, "Excluded inbound objects", payload.get("excluded_inbound_objects", []))
    _html_table(parts, "Not probed", payload.get("not_probed", []))
    _html_table(parts, "Failures", payload.get("failures", []))
    parts.append("</body></html>")
    return "\n".join(parts)


def run_discovery(client: DatabaseClient) -> dict:
    """Run the metadata search and, when a both-year source exists, its aggregate profiles."""
    queries: list[dict] = []
    failures: list[dict] = []
    catalog_sql = load_query_file("discovery/enrollment_source_candidates.sql")
    catalog_result = _run(client, queries, failures, "catalog", catalog_sql, None)
    if catalog_result is None:
        return _payload(queries, failures, None, [], [], [], [])

    sources = parse_catalog(catalog_result)
    excluded, candidates, not_probed = split_sources(sources)
    probed = candidates[:MAX_YEAR_PROBES]
    for skipped in candidates[MAX_YEAR_PROBES:]:
        not_probed.append(_public_source(skipped, reason=f"not probed; limit is {MAX_YEAR_PROBES} objects"))

    matrix = []
    for source in probed:
        label = f"year_presence:{source['schema_name']}.{source['table_name']}"
        try:
            sql = build_year_presence_sql(source)
        except QueryExecutionError as exc:
            failures.append({"step": label, "error": sanitize_database_message(str(exc))})
            continue
        result = _run(client, queries, failures, label, sql, PLAN_YEARS)
        if result is None:
            continue
        matrix.append(parse_year_matrix_row(source, result))

    selected_row = choose_source(matrix)
    selected_source = _find_source(probed, selected_row)
    payload = _payload(queries, failures, selected_row, matrix, excluded, not_probed, [])
    if selected_source is None:
        payload["notes"] = list(DISCOVERY_NOTES) + [
            "No probed object has both coverage years, so profiles were not run.",
        ]
        return payload

    try:
        columns = column_map(selected_source)
        qualified = qualified_name(selected_source["schema_name"], selected_source["table_name"])
    except QueryExecutionError as exc:
        failures.append({"step": "profile", "error": sanitize_database_message(str(exc))})
        payload["queries_executed"] = queries
        payload["failures"] = failures
        return payload
    payload["schema"] = schema_report(selected_source)
    profile_sql = _built(failures, "year_profile", lambda: build_year_profile_sql(qualified, columns))
    profile = None if profile_sql is None else _run(client, queries, failures, "year_profile", profile_sql, PLAN_YEARS)
    if profile is not None:
        records = rows_to_records(profile)
        payload["year_profile"] = [_without_null_fields(record) for record in records]
        payload["nulls"] = [_null_fields(record) for record in records]

    _grouped(client, queries, failures, payload, qualified, columns, "enrollment_status_description", "status_profile", "enrollment_status")
    _grouped(client, queries, failures, payload, qualified, columns, "enrollee_status_description", "status_profile", "enrollee_status")
    _grouped(client, queries, failures, payload, qualified, columns, "application_type", "application_type_profile", None)
    _grouped(client, queries, failures, payload, qualified, columns, "hios_issuer_id", "issuer_profile", None)

    if "enrollment_confirmation_date" in columns:
        month_sql = _built(failures, "confirmation_month", lambda: build_confirmation_month_sql(qualified, columns))
        month = None if month_sql is None else _run(
            client, queries, failures, "confirmation_month", month_sql, PLAN_YEARS,
        )
        if month is not None:
            payload["confirmation_month_profile"]["row_level"] = rows_to_records(month)
        if "enrollee_id" in columns:
            person_sql = _built(
                failures, "person_first_confirmation",
                lambda: build_first_confirmation_sql(qualified, columns, "enrollee_id", "person_count"),
            )
            person = None if person_sql is None else _run(
                client, queries, failures, "person_first_confirmation", person_sql, PLAN_YEARS,
            )
            if person is not None:
                payload["confirmation_month_profile"]["person_first"] = rows_to_records(person)
        if "enrollment_id" in columns:
            enrollment_sql = _built(
                failures, "enrollment_first_confirmation",
                lambda: build_first_confirmation_sql(qualified, columns, "enrollment_id", "enrollment_count"),
            )
            enrollment = None if enrollment_sql is None else _run(
                client, queries, failures, "enrollment_first_confirmation", enrollment_sql, PLAN_YEARS,
            )
            if enrollment is not None:
                payload["confirmation_month_profile"]["enrollment_first"] = rows_to_records(enrollment)
    else:
        payload["skipped"].append({
            "step": "confirmation_month",
            "reason": "enrollment_confirmation_date is not present",
        })

    overlap = payload["overlap"]
    if "enrollee_id" in columns:
        overlap_sql = _built(failures, "enrollee_overlap", lambda: build_overlap_sql(qualified, columns, "enrollee_id"))
        overlap_result = None if overlap_sql is None else _run(
            client, queries, failures, "enrollee_overlap", overlap_sql, PLAN_YEARS,
        )
        if overlap_result is not None and overlap_result.rows:
            overlap["enrollee_id_in_both_years"] = _json_value(overlap_result.rows[0][0])
    if "enrollment_id" in columns:
        overlap_sql = _built(
            failures, "enrollment_overlap", lambda: build_overlap_sql(qualified, columns, "enrollment_id"),
        )
        overlap_result = None if overlap_sql is None else _run(
            client, queries, failures, "enrollment_overlap", overlap_sql, PLAN_YEARS,
        )
        if overlap_result is not None and overlap_result.rows:
            overlap["enrollment_id_in_both_years"] = _json_value(overlap_result.rows[0][0])
    payload["queries_executed"] = queries
    payload["failures"] = failures
    return payload


def main() -> int:
    try:
        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            payload = run_discovery(DatabaseClient(connection))
        json_path, html_path = write_discovery_report(payload)
    except (DatabaseError, DiscoveryOutputError) as exc:
        print("Enrollment source discovery: FAILED")
        print(sanitize_database_message(str(exc)))
        return 1
    print("Enrollment source discovery: COMPLETE")
    selected = payload.get("selected_source")
    if selected:
        print(f"Selected source: {selected['schema_name']}.{selected['table_name']}")
    else:
        print("Selected source: none with both coverage_year 2026 and 2027")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


def _built(failures, name, builder):
    try:
        return builder()
    except QueryExecutionError as exc:
        failures.append({"step": name, "error": sanitize_database_message(str(exc))})
        return None


def _run(client, queries, failures, name, sql, params):
    assert_read_only(sql)
    if "SELECT *" in sql.upper() or "SELECT*" in sql.upper().replace(" ", ""):
        raise QueryExecutionError("SELECT * is not used for enrollment discovery")
    queries.append({
        "name": name,
        "parameter_count": 0 if params is None else len(tuple(params)),
        "sql": sql,
    })
    try:
        if params is None:
            return client.execute_query(sql)
        return client.execute_query(sql, tuple(params))
    except QueryExecutionError as exc:
        failures.append({"step": name, "error": sanitize_database_message(str(exc))})
        return None


def _grouped(client, queries, failures, payload, qualified, columns, column_name, section, child):
    if column_name not in columns:
        payload["skipped"].append({"step": column_name, "reason": f"{column_name} is not present"})
        return
    sql = _built(failures, column_name, lambda: build_grouped_profile_sql(qualified, columns, column_name))
    if sql is None:
        return
    result = _run(client, queries, failures, column_name, sql, PLAN_YEARS)
    if result is None:
        return
    records = rows_to_records(result)
    if child is None:
        payload[section] = records
    else:
        payload[section][child] = records


def _payload(queries, failures, selected, matrix, excluded, not_probed, skipped):
    return {
        "notes": list(DISCOVERY_NOTES),
        "plan_years": list(PLAN_YEARS),
        "plan_year_field": "coverage_year",
        "selected_source": selected,
        "year_matrix": matrix,
        "schema": [],
        "year_profile": [],
        "nulls": [],
        "status_profile": {"enrollment_status": [], "enrollee_status": []},
        "application_type_profile": [],
        "issuer_profile": [],
        "confirmation_month_profile": {
            "row_level": [],
            "person_first": [],
            "enrollment_first": [],
            "meaning": (
                "Calendar year and month of enrollment_confirmation_date. "
                "Not a production month definition and not Month 1."
            ),
        },
        "overlap": {
            "enrollee_id_in_both_years": None,
            "enrollment_id_in_both_years": None,
        },
        "excluded_inbound_objects": excluded,
        "not_probed": not_probed,
        "skipped": skipped,
        "failures": failures,
        "queries_executed": queries,
    }


def _public_source(source: dict, reason: str) -> dict:
    return {
        "schema_name": source["schema_name"],
        "table_name": source["table_name"],
        "table_type": source["table_type"],
        "columns_present": sorted(source["columns"]),
        "reason": reason,
    }


def _selection_key(item: dict):
    return (
        -int(item["target_column_count"]),
        0 if item["table_type"] == "BASE TABLE" else 1,
        item["schema_name"].lower(),
        item["table_name"].lower(),
    )


def _find_source(sources: list[dict], selected: dict | None) -> dict | None:
    if selected is None:
        return None
    for source in sources:
        if source["schema_name"] == selected["schema_name"] and source["table_name"] == selected["table_name"]:
            return source
    return None


def _without_null_fields(record: dict) -> dict:
    return {key: value for key, value in record.items() if not str(key).startswith("null_")}


def _null_fields(record: dict) -> dict:
    kept = {"coverage_year": record.get("coverage_year")}
    for key, value in record.items():
        if str(key).startswith("null_"):
            kept[key] = value
    return kept


def _year_value(value):
    number = _json_value(value)
    if isinstance(number, str) and number.isdigit():
        return int(number)
    return number


def _json_value(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        raise DiscoveryOutputError("Discovery output refused a binary value")
    return value


def _html_table(parts: list[str], title: str | None, rows) -> None:
    if title:
        parts.append(f"<h2>{html.escape(title)}</h2>")
    if not rows:
        parts.append("<p>None.</p>")
        return
    if isinstance(rows, dict):
        rows = [rows]
    headers = []
    for row in rows:
        if isinstance(row, dict):
            for key in row:
                if key not in headers:
                    headers.append(key)
    parts.append("<table><thead><tr>")
    for header in headers:
        parts.append(f"<th>{html.escape(str(header))}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for header in headers:
            cell = "" if not isinstance(row, dict) or row.get(header) is None else row.get(header)
            parts.append(f"<td>{html.escape(str(cell))}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")


if __name__ == "__main__":
    raise SystemExit(main())
