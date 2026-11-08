"""First PY2026 vs PY2027 Currently Enrolled People gate.

The verified Sisense payload is read locally and interpreted. No JAQL is sent,
the payload file is not rewritten, and no enrollment table is queried.

A numeric comparison is produced only when the period, the person identifier,
and every population-critical filter are EXACT or EQUIVALENT and a PY2026
snapshot has been named by that evidence. The verified payload does not meet
that bar, so the command writes ``COMPARISON_PERIOD_UNRESOLVED`` and stops.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIED_PAYLOAD_FILE = (
    PROJECT_ROOT / "sisense" / "payloads" / "currently_enrolled.verified.json"
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "comparison"
JSON_NAME = "py2026_vs_py2027_currently_enrolled.json"
HTML_NAME = "py2026_vs_py2027_currently_enrolled.html"

PERIOD_UNRESOLVED = "COMPARISON_PERIOD_UNRESOLVED"
PERSON_MISMATCH = "PERSON_IDENTIFIER_MISMATCH"
FILTER_BLOCKED = "FILTER_MAPPING_BLOCKED"
PY2026_PERIOD_UNRESOLVED = "PY2026_PERIOD_UNRESOLVED"
PERSON_UNRESOLVED = "PERSON_IDENTIFIER_UNRESOLVED"
READY = "READY"

EXACT = "EXACT"
EQUIVALENT = "EQUIVALENT"
NOT_AVAILABLE = "NOT_AVAILABLE"
UNCERTAIN = "UNCERTAIN"
ACCEPTABLE = {EXACT, EQUIVALENT}

PEOPLE_MEASURE_TITLE = "Currently Enrolled People"
WIDGET_ID = "6a98048ccef60720fe7f9b47"

# Named in the phase request. A filename date is not a selection rule.
PY2026_SNAPSHOT_CANDIDATES = (
    "dbo.Enrollments_PY2526_01312026",
    "dbo.Enrollments_PY2526_02272026",
    "dbo.Enrollments_PY2526_10312026",
)

PROHIBITED_OUTPUT_KEYS = {
    "enrollee_id",
    "enrollment_id",
    "exchg_indiv_identifier",
    "ssn",
    "social_security_number",
    "email",
    "email_address",
    "birth_date",
    "dob",
    "date_of_birth",
    "first_name",
    "last_name",
    "address",
    "street_address",
}

_STATUS_COLUMNS = ("enrollment_status_description", "enrollee_status_description")


class ComparisonOutputError(ValueError):
    """The comparison artifact would contain an individual identifier field."""


@dataclass(frozen=True)
class PeriodAssessment:
    """What the verified request proves, and whether a snapshot may be chosen."""

    status: str
    evidenced_population: str
    evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    equivalent_snapshot: str | None = None


def interpret_payload(payload: dict) -> dict:
    """Read the verified JAQL structure. The payload object is not modified."""
    measure = _people_measure(payload)
    scope = _scope_filters(payload)
    widget_id, widget_title = _split_label(str(payload.get("widget") or ""))
    dashboard_id, dashboard_title = _split_label(str(payload.get("dashboard") or ""))
    datasource = payload.get("datasource") or {}
    return {
        "dashboard_id": dashboard_id,
        "dashboard_title": dashboard_title,
        "widget_id": widget_id,
        "widget_title": widget_title,
        "widget_type": payload.get("widgetType"),
        "datasource_title": datasource.get("title"),
        "datasource_live": datasource.get("live"),
        "measure": measure,
        "scope_filters": scope,
        "secondary_measure_filters": _secondary_measure_filters(payload),
    }


def classify_period(definition: dict) -> PeriodAssessment:
    """Classify the comparison period from the verified request alone."""
    scope = definition["scope_filters"]
    coverage = _by_column(scope, "coverage_year")
    benefit = _by_column(scope, "benefit_effective_date")
    evidence = _evidence_lines(definition, coverage, benefit)
    missing = _missing_evidence(definition, coverage, benefit)
    population = _population_sentence(definition, coverage, benefit)
    return PeriodAssessment(
        status=PERIOD_UNRESOLVED,
        evidenced_population=population,
        evidence=tuple(evidence),
        missing_evidence=tuple(missing),
        equivalent_snapshot=None,
    )


def select_py2026_snapshot(period: PeriodAssessment) -> dict:
    """Choose a snapshot only when the period assessment already names one.

    The table name is never parsed for a month. An unresolved period selects
    nothing, including when a caller has placed a candidate name on the object.
    """
    refused = [
        {
            "table": name,
            "selected": False,
            "reason": "The filename date is not evidence of an equivalent period.",
        }
        for name in PY2026_SNAPSHOT_CANDIDATES
    ]
    named = period.equivalent_snapshot
    if period.status == PERIOD_UNRESOLVED or named is None:
        return {
            "selected": None,
            "reason": (
                "No PY2026 snapshot is selected. The verified request does not "
                "establish a lifecycle point that names one of the historical tables."
            ),
            "candidates_not_selected": refused,
        }
    if named not in PY2026_SNAPSHOT_CANDIDATES:
        return {
            "selected": None,
            "reason": "The evidenced snapshot is not one of the named historical candidates.",
            "candidates_not_selected": refused,
        }
    chosen = []
    for item in refused:
        if item["table"] == named:
            chosen.append({
                "table": named,
                "selected": True,
                "reason": "The period assessment names this snapshot.",
            })
        else:
            chosen.append(item)
    return {
        "selected": named,
        "reason": "The period assessment names this snapshot.",
        "candidates_not_selected": [item for item in chosen if not item["selected"]],
    }


def map_person_identifier(definition: dict, db_columns: set[str] | None) -> dict:
    """Map the Sisense person field onto a snapshot column when the schema was read."""
    field = definition["measure"]["fields"][0]
    base = {
        "sisense_table": field["table"],
        "sisense_column": field["column"],
        "sisense_dim": field["dim"],
        "datatype": field["datatype"],
        "aggregation": definition["measure"]["aggregation"],
        "column_title": definition["measure"]["column_title"],
    }
    if db_columns is None:
        return {
            **base,
            "py2026_db_column": None,
            "translation_status": UNCERTAIN,
            "identity_status": PERSON_UNRESOLVED,
            "reason": (
                "The PY2026 snapshot schema was not read. Comparison stopped at "
                f"{PERIOD_UNRESOLVED}."
            ),
        }
    if field["column"] in db_columns:
        return {
            **base,
            "py2026_db_column": field["column"],
            "translation_status": EXACT,
            "identity_status": EXACT,
            "reason": "The snapshot column name matches the Sisense person field.",
        }
    if "enrollee_id" in db_columns:
        return {
            **base,
            "py2026_db_column": "enrollee_id",
            "translation_status": UNCERTAIN,
            "identity_status": PERSON_MISMATCH,
            "reason": (
                "The snapshot has enrollee_id and does not have "
                f"{field['column']}. Those names are not treated as one identifier."
            ),
        }
    return {
        **base,
        "py2026_db_column": None,
        "translation_status": NOT_AVAILABLE,
        "identity_status": PERSON_UNRESOLVED,
        "reason": f"The snapshot schema has no {field['column']} column.",
    }


def translate_filters(definition: dict, db_columns: set[str] | None) -> list[dict]:
    """Map each population filter. Date bounds are not shifted to another plan year."""
    scope = definition["scope_filters"]
    rows = [
        _coverage_row(scope, db_columns),
        _benefit_row(scope, db_columns),
        _status_row(scope, "enrollment_status_description", "Enrollment status", db_columns),
        _status_row(scope, "enrollee_status_description", "Enrollee status", db_columns),
        _member_row(
            scope, "person_type_description", "Person type",
            "enrollee.person_type_description", db_columns,
        ),
        _member_row(
            scope, "insurance_type_description", "Insurance type",
            "enrollment.insurance_type_description", db_columns,
        ),
        _absence_row(
            "Application type",
            "The verified people count has no application_type filter, so the translation adds no application_type predicate.",
        ),
        _absence_row(
            "Issuer",
            "The verified people count has no issuer filter, so the total adds no issuer predicate.",
        ),
    ]
    return rows


def issuer_readiness(definition: dict, db_columns: set[str] | None) -> dict:
    """The approved people widget has no issuer breakout, so grouping is not ready."""
    issuer_filter = _by_column(definition["scope_filters"], "hios_issuer_id")
    return {
        "issuer_comparison_ready": False,
        "sisense_dimension": None if issuer_filter is None else "enrollment.hios_issuer_id",
        "database_dimension": "hios_issuer_id" if db_columns and "hios_issuer_id" in db_columns else None,
        "reason": (
            "The verified Currently Enrolled People request is an indicator with no "
            "issuer dimension. The Issuers module is pending and has no verified JAQL. "
            "A later issuer comparison needs an approved Sisense breakout of this same population."
        ),
    }


def comparison_status(period: PeriodAssessment, person: dict, mappings: list[dict]) -> str:
    """Fail closed. The period gate is first and stops the later gates."""
    if period.status == PERIOD_UNRESOLVED:
        return PERIOD_UNRESOLVED
    if person.get("identity_status") == PERSON_MISMATCH:
        return PERSON_MISMATCH
    if person.get("translation_status") not in ACCEPTABLE:
        return PERSON_UNRESOLVED
    if any(row["population_critical"] and row["translation_status"] not in ACCEPTABLE for row in mappings):
        return FILTER_BLOCKED
    if period.equivalent_snapshot is None:
        return PY2026_PERIOD_UNRESOLVED
    return READY


def percent_change(baseline: int | None, current: int | None) -> dict:
    """PY2027 - PY2026, then that difference divided by PY2026.

    A missing input is not treated as zero. A zero PY2026 baseline leaves the
    percent unset.
    """
    if baseline is None or current is None:
        return {
            "absolute_difference": None,
            "percent_change": None,
            "percent_status": "NOT_COMPUTED",
        }
    absolute = current - baseline
    if baseline == 0:
        return {
            "absolute_difference": absolute,
            "percent_change": None,
            "percent_status": "ZERO_DENOMINATOR",
        }
    ratio = (Decimal(current) - Decimal(baseline)) / Decimal(baseline) * Decimal(100)
    quantized = ratio.quantize(Decimal("0.0001"))
    number: int | str = int(quantized) if quantized == quantized.to_integral() else format(quantized, "f")
    return {
        "absolute_difference": absolute,
        "percent_change": number,
        "percent_status": "COMPUTED",
    }


def apply_counts(status: str, *, py2026_people: int | None, py2027_people: int | None,
                 raw_row_count: int | None) -> dict:
    """Attach totals only for a ready comparison. Raw row counts are never the metric."""
    refused = []
    if raw_row_count is not None:
        refused.append("raw_row_count")
    if status != READY:
        if py2026_people is not None:
            refused.append("py2026_people")
        if py2027_people is not None:
            refused.append("py2027_people")
        return {
            "py2026_currently_enrolled_people": None,
            "py2027_currently_enrolled_people": None,
            "absolute_difference": None,
            "percent_change": None,
            "percent_status": "NOT_COMPUTED",
            "refused_inputs": refused,
        }
    change = percent_change(py2026_people, py2027_people)
    return {
        "py2026_currently_enrolled_people": py2026_people,
        "py2027_currently_enrolled_people": py2027_people,
        "absolute_difference": change["absolute_difference"],
        "percent_change": change["percent_change"],
        "percent_status": change["percent_status"],
        "refused_inputs": refused,
    }


def build_report(payload: dict, *, db_columns: set[str] | None = None,
                 py2026_people: int | None = None, py2027_people: int | None = None,
                 raw_row_count: int | None = None) -> dict:
    """Build the comparison artifact. Counts passed in are ignored unless the gate is ready."""
    definition = interpret_payload(payload)
    period = classify_period(definition)
    person = map_person_identifier(definition, db_columns)
    mappings = translate_filters(definition, db_columns)
    status = comparison_status(period, person, mappings)
    counts = apply_counts(
        status,
        py2026_people=py2026_people,
        py2027_people=py2027_people,
        raw_row_count=raw_row_count,
    )
    source = select_py2026_snapshot(period)
    report = {
        "comparison_status": status,
        "comparison_executed": (
            status == READY
            and counts["py2026_currently_enrolled_people"] is not None
            and counts["py2027_currently_enrolled_people"] is not None
        ),
        "py2027_sisense_definition": {
            "dashboard_title": definition["dashboard_title"],
            "dashboard_id": definition["dashboard_id"],
            "widget_title": definition["widget_title"],
            "widget_id": definition["widget_id"],
            "widget_type": definition["widget_type"],
            "datasource_title": definition["datasource_title"],
            "datasource_live": definition["datasource_live"],
            "person_count": {
                "aggregation": definition["measure"]["aggregation"],
                "column_title": definition["measure"]["column_title"],
                "table": person["sisense_table"],
                "column": person["sisense_column"],
                "dim": person["sisense_dim"],
                "datatype": person["datatype"],
            },
            "scope_filters": definition["scope_filters"],
            "secondary_measure_filters_not_used_for_this_count": definition["secondary_measure_filters"],
        },
        "comparison_period": {
            "status": period.status,
            "evidenced_population": period.evidenced_population,
            "evidence": list(period.evidence),
            "missing_evidence": list(period.missing_evidence),
        },
        "py2026_source": source,
        "person_identifier": {
            "sisense_table": person["sisense_table"],
            "sisense_column": person["sisense_column"],
            "aggregation": person["aggregation"],
            "py2026_db_column": person["py2026_db_column"],
            "translation_status": person["translation_status"],
            "identity_status": person["identity_status"],
            "reason": person["reason"],
        },
        "filter_mapping": mappings,
        "py2026_currently_enrolled_people": counts["py2026_currently_enrolled_people"],
        "py2027_currently_enrolled_people": counts["py2027_currently_enrolled_people"],
        "absolute_difference": counts["absolute_difference"],
        "percent_change": counts["percent_change"],
        "percent_status": counts["percent_status"],
        "refused_inputs": counts["refused_inputs"],
        "warnings": _warnings(period, person, mappings, counts),
        "issuer_comparison_ready": False,
        "issuer_dimensions": issuer_readiness(definition, db_columns),
        "py2027_retrieval": (
            "The approved Sisense retrieval is python main.py. "
            "This comparison did not call it."
        ),
    }
    guard_no_pii(report)
    return report


def guard_no_pii(value) -> None:
    """Reject identifier field names and binary values. Column-name strings may remain."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PROHIBITED_OUTPUT_KEYS:
                raise ComparisonOutputError(f"Comparison output refuses field {key}")
            guard_no_pii(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            guard_no_pii(item)
    elif isinstance(value, (bytes, bytearray)):
        raise ComparisonOutputError("Comparison output refuses binary values")


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def render_html(report: dict) -> str:
    period = report["comparison_period"]
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['sisense_concept']))}</td>"
        f"<td>{html.escape(str(item['sisense_source']))}</td>"
        f"<td>{html.escape(str(item['py2026_db_column']))}</td>"
        f"<td>{html.escape(str(item['translation_status']))}</td>"
        f"<td>{html.escape(str(item['reason']))}</td>"
        "</tr>"
        for item in report["filter_mapping"]
    )
    missing = "".join(f"<li>{html.escape(item)}</li>" for item in period["missing_evidence"])
    evidence = "".join(f"<li>{html.escape(item)}</li>" for item in period["evidence"])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>PY2026 vs PY2027 Currently Enrolled People</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; color: #1e293b; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #d8e0ea; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }}
th {{ background: #f4f7fb; }}
</style></head><body>
<h1>PY2026 vs PY2027 Currently Enrolled People</h1>
<p>Status: {html.escape(str(report["comparison_status"]))}</p>
<p>{html.escape(period["evidenced_population"])}</p>
<h2>Evidence</h2><ul>{evidence}</ul>
<h2>Evidence still required</h2><ul>{missing}</ul>
<h2>Filter mapping</h2>
<table><thead><tr><th>Sisense concept</th><th>Sisense source</th><th>PY2026 column</th><th>Status</th><th>Reason</th></tr></thead>
<tbody>{rows}</tbody></table>
<p>Issuer comparison ready: {html.escape(str(report["issuer_comparison_ready"]))}</p>
<p>PY2026 Currently Enrolled People: {html.escape(str(report["py2026_currently_enrolled_people"]))}</p>
<p>PY2027 Currently Enrolled People: {html.escape(str(report["py2027_currently_enrolled_people"]))}</p>
</body></html>
"""


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    report = build_report(payload)
    json_path, html_path = write_report(report)
    print(f"Comparison status: {report['comparison_status']}")
    print("PY2026 Currently Enrolled People: not retrieved")
    print("PY2027 Currently Enrolled People: not retrieved")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0 if report["comparison_status"] == READY else 2


def _people_measure(payload: dict) -> dict:
    for item in payload.get("metadata") or []:
        jaql = item.get("jaql") or {}
        if jaql.get("type") == "measure" and jaql.get("title") == PEOPLE_MEASURE_TITLE:
            fields = []
            for ctx in (jaql.get("context") or {}).values():
                fields.append({
                    "table": ctx.get("table"),
                    "column": ctx.get("column"),
                    "dim": ctx.get("dim"),
                    "datatype": ctx.get("datatype"),
                })
            if len(fields) != 1:
                raise ComparisonOutputError("Currently Enrolled People measure context is not a single field")
            formula = str(jaql.get("formula") or "")
            aggregation = "Count" if formula.lstrip().startswith("Count(") else None
            if aggregation is None:
                raise ComparisonOutputError("Currently Enrolled People aggregation is not Count")
            return {
                "aggregation": aggregation,
                "column_title": jaql.get("columnTitle"),
                "fields": fields,
            }
    raise ComparisonOutputError("Currently Enrolled People measure is absent from the payload")


def _scope_filters(payload: dict) -> list[dict]:
    rows = []
    for item in payload.get("metadata") or []:
        if item.get("panel") != "scope":
            continue
        jaql = item.get("jaql") or {}
        filt = jaql.get("filter") or {}
        nested = filt.get("filter")
        rows.append({
            "table": jaql.get("table"),
            "column": jaql.get("column"),
            "title": jaql.get("title"),
            "level": jaql.get("level"),
            "disabled": item.get("disabled"),
            "locked": item.get("locked"),
            "is_dashboard_filter": jaql.get("isDashboardFilter"),
            "members": list(filt.get("members") or []),
            "from": filt.get("from"),
            "to": filt.get("to"),
            "nested_filter": json.loads(json.dumps(nested)) if nested is not None else None,
        })
    return rows


def _secondary_measure_filters(payload: dict) -> list[dict]:
    """Context filters on measures other than the people count. They do not define it."""
    found = []
    for item in payload.get("metadata") or []:
        jaql = item.get("jaql") or {}
        if jaql.get("type") != "measure" or jaql.get("title") == PEOPLE_MEASURE_TITLE:
            continue
        for ctx in (jaql.get("context") or {}).values():
            filt = ctx.get("filter")
            if not filt:
                continue
            found.append({
                "measure_title": jaql.get("title"),
                "table": ctx.get("table"),
                "column": ctx.get("column"),
                "members": list((filt.get("members") or [])),
            })
    return found


def _split_label(value: str) -> tuple[str, str]:
    if ";" in value:
        ident, title = value.split(";", 1)
        return ident, title
    return value, value


def _by_column(scope: list[dict], column: str) -> dict | None:
    for item in scope:
        if item.get("column") == column:
            return item
    return None


def _evidence_lines(definition: dict, coverage: dict | None, benefit: dict | None) -> list[str]:
    lines = [
        f"Dashboard title in the verified request: {definition['dashboard_title']}.",
        f"Widget id {definition['widget_id']}, title {definition['widget_title']}, widgetType {definition['widget_type']}.",
        f"Datasource {definition['datasource_title']}, live={definition['datasource_live']}.",
        (
            f"People measure aggregation {definition['measure']['aggregation']} of "
            f"{definition['measure']['fields'][0]['table']}.{definition['measure']['fields'][0]['column']} "
            f"({definition['measure']['fields'][0]['datatype']})."
        ),
    ]
    if coverage is not None:
        lines.append(
            f"Plan-year filter {coverage['table']}.{coverage['column']} members {coverage['members']}, "
            f"dashboard filter={coverage['is_dashboard_filter']}, locked={coverage['locked']}."
        )
    if benefit is not None:
        lines.append(
            f"benefit_effective_date level {benefit['level']} from {benefit['from']} to {benefit['to']}."
        )
    for column in _STATUS_COLUMNS:
        item = _by_column(definition["scope_filters"], column)
        if item is not None:
            lines.append(
                f"{column} scope members {item['members']}; nested filter {item['nested_filter']}."
            )
    for column in ("person_type_description", "insurance_type_description"):
        item = _by_column(definition["scope_filters"], column)
        if item is not None:
            lines.append(f"{item['table']}.{column} members {item['members']}.")
    lines.append("The people-count scope has no application_type filter and no issuer filter.")
    lines.append("The people-count scope has no enrollment_confirmation_date filter and no as-of field.")
    return lines


def _missing_evidence(definition: dict, coverage: dict | None, benefit: dict | None) -> list[str]:
    missing = []
    if benefit is None or not benefit.get("from") or not benefit.get("to"):
        missing.append("The verified request has no benefit_effective_date from/to range.")
    else:
        missing.append(
            "benefit_effective_date is a day-level range "
            f"{benefit['from']} through {benefit['to']}. That range does not name one coverage month, "
            "and it does not establish a January coverage population."
        )
    missing.append("The verified request has no as-of date, snapshot date, or Elasticube build timestamp.")
    if definition.get("datasource_live") is False:
        missing.append(
            "datasource.live is false. The request does not say when Elasticube MAIN was built."
        )
    missing.append("The verified request has no enrollment_confirmation_date filter.")
    for column in _STATUS_COLUMNS:
        item = _by_column(definition["scope_filters"], column)
        nested = (item or {}).get("nested_filter") or {}
        if isinstance(nested, dict) and "turnedOff" in nested:
            missing.append(
                f"{column} members are {item['members']} and the nested filter is {nested}. "
                "The verified request does not define how turnedOff is evaluated."
            )
    if coverage is None:
        missing.append("The verified request has no coverage_year filter.")
    missing.append(
        "No approved artifact maps this indicator to one dbo.Enrollments_PY2526 snapshot. "
        "A filename date is not that mapping."
    )
    return missing


def _population_sentence(definition: dict, coverage: dict | None, benefit: dict | None) -> str:
    year = (coverage or {}).get("members")
    window = ""
    if benefit is not None:
        window = (
            f" Benefit effective dates are level {benefit.get('level')} "
            f"from {benefit.get('from')} through {benefit.get('to')}."
        )
    return (
        f"Current {definition.get('widget_type')} of "
        f"{definition['measure']['aggregation']}("
        f"{definition['measure']['fields'][0]['table']}.{definition['measure']['fields'][0]['column']}) "
        f"on datasource {definition.get('datasource_title')} (live={definition.get('datasource_live')}), "
        f"with {((coverage or {}).get('table'))}.coverage_year members {year}."
        f"{window} This request does not name a single comparison month."
    )


def _schema_note(db_columns: set[str] | None) -> str | None:
    if db_columns is None:
        return (
            "The PY2026 snapshot schema was not read. Comparison stopped at "
            f"{PERIOD_UNRESOLVED}."
        )
    return None


def _coverage_row(scope: list[dict], db_columns: set[str] | None) -> dict:
    item = _by_column(scope, "coverage_year")
    source = f"{item['table']}.{item['column']} members {item['members']}" if item else "absent"
    note = _schema_note(db_columns)
    if note is not None:
        status, column, reason = UNCERTAIN, None, note
    elif "coverage_year" not in db_columns:
        status, column = NOT_AVAILABLE, None
        reason = "The snapshot schema has no coverage_year column."
    else:
        status, column = UNCERTAIN, "coverage_year"
        reason = (
            "Sisense filters ssap_applications.coverage_year. A snapshot column of the same "
            "name is not evidenced to be that field."
        )
    return _row("Plan year", source, column, status, True, reason)


def _benefit_row(scope: list[dict], db_columns: set[str] | None) -> dict:
    item = _by_column(scope, "benefit_effective_date")
    source = (
        f"enrollment.benefit_effective_date level {item['level']} from {item['from']} to {item['to']}"
        if item else "absent"
    )
    note = _schema_note(db_columns)
    if note is not None:
        status, column, reason = UNCERTAIN, None, note
    elif "benefit_effective_date" not in db_columns:
        status, column = NOT_AVAILABLE, None
        reason = "The snapshot schema has no benefit_effective_date column."
    else:
        status, column = UNCERTAIN, "benefit_effective_date"
        reason = (
            "The verified window stays on the dates in the payload. "
            "A PY2026 date predicate is not in the request, so none is invented."
        )
    return _row("Benefit effective date", source, column, status, True, reason)


def _status_row(scope: list[dict], column: str, concept: str, db_columns: set[str] | None) -> dict:
    item = _by_column(scope, column)
    source = f"{item['table']}.{column} members {item['members']}; nested {item['nested_filter']}" if item else "absent"
    note = _schema_note(db_columns)
    if note is not None:
        status, db_column, reason = UNCERTAIN, None, note
    elif column not in db_columns:
        status, db_column = NOT_AVAILABLE, None
        reason = f"The snapshot schema has no {column} column."
    else:
        status, db_column = UNCERTAIN, column
        reason = "The nested turnedOff filter has no evaluated predicate in the verified request."
    return _row(concept, source, db_column, status, True, reason)


def _member_row(scope: list[dict], column: str, concept: str, source_label: str,
                db_columns: set[str] | None) -> dict:
    item = _by_column(scope, column)
    source = f"{source_label} members {item['members']}" if item else "absent"
    note = _schema_note(db_columns)
    if note is not None:
        return _row(concept, source, None, UNCERTAIN, True, note)
    if column not in db_columns:
        return _row(concept, source, None, NOT_AVAILABLE, True, f"The snapshot schema has no {column} column.")
    return _row(concept, source, column, EXACT, True, "The snapshot column name and the explicit members match the verified filter.")


def _absence_row(concept: str, reason: str) -> dict:
    return _row(concept, "no filter in the verified people-count scope", None, EXACT, False, reason)


def _row(concept: str, source: str, column: str | None, status: str, critical: bool, reason: str) -> dict:
    return {
        "sisense_concept": concept,
        "sisense_source": source,
        "py2026_db_column": column,
        "translation_status": status,
        "population_critical": critical,
        "reason": reason,
    }


def _warnings(period: PeriodAssessment, person: dict, mappings: list[dict], counts: dict) -> list[str]:
    warnings = list(period.missing_evidence)
    if person["identity_status"] != EXACT:
        warnings.append(person["reason"])
    for row in mappings:
        if row["population_critical"] and row["translation_status"] not in ACCEPTABLE:
            warnings.append(f"{row['sisense_concept']}: {row['translation_status']}. {row['reason']}")
    if "raw_row_count" in counts["refused_inputs"]:
        warnings.append("A raw row count was supplied and was not used as Currently Enrolled People.")
    warnings.append("Stored Sisense history is retrieval time of this same indicator and is not used as the comparison number.")
    return warnings


if __name__ == "__main__":
    raise SystemExit(main())
