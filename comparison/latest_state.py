"""Latest-state people diagnostic for PY2026 snapshots.

Phase 4D snapshot-population counts are unchanged. This module answers a
different question: the latest qualifying row per person inside a snapshot.

NOV30 and DEC31 are not dated by the MMDDYYYY parser. Their calendar dates are
accepted only when aggregate timestamps support them.
``Enrollments_PY2526_06312026`` is labeled ``INVALID_FILENAME_DATE``.

``main.py`` does not import this module. No JAQL is sent.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, guard_no_pii, interpret_payload
from comparison.diagnostic_numbers import (
    COLUMNS_SQL,
    TABLES_SQL,
    choose_person_column,
    diagnostic_filters,
)
from comparison.lifecycle import (
    OUTPUT_DIR,
    QUERY_TIMEOUT_SECONDS,
    SNAPSHOT_PREFIX,
    infer_filename_date,
    read_observation,
    resolve_history_path,
    shift_one_calendar_year,
)
from comparison.monthly_population import (
    CATEGORY_FIELDS,
    FILENAME_INFERRED,
    categorize_status,
    collapse_categories,
    py2027_reference,
)
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_latest_state_diagnostic.json"
HTML_NAME = "py2026_latest_state_diagnostic.html"

LATEST_STATE = "LATEST_STATE_DIAGNOSTIC"
SEMANTIC = "SEMANTICALLY_SUPPORTED_SNAPSHOT_DATE"
UNRESOLVED = "UNRESOLVED_SNAPSHOT_DATE"
INVALID_FILENAME = "INVALID_FILENAME_DATE"
NOT_RECONSTRUCTABLE = "LATEST_STATE_NOT_RECONSTRUCTABLE"
MONTH_DAY_TOKEN = "MONTH_DAY_TOKEN"

PRIORITY_TABLES = (
    "Enrollments_PY2526_NOV30",
    "Enrollments_PY2526_DEC31",
    "Enrollments_PY2526_01312026",
)
MONTH_DAY_TOKENS = {
    "NOV30": (11, 30),
    "DEC31": (12, 31),
}
PRIMARY_ORDER_FIELDS = (
    "enrollment_last_update_date",
    "enrollment_update_date",
    "last_update_timestamp",
)
SEMANTIC_DATE_FIELDS = (
    "enrollment_last_update_date",
    "enrollment_update_date",
    "last_update_timestamp",
    "enrollment_create_date",
    "enrollment_confirmation_date",
)
EXTREMA_FIELDS = SEMANTIC_DATE_FIELDS + (
    "benefit_effective_date",
    "benefit_end_date",
)


def classify_snapshot_name(table_name: str) -> dict:
    """Classify a PY2526 table name without treating PY2526 as a calendar year."""
    if not table_name.lower().startswith(SNAPSHOT_PREFIX.lower()):
        return {"table": table_name, "filename_status": UNRESOLVED, "inferred_date": None, "month_day": None}
    suffix = table_name[len(SNAPSHOT_PREFIX):]
    token = suffix.upper()
    if token in MONTH_DAY_TOKENS:
        month, day = MONTH_DAY_TOKENS[token]
        return {
            "table": table_name,
            "filename_status": MONTH_DAY_TOKEN,
            "token": token,
            "inferred_date": None,
            "month_day": {"month": month, "day": day},
        }
    inferred = infer_filename_date(table_name)
    if inferred:
        return {
            "table": table_name,
            "filename_status": FILENAME_INFERRED,
            "inferred_date": inferred,
            "month_day": None,
        }
    if len(suffix) == 8 and suffix.isdigit():
        return {
            "table": table_name,
            "filename_status": INVALID_FILENAME,
            "invalid_suffix": suffix,
            "inferred_date": None,
            "month_day": None,
            "reason": f"{suffix} is not a valid calendar date.",
        }
    return {"table": table_name, "filename_status": UNRESOLVED, "inferred_date": None, "month_day": None}


def choose_ordering_fields(columns: dict) -> dict:
    """Latest-state order: recency timestamps first. Confirmation and benefit dates are not used."""
    primary = next((name for name in PRIMARY_ORDER_FIELDS if name in columns), None)
    if primary is None:
        extras = [
            name for name in columns
            if "last_update" in name and name not in PRIMARY_ORDER_FIELDS
        ]
        primary = extras[0] if len(extras) == 1 else None
    tiebreakers = [name for name in ("enrollment_create_date", "enrollment_id") if name in columns]
    reconstructable = primary is not None
    precedence = [primary] if primary else []
    precedence.extend(tiebreakers)
    return {
        "primary": primary,
        "tiebreakers": tiebreakers,
        "precedence": precedence,
        "reconstructable": reconstructable,
        "reason": (
            "ROW_NUMBER per person ordered by "
            + ", ".join(f"{name} DESC" for name in precedence)
            + ". Nulls sort last. enrollment_id is a tiebreaker only."
            if reconstructable else
            "No last-update timestamp is present, so latest state is not reconstructed. "
            "Confirmation and benefit dates are not used as recency."
        ),
    }


def interpret_semantic_date(classification: dict, evidence: dict) -> dict:
    """Accept a month-day token date only when a timestamp max matches that month and day."""
    month_day = classification.get("month_day")
    people = (evidence or {}).get("people_coverage_year_2026")
    if not month_day:
        return {"status": UNRESOLVED, "snapshot_date": None, "matching_fields": [], "reason": "No month-day token."}
    if not people:
        return {
            "status": UNRESOLVED,
            "snapshot_date": None,
            "matching_fields": [],
            "reason": "coverage_year 2026 people were not found, so the year is not taken from the table name.",
        }
    matching = []
    for field in SEMANTIC_DATE_FIELDS:
        maximum = _as_date((evidence.get("extrema") or {}).get(f"max_{field}"))
        if maximum and maximum.month == month_day["month"] and maximum.day == month_day["day"]:
            matching.append({"field": field, "max_date": maximum.isoformat()})
    dates = sorted({item["max_date"] for item in matching})
    if len(dates) == 1:
        return {
            "status": SEMANTIC,
            "snapshot_date": dates[0],
            "matching_fields": matching,
            "reason": (
                f"MAX({matching[0]['field']}) is {dates[0]}, which matches the "
                f"{classification.get('token')} month-day. The year comes from that timestamp, not from PY2526."
            ),
        }
    if len(dates) > 1:
        return {
            "status": UNRESOLVED,
            "snapshot_date": None,
            "matching_fields": matching,
            "reason": "More than one timestamp max matches the month-day with different years.",
        }
    return {
        "status": UNRESOLVED,
        "snapshot_date": None,
        "matching_fields": [],
        "reason": "No last-update, create, or confirmation max matches the token month-day.",
    }


def lifecycle_distance(snapshot_date: str | None, lifecycle: date | None) -> dict | None:
    if snapshot_date is None or lifecycle is None:
        return None
    day = date.fromisoformat(snapshot_date)
    delta = (day - lifecycle).days
    return {
        "lifecycle_target": lifecycle.isoformat(),
        "snapshot_date": snapshot_date,
        "distance_days": delta,
        "distance_days_absolute": abs(delta),
        "position": "after" if delta > 0 else ("before" if delta < 0 else "same_day"),
    }


def build_evidence_sql(qualified: str, columns: dict, person_column: str | None) -> tuple[str, tuple]:
    """Coverage-year counts and date extrema. No enrollment rows are returned."""
    year = quote_identifier(columns["coverage_year"]["name"]) if "coverage_year" in columns else None
    if year is None:
        raise QueryExecutionError("coverage_year is required for snapshot evidence")
    pieces = [
        "COUNT_BIG(*) AS raw_rows",
        f"SUM(CASE WHEN {year} = ? THEN 1 ELSE 0 END) AS rows_2025",
        f"SUM(CASE WHEN {year} = ? THEN 1 ELSE 0 END) AS rows_2026",
    ]
    params: list = [2025, 2026]
    if person_column:
        person = quote_identifier(person_column)
        pieces += [
            f"COUNT_BIG(DISTINCT CASE WHEN {year} = ? THEN {person} END) AS people_2025",
            f"COUNT_BIG(DISTINCT CASE WHEN {year} = ? THEN {person} END) AS people_2026",
        ]
        params += [2025, 2026]
    for field in EXTREMA_FIELDS:
        if field not in columns:
            continue
        quoted = quote_identifier(columns[field]["name"])
        pieces += [f"MIN({quoted}) AS min_{field}", f"MAX({quoted}) AS max_{field}"]
    sql = (
        "SELECT\n    " + ",\n    ".join(pieces) + "\n"
        f"FROM {qualified}\n"
        f"WHERE {year} IN (?, ?)"
    )
    params += [2025, 2026]
    return sql, tuple(params)


def build_latest_state_sql(qualified: str, person_column: str, status_column: str | None,
                           filters: list[dict], ordering: dict, columns: dict) -> tuple[str, tuple]:
    """One latest row per person. Python receives category aggregates only."""
    where_sql, params = _where(filters)
    person = quote_identifier(person_column)
    primary = quote_identifier(columns[ordering["primary"]]["name"])
    select_cols = [f"{person} AS person_key", f"{primary} AS order_ts"]
    order_bits = [
        "CASE WHEN order_ts IS NULL THEN 1 ELSE 0 END",
        "order_ts DESC",
    ]
    if "enrollment_create_date" in ordering["tiebreakers"]:
        create = quote_identifier(columns["enrollment_create_date"]["name"])
        select_cols.append(f"{create} AS create_ts")
        order_bits += ["CASE WHEN create_ts IS NULL THEN 1 ELSE 0 END", "create_ts DESC"]
    if "enrollment_id" in ordering["tiebreakers"]:
        ident = quote_identifier(columns["enrollment_id"]["name"])
        select_cols.append(f"{ident} AS tie_id")
        order_bits += ["CASE WHEN tie_id IS NULL THEN 1 ELSE 0 END", "tie_id DESC"]
    if status_column:
        status = quote_identifier(columns[status_column]["name"])
        select_cols.append(f"{status} AS status_value")
        status_expr = "CAST(status_value AS varchar(256))"
    else:
        status_expr = "CAST(NULL AS varchar(256))"
    where = f"\n    WHERE {where_sql}" if where_sql else ""
    sql = (
        "WITH base AS (\n"
        f"    SELECT {', '.join(select_cols)}\n"
        f"    FROM {qualified}{where}\n"
        "), ranked AS (\n"
        "    SELECT person_key, "
        + ("status_value, " if status_column else "")
        + "ROW_NUMBER() OVER (\n"
        "        PARTITION BY person_key\n"
        f"        ORDER BY {', '.join(order_bits)}\n"
        "    ) AS rn\n"
        "    FROM base\n"
        ")\n"
        "SELECT 'status' AS kind, "
        f"{status_expr} AS status_value, COUNT_BIG(*) AS people_count\n"
        "FROM ranked\n"
        "WHERE rn = 1\n"
        + ("GROUP BY status_value\n" if status_column else "")
        + "UNION ALL\n"
        "SELECT 'total', CAST(NULL AS varchar(256)), COUNT_BIG(*)\n"
        "FROM ranked\n"
        "WHERE rn = 1"
    )
    return sql, tuple(params)


def parse_latest_rows(rows: list[tuple]) -> dict:
    total = None
    status_rows = []
    for kind, status_value, people in rows:
        count = _as_int(people)
        label = str(kind).lower() if kind is not None else ""
        if label == "total":
            total = count
        else:
            status_rows.append({
                "status_value": None if status_value is None else str(status_value),
                "people_count": count,
                "category": categorize_status(status_value),
            })
    categories = collapse_categories(status_rows)
    category_sum = sum(categories[field] or 0 for field in CATEGORY_FIELDS.values())
    return {
        "latest_state_total_people": total,
        "enrolled_people": categories["enrolled_people"],
        "pending_people": categories["pending_people"],
        "cancelled_people": categories["cancelled_people"],
        "terminated_people": categories["terminated_people"],
        "other_people": categories["other_people"],
        "unknown_status_values": categories["unknown_status_values"],
        "status_values": status_rows,
        "category_sum": category_sum,
        "reconciles": total is not None and category_sum == total,
        "discrepancy": None if total is None else category_sum - total,
    }


def collect_evidence(client, payload: dict, observation: dict | None) -> dict:
    failures = []
    definition = interpret_payload(payload)
    lifecycle = None
    if observation and observation.get("observation_date"):
        lifecycle = shift_one_calendar_year(date.fromisoformat(observation["observation_date"]))
    tables = _list_snapshots(client, failures)
    classified = [classify_snapshot_name(item["table"]) | item for item in tables]
    invalid = [item for item in classified if item["filename_status"] == INVALID_FILENAME]
    targets = _targets(classified)
    column_cache = {}
    date_evidence = []
    latest_rows = []
    for item in targets:
        columns = _columns(client, failures, item, column_cache)
        if columns is None:
            continue
        person = choose_person_column(columns)
        evidence = None
        semantic = None
        if item["filename_status"] == MONTH_DAY_TOKEN:
            evidence = _date_evidence(client, failures, item, columns, person["column"])
            semantic = interpret_semantic_date(item, evidence or {})
            date_evidence.append({
                "table": item["table"],
                "schema_name": item["schema_name"],
                "token": item.get("token"),
                "filename_status": item["filename_status"],
                "semantic_status": semantic["status"],
                "snapshot_date": semantic["snapshot_date"],
                "matching_fields": semantic["matching_fields"],
                "reason": semantic["reason"],
                "coverage_year_distribution": None if evidence is None else {
                    "rows_2025": evidence.get("rows_2025"),
                    "rows_2026": evidence.get("rows_2026"),
                    "people_2025": evidence.get("people_coverage_year_2025"),
                    "people_2026": evidence.get("people_coverage_year_2026"),
                },
                "extrema": None if evidence is None else evidence.get("extrema"),
                "person_identifier": person["label"],
                "status_columns": [
                    name for name in ("enrollment_status_description", "enrollee_status_description")
                    if name in columns
                ],
            })
        snapshot_date = (
            (semantic or {}).get("snapshot_date")
            if item["filename_status"] == MONTH_DAY_TOKEN
            else item.get("inferred_date")
        )
        date_status = (
            (semantic or {}).get("status", UNRESOLVED)
            if item["filename_status"] == MONTH_DAY_TOKEN
            else FILENAME_INFERRED
        )
        latest_rows.append(_latest_for_snapshot(
            client, failures, definition, item, columns, person, snapshot_date, date_status, lifecycle,
        ))
    return {
        "date_evidence": date_evidence,
        "invalid_filename_dates": invalid,
        "latest_state": latest_rows,
        "failures": failures,
        "reference": py2027_reference(observation, definition),
        "lifecycle_target": None if lifecycle is None else lifecycle.isoformat(),
    }


def build_report(payload: dict, observation: dict | None, *, date_evidence=None, latest_state=None,
                 invalid_filename_dates=None, failures=None) -> dict:
    definition = interpret_payload(payload)
    report = {
        "report_kind": LATEST_STATE,
        "concepts_preserved": [
            "SNAPSHOT POPULATION",
            "MONTHLY ENROLLMENT ACTIVITY",
            "LATEST STATE POPULATION",
        ],
        "lifecycle_equivalent": False,
        "py2026_equivalent_lifecycle_date": (
            shift_one_calendar_year(date.fromisoformat(observation["observation_date"])).isoformat()
            if observation and observation.get("observation_date") else None
        ),
        "date_evidence": date_evidence or [],
        "invalid_filename_dates": invalid_filename_dates or [],
        "latest_state_population": latest_state or [],
        "dataset_py2027_current_reference": py2027_reference(observation, definition),
        "failures": failures or [],
        "assumptions": [
            "This is LATEST_STATE_POPULATION. It does not replace Phase 4D snapshot-population counts.",
            "NOV30/DEC31 dates are SEMANTICALLY_SUPPORTED only when a timestamp max matches that month-day.",
            "The year of a semantic date comes from the matching timestamp, not from PY2526.",
            "06312026 is INVALID_FILENAME_DATE and is not a June snapshot.",
            "Latest state uses one row per person. Category counts should reconcile to total people.",
            "PY2027 Cancelled and Terminated are not produced here.",
        ],
    }
    guard_no_pii(report)
    return report


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def render_html(report: dict) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td>"
        f"<td>{_e(item.get('latest_state_total_people'))}</td>"
        f"<td>{_e(item.get('enrolled_people'))}</td>"
        f"<td>{_e(item.get('pending_people'))}</td>"
        f"<td>{_e(item.get('cancelled_people'))}</td>"
        f"<td>{_e(item.get('terminated_people'))}</td>"
        f"<td>{_e(item.get('other_people'))}</td>"
        f"<td>{_e((item.get('lifecycle_proximity') or {}).get('distance_days_absolute'))}</td>"
        "</tr>"
        for item in report.get("latest_state_population") or []
    )
    reference = report.get("dataset_py2027_current_reference") or {}
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 latest-state diagnostic</title>"
        "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left}</style></head><body>"
        "<h1>Latest-state population — diagnostic, not a chart</h1>"
        "<table><thead><tr><th>Snapshot</th><th>Total</th><th>Latest Enrolled</th>"
        "<th>Pending</th><th>Cancelled</th><th>Terminated</th><th>Other</th><th>Distance</th>"
        "</tr></thead><tbody>"
        f"{rows}</tbody></table>"
        "<h2>PY2027 current reference</h2>"
        f"<p>Observation: {_e(reference.get('observation_date'))}</p>"
        f"<p>Currently Enrolled People: {_e(reference.get('currently_enrolled_people'))}</p>"
        "</body></html>"
    )


def print_table(report: dict) -> None:
    print("LATEST STATE POPULATION — diagnostic; not snapshot population; not YoY")
    print(
        f"{'Snapshot':<28}{'Total':>10}{'Enrolled':>10}{'Pending':>10}"
        f"{'Cancelled':>10}{'Terminated':>12}{'Other':>8}{'Distance':>10}"
    )
    for item in report.get("latest_state_population") or []:
        prox = item.get("lifecycle_proximity") or {}
        print(
            f"{item.get('table', ''):<28}"
            f"{_num(item.get('latest_state_total_people')):>10}"
            f"{_num(item.get('enrolled_people')):>10}"
            f"{_num(item.get('pending_people')):>10}"
            f"{_num(item.get('cancelled_people')):>10}"
            f"{_num(item.get('terminated_people')):>12}"
            f"{_num(item.get('other_people')):>8}"
            f"{_num(prox.get('distance_days_absolute')):>10}"
        )
    print("")
    reference = report.get("dataset_py2027_current_reference") or {}
    print("PY2027 CURRENT REFERENCE")
    print(f"Observation date: {reference.get('observation_date')}")
    print(f"Currently Enrolled People: {reference.get('currently_enrolled_people')}")


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    observation = read_observation(resolve_history_path())
    evidence = {
        "date_evidence": [], "invalid_filename_dates": [], "latest_state": [], "failures": [],
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect_evidence(DatabaseClient(connection), payload, observation)
    except DatabaseError as exc:
        evidence["failures"] = [{"step": "database", "error": str(exc)}]
    report = build_report(
        payload, observation,
        date_evidence=evidence["date_evidence"],
        latest_state=evidence["latest_state"],
        invalid_filename_dates=evidence["invalid_filename_dates"],
        failures=evidence["failures"],
    )
    json_path, html_path = write_report(report)
    print_table(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0 if report["latest_state_population"] else 2


def _targets(classified: list[dict]) -> list[dict]:
    chosen = []
    seen = set()
    for name in PRIORITY_TABLES:
        for item in classified:
            if item["table"] == name and item["filename_status"] != INVALID_FILENAME:
                key = (item["schema_name"], item["table"])
                if key not in seen:
                    chosen.append(item)
                    seen.add(key)
    for item in classified:
        key = (item["schema_name"], item["table"])
        if key in seen or _inbound(item["table"]):
            continue
        if item["filename_status"] == FILENAME_INFERRED:
            chosen.append(item)
            seen.add(key)
        elif item["filename_status"] == MONTH_DAY_TOKEN and item["table"] not in PRIORITY_TABLES:
            chosen.append(item)
            seen.add(key)
    return chosen


def _list_snapshots(client, failures) -> list[dict]:
    result = _run(client, failures, "snapshot tables", TABLES_SQL, None)
    if result is None:
        return []
    rows = []
    for schema_name, table_name, table_type in result.rows:
        name = str(table_name)
        if not name.lower().startswith(SNAPSHOT_PREFIX.lower()) or _inbound(name):
            continue
        rows.append({
            "schema_name": str(schema_name),
            "table": name,
            "table_type": str(table_type or ""),
        })
    return rows


def _date_evidence(client, failures, item, columns, person_column) -> dict | None:
    if "coverage_year" not in columns:
        return None
    try:
        sql, params = build_evidence_sql(
            qualified_name(item["schema_name"], item["table"]), columns, person_column)
    except QueryExecutionError as exc:
        failures.append({"step": f"evidence {item['table']}", "error": str(exc)})
        return None
    result = _run(client, failures, f"evidence {item['table']}", sql, params)
    if result is None or result.row_count != 1:
        return None
    record = {column.lower(): value for column, value in zip(result.columns, result.rows[0])}
    extrema = {}
    for field in EXTREMA_FIELDS:
        for prefix in ("min_", "max_"):
            key = prefix + field
            if key in record:
                extrema[key] = _as_date(record[key]).isoformat() if _as_date(record[key]) else None
    return {
        "rows_2025": _as_int(record.get("rows_2025")),
        "rows_2026": _as_int(record.get("rows_2026")),
        "people_coverage_year_2025": _as_int(record.get("people_2025")),
        "people_coverage_year_2026": _as_int(record.get("people_2026")),
        "extrema": extrema,
    }


def _latest_for_snapshot(client, failures, definition, item, columns, person,
                         snapshot_date, date_status, lifecycle) -> dict:
    filters = diagnostic_filters(definition, columns)
    ordering = choose_ordering_fields(columns)
    status_column = (
        "enrollment_status_description" if "enrollment_status_description" in columns
        else ("enrollee_status_description" if "enrollee_status_description" in columns else None)
    )
    row = {
        "table": item["table"],
        "schema_name": item["schema_name"],
        "date_status": date_status,
        "snapshot_date": snapshot_date,
        "lifecycle_proximity": lifecycle_distance(snapshot_date, lifecycle),
        "person_identifier": person["label"],
        "ordering": {
            "primary": ordering["primary"],
            "tiebreakers": ordering["tiebreakers"],
            "precedence": ordering["precedence"],
            "reason": ordering["reason"],
        },
        "filters_applied": [entry["sisense_concept"] for entry in filters if entry.get("sql")],
        "filters_unavailable": [
            {"sisense_concept": entry["sisense_concept"], "reason": entry.get("reason")}
            for entry in filters if not entry.get("sql")
        ],
        "latest_state_status": None,
        "latest_state_total_people": None,
        "enrolled_people": None,
        "pending_people": None,
        "cancelled_people": None,
        "terminated_people": None,
        "other_people": None,
        "reconciles": None,
        "discrepancy": None,
        "unknown_status_values": [],
    }
    if person["column"] is None:
        row["latest_state_status"] = NOT_RECONSTRUCTABLE
        row["reason"] = person["reason"]
        return row
    if not ordering["reconstructable"]:
        row["latest_state_status"] = NOT_RECONSTRUCTABLE
        row["reason"] = ordering["reason"]
        return row
    try:
        sql, params = build_latest_state_sql(
            qualified_name(item["schema_name"], item["table"]),
            person["column"], status_column, filters, ordering, columns,
        )
    except QueryExecutionError as exc:
        failures.append({"step": f"latest {item['table']}", "error": str(exc)})
        row["latest_state_status"] = NOT_RECONSTRUCTABLE
        row["reason"] = str(exc)
        return row
    result = _run(client, failures, f"latest {item['table']}", sql, params)
    if result is None:
        row["latest_state_status"] = NOT_RECONSTRUCTABLE
        row["reason"] = "Latest-state query did not return a result."
        return row
    parsed = parse_latest_rows(result.rows)
    row.update({
        "latest_state_status": LATEST_STATE,
        "latest_state_total_people": parsed["latest_state_total_people"],
        "enrolled_people": parsed["enrolled_people"],
        "pending_people": parsed["pending_people"],
        "cancelled_people": parsed["cancelled_people"],
        "terminated_people": parsed["terminated_people"],
        "other_people": parsed["other_people"],
        "unknown_status_values": parsed["unknown_status_values"],
        "reconciles": parsed["reconciles"],
        "discrepancy": parsed["discrepancy"],
        "status_column": status_column,
        "reason": "One latest qualifying row per person.",
    })
    return row


def _columns(client, failures, item, cache) -> dict | None:
    key = (item["schema_name"], item["table"])
    if key in cache:
        return cache[key]
    result = _run(client, failures, f"columns {item['table']}", COLUMNS_SQL, key)
    if result is None:
        cache[key] = None
        return None
    columns = {
        str(name).lower(): {"name": str(name), "data_type": str(dtype or "")}
        for name, dtype in result.rows if name
    }
    cache[key] = columns
    return columns


def _run(client, failures: list, step: str, sql: str, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


def _where(filters: list[dict]) -> tuple[str, list]:
    clauses, params = [], []
    for item in filters:
        if item.get("sql"):
            clauses.append(item["sql"])
            params.extend(item["params"])
    return " AND ".join(clauses), params


def _inbound(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in ("834", "inbound", "rcni", "sftp"))


def _as_int(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value)


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    return date.fromisoformat(text[:10]) if text else None


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _num(value) -> str:
    return "n/a" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
