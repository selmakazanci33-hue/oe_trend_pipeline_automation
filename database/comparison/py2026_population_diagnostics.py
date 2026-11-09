"""Phase 6B: PY2026 population diagnostic validation on dbo.Enrollments_TEST only.

Read-only, aggregates only, coverage_year = 2026 only. No other table, no PY2027
database query, no Sisense request. Currently Enrolled candidates, the
% Effectuated status semantics and the cancelled/terminated population
definitions are consumed from the Phase 6A bridge, never restated here.
Every output is DIAGNOSTIC_ONLY and production_comparable = false.

Two views are kept apart: (A) population/state diagnostics (CE1-CE4, % Effectuated)
under PLAN_YEAR_SHIFT_ASSUMPTION, and (B) Stage 7B OE temporal/activity diagnostics
by create/confirmation date under OE_LIFECYCLE_ALIGNMENT_DIAGNOSTIC. View B shows
when records entered or were confirmed, never a historical enrolled population.

dbo.Enrollments_TEST is a "PY2026 Oct-31-equivalent latest-state dataset":
equivalent, for the tested PY2026 records and business fields, to
dbo.Enrollments_PY2526_10312026.

    python -m database.comparison.py2026_population_diagnostics
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.py2026_py2027_metric_bridge import (
    BLOCKED_EVENT_HISTORY,
    BLOCKED_IDENTIFIER,
    BLOCKED_LIFECYCLE,
    DATASET_LABEL,
    DIAGNOSTIC,
    PLAN_YEAR_SHIFT_ASSUMPTION,
    PY2026_YEAR,
    WRONG_SOURCE_FAMILY,
    condition_text,
    currently_enrolled_candidates,
    load_definition,
    population_definitions,
)
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only

JSON_NAME = "py2026_population_diagnostics.json"
HTML_NAME = "py2026_population_diagnostics.html"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.py2026_population_diagnostics"
QUERY_TIMEOUT_SECONDS = 900

SCHEMA = "dbo"
TABLE = "Enrollments_TEST"
DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
PERSON_PROXY = "DB_PERSON_IDENTIFIER_PROXY"
EXCHANGE_NOT_AVAILABLE = "DB_EXCHANGE_PERSON_IDENTIFIER_NOT_AVAILABLE"
MONTHLY_STATEMENT = "Latest-state record distribution by last-update month; NOT historical event activity."

YEAR = "coverage_year"
POLICY = "enrollment_id"
PERSON = "enrollee_id"
EXCHANGE = "exchg_indiv_identifier"
ENROLLMENT_STATUS = "enrollment_status_description"
ENROLLEE_STATUS = "enrollee_status_description"
BENEFIT = "benefit_effective_date"
LAST_UPDATE = "enrollment_last_update_date"
CREATE = "enrollment_create_date"
CONFIRM = "enrollment_confirmation_date"
REQUESTED_COLUMNS = ("person_type_description", "insurance_type_description", EXCHANGE)
OE_DATE_COLUMNS = (CREATE, CONFIRM)

POPULATION_STATE_VIEW = "A_POPULATION_STATE_DIAGNOSTIC"
OE_ACTIVITY_VIEW = "B_OE_TEMPORAL_ACTIVITY_DIAGNOSTIC"
OE_LIFECYCLE_LABEL = "OE_LIFECYCLE_ALIGNMENT_DIAGNOSTIC"
OE_ANCHOR_PROVEN = "OE_START_ANCHOR_PROVEN"
OE_ANCHOR_UNPROVEN = "OE_START_ANCHOR_UNPROVEN"
OE_MONTHLY_FIELDS = (CREATE, CONFIRM, LAST_UPDATE)
OE_CUMULATIVE_FIELDS = (CREATE, CONFIRM)
OE_MONTHLY_START = "2025-10-01"
OE_CUTOFFS = ("2025-11-08", "2025-11-15", "2025-11-30", "2025-12-15", "2025-12-31")
OE_ALIGNMENT_STATEMENT = (
    "Calendar-year alignment is not sufficient. The preferred future YoY comparison is equivalent Open "
    "Enrollment lifecycle position (OE Day N) when defensible anchors and semantics are available."
)
OE_ACTIVITY_CAVEAT = (
    "Shows when PY2026 records in the latest-state dataset were created or confirmed. It does NOT show the "
    "enrolled population on any historical day; no status filter is applied and no historical state is inferred."
)
LAST_UPDATE_MONTHLY_CAVEAT = (
    "Latest update month of each record; a record's current last-update date does not prove its state on an "
    "earlier day and is never used as a historical cutoff."
)
PROVEN_COLUMNS = (YEAR, POLICY, PERSON, ENROLLMENT_STATUS, ENROLLEE_STATUS, BENEFIT, LAST_UPDATE)
BUCKET_CAP = 10

ONE_TO_ONE = "ONE_TO_ONE"
ONE_TO_ONE_WITH_NULLS = "ONE_TO_ONE_WITH_NULLS"
ONE_PROXY_TO_MANY = "ONE_PROXY_TO_MANY_EXCHANGE"
MANY_PROXY_TO_ONE = "MANY_PROXY_TO_ONE_EXCHANGE"
MANY_TO_MANY = "MANY_TO_MANY"

COLUMNS_SQL = (
    "SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type\n"
    "FROM INFORMATION_SCHEMA.COLUMNS\n"
    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?\n"
    "ORDER BY ORDINAL_POSITION"
)


class MissingColumn(Exception):
    """A query needs a column dbo.Enrollments_TEST does not have."""


def _source() -> str:
    return qualified_name(SCHEMA, TABLE)


def _col(columns: dict, name: str) -> str:
    if name not in columns:
        raise MissingColumn(name)
    return quote_identifier(columns[name]["name"])


def _year(columns: dict) -> str:
    return f"{_col(columns, YEAR)} = ?"


def where_from_conditions(conditions: list[dict], columns: dict) -> tuple[str, tuple]:
    """Phase 6A structured conditions -> parameterized predicates. Day ranges include the whole end day."""
    parts, params = [], []
    for item in conditions:
        column, values = _col(columns, item["column"]), list(item["values"])
        if item["op"] == "=":
            parts.append(f"{column} = ?")
            params.append(values[0])
        elif item["op"] == "in":
            parts.append(f"{column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        elif item["op"] == "like":
            parts.append(f"{column} LIKE ?")
            params.append(values[0])
        elif item["op"] == "day_range":
            end = date.fromisoformat(values[1]) + timedelta(days=1)
            parts.append(f"{column} >= ? AND {column} < ?")
            params.extend([values[0], end.isoformat()])
        else:
            raise ValueError(f"Unknown condition operator {item['op']!r}")
    if not any(item["column"] == YEAR and item["values"] == [PY2026_YEAR] for item in conditions):
        raise ValueError("Every diagnostic must be restricted to coverage_year = 2026")
    return " AND ".join(parts), tuple(params)


def _checked(sql: str, params: tuple) -> tuple[str, tuple]:
    assert_read_only(sql)
    if 2027 in params or "2027" in sql:
        raise ValueError("PY2027 is never queried in the database")
    return sql, params


def build_status_sql(columns: dict, field: str) -> tuple[str, tuple]:
    status = _col(columns, field)
    return _checked(
        f"SELECT {status} AS status_value, COUNT_BIG(*) AS row_count,\n"
        f"       COUNT_BIG(DISTINCT {_col(columns, PERSON)}) AS distinct_people,\n"
        f"       COUNT_BIG(DISTINCT {_col(columns, POLICY)}) AS distinct_policies\n"
        f"FROM {_source()}\nWHERE {_year(columns)}\nGROUP BY {status}",
        (PY2026_YEAR,))


def build_identity_sql(columns: dict) -> tuple[str, tuple]:
    person, policy = _col(columns, PERSON), _col(columns, POLICY)
    return _checked(
        f"SELECT COUNT_BIG(*) AS row_count,\n"
        f"       COUNT_BIG(DISTINCT {person}) AS distinct_people,\n"
        f"       COUNT_BIG(DISTINCT {policy}) AS distinct_policies,\n"
        f"       COUNT_BIG(CASE WHEN {person} IS NULL THEN 1 END) AS null_person_rows,\n"
        f"       COUNT_BIG(CASE WHEN {policy} IS NULL THEN 1 END) AS null_policy_rows\n"
        f"FROM {_source()}\nWHERE {_year(columns)}",
        (PY2026_YEAR,))


def build_fanout_sql(columns: dict, key: str, counted: str) -> tuple[str, tuple]:
    """Bucketed distribution of distinct `counted` per `key`; only counts return."""
    k, c = _col(columns, key), _col(columns, counted)
    bucket = f"CASE WHEN n >= {BUCKET_CAP} THEN {BUCKET_CAP} ELSE n END"
    return _checked(
        "WITH f AS (\n"
        f"    SELECT COUNT(DISTINCT {c}) AS n\n"
        f"    FROM {_source()}\n"
        f"    WHERE {_year(columns)} AND {k} IS NOT NULL AND {c} IS NOT NULL\n"
        f"    GROUP BY {k}\n"
        ")\n"
        f"SELECT {bucket} AS bucket, COUNT_BIG(*) AS key_count, SUM(CAST(n AS bigint)) AS link_count, "
        "MAX(n) AS max_n\n"
        f"FROM f\nGROUP BY {bucket}",
        (PY2026_YEAR,))


def build_exchange_profile_sql(columns: dict) -> tuple[str, tuple]:
    exchange, person = _col(columns, EXCHANGE), _col(columns, PERSON)
    return _checked(
        f"SELECT COUNT_BIG(CASE WHEN {exchange} IS NULL THEN 1 END) AS null_exchange_rows,\n"
        f"       COUNT_BIG(DISTINCT {exchange}) AS distinct_exchange_people,\n"
        f"       COUNT_BIG(CASE WHEN {exchange} IS NULL AND {person} IS NOT NULL THEN 1 END) "
        "AS rows_proxy_without_exchange,\n"
        f"       COUNT_BIG(CASE WHEN {exchange} IS NOT NULL AND {person} IS NULL THEN 1 END) "
        "AS rows_exchange_without_proxy\n"
        f"FROM {_source()}\nWHERE {_year(columns)}",
        (PY2026_YEAR,))


def build_exchange_mapping_sql(columns: dict) -> tuple[str, tuple]:
    exchange, person, year = _col(columns, EXCHANGE), _col(columns, PERSON), _year(columns)
    scope = f"FROM {_source()} WHERE {year} AND {person} IS NOT NULL AND {exchange} IS NOT NULL"
    return _checked(
        f"WITH m AS (SELECT {person} AS p, MIN({exchange}) AS x1, COUNT(DISTINCT {exchange}) AS xs "
        f"{scope} GROUP BY {person}),\n"
        f"r AS (SELECT {exchange} AS x, COUNT(DISTINCT {person}) AS ps {scope} GROUP BY {exchange})\n"
        "SELECT (SELECT COUNT_BIG(*) FROM m) AS proxy_keys,\n"
        "       (SELECT COUNT_BIG(*) FROM m WHERE xs = 1) AS proxy_with_one_exchange,\n"
        "       (SELECT COUNT_BIG(*) FROM m WHERE xs > 1) AS proxy_with_many_exchange,\n"
        "       (SELECT COUNT_BIG(*) FROM r) AS exchange_keys,\n"
        "       (SELECT COUNT_BIG(*) FROM r WHERE ps = 1) AS exchange_with_one_proxy,\n"
        "       (SELECT COUNT_BIG(*) FROM r WHERE ps > 1) AS exchange_with_many_proxy,\n"
        "       (SELECT COUNT_BIG(*) FROM m INNER JOIN r ON r.x = m.x1 WHERE m.xs = 1 AND r.ps = 1) "
        "AS one_to_one_pairs",
        (PY2026_YEAR, PY2026_YEAR))


def build_candidate_sql(columns: dict, candidate: dict, effectuated: dict | None) -> tuple[str, tuple]:
    where, where_params = where_from_conditions(candidate["conditions"], columns)
    person = _col(columns, PERSON)
    selects = [
        "COUNT_BIG(*) AS row_count",
        f"COUNT_BIG(DISTINCT {person}) AS distinct_people",
        f"COUNT_BIG(DISTINCT {_col(columns, POLICY)}) AS distinct_policies",
    ]
    select_params: list = []
    if effectuated:
        status = _col(columns, effectuated["status_field"])
        for alias, members in (("effectuated_numerator_people", effectuated["numerator_members"]),
                               ("effectuated_denominator_people", effectuated["denominator_members"])):
            selects.append(f"COUNT_BIG(DISTINCT CASE WHEN {status} IN ({', '.join('?' for _ in members)}) "
                           f"THEN {person} END) AS {alias}")
            select_params.extend(members)
    return _checked(
        f"SELECT {', '.join(selects)}\nFROM {_source()}\nWHERE {where}",
        tuple(select_params) + where_params)


def build_population_sql(columns: dict, definition: dict) -> tuple[str, tuple]:
    where, params = where_from_conditions(definition["conditions"], columns)
    return _checked(
        f"SELECT COUNT_BIG(*) AS row_count, COUNT_BIG(DISTINCT {_col(columns, POLICY)}) AS distinct_policies,\n"
        f"       COUNT_BIG(DISTINCT {_col(columns, PERSON)}) AS distinct_people\n"
        f"FROM {_source()}\nWHERE {where}",
        params)


def build_temporal_sql(columns: dict) -> tuple[str, tuple]:
    benefit, update = _col(columns, BENEFIT), _col(columns, LAST_UPDATE)
    return _checked(
        f"SELECT MIN({benefit}) AS min_benefit_effective_date, MAX({benefit}) AS max_benefit_effective_date,\n"
        f"       MIN({update}) AS min_last_update_date, MAX({update}) AS max_last_update_date,\n"
        f"       COUNT_BIG(CASE WHEN {update} IS NULL THEN 1 END) AS null_last_update_rows\n"
        f"FROM {_source()}\nWHERE {_year(columns)}",
        (PY2026_YEAR,))


def build_monthly_sql(columns: dict) -> tuple[str, tuple]:
    update = _col(columns, LAST_UPDATE)
    return _checked(
        f"SELECT YEAR({update}) AS update_year, MONTH({update}) AS update_month, COUNT_BIG(*) AS row_count,\n"
        f"       COUNT_BIG(DISTINCT {_col(columns, POLICY)}) AS distinct_policies\n"
        f"FROM {_source()}\nWHERE {_year(columns)} AND {update} IS NOT NULL\n"
        f"GROUP BY YEAR({update}), MONTH({update})",
        (PY2026_YEAR,))


def build_oe_monthly_sql(columns: dict, field: str) -> tuple[str, tuple]:
    """Stage 7B monthly activity from OE_MONTHLY_START; earlier and null dates are counted separately."""
    value, person, policy = _col(columns, field), _col(columns, PERSON), _col(columns, POLICY)
    return _checked(
        f"SELECT YEAR({value}) AS activity_year, MONTH({value}) AS activity_month, COUNT_BIG(*) AS row_count,\n"
        f"       COUNT_BIG(DISTINCT {person}) AS distinct_people, COUNT_BIG(DISTINCT {policy}) AS distinct_policies\n"
        f"FROM {_source()}\nWHERE {_year(columns)} AND {value} >= ?\n"
        f"GROUP BY YEAR({value}), MONTH({value})",
        (PY2026_YEAR, OE_MONTHLY_START))


def build_oe_context_sql(columns: dict, field: str) -> tuple[str, tuple]:
    """Context bounds. Last-update dates never get an upper cutoff, so no before-window count for them."""
    value = _col(columns, field)
    before, params = "", ()
    if field in OE_CUMULATIVE_FIELDS:
        before, params = f"COUNT_BIG(CASE WHEN {value} < ? THEN 1 END) AS rows_before_window,\n       ", (
            OE_MONTHLY_START,)
    return _checked(
        f"SELECT {before}COUNT_BIG(CASE WHEN {value} IS NULL THEN 1 END) AS null_rows,\n"
        f"       MIN({value}) AS min_value, MAX({value}) AS max_value\n"
        f"FROM {_source()}\nWHERE {_year(columns)}",
        params + (PY2026_YEAR,))


def _next_day(text: str) -> str:
    return (date.fromisoformat(text) + timedelta(days=1)).isoformat()


def build_oe_cumulative_sql(columns: dict, field: str) -> tuple[str, tuple]:
    """Records whose create/confirmation date is on or before each cutoff (inclusive of the whole day)."""
    if field not in OE_CUMULATIVE_FIELDS:
        raise ValueError("Cumulative cutoffs use only create or confirmation dates")
    value, person, policy = _col(columns, field), _col(columns, PERSON), _col(columns, POLICY)
    selects, params = [], []
    for index, cutoff in enumerate(OE_CUTOFFS):
        selects += [f"COUNT_BIG(CASE WHEN {value} < ? THEN 1 END) AS rows_{index}",
                    f"COUNT_BIG(DISTINCT CASE WHEN {value} < ? THEN {person} END) AS people_{index}",
                    f"COUNT_BIG(DISTINCT CASE WHEN {value} < ? THEN {policy} END) AS policies_{index}"]
        params += [_next_day(cutoff)] * 3
    return _checked(
        f"SELECT {', '.join(selects)}\nFROM {_source()}\nWHERE {_year(columns)}",
        tuple(params) + (PY2026_YEAR,))


# --- interpretation ---------------------------------------------------------------

def fanout_summary(rows: list[dict] | None) -> dict | None:
    if rows is None:
        return None
    keys = sum(_int(row["key_count"]) or 0 for row in rows)
    links = sum(_int(row["link_count"]) or 0 for row in rows)
    buckets = {}
    for row in sorted(rows, key=lambda item: _int(item["bucket"])):
        label = f"{BUCKET_CAP}+" if _int(row["bucket"]) >= BUCKET_CAP else str(_int(row["bucket"]))
        buckets[label] = _int(row["key_count"])
    return {
        "keys": keys,
        "links": links,
        "mean": round(links / keys, 6) if keys else None,
        "max": max((_int(row["max_n"]) for row in rows), default=None),
        "buckets": buckets,
    }


def classify_mapping(profile: dict | None, mapping: dict | None) -> dict:
    if mapping is None:
        return {"status": None, "reason": "Mapping query did not run (N/A, not zero)."}
    m = {key: _int(value) for key, value in mapping.items()}
    nulls = (profile or {}).get("rows_proxy_without_exchange"), (profile or {}).get("rows_exchange_without_proxy")
    many_proxy, many_exchange = m["proxy_with_many_exchange"], m["exchange_with_many_proxy"]
    if many_proxy and many_exchange:
        status = MANY_TO_MANY
    elif many_proxy:
        status = ONE_PROXY_TO_MANY
    elif many_exchange:
        status = MANY_PROXY_TO_ONE
    elif m["one_to_one_pairs"] == m["proxy_keys"] == m["exchange_keys"]:
        status = ONE_TO_ONE if all(value == 0 for value in nulls) else ONE_TO_ONE_WITH_NULLS
    else:
        status = None
    return {"status": status, "counts": m, "reason": (
        "Aggregate mapping between the DB exchange person identifier and enrollee_id inside "
        "dbo.Enrollments_TEST. Equivalence to the Sisense identifier is not inferred.")}


def oe_monthly(rows: list[dict] | None) -> list[dict] | None:
    if rows is None:
        return None
    return [{"month": f"{_int(row['activity_year']):04d}-{_int(row['activity_month']):02d}",
             "row_count": _int(row["row_count"]), "distinct_people": _int(row["distinct_people"]),
             "distinct_policies": _int(row["distinct_policies"])}
            for row in sorted(rows, key=lambda item: (_int(item["activity_year"]), _int(item["activity_month"])))]


def oe_cumulative(row: dict | None, available: bool) -> list[dict] | None:
    if not available:
        return None
    return [{"through": cutoff,
             "row_count": None if row is None else _int(row[f"rows_{index}"]),
             "distinct_people": None if row is None else _int(row[f"people_{index}"]),
             "distinct_policies": None if row is None else _int(row[f"policies_{index}"])}
            for index, cutoff in enumerate(OE_CUTOFFS)]


def oe_temporal_section(evidence: dict) -> dict:
    oe = evidence.get("oe") or {}
    fields = {}
    for field in OE_MONTHLY_FIELDS:
        found = oe.get(field) or {}
        fields[field] = {
            "label": OE_LIFECYCLE_LABEL,
            "view": OE_ACTIVITY_VIEW,
            "monthly_from": OE_MONTHLY_START,
            "context": found.get("context"),
            "monthly": oe_monthly(found.get("monthly")),
            "cumulative": oe_cumulative(found.get("cumulative"), field in OE_CUMULATIVE_FIELDS
                                        and found.get("cumulative_ran", False)),
            "caveat": LAST_UPDATE_MONTHLY_CAVEAT if field == LAST_UPDATE else OE_ACTIVITY_CAVEAT,
            "production_comparable": False,
        }
    return {
        "label": OE_LIFECYCLE_LABEL,
        "view": OE_ACTIVITY_VIEW,
        "statement": OE_ACTIVITY_CAVEAT,
        "status_filter_applied": False,
        "cumulative_cutoffs": list(OE_CUTOFFS),
        "cumulative_fields": list(OE_CUMULATIVE_FIELDS),
        "fields": fields,
        "historical_state_reconstructable": False,
        "production_comparable": False,
    }


def november_2025_evidence(temporal: dict) -> bool | None:
    """True if create or confirmation activity exists in November 2025; None if neither query ran."""
    seen = None
    for field in OE_CUMULATIVE_FIELDS:
        monthly = temporal["fields"][field]["monthly"]
        if monthly is None:
            continue
        seen = bool(seen) or any(item["month"] == "2025-11" and item["row_count"] for item in monthly)
    return seen


def oe_lifecycle_alignment(november: bool | None) -> dict:
    return {
        "label": OE_LIFECYCLE_LABEL,
        "py2026_activity_period": "late 2025 / November 2025 onward",
        "py2027_observation_period": "November 2026",
        "statement": OE_ALIGNMENT_STATEMENT,
        "oe_alignment_status": OE_ANCHOR_UNPROVEN,
        "oe_start_anchor": {"PY2026": None, "PY2027": None},
        "anchor_evidence": "No proven official PY2026 or PY2027 OE start date exists in the repository. "
                           "No start date is assumed from where record dates cluster.",
        "historical_state_reconstructable": False,
        "november_2025_activity_observed": november,
        "preserve_for_next_phase": bool(november),
        "future_comparison_preference": "PY2026 OE Day N vs PY2027 OE Day N, rather than PY2026 Oct-31 latest "
                                        "state vs PY2027 early-November current state, where metric semantics "
                                        "allow lifecycle alignment.",
        "metric_applicability": {
            "population_metrics": "Currently Enrolled and other state metrics still require historical-state "
                                  "evidence; OE Day N alignment does not supply it.",
            "activity_metrics": "Cumulative counts by create or confirmation date may be defensible at OE Day N "
                                "once anchors and semantics are proven.",
        },
        "not_the_same_as": PLAN_YEAR_SHIFT_ASSUMPTION,
        "production_comparable": False,
    }


def candidate_result(candidate: dict, counts: dict | None, absent: set[str], effectuated: dict | None,
                     skip_reason: str | None) -> dict:
    unavailable = [name for name in candidate["unavailable_filter_fields"] if name in absent]
    present_not_applied = [name for name in candidate["unavailable_filter_fields"] if name not in absent]
    numerator = None if counts is None else _int(counts.get("effectuated_numerator_people"))
    denominator = None if counts is None else _int(counts.get("effectuated_denominator_people"))
    return {
        "candidate_id": candidate["candidate_id"],
        "label": DIAGNOSTIC_ONLY,
        "view": POPULATION_STATE_VIEW,
        "alignment_assumption": PLAN_YEAR_SHIFT_ASSUMPTION,
        "not_the_same_as": OE_LIFECYCLE_LABEL,
        "description": candidate["description"],
        "formula": candidate["formula"],
        "person_identifier": PERSON_PROXY,
        "distinct_people": None if counts is None else _int(counts.get("distinct_people")),
        "distinct_policies": None if counts is None else _int(counts.get("distinct_policies")),
        "row_count": None if counts is None else _int(counts.get("row_count")),
        "filters_applied": [
            condition_text(item) + (f" [{item['label']}]" if item.get("label") else "")
            for item in candidate["conditions"]],
        "filters_unavailable": unavailable,
        "filters_present_not_applied": present_not_applied,
        "assumptions": candidate["assumptions"],
        "skip_reason": skip_reason,
        "percent_effectuated": None if effectuated is None else {
            "label": DIAGNOSTIC_ONLY,
            "name": "PY2026 DB % Effectuated diagnostic (not the official PY2026 % Effectuated)",
            "person_identifier": PERSON_PROXY,
            "numerator": f"{effectuated['status_field']} IN {effectuated['numerator_members']}",
            "denominator": f"{effectuated['status_field']} IN {effectuated['denominator_members']}",
            "numerator_people": numerator,
            "denominator_people": denominator,
            "ratio": round(numerator / denominator, 6) if numerator is not None and denominator else None,
            "filters_unavailable": unavailable,
            "production_comparable": False,
        },
        "production_comparable": False,
    }


def population_result(definition: dict, counts: dict | None, status_rows: list[dict] | None,
                      skip_reason: str | None) -> dict:
    cond = next(item for item in definition["conditions"] if item["column"] == ENROLLMENT_STATUS)
    if status_rows is None:
        matched = None
    elif cond["op"] == "like":
        prefix = cond["values"][0].rstrip("%").lower()
        matched = [row["status_value"] for row in status_rows
                   if isinstance(row["status_value"], str) and row["status_value"].lower().startswith(prefix)]
    else:
        matched = [row["status_value"] for row in status_rows if row["status_value"] == cond["values"][0]]
    return {
        "label": definition["diagnostic_label"],
        "formula": "COUNT(DISTINCT enrollment_id) WHERE " + " AND ".join(
            condition_text(item) for item in definition["conditions"]),
        "distinct_policies": None if counts is None else _int(counts.get("distinct_policies")),
        "distinct_people": None if counts is None else _int(counts.get("distinct_people")),
        "row_count": None if counts is None else _int(counts.get("row_count")),
        "status_values_matched": matched,
        "skip_reason": skip_reason,
        "not_equivalent_to": ("Sisense Cancelled Policies daily activity" if "CANCELLED" in definition["diagnostic_label"]
                              else "Sisense Terminated Policies daily activity"),
        "interpretation": f"Latest-state population in the {DATASET_LABEL}; not daily activity.",
        "production_comparable": False,
    }


def decide(columns_present: set[str] | None, mapping: dict, absent: set[str]) -> dict:
    exchange_one_to_one = mapping.get("status") == ONE_TO_ONE
    blockers = [f"{DATASET_LABEL} vs PY2027 read during Open Enrollment: lifecycle point not aligned.",
                f"{PLAN_YEAR_SHIFT_ASSUMPTION}: shifted benefit window is not proven equivalent.",
                "ssap_applications.coverage_year vs DB coverage_year: same name, unproven same meaning."]
    blockers += [f"Sisense filter field {name} absent from dbo.Enrollments_TEST." for name in sorted(absent)
                 if name != EXCHANGE]
    if columns_present is None:
        ce_class = None
        identifier = "N/A: column metadata query did not run."
    elif exchange_one_to_one:
        ce_class = DIAGNOSTIC
        identifier = ("DB exchg_indiv_identifier maps one-to-one to enrollee_id with no nulls. This supports the "
                      "proxy inside the DB; equivalence to the Sisense identifier is still not proven.")
    else:
        ce_class = BLOCKED_IDENTIFIER
        identifier = (EXCHANGE_NOT_AVAILABLE if EXCHANGE in absent else
                      f"DB exchange identifier mapping is {mapping.get('status') or 'N/A'}; not one-to-one.")
        blockers.insert(0, "Person identifier equivalence unproven.")
    return {
        "currently_enrolled": {"classification": ce_class, "identifier_evidence": identifier,
                               "remaining_blockers": blockers, "production_comparable": False},
        "percent_effectuated": {"classification": ce_class, "remaining_blockers": blockers,
                                "note": "Diagnostic only; not the official PY2026 % Effectuated.",
                                "production_comparable": False},
        "cancelled_population": {"classification": DIAGNOSTIC, "production_comparable": False},
        "terminated_population": {"classification": DIAGNOSTIC, "production_comparable": False},
        "cancelled_policies_daily": {"classification": BLOCKED_EVENT_HISTORY, "production_comparable": False},
        "terminated_policies_daily": {"classification": BLOCKED_EVENT_HISTORY, "production_comparable": False},
        "oe_temporal_activity": {"classification": DIAGNOSTIC, "view": OE_ACTIVITY_VIEW,
                                 "oe_alignment_status": OE_ANCHOR_UNPROVEN,
                                 "historical_state_reconstructable": False, "production_comparable": False},
        "new_active_passive": {"classification": BLOCKED_LIFECYCLE, "queried": False, "production_comparable": False},
        "application_metrics": {"classification": WRONG_SOURCE_FAMILY, "queried": False,
                                "production_comparable": False},
        "promotion": "None. No metric is promoted in Phase 6B.",
    }


# --- collection ---------------------------------------------------------------------

def collect(client, definition: dict | None) -> dict:
    failures: list[dict] = []
    skipped: list[dict] = []
    result = _run(client, failures, "columns", *_checked(COLUMNS_SQL, (SCHEMA, TABLE)))
    columns = None if result is None else {
        str(name).lower(): {"name": str(name), "data_type": str(dtype or "")} for name, dtype in result.rows}

    def query(step, builder):
        if columns is None:
            skipped.append({"step": step, "reason": "column metadata unavailable"})
            return None
        try:
            sql, params = builder()
        except MissingColumn as exc:
            skipped.append({"step": step, "reason": f"column {exc} absent from dbo.Enrollments_TEST"})
            return None
        except QueryExecutionError as exc:
            failures.append({"step": step, "error": str(exc)})
            return None
        return _run(client, failures, step, sql, params)

    def rows(step, builder):
        found = query(step, builder)
        return None if found is None else [_clean(_row(found, index)) for index in range(len(found.rows))]

    def one(step, builder):
        found = rows(step, builder)
        return found[0] if found else None

    status = {field: rows(f"status {field}", lambda field=field: build_status_sql(columns, field))
              for field in (ENROLLMENT_STATUS, ENROLLEE_STATUS)}
    identity = one("identity", lambda: build_identity_sql(columns))
    per_person = rows("policies per person", lambda: build_fanout_sql(columns, PERSON, POLICY))
    per_policy = rows("persons per policy", lambda: build_fanout_sql(columns, POLICY, PERSON))
    exchange_profile = exchange_mapping = None
    if columns is not None and EXCHANGE in columns:
        exchange_profile = one("exchange profile", lambda: build_exchange_profile_sql(columns))
        exchange_mapping = one("exchange mapping", lambda: build_exchange_mapping_sql(columns))
    effectuated = (definition or {}).get("effectuated")
    candidates = []
    for candidate in currently_enrolled_candidates(definition):
        before = len(skipped)
        counts = one(f"candidate {candidate['candidate_id']}",
                     lambda candidate=candidate: build_candidate_sql(columns, candidate, effectuated))
        reason = skipped[-1]["reason"] if len(skipped) > before else None
        candidates.append((candidate, counts, reason))
    populations = []
    for item in population_definitions():
        before = len(skipped)
        counts = one(item["diagnostic_label"], lambda item=item: build_population_sql(columns, item))
        populations.append((item, counts, skipped[-1]["reason"] if len(skipped) > before else None))
    temporal = one("temporal", lambda: build_temporal_sql(columns))
    monthly = rows("monthly last update", lambda: build_monthly_sql(columns))
    oe = {}
    for field in OE_MONTHLY_FIELDS:
        oe[field] = {
            "monthly": rows(f"oe monthly {field}", lambda field=field: build_oe_monthly_sql(columns, field)),
            "context": one(f"oe context {field}", lambda field=field: build_oe_context_sql(columns, field)),
        }
        if field in OE_CUMULATIVE_FIELDS:
            oe[field]["cumulative"] = one(f"oe cumulative {field}",
                                          lambda field=field: build_oe_cumulative_sql(columns, field))
            oe[field]["cumulative_ran"] = columns is not None and field in columns
    return {
        "oe": oe,
        "columns": columns, "status": status, "identity": identity, "per_person": per_person,
        "per_policy": per_policy, "exchange_profile": exchange_profile, "exchange_mapping": exchange_mapping,
        "effectuated": effectuated, "candidates": candidates, "populations": populations,
        "temporal": temporal, "monthly": monthly, "definition_loaded": definition is not None,
        "failures": failures, "skipped": skipped,
    }


def build_report(evidence: dict) -> dict:
    columns = evidence.get("columns")
    absent = set() if columns is None else {name for name in REQUESTED_COLUMNS + PROVEN_COLUMNS
                                            if name not in columns}
    availability = [{
        "column": name,
        "role": "requested_sisense_field" if name in REQUESTED_COLUMNS else "proven_field",
        "exists": None if columns is None else name in columns,
        "data_type": None if columns is None or name not in columns else columns[name]["data_type"],
    } for name in REQUESTED_COLUMNS + PROVEN_COLUMNS]
    mapping = classify_mapping(evidence.get("exchange_profile"), evidence.get("exchange_mapping"))
    if columns is not None and EXCHANGE in absent:
        exchange = {"status": EXCHANGE_NOT_AVAILABLE, "profile": None, "mapping": None}
    else:
        exchange = {"status": mapping.get("status"), "profile": evidence.get("exchange_profile"),
                    "mapping": mapping}
    status_rows = evidence.get("status") or {}
    effectuated = evidence.get("effectuated")
    candidates = [candidate_result(c, counts, absent, effectuated, reason)
                  for c, counts, reason in evidence.get("candidates") or []]
    populations = [population_result(item, counts, status_rows.get(ENROLLMENT_STATUS), reason)
                   for item, counts, reason in evidence.get("populations") or []]
    monthly = evidence.get("monthly")
    monthly_rows = None if monthly is None else [
        {"month": f"{_int(row['update_year']):04d}-{_int(row['update_month']):02d}",
         "row_count": _int(row["row_count"]), "distinct_policies": _int(row["distinct_policies"])}
        for row in sorted(monthly, key=lambda item: (_int(item["update_year"]), _int(item["update_month"])))]
    identity = evidence.get("identity")
    per_person = fanout_summary(evidence.get("per_person"))
    oe_temporal = oe_temporal_section(evidence)
    report = {
        "report_kind": "PY2026_POPULATION_DIAGNOSTICS",
        "phase": "6B",
        "source": {
            "table": f"{SCHEMA}.{TABLE}",
            "description": DATASET_LABEL,
            "equivalence": "Equivalent, for the tested PY2026 records and business fields, to "
                           "dbo.Enrollments_PY2526_10312026 (Phase 5D).",
            "filter": f"coverage_year = {PY2026_YEAR}",
            "py2027": "Current Sisense OE Executive Dashboard; not queried here.",
        },
        "column_availability": availability,
        "status_distributions": {
            "enrollment_status": status_rows.get(ENROLLMENT_STATUS),
            "enrollee_status": status_rows.get(ENROLLEE_STATUS),
        },
        "identifier_behavior": {
            "person_identifier": PERSON_PROXY,
            "row_count": None if identity is None else _int(identity.get("row_count")),
            "distinct_people": None if identity is None else _int(identity.get("distinct_people")),
            "distinct_policies": None if identity is None else _int(identity.get("distinct_policies")),
            "distinct_policy_person_pairs": None if per_person is None else per_person["links"],
            "null_person_rows": None if identity is None else _int(identity.get("null_person_rows")),
            "null_policy_rows": None if identity is None else _int(identity.get("null_policy_rows")),
            "policies_per_person": per_person,
            "persons_per_policy": fanout_summary(evidence.get("per_policy")),
            "exchange_person_identifier": exchange,
        },
        "currently_enrolled_candidates": candidates,
        "percent_effectuated": {
            "label": DIAGNOSTIC_ONLY,
            "semantics_source": "Phase 6A bridge (verified Currently Enrolled payload, formula order)",
            "per_candidate": [{"candidate_id": c["candidate_id"], **c["percent_effectuated"]}
                              for c in candidates if c["percent_effectuated"]],
            "production_comparable": False,
        },
        "population_diagnostics": populations,
        "temporal_diagnostics": {
            "statement": MONTHLY_STATEMENT,
            "bounds": evidence.get("temporal"),
            "last_update_by_month": monthly_rows,
            "daily_trend": "Not constructed.",
        },
        "oe_aligned_temporal_diagnostics": oe_temporal,
        "oe_lifecycle_alignment": oe_lifecycle_alignment(november_2025_evidence(oe_temporal)),
        "remaining_semantic_gaps": remaining_gaps(absent, columns is not None, evidence.get("definition_loaded")),
        "phase_6b_decision": decide(None if columns is None else set(columns), mapping, absent),
        "production_comparable": False,
        "failures": evidence.get("failures") or [],
        "skipped": evidence.get("skipped") or [],
        "command": COMMAND,
    }
    guard_no_pii(report)
    return report


def remaining_gaps(absent: set[str], metadata_read: bool, definition_loaded: bool | None) -> list[str]:
    gaps = []
    if not definition_loaded:
        gaps.append("Verified Currently Enrolled payload not loaded; CE1-CE4 and % Effectuated not evaluated.")
    if not metadata_read:
        gaps.append("Column metadata unavailable; every result is N/A.")
    for name in sorted(absent):
        gaps.append(EXCHANGE_NOT_AVAILABLE if name == EXCHANGE else
                    f"{name} absent: the Sisense filter on it cannot be represented.")
    gaps += [
        "Person identifier: enrollee_id is a DB proxy; equivalence to enrollee.exchg_indiv_identifier is unproven.",
        f"{PLAN_YEAR_SHIFT_ASSUMPTION}: the benefit window shifted back one year is not proven equivalent "
        "to the PY2027 window.",
        f"Temporal: {DATASET_LABEL} vs PY2027 read during Open Enrollment.",
        "Status-filter interpretation: CE1-CE4 remain competing; none is selected.",
        f"{OE_ANCHOR_UNPROVEN}: no proven official PY2026/PY2027 OE start date; OE Day N alignment unavailable.",
        "Historical state is not reconstructable from the latest-state dataset; create/confirmation-date "
        "activity is not a historical enrolled population.",
        "Cancelled/Terminated daily activity remains BLOCKED_EVENT_HISTORY.",
    ]
    return gaps


# --- output -------------------------------------------------------------------------

def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def _e(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (list, tuple)):
        return "<br>".join(_e(item) for item in value) or "none"
    if isinstance(value, dict):
        return "<br>".join(f"{html.escape(str(k))}: {_e(v)}" for k, v in value.items()) or "none"
    return html.escape(str(value))


def _table(rows: list[dict] | None, columns: tuple[str, ...]) -> str:
    if rows is None:
        return "<p>N/A (query did not run).</p>"
    head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
    body = "".join("<tr>" + "".join(f"<td>{_e(row.get(name))}</td>" for name in columns) + "</tr>" for row in rows)
    return f"<table border='1' cellpadding='4'><tr>{head}</tr>{body}</table>"


def render_html(report: dict) -> str:
    source = report["source"]
    ident = report["identifier_behavior"]
    status = report["status_distributions"]
    status_cols = ("status_value", "row_count", "distinct_people", "distinct_policies")
    candidate_cols = ("candidate_id", "label", "view", "alignment_assumption", "distinct_people", "distinct_policies", "row_count",
                      "filters_applied", "filters_unavailable", "filters_present_not_applied", "assumptions")
    effectuated_cols = ("candidate_id", "label", "numerator", "denominator", "numerator_people",
                        "denominator_people", "ratio", "filters_unavailable")
    population_cols = ("label", "formula", "distinct_policies", "distinct_people", "row_count",
                       "status_values_matched", "not_equivalent_to")
    temporal = report["temporal_diagnostics"]
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>PY2026 population diagnostics</title></head>"
        "<body><h1>PY2026 Population Diagnostics (Phase 6B)</h1>"
        "<p><b>production_comparable = false</b> for every output. All results are DIAGNOSTIC_ONLY.</p>"
        f"<h2>Source</h2><p>{_e(source['table'])}: {_e(source['description'])}. {_e(source['equivalence'])} "
        f"Filter: {_e(source['filter'])}.</p>"
        "<h2>Column Availability</h2>"
        + _table(report["column_availability"], ("column", "role", "exists", "data_type"))
        + "<h2>Status Distributions</h2><h3>enrollment_status_description</h3>"
        + _table(status["enrollment_status"], status_cols)
        + "<h3>enrollee_status_description</h3>" + _table(status["enrollee_status"], status_cols)
        + "<h2>Identifier Behavior</h2>"
        + f"<p>{_e({k: v for k, v in ident.items() if k != 'exchange_person_identifier'})}</p>"
        + f"<p>Exchange person identifier: {_e(ident['exchange_person_identifier'])}</p>"
        + "<h2>Currently Enrolled Candidates</h2>"
        + _table(report["currently_enrolled_candidates"], candidate_cols)
        + "<h2>Percent Effectuated Diagnostic</h2>"
        + _table(report["percent_effectuated"]["per_candidate"], effectuated_cols)
        + "<h2>Cancellation / Termination Population Diagnostics</h2>"
        + _table(report["population_diagnostics"], population_cols)
        + f"<h2>Temporal Diagnostics</h2><p><b>{_e(temporal['statement'])}</b></p>"
        + f"<p>{_e(temporal['bounds'])}</p>"
        + _table(temporal["last_update_by_month"], ("month", "row_count", "distinct_policies"))
        + render_oe_html(report)
        + f"<h2>Remaining Semantic Gaps</h2><ul>{''.join(f'<li>{_e(g)}</li>' for g in report['remaining_semantic_gaps'])}</ul>"
        + f"<h2>Phase 6B Decision</h2><p>{_e(report['phase_6b_decision'])}</p>"
        + "<p>N/A means a query did not run or a column is absent; it is never zero.</p></body></html>"
    )


def render_oe_html(report: dict) -> str:
    temporal, alignment = report["oe_aligned_temporal_diagnostics"], report["oe_lifecycle_alignment"]
    count_cols = ("row_count", "distinct_people", "distinct_policies")
    parts = [f"<h2>OE-Aligned Temporal Diagnostics</h2><p><b>{_e(temporal['label'])}</b>: "
             f"{_e(temporal['statement'])} historical_state_reconstructable = false.</p>"]
    for field, item in temporal["fields"].items():
        parts.append(f"<h3>{_e(field)}</h3><p>{_e(item['caveat'])}</p><p>Context: {_e(item['context'])}</p>"
                     + _table(item["monthly"], ("month",) + count_cols))
        if field in temporal["cumulative_fields"]:
            parts.append("<p>Cumulative through cutoff (diagnostic cohort; not Currently Enrolled):</p>"
                         + _table(item["cumulative"], ("through",) + count_cols))
    parts.append(f"<h2>OE Lifecycle Alignment</h2><p><b>{_e(alignment['statement'])}</b></p>"
                 f"<p>{_e({k: v for k, v in alignment.items() if k != 'statement'})}</p>")
    return "".join(parts)


def print_summary(report: dict) -> None:
    print("PY2026 POPULATION DIAGNOSTICS (dbo.Enrollments_TEST, coverage_year = 2026, aggregates only)")
    for item in report["column_availability"]:
        print(f"  column {item['column']}: exists={_text(item['exists'])}")
    ident = report["identifier_behavior"]
    print(f"Rows={_text(ident['row_count'])} people={_text(ident['distinct_people'])} "
          f"policies={_text(ident['distinct_policies'])} pairs={_text(ident['distinct_policy_person_pairs'])} "
          f"exchange_identifier={_text(ident['exchange_person_identifier']['status'])}")
    for c in report["currently_enrolled_candidates"]:
        pe = c["percent_effectuated"] or {}
        print(f"{c['candidate_id']} [{c['label']}] people={_text(c['distinct_people'])} "
              f"policies={_text(c['distinct_policies'])} rows={_text(c['row_count'])} "
              f"effectuated={_text(pe.get('numerator_people'))}/{_text(pe.get('denominator_people'))}="
              f"{_text(pe.get('ratio'))} unavailable={c['filters_unavailable']}")
    for p in report["population_diagnostics"]:
        print(f"{p['label']}: policies={_text(p['distinct_policies'])} people={_text(p['distinct_people'])}")
    print(MONTHLY_STATEMENT)
    for field in OE_CUMULATIVE_FIELDS:
        for item in report["oe_aligned_temporal_diagnostics"]["fields"][field]["cumulative"] or []:
            print(f"{OE_LIFECYCLE_LABEL} {field} through {item['through']}: rows={_text(item['row_count'])} "
                  f"people={_text(item['distinct_people'])} policies={_text(item['distinct_policies'])}")
    print(f"oe_alignment_status={report['oe_lifecycle_alignment']['oe_alignment_status']} "
          "historical_state_reconstructable=false")
    decision = report["phase_6b_decision"]
    print(f"Currently Enrolled: {_text(decision['currently_enrolled']['classification'])} "
          "(production_comparable=false)")
    for failure in report["failures"]:
        print(f"Failure: {failure['step']}: {failure['error']}")
    for item in report["skipped"]:
        print(f"Skipped: {item['step']}: {item['reason']}")


def _run(client, failures, step, sql, params):
    assert_read_only(sql)
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


def _row(result, index: int = 0) -> dict:
    return {str(column): value for column, value in zip(result.columns, result.rows[index])}


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


def retrieve_live() -> tuple[dict, bool]:
    """(report, connected). One read-only aggregate pass over the approved connection layer."""
    definition = load_definition()
    evidence = {"failures": [], "skipped": [], "definition_loaded": definition is not None}
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            evidence = collect(DatabaseClient(connection), definition)
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    return build_report(evidence), evidence.get("columns") is not None


def main() -> int:
    report, _ = retrieve_live()
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
