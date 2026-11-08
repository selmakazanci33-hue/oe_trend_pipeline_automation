"""PY2026 historical snapshot population and confirmation activity.

Filename dates are ``FILENAME_INFERRED_SNAPSHOT_DATE``. They are diagnostic
points, not proven as-of dates. This module does not send JAQL and does not
invent PY2027 Cancelled or Terminated values.

``main.py`` does not import this module.
"""

from __future__ import annotations

import html
import json
from calendar import month_abbr
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, guard_no_pii, interpret_payload
from comparison.diagnostic_numbers import (
    COLUMNS_SQL,
    ENROLLEE_PROXY,
    FILENAME_INFERRED,
    NOT_APPLIED,
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
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_monthly_population.json"
HTML_NAME = "py2026_monthly_population.html"

PY2027_NOT_VERIFIED = "NOT_VERIFIED"
ACTIVITY_LABEL = "MONTHLY_ENROLLMENT_ACTIVITY"
CONFIRMATION_FIELD = "enrollment_confirmation_date"

CATEGORY_MAP = {
    "enrolled": "ENROLLED",
    "pending": "PENDING",
    "terminated": "TERMINATED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
}
CATEGORY_FIELDS = {
    "ENROLLED": "enrolled_people",
    "PENDING": "pending_people",
    "CANCELLED": "cancelled_people",
    "TERMINATED": "terminated_people",
    "OTHER": "other_people",
}


def categorize_status(label) -> str:
    """Map only explicit labels. Unknown values stay OTHER."""
    if label is None:
        return "OTHER"
    return CATEGORY_MAP.get(str(label).strip().lower(), "OTHER")


def dated_snapshots(tables: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split catalog rows into dated series, undated objects, and inbound exclusions."""
    dated, undated, excluded = [], [], []
    seen_dates: dict[str, int] = {}
    for item in tables:
        name = item["table"]
        if _inbound(name):
            excluded.append({**item, "reason": "834/inbound name; rows were not read"})
            continue
        inferred = item.get("inferred_filename_date") or infer_filename_date(name)
        row = {**item, "inferred_filename_date": inferred, "snapshot_date_evidence": FILENAME_INFERRED}
        if inferred is None:
            undated.append(row)
            continue
        seen_dates[inferred] = seen_dates.get(inferred, 0) + 1
        dated.append(row)
    for row in dated:
        row["duplicate_inferred_date"] = seen_dates[row["inferred_filename_date"]] > 1
    dated.sort(key=lambda item: (item["inferred_filename_date"], item["schema_name"], item["table"]))
    return dated, undated, excluded


def build_snapshot_sql(qualified: str, person_column: str, filters: list[dict], columns: dict) -> tuple[str, tuple]:
    """One aggregate round-trip: total people, status breakdowns, multi-status overlap."""
    where_sql, params = _where(filters)
    person = quote_identifier(person_column)
    enrollment = columns.get("enrollment_status_description")
    enrollee = columns.get("enrollee_status_description")
    enrollment_expr = quote_identifier(enrollment["name"]) if enrollment else "CAST(NULL AS varchar(256))"
    enrollee_expr = quote_identifier(enrollee["name"]) if enrollee else "CAST(NULL AS varchar(256))"
    parts = [
        "WITH base AS (",
        f"    SELECT {person} AS person_key, {enrollment_expr} AS enrollment_status,",
        f"           {enrollee_expr} AS enrollee_status",
        f"    FROM {qualified}",
        (f"    WHERE {where_sql}" if where_sql else ""),
        ")",
        "SELECT 'total' AS kind, CAST(NULL AS varchar(256)) AS status_value,",
        "       COUNT_BIG(DISTINCT person_key) AS people_count",
        "FROM base",
    ]
    if enrollment:
        parts += [
            "UNION ALL",
            "SELECT 'enrollment_status', CAST(enrollment_status AS varchar(256)),",
            "       COUNT_BIG(DISTINCT person_key)",
            "FROM base",
            "GROUP BY enrollment_status",
            "UNION ALL",
            "SELECT 'enrollment_multi', CAST(NULL AS varchar(256)), COUNT_BIG(*)",
            "FROM (",
            "    SELECT person_key FROM base GROUP BY person_key",
            "    HAVING COUNT(DISTINCT enrollment_status) > 1",
            ") AS enrollment_multi",
        ]
    if enrollee:
        parts += [
            "UNION ALL",
            "SELECT 'enrollee_status', CAST(enrollee_status AS varchar(256)),",
            "       COUNT_BIG(DISTINCT person_key)",
            "FROM base",
            "GROUP BY enrollee_status",
            "UNION ALL",
            "SELECT 'enrollee_multi', CAST(NULL AS varchar(256)), COUNT_BIG(*)",
            "FROM (",
            "    SELECT person_key FROM base GROUP BY person_key",
            "    HAVING COUNT(DISTINCT enrollee_status) > 1",
            ") AS enrollee_multi",
        ]
    sql = "\n".join(line for line in parts if line != "")
    return sql, tuple(params)


def build_activity_sql(qualified: str, person_column: str, confirm_column: str,
                       filters: list[dict]) -> tuple[str, tuple]:
    """First qualifying confirmation per person, then calendar month counts. No ids in the result."""
    where_sql, params = _where(filters)
    person = quote_identifier(person_column)
    confirm = quote_identifier(confirm_column)
    extra = f"{confirm} IS NOT NULL"
    where = f"{where_sql}\n  AND {extra}" if where_sql else extra
    sql = (
        "WITH first_confirmation AS (\n"
        f"    SELECT {person} AS person_key, MIN({confirm}) AS first_confirmation_date\n"
        f"    FROM {qualified}\n"
        f"    WHERE {where}\n"
        f"    GROUP BY {person}\n"
        ")\n"
        "SELECT\n"
        "    YEAR(first_confirmation_date) AS activity_year,\n"
        "    MONTH(first_confirmation_date) AS activity_month,\n"
        "    COUNT_BIG(*) AS newly_confirmed_people\n"
        "FROM first_confirmation\n"
        "GROUP BY YEAR(first_confirmation_date), MONTH(first_confirmation_date)\n"
        "ORDER BY activity_year, activity_month"
    )
    return sql, tuple(params)


def parse_profile_rows(rows: list[tuple], columns: dict) -> dict:
    """Turn the snapshot aggregate result into totals, raw statuses, and categories."""
    total = None
    enrollment_rows, enrollee_rows = [], []
    enrollment_multi = enrollee_multi = None
    for kind, status_value, people in rows:
        count = _as_int(people)
        label = kind.lower() if isinstance(kind, str) else str(kind)
        if label == "total":
            total = count
        elif label == "enrollment_status":
            enrollment_rows.append(_status_row(status_value, count))
        elif label == "enrollee_status":
            enrollee_rows.append(_status_row(status_value, count))
        elif label == "enrollment_multi":
            enrollment_multi = count
        elif label == "enrollee_multi":
            enrollee_multi = count
    enrollment_cat = collapse_categories(enrollment_rows) if "enrollment_status_description" in columns else None
    enrollee_cat = collapse_categories(enrollee_rows) if "enrollee_status_description" in columns else None
    primary = enrollment_cat or enrollee_cat
    if primary is None:
        primary = {field: None for field in CATEGORY_FIELDS.values()}
        primary["unknown_status_values"] = []
    return {
        "total_people": total,
        "enrollment_status": enrollment_rows,
        "enrollee_status": enrollee_rows,
        "categories": primary,
        "enrollment_categories": enrollment_cat,
        "enrollee_categories": enrollee_cat,
        "status_overlap": {
            "people_with_multiple_enrollment_statuses": enrollment_multi,
            "people_with_multiple_enrollee_statuses": enrollee_multi,
            "status_counts_additive": _additive(total, enrollment_rows, enrollment_multi),
            "note": (
                "Status category counts are not automatically additive. "
                "A person may appear under more than one status in the same snapshot."
            ),
        },
    }


def collapse_categories(status_rows: list[dict]) -> dict:
    """Sum DISTINCT-people counts by explicit category. Unknown labels stay OTHER."""
    out = {field: 0 for field in CATEGORY_FIELDS.values()}
    unknown = []
    for row in status_rows:
        category = categorize_status(row["status_value"])
        field = CATEGORY_FIELDS[category]
        out[field] = (out[field] or 0) + (row["people_count"] or 0)
        if category == "OTHER":
            unknown.append({"status_value": row["status_value"], "people_count": row["people_count"]})
    out["unknown_status_values"] = unknown
    return out


def latest_summary(series: list[dict]) -> dict:
    """Latest unique inferred date with a total. A duplicate latest date is not chosen."""
    successful = [row for row in series if row.get("total_people") is not None]
    if not successful:
        return {"selected": None, "reason": "No snapshot returned a total people count."}
    latest_date = max(row["snapshot_date"] for row in successful)
    winners = [row for row in successful if row["snapshot_date"] == latest_date]
    if len(winners) != 1:
        return {
            "selected": None,
            "latest_date": latest_date,
            "tied_tables": [row["source_table"] for row in winners],
            "reason": "More than one table shares the latest inferred snapshot date.",
        }
    row = winners[0]
    totals = [item["total_people"] for item in successful]
    low = min(successful, key=lambda item: (item["total_people"], item["snapshot_date"]))
    high = max(successful, key=lambda item: (item["total_people"], item["snapshot_date"]))
    return {
        "selected": row["source_table"],
        "snapshot_date": row["snapshot_date"],
        "total_people": row["total_people"],
        "enrolled_people": row["enrolled_people"],
        "pending_people": row["pending_people"],
        "cancelled_people": row["cancelled_people"],
        "terminated_people": row["terminated_people"],
        "other_people": row["other_people"],
        "historical_minimum_total": {"total_people": low["total_people"], "snapshot_date": low["snapshot_date"],
                                     "source_table": low["source_table"]},
        "historical_maximum_total": {"total_people": high["total_people"], "snapshot_date": high["snapshot_date"],
                                     "source_table": high["source_table"]},
        "reason": "Latest unique FILENAME_INFERRED_SNAPSHOT_DATE with a successful total.",
    }


def build_report(payload: dict, observation: dict | None, *, series: list[dict] | None = None,
                 activity: list[dict] | None = None, undated: list[dict] | None = None,
                 failures: list | None = None, activity_source: dict | None = None) -> dict:
    definition = interpret_payload(payload)
    rows = [_public_series_row(item) for item in (series or [])]
    summary = latest_summary(rows)
    reference = py2027_reference(observation, definition)
    lifecycle = None
    if observation and observation.get("observation_date"):
        lifecycle = shift_one_calendar_year(date.fromisoformat(observation["observation_date"])).isoformat()
    report = {
        "report_kind": "PY2026_MONTHLY_POPULATION",
        "snapshot_date_evidence": FILENAME_INFERRED,
        "lifecycle_equivalent": False,
        "py2026_equivalent_lifecycle_date": lifecycle,
        "alignment_note": (
            "PY2026 snapshot dates are filename-inferred historical points. "
            "They are not lifecycle-aligned with the current PY2027 Currently Enrolled People observation. "
            "Do not read snapshot-to-current differences as year-over-year change."
        ),
        "dataset_snapshot_population": rows,
        "dataset_monthly_enrollment_activity": activity or [],
        "activity_source": activity_source,
        "dataset_py2027_current_reference": reference,
        "py2026_latest_snapshot_summary": summary,
        "py2027_cancelled_metric_status": PY2027_NOT_VERIFIED,
        "py2027_terminated_metric_status": PY2027_NOT_VERIFIED,
        "undated_objects": undated or [],
        "failures": failures or [],
        "assumptions": [
            "Filename dates are FILENAME_INFERRED_SNAPSHOT_DATE, not proven as-of dates.",
            "Status categories map only Enrolled, Pending, Cancelled/Canceled, and Terminated. Other labels stay OTHER.",
            "Status counts are DISTINCT people per label and are not automatically additive.",
            "MONTHLY_ENROLLMENT_ACTIVITY uses MIN(enrollment_confirmation_date) per person. It is not snapshot population.",
            "PY2027 Cancelled and Terminated remain NOT_VERIFIED. No JAQL was sent for them.",
            "COUNT(*) and enrollment_id were not used for the people series.",
        ],
    }
    guard_no_pii(report)
    return report


def _public_series_row(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "person_identifier_column"}


def py2027_reference(observation: dict | None, definition: dict) -> dict:
    field = definition["measure"]["fields"][0]
    if observation is None:
        return {
            "plan_year": 2027,
            "observation_date": None,
            "currently_enrolled_people": None,
            "source": None,
            "metric_definition": f"{definition['measure']['aggregation']}({field['table']}.{field['column']})",
            "label": "PY2027_CURRENT_REFERENCE",
            "retrieved_at_utc": None,
        }
    return {
        "plan_year": 2027,
        "observation_date": observation.get("observation_date"),
        "currently_enrolled_people": _as_int(observation.get("metric_value")),
        "source": observation.get("source"),
        "metric_definition": f"{definition['measure']['aggregation']}({field['table']}.{field['column']})",
        "label": "PY2027_CURRENT_REFERENCE",
        "retrieved_at_utc": observation.get("retrieved_at_utc"),
        "metric_id": "currently_enrolled_people",
    }


def collect_evidence(client, payload: dict) -> dict:
    """Catalog dated snapshots, then one combined aggregate per table. Timeouts stay local."""
    failures = []
    definition = interpret_payload(payload)
    tables = _list_snapshots(client, failures)
    dated, undated, excluded = dated_snapshots(tables)
    failures.extend({"step": "undated", "table": item["table"], "error": "No inferred filename date"}
                    for item in undated)
    series = []
    column_cache = {}
    for item in dated:
        profile = _profile_snapshot(client, failures, definition, item, column_cache)
        if profile is not None:
            series.append(profile)
    activity, activity_source = _activity(client, failures, definition, series, column_cache)
    return {
        "series": series,
        "activity": activity,
        "activity_source": activity_source,
        "undated": undated + excluded,
        "failures": failures,
    }


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
        f"<td>{_e(_month_label(item.get('snapshot_date')))}</td>"
        f"<td>{_e(item.get('total_people'))}</td>"
        f"<td>{_e(item.get('enrolled_people'))}</td>"
        f"<td>{_e(item.get('pending_people'))}</td>"
        f"<td>{_e(item.get('cancelled_people'))}</td>"
        f"<td>{_e(item.get('terminated_people'))}</td>"
        "</tr>"
        for item in report.get("dataset_snapshot_population") or []
    )
    reference = report.get("dataset_py2027_current_reference") or {}
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 monthly population</title>"
        "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left}</style></head><body>"
        "<h1>PY2026 snapshot population</h1>"
        "<p>Filename-inferred dates. Not a YoY comparison and not a chart.</p>"
        "<table><thead><tr><th>Snapshot</th><th>Total</th><th>Enrolled</th>"
        "<th>Pending</th><th>Cancelled</th><th>Terminated</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<h2>PY2027 current reference</h2>"
        f"<p>Observation: {_e(reference.get('observation_date'))}</p>"
        f"<p>Currently Enrolled People: {_e(reference.get('currently_enrolled_people'))}</p>"
        f"<p>Cancelled: {_e(report.get('py2027_cancelled_metric_status'))}</p>"
        f"<p>Terminated: {_e(report.get('py2027_terminated_metric_status'))}</p>"
        "</body></html>"
    )


def print_table(report: dict) -> None:
    print("PY2026 SNAPSHOT POPULATION — filename-inferred dates, not proven as-of")
    print(f"{'Snapshot':<12}{'Total':>12}{'Enrolled':>12}{'Pending':>12}{'Cancelled':>12}{'Terminated':>12}")
    for item in report.get("dataset_snapshot_population") or []:
        print(
            f"{_month_label(item.get('snapshot_date')):<12}"
            f"{_num(item.get('total_people')):>12}"
            f"{_num(item.get('enrolled_people')):>12}"
            f"{_num(item.get('pending_people')):>12}"
            f"{_num(item.get('cancelled_people')):>12}"
            f"{_num(item.get('terminated_people')):>12}"
        )
    if not report.get("dataset_snapshot_population"):
        print("(no dated snapshots)")
    print("")
    print("PY2026 MONTHLY ENROLLMENT ACTIVITY — first confirmation month, not snapshot population")
    print(f"{'Month':<12}{'Newly confirmed':>16}")
    for item in report.get("dataset_monthly_enrollment_activity") or []:
        print(f"{item['activity_month']:<12}{_num(item.get('newly_confirmed_people')):>16}")
    if not report.get("dataset_monthly_enrollment_activity"):
        print("(not available)")
    print("")
    reference = report.get("dataset_py2027_current_reference") or {}
    print("PY2027 CURRENT REFERENCE")
    print(f"Observation: {reference.get('observation_date')}")
    print(f"Currently Enrolled People: {reference.get('currently_enrolled_people')}")
    print(f"Cancelled: {report.get('py2027_cancelled_metric_status')}")
    print(f"Terminated: {report.get('py2027_terminated_metric_status')}")
    print(report["alignment_note"])


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    observation = read_observation(resolve_history_path())
    evidence = {
        "series": [], "activity": [], "activity_source": None, "undated": [],
        "failures": [],
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect_evidence(DatabaseClient(connection), payload)
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(
        payload, observation,
        series=evidence["series"],
        activity=evidence["activity"],
        undated=evidence["undated"],
        failures=evidence["failures"],
        activity_source=evidence["activity_source"],
    )
    json_path, html_path = write_report(report)
    print_table(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0 if report["dataset_snapshot_population"] else 2


def _list_snapshots(client, failures) -> list[dict]:
    result = _run(client, failures, "snapshot tables", TABLES_SQL, None)
    if result is None:
        return []
    rows = []
    for schema_name, table_name, table_type in result.rows:
        name = str(table_name)
        if not name.lower().startswith(SNAPSHOT_PREFIX.lower()):
            continue
        rows.append({
            "schema_name": str(schema_name),
            "table": name,
            "table_type": str(table_type or ""),
            "inferred_filename_date": infer_filename_date(name),
        })
    return rows


def _profile_snapshot(client, failures, definition, item, column_cache) -> dict | None:
    columns = _columns(client, failures, item, column_cache)
    if columns is None:
        return None
    person = choose_person_column(columns)
    if person["column"] is None:
        failures.append({"step": item["table"], "error": person["reason"]})
        return _series_row(item, person, diagnostic_filters(definition, columns), None)
    try:
        qualified = qualified_name(item["schema_name"], item["table"])
        filters = diagnostic_filters(definition, columns)
        sql, params = build_snapshot_sql(qualified, person["column"], filters, columns)
    except QueryExecutionError as exc:
        failures.append({"step": item["table"], "error": str(exc)})
        return None
    result = _run(client, failures, f"population {item['table']}", sql, params)
    profile = parse_profile_rows(result.rows, columns) if result is not None else None
    return _series_row(item, person, diagnostic_filters(definition, columns), profile)


def _activity(client, failures, definition, series, column_cache):
    candidates = [row for row in reversed(series) if row.get("total_people") is not None]
    for row in candidates:
        item = {"schema_name": row["schema_name"], "table": row["source_table"]}
        columns = column_cache.get((item["schema_name"], item["table"]))
        if not columns or CONFIRMATION_FIELD not in columns or not row.get("person_identifier_column"):
            continue
        person_column = row["person_identifier_column"]
        try:
            qualified = qualified_name(item["schema_name"], item["table"])
            filters = diagnostic_filters(definition, columns)
            sql, params = build_activity_sql(
                qualified, person_column, columns[CONFIRMATION_FIELD]["name"], filters)
        except QueryExecutionError as exc:
            failures.append({"step": f"activity {item['table']}", "error": str(exc)})
            continue
        result = _run(client, failures, f"activity {item['table']}", sql, params)
        if result is None:
            continue
        activity = []
        for year, month, people in result.rows:
            year_i, month_i = _as_int(year), _as_int(month)
            activity.append({
                "activity_month": f"{year_i:04d}-{month_i:02d}" if year_i and month_i else None,
                "newly_confirmed_people": _as_int(people),
                "person_identifier": row["person_identifier"],
                "date_field": CONFIRMATION_FIELD,
                "definition": ACTIVITY_LABEL,
            })
        return activity, {
            "schema_name": item["schema_name"],
            "table": item["table"],
            "snapshot_date": row["snapshot_date"],
            "definition": (
                "COUNT DISTINCT people whose MIN(enrollment_confirmation_date) falls in the calendar month, "
                "after the same base population filters. This is not snapshot population."
            ),
        }
    return [], {"reason": "No successful snapshot had enrollment_confirmation_date and a person identifier."}


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


def _series_row(item, person, filters, profile) -> dict:
    categories = (profile or {}).get("categories") or {}
    inferred = item["inferred_filename_date"]
    applied = [entry["sisense_concept"] for entry in filters if entry.get("sql")]
    unavailable = [
        {"sisense_concept": entry["sisense_concept"], "status": NOT_APPLIED,
         "reason": entry.get("reason")}
        for entry in filters if not entry.get("sql")
    ]
    return {
        "snapshot_date": inferred,
        "snapshot_date_evidence": FILENAME_INFERRED,
        "source_table": item["table"],
        "schema_name": item["schema_name"],
        "duplicate_inferred_date": item.get("duplicate_inferred_date", False),
        "total_people": None if profile is None else profile.get("total_people"),
        "enrolled_people": categories.get("enrolled_people"),
        "pending_people": categories.get("pending_people"),
        "cancelled_people": categories.get("cancelled_people"),
        "terminated_people": categories.get("terminated_people"),
        "other_people": categories.get("other_people"),
        "unknown_status_values": categories.get("unknown_status_values") or [],
        "enrollment_status_values": None if profile is None else profile.get("enrollment_status"),
        "enrollee_status_values": None if profile is None else profile.get("enrollee_status"),
        "status_overlap": None if profile is None else profile.get("status_overlap"),
        "person_identifier": person["label"],
        "person_identifier_column": person["column"],
        "person_identity_status": person["identity_status"],
        "filters_applied": applied,
        "filters_unavailable": unavailable,
        "filter_notes": applied + [f"{entry['sisense_concept']}: {NOT_APPLIED}" for entry in unavailable],
    }


def _status_row(status_value, count: int | None) -> dict:
    return {
        "status_value": None if status_value is None else str(status_value),
        "people_count": count,
        "category": categorize_status(status_value),
    }


def _additive(total, status_rows, multi) -> bool:
    if total is None or multi is None or multi != 0:
        return False
    return sum(row["people_count"] or 0 for row in status_rows) == total


def _where(filters: list[dict]) -> tuple[str, list]:
    clauses, params = [], []
    for item in filters:
        if item.get("sql"):
            clauses.append(item["sql"])
            params.extend(item["params"])
    return " AND ".join(clauses), params


def _run(client, failures: list, step: str, sql: str, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


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


def _month_label(value) -> str:
    if not value:
        return "n/a"
    day = date.fromisoformat(value)
    return f"{month_abbr[day.month]} {day.year}"


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _num(value) -> str:
    return "n/a" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
