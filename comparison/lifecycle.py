"""Same-lifecycle PY2026 vs PY2027 gate.

The PY2027 observation date comes from a stored validated Currently Enrolled
People snapshot. The previous-plan-year date is that UTC calendar date minus
one calendar year. February 29 moves to February 28. That shift is a lifecycle
rule, not a date from Sisense.

Historical tables are catalogued from metadata. A filename date is recorded as
inferred and is not treated as an as-of date. No population total is labeled
an official year-over-year comparison unless as-of reconstruction, the person
identifier, and every population-critical filter are proven.

``main.py`` does not import this module. The command does not send JAQL.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from comparison.currently_enrolled import (
    VERIFIED_PAYLOAD_FILE,
    ComparisonOutputError,
    guard_no_pii,
    interpret_payload,
    percent_change,
)
from database.discovery.enrollment_source import quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY_DB = PROJECT_ROOT / "data" / "history" / "oe_metrics.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "comparison"
JSON_NAME = "py2026_vs_py2027_lifecycle_comparison.json"

METRIC_ID = "currently_enrolled_people"
SISENSE_PLAN_YEAR = 2027
PREVIOUS_PLAN_YEAR = 2026
QUERY_TIMEOUT_SECONDS = 180

RECONSTRUCTABLE = "HISTORICAL_ASOF_RECONSTRUCTABLE"
SNAPSHOT_NEAR = "SNAPSHOT_AVAILABLE_NEAR_CUTOFF"
NOT_PROVEN = "HISTORICAL_ASOF_NOT_PROVEN"
PERSON_UNPROVEN = "PERSON_IDENTIFIER_EQUIVALENCE_UNPROVEN"

EXACT = "EXACT"
SHIFTED = "PLAN_YEAR_SHIFTED_EQUIVALENT"
UNCERTAIN = "UNCERTAIN"
NOT_AVAILABLE = "NOT_AVAILABLE"
ACCEPTABLE = {EXACT, SHIFTED}

BASIS_EXACT = "EXACT_LIFECYCLE"
BASIS_SNAPSHOT = "NEAREST_HISTORICAL_SNAPSHOT"
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

OBSERVATION_SOURCE = "metric_snapshots.retrieved_at_utc"
LIFECYCLE_RULE = (
    "Subtract one calendar year from the UTC date of the stored PY2027 observation. "
    "February 29 shifts to February 28. This date is a lifecycle-alignment rule, not a Sisense date."
)
BENEFIT_WINDOW_RULE = (
    "The verified benefit window is October 1 of the calendar year before the plan year "
    "through December 31 of the plan year. The previous plan year keeps those month and day bounds."
)

CATALOG_COLUMNS = (
    "coverage_year",
    "enrollee_id",
    "exchg_indiv_identifier",
    "exchange_individual_identifier",
    "exchange_indiv_identifier",
    "enrollment_id",
    "enrollment_status_description",
    "enrollee_status_description",
    "person_type_description",
    "insurance_type_description",
    "benefit_effective_date",
    "benefit_end_date",
    "enrollment_confirmation_date",
    "enrollment_create_date",
    "enrollment_last_update_date",
    "hios_issuer_id",
    "valid_from",
    "valid_to",
    "system_start_time",
    "system_end_time",
    "snapshot_date",
    "as_of_date",
)
EXCHANGE_COLUMNS = (
    "exchg_indiv_identifier",
    "exchange_individual_identifier",
    "exchange_indiv_identifier",
)
VERSION_PAIRS = (("valid_from", "valid_to"), ("system_start_time", "system_end_time"))
PROVEN_DATE_COLUMNS = ("snapshot_date", "as_of_date")
SNAPSHOT_PREFIX = "Enrollments_PY2526_"
INBOUND_MARKERS = ("834", "inbound", "rcni", "sftp")
MAX_PRESENCE_PROBES = 30

CATALOG_SQL = """
SELECT
    c.TABLE_SCHEMA AS table_schema,
    c.TABLE_NAME AS table_name,
    t.TABLE_TYPE AS table_type,
    c.COLUMN_NAME AS column_name,
    c.DATA_TYPE AS data_type
FROM INFORMATION_SCHEMA.COLUMNS AS c
INNER JOIN INFORMATION_SCHEMA.TABLES AS t
    ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
   AND t.TABLE_NAME = c.TABLE_NAME
WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
  AND c.TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'sys')
  AND c.COLUMN_NAME IN ({columns})
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME
""".format(columns=", ".join(f"'{name}'" for name in CATALOG_COLUMNS))


class LifecycleOutputError(ComparisonOutputError):
    """The lifecycle artifact would contain an individual identifier field."""


def resolve_history_path() -> Path:
    """History file used by the pipeline. Environment override, then the project default."""
    override = os.environ.get("HISTORY_DB_PATH")
    if override:
        return Path(override)
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "HISTORY_DB_PATH" and value.strip():
                return Path(value.strip().strip('"').strip("'"))
    return DEFAULT_HISTORY_DB


def read_observation(path: Path) -> dict | None:
    """Latest validated Currently Enrolled People observation. No wall-clock fallback."""
    if not path.is_file():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute(
            """
            SELECT s.retrieved_at_utc, s.metric_value, s.run_id, s.snapshot_slot_utc,
                   r.started_at_utc, r.finished_at_utc
            FROM metric_snapshots AS s
            LEFT JOIN pipeline_runs AS r ON r.run_id = s.run_id
            WHERE s.metric_id = ? AND s.plan_year = ? AND s.retrieval_status = 'VALIDATED'
            ORDER BY s.retrieved_at_utc DESC
            LIMIT 1
            """,
            (METRIC_ID, SISENSE_PLAN_YEAR),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    retrieved = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    return {
        "source": OBSERVATION_SOURCE,
        "retrieved_at_utc": row[0],
        "observation_date": retrieved.date().isoformat(),
        "metric_value": row[1],
        "run_id": row[2],
        "snapshot_slot_utc": row[3],
        "run_started_at_utc": row[4],
        "run_finished_at_utc": row[5],
    }


def shift_one_calendar_year(day: date) -> date:
    """Previous calendar year. February 29 becomes February 28."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        if day.month == 2 and day.day == 29:
            return date(day.year - 1, 2, 28)
        raise


def shift_benefit_window(plan_year: int, start: date, end: date) -> dict:
    """Shift the verified window only when it is October 1 through December 31 of that plan year."""
    expected_start = date(plan_year - 1, 10, 1)
    expected_end = date(plan_year, 12, 31)
    if start == expected_start and end == expected_end:
        return {
            "translation_status": SHIFTED,
            "from": date(plan_year - 2, 10, 1).isoformat(),
            "to": date(plan_year - 1, 12, 31).isoformat(),
            "rule": BENEFIT_WINDOW_RULE,
        }
    return {
        "translation_status": UNCERTAIN,
        "from": None,
        "to": None,
        "rule": (
            "The verified benefit window does not match October 1 before the plan year "
            "through December 31 of the plan year, so no previous-plan-year window was created."
        ),
    }


def infer_filename_date(table_name: str) -> str | None:
    """MMDDYYYY suffix on an Enrollments_PY2526 table. Invalid dates stay empty."""
    if not table_name.lower().startswith(SNAPSHOT_PREFIX.lower()):
        return None
    suffix = table_name[len(SNAPSHOT_PREFIX):]
    if len(suffix) != 8 or not suffix.isdigit():
        return None
    month, day, year = int(suffix[:2]), int(suffix[2:4]), int(suffix[4:])
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_catalog(result) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in result.rows:
        record = {column.lower(): value for column, value in zip(result.columns, row)}
        key = (str(record.get("table_schema") or ""), str(record.get("table_name") or ""))
        source = grouped.get(key)
        if source is None:
            source = {
                "schema_name": key[0],
                "table_name": key[1],
                "table_type": str(record.get("table_type") or ""),
                "columns": {},
            }
            grouped[key] = source
        name = str(record.get("column_name") or "")
        if name:
            source["columns"][name.lower()] = {"name": name, "data_type": str(record.get("data_type") or "")}
    return sorted(grouped.values(), key=lambda item: (item["schema_name"].lower(), item["table_name"].lower()))


def snapshot_rows(sources: list[dict], lifecycle_date: date | None, proven_dates: dict | None = None) -> list[dict]:
    """Describe PY2526 snapshots. Filename dates are inferred, not selected."""
    proven_dates = proven_dates or {}
    rows = []
    for source in sources:
        if not source["table_name"].lower().startswith(SNAPSHOT_PREFIX.lower()):
            continue
        if _inbound(source["table_name"]):
            continue
        inferred = infer_filename_date(source["table_name"])
        proof = proven_dates.get((source["schema_name"], source["table_name"]))
        proven = proof.get("date") if proof and proof.get("distinct_dates") == 1 else None
        supported = proven is not None
        rows.append({
            "schema_name": source["schema_name"],
            "table": source["table_name"],
            "table_type": source["table_type"],
            "inferred_filename_date": inferred,
            "date_independently_supported": supported,
            "proven_snapshot_date": proven,
            "distance_days_from_filename": _distance(lifecycle_date, inferred),
            "distance_days_from_proven_date": _distance(lifecycle_date, proven),
            "columns_present": _column_inventory(source["columns"]),
            "version_columns_present": _has_version_pair(source["columns"]),
            "coverage_year_2026_present": source.get("coverage_year_2026_present"),
        })
    return rows


def classify_asof(snapshots: list[dict]) -> str:
    """A requires version columns on a PY2526 snapshot. B requires a proven date on the lifecycle date."""
    if any(item["version_columns_present"] for item in snapshots):
        return RECONSTRUCTABLE
    if any(item["date_independently_supported"] and item["distance_days_from_proven_date"] == 0 for item in snapshots):
        return SNAPSHOT_NEAR
    return NOT_PROVEN


def nearest_snapshot(snapshots: list[dict]) -> dict | None:
    """Closest dated snapshot. A tie selects nothing. NOV30 has no date and cannot win."""
    ranked = []
    for item in snapshots:
        distance = item["distance_days_from_proven_date"]
        if distance is None:
            distance = item["distance_days_from_filename"]
        if distance is None:
            continue
        ranked.append((distance, item["date_independently_supported"], item))
    if not ranked:
        return None
    best = min(distance for distance, _supported, _item in ranked)
    winners = [item for distance, _supported, item in ranked if distance == best]
    if len(winners) != 1:
        return None
    return winners[0]


def translate_filters(definition: dict, columns: set[str] | None) -> list[dict]:
    scope = definition["scope_filters"]
    coverage = _scope(scope, "coverage_year")
    benefit = _scope(scope, "benefit_effective_date")
    plan_year = _single_year(coverage)
    window = None
    if plan_year is not None and benefit and benefit.get("from") and benefit.get("to"):
        window = shift_benefit_window(plan_year, date.fromisoformat(benefit["from"]), date.fromisoformat(benefit["to"]))
    rows = [
        _plan_year_row(coverage, plan_year, columns),
        _benefit_row(benefit, window, columns),
        _status_row(scope, "enrollment_status_description", "Enrollment status", columns),
        _status_row(scope, "enrollee_status_description", "Enrollee status", columns),
        _member_row(scope, "person_type_description", "Person type", columns),
        _member_row(scope, "insurance_type_description", "Insurance type", columns),
    ]
    return rows


def map_person(definition: dict, columns: set[str] | None) -> dict:
    field = definition["measure"]["fields"][0]
    base = {
        "sisense_table": field["table"],
        "sisense_column": field["column"],
        "aggregation": definition["measure"]["aggregation"],
    }
    if columns is None:
        return {**base, "py2026_db_column": None, "translation_status": UNCERTAIN,
                "identity_status": PERSON_UNPROVEN,
                "reason": "The PY2026 schema was not read."}
    exact = field["column"]
    if exact in columns:
        return {**base, "py2026_db_column": exact, "translation_status": EXACT,
                "identity_status": EXACT,
                "reason": "The snapshot column name matches the Sisense person field."}
    present = [name for name in EXCHANGE_COLUMNS if name in columns and name != exact]
    if "enrollee_id" in columns or present:
        return {**base, "py2026_db_column": "enrollee_id" if "enrollee_id" in columns else present[0],
                "translation_status": UNCERTAIN, "identity_status": PERSON_UNPROVEN,
                "reason": (
                    "The Sisense field is exchg_indiv_identifier. "
                    "A different column is present and is not treated as the same identifier."
                )}
    return {**base, "py2026_db_column": None, "translation_status": NOT_AVAILABLE,
            "identity_status": PERSON_UNPROVEN,
            "reason": "The snapshot schema has no exchange individual identifier column."}


def official_comparison(asof: str, person: dict, mappings: list[dict], observation: dict | None) -> tuple[bool, str, str | None]:
    """Official totals require a proven as-of, the exchange identifier, and acceptable filters."""
    filters_ok = all(row["translation_status"] in ACCEPTABLE for row in mappings)
    person_ok = person.get("translation_status") == EXACT
    if observation is None or not person_ok or not filters_ok:
        return False, CONFIDENCE_LOW, None
    if asof == RECONSTRUCTABLE:
        return True, CONFIDENCE_HIGH, BASIS_EXACT
    if asof == SNAPSHOT_NEAR:
        return True, CONFIDENCE_MEDIUM, BASIS_SNAPSHOT
    return False, CONFIDENCE_LOW, None


def build_report(payload: dict, observation: dict | None, sources: list[dict] | None = None,
                 proven_dates: dict | None = None, diagnostics: dict | None = None,
                 failures: list | None = None, raw_row_count: int | None = None) -> dict:
    definition = interpret_payload(payload)
    observation_date = date.fromisoformat(observation["observation_date"]) if observation else None
    lifecycle = shift_one_calendar_year(observation_date) if observation_date else None
    catalog = sources or []
    snapshots = snapshot_rows(catalog, lifecycle, proven_dates)
    asof = classify_asof(snapshots) if catalog else NOT_PROVEN
    nearest = nearest_snapshot(snapshots)
    columns = None
    if nearest is not None:
        match = _find(catalog, nearest["schema_name"], nearest["table"])
        columns = set(match["columns"]) if match else set()
    person = map_person(definition, columns)
    mappings = translate_filters(definition, columns)
    official, confidence, basis = official_comparison(asof, person, mappings, observation)
    py2027 = observation.get("metric_value") if observation else None
    py2026 = None
    change = percent_change(None, None)
    if official and diagnostics and diagnostics.get("official_people_count") is not None:
        py2026 = diagnostics["official_people_count"]
        change = percent_change(py2026, py2027)
    refused = ["raw_row_count"] if raw_row_count is not None else []
    report = {
        "official_comparison": official and change["percent_status"] == "COMPUTED",
        "comparison_basis": basis,
        "comparison_confidence": confidence,
        "asof_outcome": asof,
        "py2027_observation_date": observation_date.isoformat() if observation_date else None,
        "py2027_observation": observation,
        "py2026_equivalent_lifecycle_date": lifecycle.isoformat() if lifecycle else None,
        "lifecycle_alignment_rule": LIFECYCLE_RULE,
        "py2027_sisense_definition": {
            "dashboard_title": definition["dashboard_title"],
            "widget_id": definition["widget_id"],
            "widget_type": definition["widget_type"],
            "aggregation": definition["measure"]["aggregation"],
            "person_field": f"{person['sisense_table']}.{person['sisense_column']}",
        },
        "py2026_source": None if nearest is None else {
            "schema_name": nearest["schema_name"],
            "table": nearest["table"],
            "selected_for_official_comparison": False,
            "reason": "Closest dated PY2526 snapshot. Filename proximity is not an as-of proof.",
        },
        "snapshots": snapshots,
        "person_identifier": person,
        "filter_mapping": mappings,
        "py2026_currently_enrolled_people": py2026,
        "py2027_currently_enrolled_people": py2027 if official else None,
        "py2027_stored_observation_value": py2027,
        "absolute_difference": change["absolute_difference"] if official else None,
        "percent_change": change["percent_change"] if official else None,
        "percent_status": change["percent_status"] if official else "NOT_COMPUTED",
        "diagnostics": _public_diagnostics(diagnostics),
        "refused_inputs": refused,
        "failures": failures or [],
        "unresolved": _unresolved(observation, asof, person, mappings, nearest),
    }
    if report["py2026_source"] is not None:
        report["py2026_source"]["selected_for_official_comparison"] = report["official_comparison"]
    guard_no_pii(report)
    return report


def collect_database_evidence(client, lifecycle_date: date | None) -> dict:
    """Metadata first. At most one aggregate diagnostic, and only after a single snapshot is nearest."""
    failures = []
    catalog_result = _run(client, failures, "catalog", CATALOG_SQL, None)
    if catalog_result is None:
        return {"sources": [], "proven_dates": {}, "diagnostics": None, "failures": failures}
    sources = parse_catalog(catalog_result)
    proven = {}
    probed = 0
    for source in sources:
        if _inbound(source["table_name"]):
            continue
        if source["table_name"].lower().startswith(SNAPSHOT_PREFIX.lower()) or "coverage_year" in source["columns"]:
            if probed >= MAX_PRESENCE_PROBES:
                source["coverage_year_2026_present"] = None
                continue
            probed += 1
            source["coverage_year_2026_present"] = _presence(client, failures, source)
        if any(name in source["columns"] for name in PROVEN_DATE_COLUMNS):
            proof = _proven_date(client, failures, source)
            if proof is not None:
                proven[(source["schema_name"], source["table_name"])] = proof
    nearest = nearest_snapshot(snapshot_rows(sources, lifecycle_date, proven))
    diagnostics = None
    if nearest is not None and lifecycle_date is not None:
        match = _find(sources, nearest["schema_name"], nearest["table"])
        if match is not None and match.get("coverage_year_2026_present") is not False:
            diagnostics = _diagnostics(client, failures, match)
    return {"sources": sources, "proven_dates": proven, "diagnostics": diagnostics, "failures": failures}


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> Path:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / JSON_NAME
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    observation = read_observation(resolve_history_path())
    lifecycle = None
    if observation is not None:
        lifecycle = shift_one_calendar_year(date.fromisoformat(observation["observation_date"]))
    evidence = {"sources": [], "proven_dates": {}, "diagnostics": None, "failures": []}
    if observation is None:
        evidence["failures"].append({
            "step": "observation",
            "error": "No stored validated Currently Enrolled People observation was found.",
        })
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect_database_evidence(DatabaseClient(connection), lifecycle)
            if observation is None:
                evidence["failures"].insert(0, {
                    "step": "observation",
                    "error": "No stored validated Currently Enrolled People observation was found.",
                })
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(
        payload,
        observation,
        evidence["sources"],
        evidence["proven_dates"],
        evidence["diagnostics"],
        evidence["failures"],
    )
    path = write_report(report)
    print(f"Comparison official: {report['official_comparison']}")
    print(f"PY2027 observation date: {report['py2027_observation_date']}")
    print(f"PY2026 lifecycle date: {report['py2026_equivalent_lifecycle_date']}")
    print(f"As-of outcome: {report['asof_outcome']}")
    print(f"Confidence: {report['comparison_confidence']}")
    print(f"Wrote: {path}")
    return 0 if report["official_comparison"] else 2


def _run(client, failures: list, step: str, sql: str, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


def _presence(client, failures, source) -> bool | None:
    if "coverage_year" not in source["columns"]:
        return None
    sql = (
        "SELECT TOP (1) [coverage_year] AS coverage_year\n"
        f"FROM {_qualified(source)}\n"
        "WHERE [coverage_year] = ?"
    )
    result = _run(client, failures, f"presence {source['table_name']}", sql, (PREVIOUS_PLAN_YEAR,))
    if result is None:
        return None
    return result.row_count > 0


def _proven_date(client, failures, source) -> dict | None:
    column = next(name for name in PROVEN_DATE_COLUMNS if name in source["columns"])
    quoted = quote_identifier(source["columns"][column]["name"])
    sql = (
        f"SELECT MIN({quoted}) AS min_date, MAX({quoted}) AS max_date, "
        f"COUNT_BIG(DISTINCT {quoted}) AS distinct_dates\n"
        f"FROM {_qualified(source)}"
    )
    result = _run(client, failures, f"snapshot date {source['table_name']}", sql, None)
    if result is None or result.row_count != 1:
        return None
    min_date, _max_date, distinct = result.rows[0]
    return {"date": _as_date(min_date), "distinct_dates": int(distinct or 0)}


def _diagnostics(client, failures, source) -> dict:
    columns = source["columns"]
    qualified = _qualified(source)
    empty = {
        "official_people_count": None,
        "distinct_enrollee_id": None,
        "distinct_exchange_identifier": None,
        "one_to_one": None,
        "label": "Person diagnostic was not queried.",
    }
    if "coverage_year" not in columns or "enrollee_id" not in columns:
        return empty
    year = quote_identifier(columns["coverage_year"]["name"])
    person = quote_identifier(columns["enrollee_id"]["name"])
    exchange_name = next((name for name in EXCHANGE_COLUMNS if name in columns), None)
    label = (
        "Aggregate identifier diagnostic for coverage_year 2026 on the nearest dated snapshot. "
        "This is not Currently Enrolled People and it is not an as-of population."
    )
    if exchange_name is None:
        sql = (
            f"SELECT COUNT_BIG(DISTINCT {person}) AS distinct_enrollee_id\n"
            f"FROM {qualified}\n"
            f"WHERE {year} = ?"
        )
        result = _run(client, failures, f"diagnostic {source['table_name']}", sql, (PREVIOUS_PLAN_YEAR,))
        distinct = None if result is None or result.row_count != 1 else int(result.rows[0][0])
        return {**empty, "distinct_enrollee_id": distinct, "label": label}
    exchange = quote_identifier(columns[exchange_name]["name"])
    result = _run(
        client, failures, f"identifier mapping {source['table_name']}",
        _one_to_one_sql(qualified, year, person, exchange), (PREVIOUS_PLAN_YEAR,),
    )
    if result is None or result.row_count != 1:
        return {**empty, "label": label}
    row = result.rows[0]
    return {
        "official_people_count": None,
        "distinct_enrollee_id": int(row[0]),
        "distinct_exchange_identifier": int(row[1]),
        "one_to_one": {
            "person_keys_with_one_exchange_id": int(row[2]),
            "person_keys_with_multiple_exchange_ids": int(row[3]),
            "exchange_keys_with_one_person_id": int(row[4]),
            "exchange_keys_with_multiple_person_ids": int(row[5]),
        },
        "label": label,
    }


def _one_to_one_sql(qualified: str, year: str, person: str, exchange: str) -> str:
    return (
        "WITH pairs AS (\n"
        f"    SELECT {person} AS person_key, {exchange} AS exchange_key\n"
        f"    FROM {qualified}\n"
        f"    WHERE {year} = ?\n"
        "), person_spread AS (\n"
        "    SELECT person_key, COUNT_BIG(DISTINCT exchange_key) AS exchange_ids\n"
        "    FROM pairs\n"
        "    GROUP BY person_key\n"
        "), exchange_spread AS (\n"
        "    SELECT exchange_key, COUNT_BIG(DISTINCT person_key) AS person_ids\n"
        "    FROM pairs\n"
        "    GROUP BY exchange_key\n"
        ")\n"
        "SELECT\n"
        "    (SELECT COUNT_BIG(DISTINCT person_key) FROM pairs) AS distinct_enrollee_id,\n"
        "    (SELECT COUNT_BIG(DISTINCT exchange_key) FROM pairs) AS distinct_exchange_identifier,\n"
        "    (SELECT SUM(CASE WHEN exchange_ids = 1 THEN 1 ELSE 0 END) FROM person_spread),\n"
        "    (SELECT SUM(CASE WHEN exchange_ids > 1 THEN 1 ELSE 0 END) FROM person_spread),\n"
        "    (SELECT SUM(CASE WHEN person_ids = 1 THEN 1 ELSE 0 END) FROM exchange_spread),\n"
        "    (SELECT SUM(CASE WHEN person_ids > 1 THEN 1 ELSE 0 END) FROM exchange_spread)"
    )


def _public_diagnostics(diagnostics: dict | None) -> dict | None:
    if not diagnostics:
        return None
    return {
        "distinct_enrollee_id": diagnostics.get("distinct_enrollee_id"),
        "distinct_exchange_identifier": diagnostics.get("distinct_exchange_identifier"),
        "one_to_one": diagnostics.get("one_to_one"),
        "label": diagnostics.get("label"),
    }


def _unresolved(observation, asof, person, mappings, nearest) -> list[str]:
    notes = []
    if observation is None:
        notes.append("No stored validated Currently Enrolled People observation supplies py2027_observation_date.")
    if asof == NOT_PROVEN:
        notes.append(
            "No version history was found, and no snapshot date column proves the lifecycle date. "
            "A filename date is not that proof."
        )
    if person["identity_status"] != EXACT:
        notes.append(person["reason"])
    for row in mappings:
        if row["translation_status"] not in ACCEPTABLE:
            notes.append(f"{row['sisense_concept']}: {row['translation_status']}. {row['reason']}")
    if nearest is not None and not nearest["date_independently_supported"]:
        notes.append(
            f"{nearest['table']} is the closest filename date and was not selected as the as-of population."
        )
    notes.append("The benefit-effective-date window was not used as the observation date.")
    return notes


def _plan_year_row(coverage, plan_year, columns) -> dict:
    source = "absent" if coverage is None else f"{coverage['table']}.{coverage['column']} members {coverage['members']}"
    if columns is None:
        return _row("Plan year", source, None, UNCERTAIN, "The PY2026 schema was not read.")
    if plan_year is None or "coverage_year" not in columns:
        status = NOT_AVAILABLE if columns is not None and "coverage_year" not in columns else UNCERTAIN
        return _row("Plan year", source, None, status, "A single Sisense plan year and a coverage_year column are both required.")
    return _row(
        "Plan year", source, "coverage_year", SHIFTED,
        f"Sisense plan year {plan_year} shifts to coverage_year {plan_year - 1} on the enrollment snapshot. "
        "The Sisense filter table is ssap_applications.",
    )


def _benefit_row(benefit, window, columns) -> dict:
    source = "absent" if benefit is None else f"from {benefit.get('from')} to {benefit.get('to')}"
    if columns is None or window is None:
        return _row("Benefit effective date", source, None, UNCERTAIN, "The benefit window was not shifted.")
    if "benefit_effective_date" not in columns:
        return _row("Benefit effective date", source, None, NOT_AVAILABLE, "The snapshot has no benefit_effective_date column.")
    return _row("Benefit effective date", source, "benefit_effective_date", window["translation_status"], window["rule"]
                + (f" Candidate window {window['from']} through {window['to']}." if window["from"] else ""))


def _status_row(scope, column, concept, columns) -> dict:
    item = _scope(scope, column)
    source = "absent" if item is None else f"members {item['members']}; nested {item['nested_filter']}"
    if columns is None:
        return _row(concept, source, None, UNCERTAIN, "The PY2026 schema was not read.")
    if column not in columns:
        return _row(concept, source, None, NOT_AVAILABLE, f"The snapshot has no {column} column.")
    return _row(concept, source, column, UNCERTAIN, "The nested turnedOff filter has no evaluated predicate in the verified request.")


def _member_row(scope, column, concept, columns) -> dict:
    item = _scope(scope, column)
    source = "absent" if item is None else f"members {item['members']}"
    if columns is None:
        return _row(concept, source, None, UNCERTAIN, "The PY2026 schema was not read.")
    if column not in columns:
        return _row(concept, source, None, NOT_AVAILABLE, f"The snapshot has no {column} column.")
    return _row(concept, source, column, EXACT, "The snapshot column and the explicit members match the verified filter.")


def _row(concept, source, column, status, reason) -> dict:
    return {
        "sisense_concept": concept,
        "sisense_source": source,
        "py2026_db_column": column,
        "translation_status": status,
        "reason": reason,
    }


def _column_inventory(columns: dict) -> dict:
    wanted = (
        "coverage_year", "enrollee_id", "exchg_indiv_identifier", "enrollment_id",
        "enrollment_status_description", "enrollee_status_description", "person_type_description",
        "insurance_type_description", "benefit_effective_date", "benefit_end_date",
        "enrollment_confirmation_date", "enrollment_create_date", "enrollment_last_update_date",
        "hios_issuer_id",
    )
    return [name for name in wanted if name in columns]


def _has_version_pair(columns: dict) -> bool:
    return any(left in columns and right in columns for left, right in VERSION_PAIRS)


def _distance(lifecycle: date | None, text: str | None) -> int | None:
    if lifecycle is None or text is None:
        return None
    return abs((date.fromisoformat(text) - lifecycle).days)


def _scope(scope, column):
    for item in scope:
        if item.get("column") == column:
            return item
    return None


def _single_year(coverage) -> int | None:
    if coverage is None or len(coverage.get("members") or []) != 1:
        return None
    text = str(coverage["members"][0])
    return int(text) if text.isdigit() else None


def _find(sources, schema_name, table_name):
    for source in sources:
        if source["schema_name"] == schema_name and source["table_name"] == table_name:
            return source
    return None


def _qualified(source) -> str:
    return f"{quote_identifier(source['schema_name'])}.{quote_identifier(source['table_name'])}"


def _inbound(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in INBOUND_MARKERS)


def _as_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10]


if __name__ == "__main__":
    raise SystemExit(main())
