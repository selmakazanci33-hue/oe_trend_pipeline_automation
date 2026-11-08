"""Diagnostic PY2026 vs PY2027 people counts.

This is not an official year-over-year metric. The official comparison gates in
``comparison.lifecycle`` are unchanged. Filename dates are labeled
``FILENAME_INFERRED_SNAPSHOT_DATE`` and are never proven as-of dates.

``main.py`` does not import this module. This command does not send JAQL.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import (
    VERIFIED_PAYLOAD_FILE,
    guard_no_pii,
    interpret_payload,
    percent_change,
)
from comparison.lifecycle import (
    CATALOG_SQL,
    EXACT,
    LIFECYCLE_RULE,
    OUTPUT_DIR,
    PERSON_UNPROVEN,
    PREVIOUS_PLAN_YEAR,
    QUERY_TIMEOUT_SECONDS,
    SHIFTED,
    SNAPSHOT_PREFIX,
    classify_asof,
    infer_filename_date,
    map_person,
    nearest_snapshot,
    official_comparison,
    parse_catalog,
    read_observation,
    resolve_history_path,
    shift_benefit_window,
    shift_one_calendar_year,
    snapshot_rows,
    translate_filters,
)
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_vs_py2027_diagnostic_numbers.json"
HTML_NAME = "py2026_vs_py2027_diagnostic_numbers.html"

DIAGNOSTIC = "DIAGNOSTIC_COMPARISON"
FILENAME_INFERRED = "FILENAME_INFERRED_SNAPSHOT_DATE"
ENROLLEE_PROXY = "ENROLLEE_ID_PROXY"
NOT_APPLIED = "NOT_APPLIED_COLUMN_UNAVAILABLE"
NO_MATCHING = "NO_MATCHING_HISTORICAL_SNAPSHOT"
CLOSEST = "CLOSEST_FILENAME_SNAPSHOT"
TIED = "TIED_SNAPSHOTS"
COMPLETED = "COMPLETED"
BLOCKED = "BLOCKED"
READY = "READY"
MISSING_OBSERVATION = "MISSING_OBSERVATION"

SCENARIO_A = "A_VISIBLE_MEMBERS"
SCENARIO_B = "B_WITHOUT_TERMINATED"
SCENARIO_C = "C_ENROLLED_ONLY"
SCENARIO_NONE = "STATUS_FILTERS_NOT_APPLIED"

TABLES_SQL = """
SELECT
    TABLE_SCHEMA AS table_schema,
    TABLE_NAME AS table_name,
    TABLE_TYPE AS table_type
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW')
  AND TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'sys')
  AND TABLE_NAME LIKE 'Enrollments[_]PY2526%'
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

COLUMNS_SQL = """
SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
ORDER BY ORDINAL_POSITION
"""

REFRESH_COMMAND = r".\.venv\Scripts\python.exe main.py"


def status_scenarios(definition: dict) -> list[dict]:
    """Bounded status lists from the verified members. turnedOff is not interpreted."""
    enrollment = _scope(definition["scope_filters"], "enrollment_status_description")
    members = list((enrollment or {}).get("members") or [])
    if not members:
        enrollee = _scope(definition["scope_filters"], "enrollee_status_description")
        members = list((enrollee or {}).get("members") or [])
    if not members:
        return [{
            "id": SCENARIO_NONE,
            "label": "No status members in the verified payload",
            "members": [],
        }]
    scenarios = [{
        "id": SCENARIO_A,
        "label": " + ".join(members),
        "members": members,
    }]
    without_terminated = [item for item in members if item != "Terminated"]
    if without_terminated and without_terminated != members:
        scenarios.append({
            "id": SCENARIO_B,
            "label": " + ".join(without_terminated),
            "members": without_terminated,
        })
    if "Enrolled" in members and members != ["Enrolled"]:
        scenarios.append({
            "id": SCENARIO_C,
            "label": "Enrolled only",
            "members": ["Enrolled"],
        })
    return scenarios


def find_exchange_column(columns: dict) -> str | None:
    """Return the actual exchange-individual column name, or None."""
    if not columns:
        return None
    if "exchg_indiv_identifier" in columns:
        return columns["exchg_indiv_identifier"]["name"]
    known = (
        "exchange_individual_identifier",
        "exchange_indiv_identifier",
        "exchg_individual_identifier",
    )
    for name in known:
        if name in columns:
            return columns[name]["name"]
    fuzzy = []
    for key, meta in columns.items():
        compact = key.replace("_", "")
        if "ident" in compact and ("exchgindiv" in compact or "exchangeindiv" in compact):
            fuzzy.append(meta["name"])
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None


def select_diagnostic_snapshot(snapshots: list[dict], lifecycle: date) -> dict:
    """Closest filename date. A tie is not chosen. An early lifecycle uses the earliest snapshot."""
    dated = []
    for item in snapshots:
        inferred = item.get("inferred_filename_date")
        if not inferred:
            continue
        snap_day = date.fromisoformat(inferred)
        dated.append({**item, "_day": snap_day})
    if not dated:
        return {
            "selected": None,
            "tied": [],
            "match_status": "NONE",
            "reason": "No Enrollments_PY2526 table has an inferred filename date.",
        }
    earliest = min(item["_day"] for item in dated)
    if lifecycle < earliest:
        winners = [item for item in dated if item["_day"] == earliest]
        if len(winners) != 1:
            return _tie(winners, lifecycle, (
                "The PY2026 lifecycle date is earlier than every inferred snapshot date, "
                "and more than one table shares the earliest filename date."
            ))
        return _choice(winners[0], lifecycle, NO_MATCHING, (
            "The PY2026 lifecycle date is earlier than the earliest inferred snapshot date. "
            "The earliest snapshot is a diagnostic candidate only and is not lifecycle-equivalent."
        ))
    ranked = []
    for item in dated:
        ranked.append((abs((item["_day"] - lifecycle).days), item))
    best = min(distance for distance, _item in ranked)
    winners = [item for distance, item in ranked if distance == best]
    if len(winners) != 1:
        return _tie(winners, lifecycle, "Two or more inferred snapshot dates are equally close to the lifecycle date.")
    return _choice(winners[0], lifecycle, CLOSEST, (
        "Closest inferred filename date to the PY2026 lifecycle date. "
        "This date is not a proven as-of date."
    ))


def choose_person_column(columns: dict) -> dict:
    exchange = find_exchange_column(columns)
    if exchange is not None:
        return {
            "column": exchange,
            "label": exchange,
            "identity_status": EXACT,
            "translation_status": EXACT,
            "reason": "The snapshot column matches an exchange individual identifier.",
        }
    if "enrollee_id" in columns:
        return {
            "column": columns["enrollee_id"]["name"],
            "label": ENROLLEE_PROXY,
            "identity_status": PERSON_UNPROVEN,
            "translation_status": PERSON_UNPROVEN,
            "reason": (
                "No exchange individual identifier was found. "
                "COUNT(DISTINCT enrollee_id) is a diagnostic proxy only."
            ),
        }
    return {
        "column": None,
        "label": None,
        "identity_status": PERSON_UNPROVEN,
        "translation_status": "NOT_AVAILABLE",
        "reason": "The snapshot has neither an exchange individual identifier nor enrollee_id.",
    }


def diagnostic_filters(definition: dict, columns: dict) -> list[dict]:
    """Translate known filters. Missing columns are marked, not omitted silently."""
    scope = definition["scope_filters"]
    coverage = _scope(scope, "coverage_year")
    benefit = _scope(scope, "benefit_effective_date")
    plan_year = _single_year(coverage)
    window = None
    if plan_year is not None and benefit and benefit.get("from") and benefit.get("to"):
        window = shift_benefit_window(
            plan_year, date.fromisoformat(benefit["from"]), date.fromisoformat(benefit["to"]))
    person = _scope(scope, "person_type_description")
    insurance = _scope(scope, "insurance_type_description")
    return [
        _year_filter(columns, plan_year),
        _benefit_filter(columns, window, benefit),
        _members_filter(columns, "person_type_description", "Person type",
                        list((person or {}).get("members") or [])),
        _members_filter(columns, "insurance_type_description", "Insurance type",
                        list((insurance or {}).get("members") or [])),
    ]


def build_count_sql(qualified: str, person_column: str, filters: list[dict],
                    status_members: list[str], columns: dict) -> tuple[str, tuple]:
    """Aggregate people count. Identifiers are quoted. Values are parameters."""
    clauses = []
    params: list = []
    for item in filters:
        if item.get("sql"):
            clauses.append(item["sql"])
            params.extend(item["params"])
    for column in ("enrollment_status_description", "enrollee_status_description"):
        if status_members and column in columns:
            placeholders = ", ".join("?" for _ in status_members)
            quoted = quote_identifier(columns[column]["name"])
            clauses.append(f"{quoted} IN ({placeholders})")
            params.extend(status_members)
    where = ("\nWHERE " + "\n  AND ".join(clauses)) if clauses else ""
    person = quote_identifier(person_column)
    sql = f"SELECT COUNT_BIG(DISTINCT {person}) AS people_count\nFROM {qualified}{where}"
    return sql, tuple(params)


def build_report(payload: dict, observation: dict | None, *, snapshots: list[dict] | None = None,
                 selected_columns: dict | None = None, scenario_counts: dict | None = None,
                 failures: list | None = None, official: dict | None = None) -> dict:
    definition = interpret_payload(payload)
    if observation is None:
        report = {
            "comparison_kind": DIAGNOSTIC,
            "official_comparison_status": BLOCKED,
            "diagnostic_comparison_status": MISSING_OBSERVATION,
            "py2027_observation_date": None,
            "py2026_equivalent_lifecycle_date": None,
            "status_scenarios": [],
            "failures": failures or [{
                "step": "observation",
                "error": "No stored validated Currently Enrolled People observation was found.",
            }],
            "refresh_command": REFRESH_COMMAND,
            "assumptions": ["This is not an official year-over-year metric."],
        }
        guard_no_pii(report)
        return report
    observation_date = date.fromisoformat(observation["observation_date"])
    lifecycle = shift_one_calendar_year(observation_date)
    py2027 = _as_int(observation.get("metric_value"))
    dated = snapshots or []
    selection = select_diagnostic_snapshot(dated, lifecycle)
    selected = selection["selected"]
    columns = selected_columns or {}
    person = choose_person_column(columns) if selected is not None else {
        "column": None, "label": None, "identity_status": PERSON_UNPROVEN,
        "translation_status": "NOT_AVAILABLE",
        "reason": "No diagnostic snapshot was selected.",
    }
    filters = diagnostic_filters(definition, columns) if selected is not None else []
    scenarios = []
    if selected is not None and person["column"] is not None:
        has_status = any(
            name in columns
            for name in ("enrollment_status_description", "enrollee_status_description")
        )
        scenario_defs = status_scenarios(definition) if has_status else [{
            "id": SCENARIO_NONE,
            "label": "Status columns not applied",
            "members": [],
        }]
        for scenario in scenario_defs:
            count = None if scenario_counts is None else scenario_counts.get(scenario["id"])
            change = percent_change(count, py2027)
            scenarios.append({
                "id": scenario["id"],
                "label": scenario["label"],
                "members": scenario["members"],
                "py2026_db_count": count,
                "py2027_sisense_count": py2027,
                "absolute_difference": change["absolute_difference"],
                "percent_change": change["percent_change"],
                "percent_status": change["percent_status"],
            })
    applied = [_public_filter(item) for item in filters if item.get("sql")]
    unavailable = [_public_filter(item) for item in filters if not item.get("sql")]
    diagnostic_status = _diagnostic_status(selection, person, scenarios)
    report = {
        "comparison_kind": DIAGNOSTIC,
        "official_comparison_status": (official or {}).get("status", BLOCKED),
        "official_comparison": official or {
            "status": BLOCKED,
            "reason": "Official comparison was not evaluated.",
        },
        "diagnostic_comparison_status": diagnostic_status,
        "py2027_observation_date": observation_date.isoformat(),
        "py2027_observation": {
            "source": observation.get("source"),
            "retrieved_at_utc": observation.get("retrieved_at_utc"),
            "plan_year": 2027,
            "metric_id": "currently_enrolled_people",
            "metric_value": py2027,
        },
        "py2026_equivalent_lifecycle_date": lifecycle.isoformat(),
        "lifecycle_alignment_rule": LIFECYCLE_RULE,
        "snapshot_selection": {
            "match_status": selection["match_status"],
            "reason": selection["reason"],
            "date_label": FILENAME_INFERRED,
            "selected": None if selected is None else {
                "schema_name": selected["schema_name"],
                "table": selected["table"],
                "inferred_snapshot_date": selected["inferred_filename_date"],
                "lifecycle_target_date": lifecycle.isoformat(),
                "distance_days": selected.get("distance_days"),
                "distance_days_absolute": selected.get("distance_days_absolute"),
                "position": selected.get("position"),
            },
            "tied": [
                {"schema_name": item["schema_name"], "table": item["table"],
                 "inferred_snapshot_date": item.get("inferred_filename_date")}
                for item in selection.get("tied") or []
            ],
            "candidates": [
                {
                    "schema_name": item["schema_name"],
                    "table": item["table"],
                    "inferred_snapshot_date": item.get("inferred_filename_date"),
                    "date_label": FILENAME_INFERRED,
                }
                for item in dated if item.get("inferred_filename_date")
            ],
        },
        "person_identifier": {
            "sisense": "enrollee.exchg_indiv_identifier",
            "py2026_column": person["column"],
            "label": person["label"],
            "identity_status": person["identity_status"],
            "reason": person["reason"],
        },
        "filters_applied": applied,
        "filters_unavailable": unavailable,
        "status_scenarios": scenarios,
        "failures": failures or [],
        "assumptions": _assumptions(selection, person, filters, diagnostic_status),
    }
    guard_no_pii(report)
    return report


def collect_evidence(client, payload: dict, observation: dict, lifecycle: date) -> dict:
    """Metadata first, then one aggregate per supported status scenario."""
    failures = []
    tables = _run(client, failures, "snapshot tables", TABLES_SQL, None)
    catalog = _run(client, failures, "catalog", CATALOG_SQL, None)
    sources = parse_catalog(catalog) if catalog is not None else []
    official = _official_status(payload, observation, sources)
    snapshots = []
    if tables is not None:
        for row in tables.rows:
            schema_name, table_name, table_type = str(row[0]), str(row[1]), str(row[2] or "")
            if not table_name.lower().startswith(SNAPSHOT_PREFIX.lower()):
                continue
            if _inbound(table_name):
                continue
            snapshots.append({
                "schema_name": schema_name,
                "table": table_name,
                "table_type": table_type,
                "inferred_filename_date": infer_filename_date(table_name),
            })
    selection = select_diagnostic_snapshot(snapshots, lifecycle)
    selected = selection["selected"]
    columns = {}
    scenario_counts = {}
    if selected is not None:
        column_result = _run(
            client, failures, "selected columns", COLUMNS_SQL,
            (selected["schema_name"], selected["table"]),
        )
        if column_result is not None:
            columns = {
                str(name).lower(): {"name": str(name), "data_type": str(dtype or "")}
                for name, dtype in column_result.rows if name
            }
        person = choose_person_column(columns)
        definition = interpret_payload(payload)
        filters = diagnostic_filters(definition, columns)
        if person["column"] is None:
            failures.append({"step": "person identifier", "error": person["reason"]})
        else:
            qualified = qualified_name(selected["schema_name"], selected["table"])
            scenarios = status_scenarios(definition)
            if not any(column in columns for column in (
                "enrollment_status_description", "enrollee_status_description",
            )):
                scenarios = [{
                    "id": SCENARIO_NONE,
                    "label": "Status columns not applied",
                    "members": [],
                }]
            for scenario in scenarios:
                sql, params = build_count_sql(
                    qualified, person["column"], filters, scenario["members"], columns)
                result = _run(client, failures, f"count {scenario['id']}", sql, params)
                if result is not None and result.row_count == 1:
                    scenario_counts[scenario["id"]] = _as_int(result.rows[0][0])
    return {
        "snapshots": snapshots,
        "selected_columns": columns,
        "scenario_counts": scenario_counts,
        "failures": failures,
        "official": official,
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
        f"<td>{_e(item['label'])}</td>"
        f"<td>{_e(item['py2026_db_count'])}</td>"
        f"<td>{_e(item['py2027_sisense_count'])}</td>"
        f"<td>{_e(item['absolute_difference'])}</td>"
        f"<td>{_e(item['percent_change'])}</td>"
        "</tr>"
        for item in report.get("status_scenarios") or []
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Diagnostic PY2026 vs PY2027 people counts</title>"
        "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left}</style>"
        "</head><body>"
        "<h1>Diagnostic comparison — not an official YoY metric</h1>"
        f"<p>Official status: {_e(report.get('official_comparison_status'))}</p>"
        f"<p>Diagnostic status: {_e(report.get('diagnostic_comparison_status'))}</p>"
        f"<p>PY2027 observation date: {_e(report.get('py2027_observation_date'))}</p>"
        f"<p>PY2026 lifecycle date: {_e(report.get('py2026_equivalent_lifecycle_date'))}</p>"
        "<table><thead><tr><th>Scenario</th><th>PY2026 DB Count</th>"
        "<th>PY2027 Sisense Count</th><th>Difference</th><th>% Difference</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></body></html>"
    )


def print_table(report: dict) -> None:
    print("DIAGNOSTIC COMPARISON — not an official YoY metric")
    print(f"Official comparison status: {report.get('official_comparison_status')}")
    print(f"Diagnostic comparison status: {report.get('diagnostic_comparison_status')}")
    if report.get("diagnostic_comparison_status") == MISSING_OBSERVATION:
        print("No stored validated Currently Enrolled People observation was found.")
        print(f"Run: {REFRESH_COMMAND}")
        return
    print(f"PY2027 observation date: {report.get('py2027_observation_date')}")
    print(f"PY2026 lifecycle date: {report.get('py2026_equivalent_lifecycle_date')}")
    selected = (report.get("snapshot_selection") or {}).get("selected")
    if selected is None:
        print("Selected snapshot: none")
    else:
        print(
            f"Selected snapshot: {selected['schema_name']}.{selected['table']}  "
            f"inferred {selected['inferred_snapshot_date']}  "
            f"{selected['distance_days_absolute']} day(s) {selected['position']} target  "
            f"{report['snapshot_selection']['match_status']}"
        )
    person = report.get("person_identifier") or {}
    print(f"Person identifier: {person.get('label')} ({person.get('identity_status')})")
    print("")
    print(f"{'Scenario':<36}{'PY2026':>12}{'PY2027':>12}{'Diff':>12}{'%':>12}")
    for item in report.get("status_scenarios") or []:
        print(
            f"{item['label']:<36}"
            f"{_num(item['py2026_db_count']):>12}"
            f"{_num(item['py2027_sisense_count']):>12}"
            f"{_num(item['absolute_difference']):>12}"
            f"{_num(item['percent_change']):>12}"
        )
    if not report.get("status_scenarios"):
        print("(no diagnostic counts)")


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    observation = read_observation(resolve_history_path())
    if observation is None:
        report = build_report(payload, None)
        json_path, html_path = write_report(report)
        print_table(report)
        print(f"Wrote: {json_path}")
        print(f"Wrote: {html_path}")
        return 2
    lifecycle = shift_one_calendar_year(date.fromisoformat(observation["observation_date"]))
    evidence = {
        "snapshots": [], "selected_columns": {}, "scenario_counts": {},
        "failures": [], "official": {"status": BLOCKED, "reason": "Database was not opened."},
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect_evidence(DatabaseClient(connection), payload, observation, lifecycle)
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(
        payload, observation,
        snapshots=evidence["snapshots"],
        selected_columns=evidence["selected_columns"],
        scenario_counts=evidence["scenario_counts"],
        failures=evidence["failures"],
        official=evidence["official"],
    )
    json_path, html_path = write_report(report)
    print_table(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0 if report["diagnostic_comparison_status"] == COMPLETED else 2


def _official_status(payload, observation, sources) -> dict:
    definition = interpret_payload(payload)
    lifecycle = shift_one_calendar_year(date.fromisoformat(observation["observation_date"]))
    snaps = snapshot_rows(sources, lifecycle)
    asof = classify_asof(snaps) if snaps else "HISTORICAL_ASOF_NOT_PROVEN"
    nearest = nearest_snapshot(snaps)
    columns = None
    if nearest is not None:
        match = next(
            (item for item in sources
             if item["schema_name"] == nearest["schema_name"] and item["table_name"] == nearest["table"]),
            None,
        )
        columns = set(match["columns"]) if match else set()
    person = map_person(definition, columns)
    mappings = translate_filters(definition, columns)
    ready, confidence, basis = official_comparison(asof, person, mappings, observation)
    return {
        "status": READY if ready else BLOCKED,
        "asof_outcome": asof,
        "confidence": confidence,
        "basis": basis,
        "reason": (
            "Official comparison remains subject to proven as-of reconstruction, "
            "the exchange identifier, and acceptable filter translations."
        ),
    }


def _choice(item: dict, lifecycle: date, status: str, reason: str) -> dict:
    snap_day = item["_day"]
    delta = (snap_day - lifecycle).days
    selected = {
        "schema_name": item["schema_name"],
        "table": item["table"],
        "inferred_filename_date": item["inferred_filename_date"],
        "distance_days": delta,
        "distance_days_absolute": abs(delta),
        "position": "after" if delta > 0 else ("before" if delta < 0 else "same_day"),
    }
    return {"selected": selected, "tied": [], "match_status": status, "reason": reason}


def _tie(winners: list[dict], lifecycle: date, reason: str) -> dict:
    tied = []
    for item in winners:
        delta = (item["_day"] - lifecycle).days
        tied.append({
            "schema_name": item["schema_name"],
            "table": item["table"],
            "inferred_filename_date": item["inferred_filename_date"],
            "distance_days": delta,
        })
    return {"selected": None, "tied": tied, "match_status": TIED, "reason": reason}


def _year_filter(columns: dict, plan_year: int | None) -> dict:
    if "coverage_year" not in columns:
        return _unavailable("Plan year", "coverage_year")
    year = PREVIOUS_PLAN_YEAR if plan_year is None else plan_year - 1
    quoted = quote_identifier(columns["coverage_year"]["name"])
    return {
        "sisense_concept": "Plan year",
        "py2026_db_column": columns["coverage_year"]["name"],
        "status": SHIFTED,
        "applied_value": year,
        "sql": f"{quoted} = ?",
        "params": [year],
        "reason": f"Sisense plan year shifted to coverage_year {year}.",
    }


def _benefit_filter(columns: dict, window: dict | None, benefit: dict | None) -> dict:
    if "benefit_effective_date" not in columns:
        return _unavailable("Benefit effective date", "benefit_effective_date")
    if window is None or window.get("translation_status") != SHIFTED or not window.get("from"):
        return {
            "sisense_concept": "Benefit effective date",
            "py2026_db_column": columns["benefit_effective_date"]["name"],
            "status": "UNCERTAIN",
            "applied_value": None,
            "sql": None,
            "params": [],
            "reason": "The verified benefit window was not a plan-year-relative window, so no date predicate was applied.",
        }
    quoted = quote_identifier(columns["benefit_effective_date"]["name"])
    return {
        "sisense_concept": "Benefit effective date",
        "py2026_db_column": columns["benefit_effective_date"]["name"],
        "status": SHIFTED,
        "applied_value": {"from": window["from"], "to": window["to"]},
        "sql": f"{quoted} >= ? AND {quoted} <= ?",
        "params": [window["from"], window["to"]],
        "reason": window["rule"],
    }


def _members_filter(columns: dict, column: str, concept: str, members: list[str]) -> dict:
    if column not in columns:
        return _unavailable(concept, column)
    quoted = quote_identifier(columns[column]["name"])
    placeholders = ", ".join("?" for _ in members) if members else "?"
    if not members:
        return _unavailable(concept, column)
    return {
        "sisense_concept": concept,
        "py2026_db_column": columns[column]["name"],
        "status": EXACT,
        "applied_value": members,
        "sql": f"{quoted} IN ({placeholders})",
        "params": list(members),
        "reason": "Verified members applied on the matching snapshot column.",
    }


def _unavailable(concept: str, column: str) -> dict:
    return {
        "sisense_concept": concept,
        "py2026_db_column": None,
        "status": NOT_APPLIED,
        "applied_value": None,
        "sql": None,
        "params": [],
        "reason": f"{column} is not on the selected snapshot.",
    }


def _diagnostic_status(selection: dict, person: dict, scenarios: list[dict]) -> str:
    if selection["match_status"] == TIED:
        return TIED
    if selection["selected"] is None:
        return BLOCKED
    if person["column"] is None:
        return BLOCKED
    if not scenarios or all(item["py2026_db_count"] is None for item in scenarios):
        return BLOCKED
    return COMPLETED


def _public_filter(item: dict) -> dict:
    return {
        "sisense_concept": item["sisense_concept"],
        "py2026_db_column": item["py2026_db_column"],
        "status": item["status"],
        "applied_value": item["applied_value"],
        "reason": item["reason"],
    }


def _assumptions(selection, person, filters, diagnostic_status) -> list[str]:
    notes = [
        "This is a DIAGNOSTIC_COMPARISON, not an official validated YoY metric.",
        "Filename dates are FILENAME_INFERRED_SNAPSHOT_DATE and are not proven as-of dates.",
        "Status scenarios are not an interpretation of Sisense turnedOff.",
        "The official comparison gates were not relaxed.",
    ]
    if person.get("label") == ENROLLEE_PROXY:
        notes.append("The people count uses ENROLLEE_ID_PROXY; person-identifier equivalence is unproven.")
    if selection.get("match_status") == NO_MATCHING:
        notes.append("The selected snapshot is not lifecycle-equivalent.")
    for item in filters:
        if item.get("status") == NOT_APPLIED:
            notes.append(f"{item['sisense_concept']}: {NOT_APPLIED}.")
    if diagnostic_status != COMPLETED:
        notes.append(f"Diagnostic status is {diagnostic_status}.")
    return notes


def _run(client, failures: list, step: str, sql: str, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


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


def _e(value) -> str:
    import html
    return html.escape("" if value is None else str(value))


def _num(value) -> str:
    if value is None:
        return "n/a"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
