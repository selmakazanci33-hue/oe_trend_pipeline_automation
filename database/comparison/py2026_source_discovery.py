"""Which database object holds the full PY2026 enrollment records?

PY2026 comes from the database; PY2027 comes from current Sisense and is not
searched for here. Read-only catalog metadata first, then aggregate-only
profiles restricted to coverage_year = 2026. No identifier value is returned,
logged or written. 834 / inbound / RCNI / SFTP objects are excluded in the
catalog query itself, so their columns and rows are never read.

Classifications:
  FULL_RECORD_SOURCE     PY2026 present at policy grain and keeps more than one
                         version per policy-member (or is system-versioned)
  LATEST_STATE_SOURCE    PY2026 present at policy grain, one current row per
                         policy-member
  SNAPSHOT_SOURCE        point-in-time copy (dated snapshot name or snapshot column)
  AGGREGATE_SOURCE       enrollment-named object without policy or person identifiers
  INSUFFICIENT_EVIDENCE  anything not proven above

    python -m database.comparison.py2026_source_discovery
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.latest_state import UNRESOLVED, classify_snapshot_name
from comparison.lifecycle import PREVIOUS_PLAN_YEAR, QUERY_TIMEOUT_SECONDS, SNAPSHOT_PREFIX
from database.comparison.field_mapping import is_datetime_sql_type
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_source_discovery.json"
HTML_NAME = "py2026_source_discovery.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.py2026_source_discovery"

FULL_RECORD = "FULL_RECORD_SOURCE"
LATEST_STATE = "LATEST_STATE_SOURCE"
SNAPSHOT = "SNAPSHOT_SOURCE"
AGGREGATE = "AGGREGATE_SOURCE"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

EXACT_NAME = "EXACT_NAME"
NAME_SIMILAR = "NAME_SIMILAR_UNPROVEN"
ABSENT = "ABSENT"

EXCLUDED_NAME_MARKERS = ("834", "inbound", "rcni", "sftp")
FOCUS_OBJECTS = ("Enrollments_PY2026", "Enrollments_TEST")
MAX_PROFILED = 10
STATUS_TOP = 50
HISTORY_REPEAT_SHARE = 0.01
BASELINE_COVERAGE_SHARE = 0.95

# concept -> (exact column names, name-similar column names). Only EXACT columns are queried.
CONCEPTS = {
    "coverage_year": (("coverage_year",), ("plan_year", "policy_year", "benefit_year")),
    "policy_identifier": (("enrollment_id",), ()),
    "person_identifier": (("enrollee_id",), ()),
    "exchange_policy_identifier": ((), ()),
    "exchange_person_identifier": (
        ("exchg_indiv_identifier", "exchange_individual_identifier", "exchange_indiv_identifier"), ()),
    "enrollment_status": (("enrollment_status_description",), ("enrollment_status", "enrollment_status_code")),
    "enrollee_status": (("enrollee_status_description",), ("enrollee_status", "enrollee_status_code")),
    "enrollment_create_date": (("enrollment_create_date",), ()),
    "enrollment_confirmation_date": (("enrollment_confirmation_date",), ()),
    "enrollment_last_update": (("enrollment_last_update_date", "last_update_timestamp"), ("enrollment_update_date",)),
    "benefit_effective_date": (("benefit_effective_date",), ()),
    "benefit_end_date": (("benefit_end_date",), ()),
    "issuer_id": (("hios_issuer_id",), ("issuer_id", "hios_id")),
    "application_type": (("application_type",), ()),
    "version_columns": (("valid_from", "valid_to", "system_start_time", "system_end_time"), ()),
    "snapshot_date": (("snapshot_date", "as_of_date"), ()),
}
DATE_CONCEPTS = (
    "enrollment_create_date", "enrollment_confirmation_date", "enrollment_last_update",
    "benefit_effective_date", "benefit_end_date",
)
STATUS_CONCEPTS = ("enrollment_status", "enrollee_status")
NULL_CONCEPTS = (
    "policy_identifier", "person_identifier", "enrollment_status",
    "enrollment_last_update", "benefit_effective_date", "issuer_id",
)
VERSION_PAIRS = (("valid_from", "valid_to"), ("system_start_time", "system_end_time"))

_EXCLUDE_SQL = "".join(f"\n  AND LOWER(t.TABLE_NAME) NOT LIKE '%{marker}%'" for marker in EXCLUDED_NAME_MARKERS)
CATALOG_SQL = (
    "SELECT t.TABLE_SCHEMA AS table_schema, t.TABLE_NAME AS table_name, t.TABLE_TYPE AS table_type,\n"
    "       c.COLUMN_NAME AS column_name, c.DATA_TYPE AS data_type\n"
    "FROM INFORMATION_SCHEMA.TABLES AS t\n"
    "INNER JOIN INFORMATION_SCHEMA.COLUMNS AS c\n"
    "    ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME\n"
    "WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')\n"
    "  AND t.TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'sys')"
    f"{_EXCLUDE_SQL}\n"
    "  AND (LOWER(t.TABLE_NAME) LIKE '%enroll%'\n"
    "       OR EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS AS k\n"
    "                  WHERE k.TABLE_SCHEMA = t.TABLE_SCHEMA AND k.TABLE_NAME = t.TABLE_NAME\n"
    "                    AND LOWER(k.COLUMN_NAME) IN ('enrollment_id', 'enrollee_id')))\n"
    "ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION"
)
TEMPORAL_SQL = (
    "SELECT s.name AS table_schema, t.name AS table_name, t.temporal_type_desc AS temporal_type\n"
    "FROM sys.tables AS t\n"
    "INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id\n"
    "WHERE t.temporal_type <> 0"
)


def is_excluded_name(name: str) -> bool:
    lowered = str(name).lower()
    return any(marker in lowered for marker in EXCLUDED_NAME_MARKERS)


def parse_catalog(result) -> list[dict]:
    """One object per table/view with every column. Excluded names are dropped defensively."""
    grouped: dict[tuple[str, str], dict] = {}
    for row in result.rows:
        record = {str(column).lower(): value for column, value in zip(result.columns, row)}
        schema_name, table_name = str(record.get("table_schema") or ""), str(record.get("table_name") or "")
        if not table_name or is_excluded_name(table_name):
            continue
        source = grouped.setdefault((schema_name, table_name), {
            "schema_name": schema_name, "table_name": table_name,
            "table_type": str(record.get("table_type") or ""), "columns": {},
        })
        name = str(record.get("column_name") or "")
        if name:
            source["columns"][name.lower()] = {"name": name, "data_type": str(record.get("data_type") or "")}
    return sorted(grouped.values(), key=lambda item: (item["schema_name"].lower(), item["table_name"].lower()))


def map_concepts(columns: dict) -> dict:
    """Actual column names per concept. Similar names are listed and never queried."""
    mapping = {}
    for concept, (exact, similar) in CONCEPTS.items():
        exact_hits = [columns[name]["name"] for name in exact if name in columns]
        if concept == "exchange_policy_identifier":
            similar_hits = [info["name"] for key, info in columns.items()
                            if "policy" in key and key not in ("enrollment_id",)]
        else:
            similar_hits = [columns[name]["name"] for name in similar if name in columns]
        if exact_hits:
            status = EXACT_NAME
        elif similar_hits:
            status = NAME_SIMILAR
        else:
            status = ABSENT
        mapping[concept] = {"status": status, "columns": exact_hits or similar_hits}
    return mapping


def snapshot_evidence(source: dict, concepts: dict) -> str | None:
    name = source["table_name"]
    if name.lower().startswith(SNAPSHOT_PREFIX.lower()):
        status = classify_snapshot_name(name).get("filename_status")
        if status != UNRESOLVED:
            return (f"Monthly snapshot table ({status}). Phase 5C showed these tables hold mutable latest "
                    "state at each snapshot date.")
    if concepts["snapshot_date"]["status"] == EXACT_NAME:
        return "Has a snapshot/as-of date column: point-in-time copies."
    return None


def has_version_pair(columns: dict) -> bool:
    return any(left in columns and right in columns for left, right in VERSION_PAIRS)


def _exact(source: dict, concept: str) -> dict | None:
    for name in CONCEPTS[concept][0]:
        if name in source["columns"]:
            return source["columns"][name]
    return None


def _q(source: dict, concept: str) -> str:
    return quote_identifier(_exact(source, concept)["name"])


def _year_filter(source: dict) -> str:
    return f"{_q(source, 'coverage_year')} = ?"


def build_profile_sql(source: dict) -> tuple[str, tuple]:
    qualified = qualified_name(source["schema_name"], source["table_name"])
    selects = ["COUNT_BIG(*) AS rows_2026"]
    for concept, alias in (("policy_identifier", "distinct_policies"), ("person_identifier", "distinct_persons"),
                           ("issuer_id", "distinct_issuers")):
        if _exact(source, concept):
            selects.append(f"COUNT_BIG(DISTINCT {_q(source, concept)}) AS {alias}")
    for concept in NULL_CONCEPTS:
        if _exact(source, concept):
            selects.append(f"SUM(CASE WHEN {_q(source, concept)} IS NULL THEN 1 ELSE 0 END) AS null_{concept}")
    for concept in DATE_CONCEPTS:
        if _exact(source, concept):
            selects.append(f"MIN({_q(source, concept)}) AS min_{concept}")
            selects.append(f"MAX({_q(source, concept)}) AS max_{concept}")
    sql = "SELECT " + ",\n       ".join(selects) + f"\nFROM {qualified}\nWHERE {_year_filter(source)}"
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR,)


def build_grain_sql(source: dict) -> tuple[str, tuple]:
    """Rows per policy-member key, and whether a key carries several statuses or update days."""
    qualified = qualified_name(source["schema_name"], source["table_name"])
    policy = _q(source, "policy_identifier")
    keys = [policy]
    if _exact(source, "person_identifier"):
        keys.append(_q(source, "person_identifier"))
    inner = ["COUNT_BIG(*) AS n"]
    outer = [
        "COUNT_BIG(*) AS grain_keys",
        "COUNT_BIG(CASE WHEN n > 1 THEN 1 END) AS keys_with_repeated_rows",
        "MAX(n) AS max_rows_per_key",
    ]
    if _exact(source, "enrollment_status"):
        inner.append(f"COUNT(DISTINCT {_q(source, 'enrollment_status')}) AS statuses")
        outer.append("COUNT_BIG(CASE WHEN statuses > 1 THEN 1 END) AS keys_with_multiple_statuses")
    update = _exact(source, "enrollment_last_update")
    if update:
        expression = quote_identifier(update["name"])
        if is_datetime_sql_type(update["data_type"]):
            expression = f"CAST({expression} AS date)"
        inner.append(f"COUNT(DISTINCT {expression}) AS update_days")
        outer.append("COUNT_BIG(CASE WHEN update_days > 1 THEN 1 END) AS keys_with_multiple_update_days")
    sql = (
        "WITH g AS (\n"
        f"    SELECT {', '.join(inner)}\n"
        f"    FROM {qualified}\n"
        f"    WHERE {policy} IS NOT NULL AND {_year_filter(source)}\n"
        f"    GROUP BY {', '.join(keys)}\n"
        ")\n"
        f"SELECT {', '.join(outer)}\nFROM g"
    )
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR,)


def build_status_sql(source: dict, concept: str) -> tuple[str, tuple]:
    qualified = qualified_name(source["schema_name"], source["table_name"])
    status = _q(source, concept)
    distinct = ""
    if _exact(source, "policy_identifier"):
        distinct = f", COUNT_BIG(DISTINCT {_q(source, 'policy_identifier')}) AS distinct_policies"
    sql = (
        f"SELECT TOP {STATUS_TOP} {status} AS status_value, COUNT_BIG(*) AS rows_2026{distinct}\n"
        f"FROM {qualified}\nWHERE {_year_filter(source)}\n"
        f"GROUP BY {status}\nORDER BY COUNT_BIG(*) DESC"
    )
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR,)


def classify(source: dict, concepts: dict, profile: dict | None, grain: dict | None,
             temporal_type: str | None) -> tuple[str, list[str]]:
    evidence = []
    snap = snapshot_evidence(source, concepts)
    if snap:
        return SNAPSHOT, [snap]
    has_policy = concepts["policy_identifier"]["status"] == EXACT_NAME
    has_person = concepts["person_identifier"]["status"] == EXACT_NAME
    if not has_policy and not has_person:
        if any(word in key for key in source["columns"] for word in ("count", "total")):
            return AGGREGATE, ["No policy or person identifier; count/total columns present."]
        return INSUFFICIENT, ["No policy or person identifier column."]
    if concepts["coverage_year"]["status"] != EXACT_NAME:
        return INSUFFICIENT, ["No coverage_year column; PY2026 cannot be isolated without inventing a mapping."]
    rows = None if profile is None else _int(profile.get("rows_2026"))
    if rows is None:
        return INSUFFICIENT, ["PY2026 profile did not run or failed (N/A, not zero)."]
    if rows == 0:
        return INSUFFICIENT, ["No coverage_year = 2026 rows."]
    evidence.append(f"{rows} coverage_year = 2026 rows.")
    if not has_policy:
        return INSUFFICIENT, evidence + ["Person identifier only; policy grain unavailable."]
    versioned = temporal_type is not None or has_version_pair(source["columns"])
    if versioned:
        evidence.append(f"System versioning evidence: {temporal_type or 'valid/system time column pair'}.")
    keys = None if grain is None else _int(grain.get("grain_keys"))
    repeated = None if grain is None else _int(grain.get("keys_with_repeated_rows"))
    if not has_person and not versioned:
        return INSUFFICIENT, evidence + [
            "Policy identifier without person identifier: repeated rows may be members, not versions."]
    if keys:
        changing = sum(_int(grain.get(name)) or 0
                       for name in ("keys_with_multiple_statuses", "keys_with_multiple_update_days"))
        share = repeated / keys
        evidence.append(f"{repeated} of {keys} policy-member keys have repeated rows "
                        f"({share:.4f}); {changing} key-level status/update-day variations.")
        if versioned or (share >= HISTORY_REPEAT_SHARE and changing > 0):
            return FULL_RECORD, evidence + ["Several versions per policy-member: history is preserved."]
        if share < HISTORY_REPEAT_SHARE:
            return LATEST_STATE, evidence + ["One row per policy-member: current state of each record."]
        return INSUFFICIENT, evidence + ["Repeated rows without status or update-day variation: unexplained duplication."]
    if versioned:
        return FULL_RECORD, evidence
    return INSUFFICIENT, evidence + ["Grain query did not run or failed (N/A)."]


def recommend(rows: list[dict], baseline: dict | None) -> dict:
    eligible = []
    baseline_policies = None if baseline is None else _int(baseline.get("distinct_policies"))
    for row in rows:
        if row["classification"] not in (FULL_RECORD, LATEST_STATE):
            continue
        if not (row["policy_grain_available"] and row["person_grain_available"] and row["status_fields"]):
            continue
        policies = _int(row.get("distinct_policies"))
        if baseline_policies and (policies or 0) < BASELINE_COVERAGE_SHARE * baseline_policies:
            continue
        eligible.append(row)
    if not eligible:
        return {"source": None, "reason": (
            "INSUFFICIENT_EVIDENCE: no non-snapshot object proved PY2026 policy and person grain, a status "
            "field, and a population at least as large as the latest dated snapshot.")}
    rank = sorted(eligible, key=lambda row: (row["classification"] != FULL_RECORD, -(_int(row["distinct_policies"]) or 0)))
    top = rank[0]
    if len(rank) > 1 and rank[1]["classification"] == top["classification"] and \
            _int(rank[1]["distinct_policies"]) == _int(top["distinct_policies"]):
        return {"source": None, "reason": "INSUFFICIENT_EVIDENCE: two candidates tie on every criterion.",
                "tied": [top["db_source"], rank[1]["db_source"]]}
    return {
        "source": top["db_source"],
        "classification": top["classification"],
        "reason": ("Strongest proven PY2026 source: policy and person grain, status field, "
                   f"{top['distinct_policies']} distinct policies"
                   + (f" versus {baseline_policies} in the latest dated snapshot." if baseline_policies else
                      "; latest-snapshot population was not available for comparison.")),
        "alternatives": [row["db_source"] for row in rank[1:]],
    }


def collect(client) -> dict:
    failures: list[dict] = []
    catalog = _run(client, failures, "catalog", CATALOG_SQL, None)
    sources = [] if catalog is None else parse_catalog(catalog)
    temporal = {}
    result = _run(client, failures, "temporal tables", TEMPORAL_SQL, None)
    for schema_name, table_name, temporal_type in (result.rows if result is not None else ()):
        if not is_excluded_name(table_name):
            temporal[(str(schema_name), str(table_name))] = str(temporal_type)

    for source in sources:
        source["concepts"] = map_concepts(source["columns"])
        source["snapshot"] = snapshot_evidence(source, source["concepts"])

    def profilable(source):
        concepts = source["concepts"]
        return (concepts["coverage_year"]["status"] == EXACT_NAME
                and EXACT_NAME in (concepts["policy_identifier"]["status"], concepts["person_identifier"]["status"]))

    candidates = [item for item in sources if not item["snapshot"] and profilable(item)]
    candidates.sort(key=lambda item: (item["table_name"] not in FOCUS_OBJECTS,
                                      -sum(c["status"] == EXACT_NAME for c in item["concepts"].values()),
                                      item["schema_name"].lower(), item["table_name"].lower()))
    to_profile = candidates[:MAX_PROFILED]
    over_limit = {(item["schema_name"], item["table_name"]) for item in candidates[MAX_PROFILED:]}
    baseline = latest_dated_snapshot([item for item in sources if item["snapshot"] and profilable(item)])

    for source in to_profile + ([baseline] if baseline else []):
        label = f"{source['schema_name']}.{source['table_name']}"
        result = _query(client, failures, f"profile {label}", lambda: build_profile_sql(source))
        source["profile"] = None if result is None or not result.rows else _row(result)
        if source is baseline:
            continue
        if _exact(source, "policy_identifier"):
            result = _query(client, failures, f"grain {label}", lambda: build_grain_sql(source))
            source["grain"] = None if result is None or not result.rows else _row(result)
        source["status_distribution"] = {}
        for concept in STATUS_CONCEPTS:
            if _exact(source, concept):
                result = _query(client, failures, f"{concept} {label}",
                                lambda: build_status_sql(source, concept))
                source["status_distribution"][concept] = (
                    None if result is None else [_row(result, index) for index in range(len(result.rows))])
    return {"sources": sources, "temporal": temporal, "over_limit": over_limit,
            "baseline": baseline, "failures": failures}


def latest_dated_snapshot(snapshots: list[dict]) -> dict | None:
    dated = [(classify_snapshot_name(item["table_name"]).get("inferred_date"), item) for item in snapshots]
    dated = [pair for pair in dated if pair[0]]
    return max(dated, key=lambda pair: pair[0])[1] if dated else None


def build_report(evidence: dict) -> dict:
    temporal = evidence.get("temporal") or {}
    over_limit = evidence.get("over_limit") or set()
    baseline = evidence.get("baseline")
    rows = []
    for source in evidence.get("sources") or []:
        key = (source["schema_name"], source["table_name"])
        concepts = source.get("concepts") or map_concepts(source["columns"])
        profile, grain = source.get("profile"), source.get("grain")
        classification, notes = classify(source, concepts, profile, grain, temporal.get(key))
        if key in over_limit:
            notes = notes + [f"Not profiled: limit is {MAX_PROFILED} objects."]
        if source is baseline:
            notes = notes + ["Profiled as the population baseline only."]
        rows_2026 = None if profile is None else _int(profile.get("rows_2026"))
        rows.append({
            "db_source": f"{source['schema_name']}.{source['table_name']}",
            "object_type": source["table_type"],
            "py2026_present": None if rows_2026 is None else rows_2026 > 0,
            "row_count": rows_2026,
            "distinct_policies": None if profile is None else _int(profile.get("distinct_policies")),
            "distinct_persons": None if profile is None else _int(profile.get("distinct_persons")),
            "distinct_issuers": None if profile is None else _int(profile.get("distinct_issuers")),
            "policy_grain_available": concepts["policy_identifier"]["status"] == EXACT_NAME,
            "person_grain_available": concepts["person_identifier"]["status"] == EXACT_NAME,
            "date_fields": [name for concept in DATE_CONCEPTS for name in concepts[concept]["columns"]
                            if concepts[concept]["status"] == EXACT_NAME],
            "status_fields": [name for concept in STATUS_CONCEPTS for name in concepts[concept]["columns"]
                              if concepts[concept]["status"] == EXACT_NAME],
            "issuer_field": (concepts["issuer_id"]["columns"] or [None])[0]
            if concepts["issuer_id"]["status"] == EXACT_NAME else None,
            "temporal_type": temporal.get(key),
            "classification": classification,
            "evidence": notes,
            "concepts": concepts,
            "profile": _clean(profile),
            "grain": _clean(grain),
            "status_distribution": _clean(source.get("status_distribution")),
        })
    baseline_row = None
    if baseline is not None:
        baseline_row = next((row for row in rows
                             if row["db_source"] == f"{baseline['schema_name']}.{baseline['table_name']}"), None)
    focus = []
    for name in FOCUS_OBJECTS:
        matches = [row for row in rows if row["db_source"].split(".", 1)[1].lower() == name.lower()]
        focus.append({
            "object": name,
            "exists": bool(matches),
            "found_as": [row["db_source"] for row in matches],
            "classification": [row["classification"] for row in matches] or None,
        })
    report = {
        "report_kind": "PY2026_DATABASE_SOURCE_DISCOVERY",
        "architecture": "PY2026 from the database versus PY2027 from current Sisense. PY2027 is not searched in the database.",
        "plan_year_filter": f"coverage_year = {PREVIOUS_PLAN_YEAR}",
        "exclusions": {
            "name_markers": list(EXCLUDED_NAME_MARKERS),
            "rule": "Objects whose names contain these markers are excluded in the catalog query; nothing about them is read.",
        },
        "definitions": {
            FULL_RECORD: "PY2026 at policy grain with several versions per policy-member, or system-versioned.",
            LATEST_STATE: "PY2026 at policy grain with one current row per policy-member.",
            SNAPSHOT: "Point-in-time copy (dated snapshot table name or snapshot/as-of column).",
            AGGREGATE: "Enrollment-named object without policy or person identifiers.",
            INSUFFICIENT: "Not proven by metadata and aggregates. N/A values mean a query did not run; never zero.",
            NAME_SIMILAR: "Column listed by name similarity only; never queried or mapped.",
        },
        "sources": rows,
        "focus_objects": focus,
        "baseline_snapshot": None if baseline_row is None else {
            "db_source": baseline_row["db_source"],
            "row_count": baseline_row["row_count"],
            "distinct_policies": baseline_row["distinct_policies"],
            "distinct_persons": baseline_row["distinct_persons"],
        },
        "recommendation": recommend(rows, baseline_row),
        "production_comparable": False,
        "failures": evidence.get("failures") or [],
        "notes": [
            "Catalog metadata and aggregates only. Identifier values never leave the database.",
            "Snapshot tables are not unioned or summed.",
            "Daily Cancelled/Terminated equivalence is out of scope for this phase.",
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
    if value is None:
        return "N/A"
    if isinstance(value, (list, tuple)):
        return html.escape(", ".join(str(item) for item in value)) or "none"
    return html.escape(str(value))


def render_html(report: dict) -> str:
    head = ("<tr><th>DB Source</th><th>Object Type</th><th>PY2026 Present</th><th>Row Count</th>"
            "<th>Policy Grain</th><th>Person Grain</th><th>Date Fields</th><th>Status Fields</th>"
            "<th>Issuer Field</th><th>Classification</th><th>Evidence</th></tr>")
    body = "".join(
        f"<tr><td>{_cell(row['db_source'])}</td><td>{_cell(row['object_type'])}</td>"
        f"<td>{_cell(row['py2026_present'])}</td><td>{_cell(row['row_count'])}</td>"
        f"<td>{_cell(row['policy_grain_available'])}</td><td>{_cell(row['person_grain_available'])}</td>"
        f"<td>{_cell(row['date_fields'])}</td><td>{_cell(row['status_fields'])}</td>"
        f"<td>{_cell(row['issuer_field'])}</td><td>{_cell(row['classification'])}</td>"
        f"<td>{_cell(row['evidence'])}</td></tr>"
        for row in report["sources"]
    )
    rec = report["recommendation"]
    focus = "".join(f"<li>{_cell(item['object'])}: exists={_cell(item['exists'])} "
                    f"{_cell(item['found_as'])} {_cell(item['classification'])}</li>"
                    for item in report["focus_objects"])
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>PY2026 database source discovery</title></head>"
        "<body><h1>PY2026 database source discovery</h1>"
        f"<p>{_cell(report['architecture'])}</p>"
        f"<h2>Recommendation</h2><p><b>{_cell(rec.get('source'))}</b> {_cell(rec.get('reason'))}</p>"
        f"<h2>Named objects</h2><ul>{focus}</ul>"
        f"<h2>Sources</h2><table border='1' cellpadding='4'>{head}{body}</table>"
        "<p>Aggregates only. N/A means the evidence did not run; it is never zero. "
        "834 / inbound / RCNI / SFTP objects excluded.</p></body></html>"
    )


def print_summary(report: dict) -> None:
    print("PY2026 DATABASE SOURCE DISCOVERY (read-only, aggregates only)")
    for row in report["sources"]:
        print(f"{row['db_source']} [{row['object_type']}] py2026={_text(row['py2026_present'])} "
              f"rows={_text(row['row_count'])} policies={_text(row['distinct_policies'])} "
              f"persons={_text(row['distinct_persons'])} policy_grain={row['policy_grain_available']} "
              f"person_grain={row['person_grain_available']} -> {row['classification']}")
    for item in report["focus_objects"]:
        print(f"Named object {item['object']}: exists={item['exists']} {item['found_as']} {item['classification']}")
    rec = report["recommendation"]
    print(f"Recommendation: {rec.get('source') or 'NONE'} - {rec.get('reason')}")
    for failure in report["failures"]:
        print(f"Failure: {failure['step']}: {failure['error']}")


def _run(client, failures, step, sql, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params) if params is not None else client.execute_query(sql)
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


def _row(result, index: int = 0) -> dict:
    return {str(column): value for column, value in zip(result.columns, result.rows[index])}


def _int(value):
    if value is None:
        return None
    return int(value)


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
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
    evidence = {"sources": [], "failures": []}
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
