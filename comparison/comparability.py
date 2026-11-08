"""PY2026 vs PY2027 comparison foundation.

Builds diagnostic comparison rows from the metric registry and stored PY2027
snapshots. Does not send JAQL, does not open the enrollment database, and does
not invent PY2026 equivalents.

    python -m comparison.comparability
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, VERIFIED_PAYLOAD_FILE, guard_no_pii, interpret_payload
from comparison.lifecycle import resolve_history_path
from comparison.models import (
    COMPARABLE,
    DIAGNOSTIC,
    FOUNDATION_METRICS,
    NOT_COMPARABLE,
    SECTION_ACTIVITY,
    SECTION_APPLICATIONS,
    SECTION_LIFECYCLE,
    SECTION_POPULATION,
    SECTION_UNRESOLVED,
    UNRESOLVED_DATABASE_QUESTIONS,
    ComparisonRow,
    MetricSpec,
    SemanticChecks,
    default_identifier_requirement,
)
from comparison.validator import build_row

JSON_NAME = "py2026_vs_py2027_comparison_foundation.json"
HTML_NAME = "py2026_vs_py2027_comparison_foundation.html"
PY2027 = 2027
COMMAND = r".\.venv\Scripts\python.exe -m comparison.comparability"

SECTION_HEADINGS = (
    (SECTION_POPULATION, "Enrollment Population"),
    (SECTION_LIFECYCLE, "Enrollment Lifecycle"),
    (SECTION_ACTIVITY, "Cancellation / Termination Activity"),
    (SECTION_APPLICATIONS, "Applications"),
    (SECTION_UNRESOLVED, "Unresolved Metrics"),
)


def foundation_checks(spec: MetricSpec, *, py2026, py2027) -> SemanticChecks:
    """This phase does not prove semantic equivalence. PY2026 SQL is not invented."""
    py2027_verified = spec.metric_id in {
        "currently_enrolled_people",
        "new_enrolled",
        "active_enrolled",
        "passive_enrolled",
        "applications_started",
        "submitted_applications",
    }
    return SemanticChecks(
        identifier_equivalence_proven=False,
        grain_match=True,
        family_match=True,
        date_semantics_match=False,
        py2026_definition_verified=False,
        py2027_definition_verified=py2027_verified,
        filter_mapping_proven=False,
        both_values_present=py2026 is not None and py2027 is not None,
        pending_has_verified_definition=spec.pending_verified_definition,
        people_policy_not_mixed=True,
        snapshot_not_summed=True,
        py2026_sql_not_invented=True,
    )


def read_validated_snapshots(path: Path | None = None) -> dict[str, dict]:
    history = path or resolve_history_path()
    if not history.is_file():
        return {}
    uri = f"{history.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    found: dict[str, dict] = {}
    try:
        for spec in FOUNDATION_METRICS:
            row = connection.execute(
                """
                SELECT retrieved_at_utc, metric_value
                FROM metric_snapshots
                WHERE metric_id = ? AND plan_year = ? AND retrieval_status = 'VALIDATED'
                ORDER BY retrieved_at_utc DESC
                LIMIT 1
                """,
                (spec.metric_id, PY2027),
            ).fetchone()
            if row is None:
                continue
            retrieved = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            found[spec.metric_id] = {
                "retrieved_at_utc": row[0],
                "observation_date": retrieved.date().isoformat(),
                "metric_value": _as_int(row[1]),
            }
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return found


def currently_enrolled_definition() -> dict:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    return interpret_payload(payload)


def build_foundation_report(
    *,
    snapshots: dict[str, dict] | None = None,
    py2026_values: dict[str, int | None] | None = None,
    definition: dict | None = None,
) -> dict:
    snapshots = snapshots or {}
    py2026_values = py2026_values or {}
    parsed = definition if definition is not None else currently_enrolled_definition()
    rows: list[ComparisonRow] = []
    for spec in FOUNDATION_METRICS:
        snapshot = snapshots.get(spec.metric_id) or {}
        py2027 = snapshot.get("metric_value")
        py2026 = py2026_values.get(spec.metric_id)
        checks = foundation_checks(spec, py2026=py2026, py2027=py2027)
        rows.append(build_row(
            spec,
            py2026_value=py2026,
            py2027_value=py2027,
            checks=checks,
            as_of_date_py2026=None,
            as_of_date_py2027=snapshot.get("observation_date"),
        ))
    serialized = [row.as_dict() for row in rows]
    identifier = default_identifier_requirement()
    report = {
        "report_kind": "PY2026_VS_PY2027_COMPARISON_FOUNDATION",
        "comparison_executed": False,
        "no_experimental_jaql": True,
        "verified_payload_unmodified": True,
        "visualization_created": False,
        "py2026_equivalents_manufactured": False,
        "identifier_equivalence": {
            "py2027_field": identifier.py2027_field,
            "py2026_candidate": identifier.py2026_candidate,
            "status": identifier.status,
            "reason": identifier.reason,
        },
        "currently_enrolled_py2027": {
            "widget_id": parsed.get("widget_id"),
            "widget_title": parsed.get("widget_title"),
            "aggregation": (parsed.get("measure") or {}).get("aggregation"),
            "person_field": _person_field(parsed),
            "source": "existing verified extraction path; this command does not resend JAQL",
        },
        "oe_anchors": {
            "py2026": "unaligned until an authoritative anchor is configured",
            "py2027": "unaligned until an authoritative anchor is configured",
        },
        "sections": [
            {
                "section_id": section_id,
                "heading": heading,
                "metrics": [row for row in serialized if row["section"] == section_id],
            }
            for section_id, heading in SECTION_HEADINGS
        ],
        "metrics": serialized,
        "comparable": [row["metric_name"] for row in serialized if row["comparison_status"] == COMPARABLE],
        "diagnostic": [row["metric_name"] for row in serialized if row["comparison_status"] == DIAGNOSTIC],
        "not_comparable": [row["metric_name"] for row in serialized if row["comparison_status"] == NOT_COMPARABLE],
        "unresolved_database_questions": list(UNRESOLVED_DATABASE_QUESTIONS),
        "inventory_command": COMMAND,
    }
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
    blocks = []
    for section in report.get("sections") or []:
        body = "".join(_metric_row(item) for item in section.get("metrics") or [])
        blocks.append(
            f"<h2>{html.escape(section['heading'])}</h2>"
            "<table><thead><tr>"
            "<th>METRIC</th><th>PY2026</th><th>PY2027</th><th>DIFFERENCE</th>"
            "<th>% CHANGE</th><th>STATUS</th><th>GRAIN</th><th>DATE SEMANTICS</th><th>REASON</th>"
            f"</tr></thead><tbody>{body}</tbody></table>"
        )
    questions = "".join(
        f"<li>{html.escape(item)}</li>" for item in report.get("unresolved_database_questions") or []
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 vs PY2027 comparison foundation</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:1200px;color:#1e293b}"
        "table{border-collapse:collapse;width:100%;margin-bottom:2rem}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#f4f7fb}</style></head><body>"
        "<h1>PY2026 vs PY2027 comparison foundation</h1>"
        "<p>Diagnostic scaffolding only. No production visualization. "
        "NULL is not treated as zero. COMPARABLE is not assigned without semantic checks.</p>"
        + "".join(blocks)
        + "<h2>Unresolved database semantic questions</h2>"
        f"<ul>{questions}</ul>"
        "</body></html>"
    )


def print_table(report: dict) -> None:
    print("PY2026 vs PY2027 COMPARISON FOUNDATION — no JAQL sent, no PY2026 equivalents invented")
    print(f"{'Metric':<56}{'PY2026':>8}{'PY2027':>8}  Status")
    for item in report.get("metrics") or []:
        print(
            f"{item['metric_name']:<56}{_num(item['py2026_value']):>8}"
            f"{_num(item['py2027_value']):>8}  {item['comparison_status']}"
        )
    print("COMPARABLE:", ", ".join(report.get("comparable") or []) or "(none)")
    print("DIAGNOSTIC:", ", ".join(report.get("diagnostic") or []) or "(none)")
    print("NOT_COMPARABLE:", ", ".join(report.get("not_comparable") or []) or "(none)")


def main() -> int:
    snapshots = read_validated_snapshots()
    report = build_foundation_report(snapshots=snapshots)
    json_path, html_path = write_report(report)
    print_table(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


def _person_field(definition: dict) -> str | None:
    fields = (definition.get("measure") or {}).get("fields") or []
    if not fields:
        return None
    table = fields[0].get("table")
    column = fields[0].get("column")
    if table and column:
        return f"{table}.{column}"
    return column


def _metric_row(item: dict) -> str:
    return (
        "<tr>"
        f"<td>{_e(item.get('metric_name'))}</td>"
        f"<td>{_e(item.get('py2026_value'))}</td>"
        f"<td>{_e(item.get('py2027_value'))}</td>"
        f"<td>{_e(item.get('absolute_difference'))}</td>"
        f"<td>{_e(item.get('percent_difference'))}</td>"
        f"<td>{_e(item.get('comparison_status'))}</td>"
        f"<td>{_e(item.get('grain'))}</td>"
        f"<td>{_e(item.get('date_semantics'))}</td>"
        f"<td>{_e(item.get('reason'))}</td>"
        "</tr>"
    )


def _as_int(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value)


def _e(value) -> str:
    return html.escape("n/a" if value is None else str(value))


def _num(value) -> str:
    return "n/a" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
