"""Phase 7: Georgia Access PY2026 vs PY2027 executive comparison report.

Consumes existing artifacts only. Nothing here queries the database or Sisense:

- PY2026: the Phase 6B report (dbo.Enrollments_TEST, coverage_year = 2026),
  including Stage 7B temporal diagnostics;
- PY2027: validated snapshots and stored daily SOURCE series in the history
  SQLite file (read-only), plus the runtime validation report for
  validation-only metrics;
- Phase 6D: the locked NAP decision (and its report when present).

A missing artifact makes its section unavailable; no other source is searched.
Unavailable stays unavailable: it is never shown as zero.

    python -m comparison.executive_py2026_py2027_report
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from comparison.currently_enrolled import OUTPUT_DIR, guard_no_pii
from comparison.lifecycle import resolve_history_path

REPORTS_DIR = OUTPUT_DIR.parent
EXECUTIVE_DIR = REPORTS_DIR / "executive"
JSON_NAME = "py2026_vs_py2027_comparison.json"
HTML_NAME = "py2026_vs_py2027_comparison.html"
PHASE_6B_REPORT = OUTPUT_DIR / "py2026_population_diagnostics.json"
PHASE_6D_REPORT = OUTPUT_DIR / "nap_semantic_capture.json"
VALIDATION_REPORT = REPORTS_DIR / "validation" / "wave1_runtime_validation.json"
COMMAND = r".\.venv\Scripts\python.exe -m comparison.executive_py2026_py2027_report"

PY2026_SOURCE = "dbo.Enrollments_TEST (coverage_year = 2026)"
PY2027_SOURCE = "Sisense PY 2027 - OE Executive Dashboard"
SUMMARY_STATEMENT = (
    "PY2026 and PY2027 come from different systems and lifecycle points. Only metrics with defensible semantic "
    "alignment are presented as comparisons. Other metrics are explicitly labelled diagnostic or PY2027-only."
)

COMPARABLE = "COMPARABLE"
DIAGNOSTIC = "DIAGNOSTIC"
PY2027_ONLY = "PY2027_ONLY"
PY2026_ONLY = "PY2026_ONLY"
NOT_COMPARABLE = "NOT_COMPARABLE"
DIFF_STATUSES = (COMPARABLE, DIAGNOSTIC)

PRODUCTION_VERIFIED = "PRODUCTION_VERIFIED"
VALIDATION_ONLY = "VALIDATION_ONLY"
UNAVAILABLE = "UNAVAILABLE"
NOT_FROM_ENROLLMENT = "NOT_AVAILABLE_FROM_ENROLLMENT_SOURCE"
NAP_PY2026 = "PY2026 equivalent not defensibly available from the canonical enrollment table."
NAP_NEW_CLASS = "OTHER_LIFECYCLE_CLASS"
NAP_BRIDGE = "NO_ACTIVITY_BRIDGE"
OE_LABEL = "OE_LIFECYCLE_ALIGNMENT_DIAGNOSTIC"
ANCHOR_STATEMENT = "Official OE start anchor not proven; calendar-date alignment only."
CANCEL_STATEMENT = ("PY2026 is latest-state population evidence; PY2027 is daily last-update activity. "
                    "They are different semantic families.")
DASH = "—"

CE = "currently_enrolled_people"
PE = "percent_effectuated"
STARTED = "applications_started"
SUBMITTED = "submitted_applications"
APPLICANTS = "total_applicants"
NEW, ACTIVE, PASSIVE = "new_enrolled", "active_enrolled", "passive_enrolled"
CANCELLED_DAILY, TERMINATED_DAILY = "cancelled_policies_daily", "terminated_policies_daily"
PY2027_METRICS = (
    (CE, "Currently Enrolled", PRODUCTION_VERIFIED, "count"),
    (PE, "% Effectuated", PRODUCTION_VERIFIED, "ratio"),
    (STARTED, "Applications Started", PRODUCTION_VERIFIED, "count"),
    (SUBMITTED, "Submitted Applications", PRODUCTION_VERIFIED, "count"),
    (APPLICANTS, "Total Applicants", VALIDATION_ONLY, "count"),
    (NEW, "New", PRODUCTION_VERIFIED, "count"),
    (ACTIVE, "Active", PRODUCTION_VERIFIED, "count"),
    (PASSIVE, "Passive", PRODUCTION_VERIFIED, "count"),
)
CE_CAVEAT = ("DB enrollee_id is a person-identifier proxy for Sisense exchg_indiv_identifier (unproven); "
             "person-type and insurance-type filters unavailable in PY2026; benefit window shifted one year; "
             "PY2026 Oct-31-equivalent latest state vs PY2027 live OE.")
AUTH_KEYS = frozenset({"cookie", "cookies", "set-cookie", "authorization", "token", "access_token", "csrf",
                       "x-xsrf-token", "headers", "post_data", "password", "secret"})


class ExecutiveOutputError(ValueError):
    """The executive report refused an unsafe key or value."""


# --- loading ----------------------------------------------------------------------

def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def read_history(path: Path | None) -> dict:
    """Validated PY2027 snapshots and stored daily SOURCE series (read-only); empty when absent."""
    out = {"snapshots": {}, "series": {}, "available": False}
    if path is None or not Path(path).is_file():
        return out
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "metric_snapshots" in tables:
            out["available"] = True
            for metric_id, value, retrieved in connection.execute(
                    "SELECT metric_id, metric_value, retrieved_at_utc FROM metric_snapshots "
                    "WHERE plan_year = 2027 AND retrieval_status = 'VALIDATED' ORDER BY retrieved_at_utc"):
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                out["snapshots"].setdefault(metric_id, []).append({"value": value, "retrieved_at_utc": retrieved})
        if "source_series_points" in tables:
            for metric_id, day, value, observed in connection.execute(
                    "SELECT metric_id, source_date, value, observed_at_utc FROM source_series_points "
                    "WHERE time_grain = 'days' ORDER BY source_date, id"):
                points = out["series"].setdefault(metric_id, {})
                points[day] = {"source_date": day, "value": int(value), "observed_at_utc": observed}
    finally:
        connection.close()
    out["series"] = {key: list(points.values()) for key, points in out["series"].items()}
    return out


def collect_evidence(history_path: Path | None = None) -> dict:
    return {
        "phase_6b": load_json(PHASE_6B_REPORT),
        "phase_6d": load_json(PHASE_6D_REPORT),
        "validation": load_json(VALIDATION_REPORT),
        "history": read_history(resolve_history_path() if history_path is None else history_path),
    }


# --- helpers ----------------------------------------------------------------------

def difference(py2026, py2027, status: str, ratio: bool = False) -> dict:
    """Differences only for COMPARABLE/DIAGNOSTIC rows with both values present."""
    if status not in DIFF_STATUSES or py2026 is None or py2027 is None:
        return {"absolute_difference": None, "percent_difference": None, "difference_unit": None}
    absolute = py2027 - py2026
    if ratio:
        return {"absolute_difference": round(absolute * 100, 2), "percent_difference": None,
                "difference_unit": "percentage points"}
    return {"absolute_difference": absolute,
            "percent_difference": round(absolute / py2026 * 100, 2) if py2026 else None,
            "difference_unit": "count"}


def comparison_row(metric, py2026, py2027, status, caveat, ratio=False) -> dict:
    return {"metric": metric, "py2026_value": py2026, "py2027_value": py2027, **difference(py2026, py2027, status, ratio),
            "comparison_status": status, "caveat": caveat, "value_kind": "ratio" if ratio else "count"}


def latest(history: dict, metric_id: str) -> dict | None:
    points = history.get("snapshots", {}).get(metric_id) or []
    return points[-1] if points else None


def validation_value(validation: dict | None, metric_id: str) -> dict | None:
    for item in (validation or {}).get("metrics") or []:
        if item.get("metric_id") == metric_id and item.get("runtime_value") is not None:
            return {"value": item["runtime_value"], "retrieved_at_utc": validation.get("generated_at_utc")}
    return None


def py2027_value(evidence: dict, metric_id: str) -> dict | None:
    snapshot = latest(evidence["history"], metric_id)
    if snapshot is not None:
        return {**snapshot, "source": "metric_snapshots (validated)"}
    found = validation_value(evidence.get("validation"), metric_id)
    return None if found is None else {**found, "source": "runtime validation report"}


def fmt(value, kind: str = "count") -> str:
    if value is None:
        return "N/A"
    if kind == "ratio":
        return f"{value * 100:.1f}%"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


# --- sections ---------------------------------------------------------------------

def py2027_snapshot(evidence: dict) -> list[dict]:
    rows = []
    for metric_id, label, verification, kind in PY2027_METRICS:
        found = py2027_value(evidence, metric_id)
        rows.append({"metric_id": metric_id, "metric": label, "value": None if found is None else found["value"],
                     "value_kind": kind, "retrieved_at_utc": None if found is None else found["retrieved_at_utc"],
                     "evidence_source": None if found is None else found["source"],
                     "verification": verification if found is not None else UNAVAILABLE})
    return rows


def population_comparison(evidence: dict, snapshot: dict[str, dict]) -> dict:
    six_b = evidence.get("phase_6b")
    ce27, pe27 = snapshot[CE]["value"], snapshot[PE]["value"]
    if six_b is None:
        return {"available": False, "reason": "Phase 6B report not found.", "rows": [
            comparison_row("Currently Enrolled", None, ce27, DIAGNOSTIC, "PY2026 Phase 6B artifact unavailable."),
            comparison_row("% Effectuated", None, pe27, DIAGNOSTIC, "PY2026 Phase 6B artifact unavailable.", True)]}
    rows = []
    for candidate in six_b.get("currently_enrolled_candidates") or []:
        cid = candidate.get("candidate_id")
        caveat = f"{CE_CAVEAT} Candidate {cid}: {candidate.get('description')}"
        rows.append(comparison_row(f"Currently Enrolled vs PY2026 {cid}", candidate.get("distinct_people"),
                                   ce27, DIAGNOSTIC, caveat))
    for item in (six_b.get("percent_effectuated") or {}).get("per_candidate") or []:
        rows.append(comparison_row(f"% Effectuated vs PY2026 {item.get('candidate_id')}", item.get("ratio"), pe27,
                                   DIAGNOSTIC, "Phase 6A numerator/denominator semantics on the PY2026 proxy; "
                                   "not the official PY2026 % Effectuated. " + CE_CAVEAT, ratio=True))
    ident = six_b.get("identifier_behavior") or {}
    rows.append(comparison_row("PY2026 distinct people (all statuses)", ident.get("distinct_people"), None,
                               PY2026_ONLY, "Whole PY2026 latest-state population; no PY2027 counterpart."))
    rows.append(comparison_row("PY2026 distinct policies (all statuses)", ident.get("distinct_policies"), None,
                               PY2026_ONLY, "Whole PY2026 latest-state population; no PY2027 counterpart."))
    return {"available": True, "rows": rows,
            "note": "CE1-CE4 are competing diagnostic definitions; none is selected or ranked."}


def nap_section(snapshot: dict[str, dict], evidence: dict) -> dict:
    six_d = evidence.get("phase_6d") or {}
    values = {label: snapshot[mid]["value"] for mid, label in ((NEW, "New"), (ACTIVE, "Active"), (PASSIVE, "Passive"))}
    return {
        "comparison_status": PY2027_ONLY,
        "py2027": values,
        "py2026": NAP_PY2026,
        "phase_6d": {"new_classification": NAP_NEW_CLASS, "activity_bridge": NAP_BRIDGE, "decision": "CLOSED",
                     "capture_artifact_present": bool(six_d),
                     "captured_relationship": (six_d.get("phase_6d_decision") or {}).get("lifecycle_relationship")},
        "partition_note": "New + Active + Passive is not treated as a partition of Currently Enrolled; numeric "
                          "closeness is not evidence of lifecycle logic.",
    }


def temporal_section(evidence: dict) -> dict:
    six_b = evidence.get("phase_6b")
    oe = (six_b or {}).get("oe_aligned_temporal_diagnostics")
    if not oe:
        return {"available": False, "label": OE_LABEL, "anchor": ANCHOR_STATEMENT,
                "reason": "Phase 6B Stage 7B results not found."}
    fields = {}
    for name in ("enrollment_create_date", "enrollment_confirmation_date"):
        item = (oe.get("fields") or {}).get(name) or {}
        monthly = item.get("monthly")
        fields[name] = {
            "monthly": None if monthly is None else [
                {**row, "period_role": "context (before November 2025)" if row.get("month", "") < "2025-11"
                 else "OE emphasis (November 2025 and later)"} for row in monthly],
            "cumulative": item.get("cumulative"),
        }
    return {"available": True, "label": OE_LABEL, "anchor": ANCHOR_STATEMENT, "fields": fields,
            "statement": "Activity/cohort accumulation by date; not historical population state."}


def cancellation_section(evidence: dict) -> dict:
    six_b = evidence.get("phase_6b") or {}
    populations = [{"label": item.get("label"), "distinct_policies": item.get("distinct_policies"),
                    "distinct_people": item.get("distinct_people"), "comparison_status": PY2026_ONLY}
                   for item in six_b.get("population_diagnostics") or []]
    series = {}
    for metric_id, label in ((CANCELLED_DAILY, "Cancelled Policies"), (TERMINATED_DAILY, "Terminated Policies")):
        points = evidence["history"].get("series", {}).get(metric_id)
        series[metric_id] = {"label": label, "comparison_status": PY2027_ONLY,
                             "points": [{"date": p["source_date"], "value": p["value"]} for p in points]
                             if points else None}
    return {"statement": CANCEL_STATEMENT, "py2026_population": populations or None,
            "py2027_daily_activity": series, "yoy_difference": "Not computed (different semantic families)."}


def applications_section(snapshot: dict[str, dict]) -> dict:
    rows = [{"metric": snapshot[mid]["metric"], "py2027_value": snapshot[mid]["value"],
             "verification": snapshot[mid]["verification"], "py2026_value": NOT_FROM_ENROLLMENT,
             "comparison_status": PY2027_ONLY} for mid in (STARTED, SUBMITTED, APPLICANTS)]
    started, submitted = snapshot[STARTED]["value"], snapshot[SUBMITTED]["value"]
    funnel = None
    if started is not None and submitted is not None:
        funnel = {"started": started, "submitted": submitted, "not_yet_submitted": started - submitted,
                  "submitted_share_of_started": round(submitted / started * 100, 2) if started else None}
    return {"rows": rows, "funnel": funnel}


QUALITY_MATRIX = (
    ("Currently Enrolled", PY2026_SOURCE, PY2027_SOURCE, "person", DIAGNOSTIC,
     "Shown with differences, labelled DIAGNOSTIC (identifier/filter equivalence unresolved)."),
    ("% Effectuated", PY2026_SOURCE, PY2027_SOURCE, "person ratio", DIAGNOSTIC,
     "Shown as percentage-point difference, labelled DIAGNOSTIC."),
    ("New", NAP_PY2026, PY2027_SOURCE, "lifecycle class", PY2027_ONLY, "PY2027 only (Phase 6D NO_ACTIVITY_BRIDGE)."),
    ("Active", NAP_PY2026, PY2027_SOURCE, "lifecycle class", PY2027_ONLY, "PY2027 only."),
    ("Passive", NAP_PY2026, PY2027_SOURCE, "lifecycle class", PY2027_ONLY, "PY2027 only."),
    ("Cancelled", PY2026_SOURCE + " latest-state population", PY2027_SOURCE + " daily last-update activity",
     "policy", NOT_COMPARABLE, "Shown side by side; no YoY difference."),
    ("Terminated", PY2026_SOURCE + " latest-state population", PY2027_SOURCE + " daily last-update activity",
     "policy", NOT_COMPARABLE, "Shown side by side; no YoY difference."),
    ("Applications Started", NOT_FROM_ENROLLMENT, PY2027_SOURCE, "application", PY2027_ONLY, "PY2027 only."),
    ("Submitted Applications", NOT_FROM_ENROLLMENT, PY2027_SOURCE, "application", PY2027_ONLY, "PY2027 only."),
    ("Total Applicants", NOT_FROM_ENROLLMENT, PY2027_SOURCE + " (validation-only)", "applicant", PY2027_ONLY,
     "PY2027 only; validation-only metric."),
)


def quality_matrix() -> list[dict]:
    keys = ("metric", "py2026_source", "py2027_source", "grain", "semantic_status", "report_treatment")
    return [dict(zip(keys, row)) for row in QUALITY_MATRIX]


def findings(population: dict, nap: dict, apps: dict, temporal: dict, history: dict) -> list[str]:
    out = []
    for row in population["rows"]:
        if row["comparison_status"] == DIAGNOSTIC and row["absolute_difference"] is not None:
            if row["value_kind"] == "ratio":
                out.append(f"{row['metric']} (DIAGNOSTIC): PY2027 {fmt(row['py2027_value'], 'ratio')} vs PY2026 "
                           f"{fmt(row['py2026_value'], 'ratio')}, {row['absolute_difference']:+.2f} percentage points.")
            else:
                pct = "" if row["percent_difference"] is None else f" ({row['percent_difference']:+.2f}%)"
                out.append(f"{row['metric']} (DIAGNOSTIC): PY2027 {fmt(row['py2027_value'])} vs PY2026 "
                           f"{fmt(row['py2026_value'])}, {row['absolute_difference']:+,}{pct}.")
    values = nap["py2027"]
    if all(v is not None for v in values.values()):
        total = sum(values.values())
        if total:
            out.append("PY2027 New / Active / Passive (PY2027_ONLY): " + ", ".join(
                f"{k} {fmt(v)} ({v / total * 100:.1f}% of the three measures combined)" for k, v in values.items())
                + ". Not a proven partition of Currently Enrolled.")
    if apps["funnel"]:
        f = apps["funnel"]
        out.append(f"PY2027 applications (PY2027_ONLY): {fmt(f['started'])} started, {fmt(f['submitted'])} submitted; "
                   f"difference {f['not_yet_submitted']:+,}"
                   + ("" if f["submitted_share_of_started"] is None else
                      f"; submitted = {f['submitted_share_of_started']:.1f}% of started") + ".")
    points = history.get("snapshots", {}).get(CE) or []
    if len(points) >= 2:
        first, last = points[0], points[-1]
        out.append(f"PY2027 Currently Enrolled changed from {fmt(first['value'])} ({first['retrieved_at_utc']}) to "
                   f"{fmt(last['value'])} ({last['retrieved_at_utc']}), {last['value'] - first['value']:+,} "
                   f"across {len(points)} saved snapshots.")
    if temporal.get("available"):
        for name, item in temporal["fields"].items():
            cumulative = item.get("cumulative") or []
            people = [c.get("distinct_people") for c in cumulative]
            if len(people) >= 2 and None not in (people[0], people[-1]) and people[-1]:
                out.append(f"PY2026 {name} ({OE_LABEL}): {people[0] / people[-1] * 100:.1f}% of people dated on or "
                           f"before {cumulative[-1]['through']} were already dated on or before "
                           f"{cumulative[0]['through']}.")
    return out


def management_view(population: dict, snapshot: dict[str, dict]) -> list[dict]:
    def row(metric, py26, py27, change, status):
        return {"metric": metric, "py2026": py26, "py2027": py27, "change": change, "status": status}

    ce_rows = [r for r in population["rows"] if r["metric"].startswith("Currently Enrolled")]
    pe_rows = [r for r in population["rows"] if r["metric"].startswith("% Effectuated")]

    def span(rows, kind):
        values = [r["py2026_value"] for r in rows if r["py2026_value"] is not None]
        if not values:
            return "N/A", DASH
        low, high = min(values), max(values)
        text = fmt(low, kind) if low == high else f"{fmt(low, kind)} to {fmt(high, kind)}"
        diffs = [r["absolute_difference"] for r in rows if r["absolute_difference"] is not None]
        if not diffs:
            return text, DASH
        unit = " pp" if kind == "ratio" else ""
        lo, hi = min(diffs), max(diffs)
        change = f"{lo:+,.2f}{unit}" if kind == "ratio" else f"{lo:+,}"
        if lo != hi:
            change += f" to {hi:+,.2f}{unit}" if kind == "ratio" else f" to {hi:+,}"
        return text, change

    ce26, ce_change = span(ce_rows, "count")
    pe26, pe_change = span(pe_rows, "ratio")
    rows = [row("Currently Enrolled (PY2026 CE1-CE4)", ce26, fmt(snapshot[CE]["value"]), ce_change, DIAGNOSTIC),
            row("% Effectuated (PY2026 CE1-CE4)", pe26, fmt(snapshot[PE]["value"], "ratio"), pe_change, DIAGNOSTIC)]
    for mid in (NEW, ACTIVE, PASSIVE, STARTED, SUBMITTED, APPLICANTS):
        item = snapshot[mid]
        status = PY2027_ONLY if item["verification"] != VALIDATION_ONLY else f"{PY2027_ONLY} (validation-only)"
        rows.append(row(item["metric"], DASH, fmt(item["value"]), DASH, status))
    return rows


CAVEATS = (
    "Cross-system comparison: PY2026 from the enrollment database, PY2027 from the Sisense dashboard.",
    "Person identifier equivalence (DB enrollee_id vs Sisense exchg_indiv_identifier) is unresolved.",
    "PY2026 person-type and insurance-type filters are unavailable in dbo.Enrollments_TEST.",
    "PY2026 is an Oct-31-equivalent latest-state dataset; PY2027 is read live during Open Enrollment.",
    "Phase 6D: NEW = OTHER_LIFECYCLE_CLASS; activity bridge = NO_ACTIVITY_BRIDGE (closed).",
    "Application metrics are unavailable from the PY2026 enrollment source.",
    "Historical daily cancellation/termination activity is not reconstructable from snapshots.",
    ANCHOR_STATEMENT,
)


def build_report(evidence: dict, generated_at: str | None = None) -> dict:
    snapshot_rows = py2027_snapshot(evidence)
    snapshot = {row["metric_id"]: row for row in snapshot_rows}
    population = population_comparison(evidence, snapshot)
    nap = nap_section(snapshot, evidence)
    temporal = temporal_section(evidence)
    cancellation = cancellation_section(evidence)
    apps = applications_section(snapshot)
    report = {
        "report_kind": "EXECUTIVE_PY2026_PY2027_COMPARISON",
        "phase": "7",
        "generated_at_utc": generated_at,
        "executive_summary": {
            "py2026_source": PY2026_SOURCE,
            "py2027_source": PY2027_SOURCE,
            "py2027_observed_at_utc": snapshot[CE]["retrieved_at_utc"],
            "statement": SUMMARY_STATEMENT,
            "artifacts": {"phase_6b": evidence.get("phase_6b") is not None,
                          "phase_6d": evidence.get("phase_6d") is not None,
                          "validation_report": evidence.get("validation") is not None,
                          "history": evidence["history"].get("available", False)},
        },
        "population_comparison": population,
        "py2027_current_snapshot": snapshot_rows,
        "new_active_passive": nap,
        "oe_temporal_diagnostic": temporal,
        "cancellation_termination": cancellation,
        "applications": apps,
        "comparison_quality_matrix": quality_matrix(),
        "key_findings": findings(population, nap, apps, temporal, evidence["history"]),
        "management_view": management_view(population, snapshot),
        "caveats": list(CAVEATS),
        "production_comparable": False,
    }
    guard(report)
    return report


def guard(report: dict) -> None:
    guard_no_pii(report)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in AUTH_KEYS:
                    raise ExecutiveOutputError(f"Executive report refused key {key}")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(report)


# --- rendering --------------------------------------------------------------------

def _e(value) -> str:
    return html.escape(DASH if value is None else str(value))


def _badge(status: str) -> str:
    return f"<span class='badge {html.escape(status.split(' ')[0].lower())}'>{html.escape(status)}</span>"


def _table(rows: list[dict], columns: tuple[tuple[str, str], ...], formats: dict | None = None) -> str:
    formats = formats or {}
    head = "".join(f"<th>{html.escape(title)}</th>" for _, title in columns)
    body = ""
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            if key in formats:
                value = formats[key](value, row)
            elif key.endswith("status") and isinstance(value, str):
                cells.append(f"<td>{_badge(value)}</td>")
                continue
            cells.append(f"<td>{_e(value if value is not None else 'N/A')}</td>")
        body += "<tr>" + "".join(cells) + "</tr>"
    return f"<table class='grid'><tr>{head}</tr>{body}</table>"


def bar_chart(title: str, status: str, items: list[tuple[str, float | None]], kind: str = "count",
              width: int = 640) -> str:
    present = [(label, value) for label, value in items if value is not None]
    missing = [label for label, value in items if value is None]
    if not present:
        return f"<div class='chart'><h3>{_e(title)} {_badge(status)}</h3><p class='muted'>No data available.</p></div>"
    top = max(value for _, value in present) or 1
    bar_h, gap, label_w = 22, 8, 230
    height = len(present) * (bar_h + gap) + 10
    bars = []
    for index, (label, value) in enumerate(present):
        y = index * (bar_h + gap) + 5
        length = max(1, (width - label_w - 110) * value / top)
        bars.append(f"<text x='0' y='{y + 15}' font-size='12'>{_e(label)}</text>"
                    f"<rect x='{label_w}' y='{y}' width='{length:.1f}' height='{bar_h}' fill='#2f6fb0'/>"
                    f"<text x='{label_w + length + 6:.1f}' y='{y + 15}' font-size='12'>{_e(fmt(value, kind))}</text>")
    note = f"<p class='muted'>Not available: {_e(', '.join(missing))}</p>" if missing else ""
    return (f"<div class='chart'><h3>{_e(title)} {_badge(status)}</h3>"
            f"<svg width='{width}' height='{height}' role='img'>{''.join(bars)}</svg>{note}</div>")


def charts(report: dict) -> str:
    population = [r for r in report["population_comparison"]["rows"]
                  if r["comparison_status"] == DIAGNOSTIC and r["value_kind"] == "count"]
    population_items = [(f"PY2026 {r['metric'].split()[-1]}", r["py2026_value"]) for r in population]
    population_items.append(("PY2027 Currently Enrolled", population[0]["py2027_value"] if population else None))
    nap = report["new_active_passive"]["py2027"]
    apps = [(r["metric"], r["py2027_value"]) for r in report["applications"]["rows"]]
    parts = [bar_chart("Population comparison (PY2026 candidates vs PY2027)", DIAGNOSTIC, population_items),
             bar_chart("PY2027 New / Active / Passive", PY2027_ONLY, list(nap.items())),
             bar_chart("PY2027 application funnel", PY2027_ONLY, apps)]
    temporal = report["oe_temporal_diagnostic"]
    if temporal.get("available"):
        for name, item in temporal["fields"].items():
            parts.append(bar_chart(f"PY2026 {name} monthly people", OE_LABEL,
                                   [(m["month"], m.get("distinct_people")) for m in item["monthly"] or []]))
    for metric_id, item in report["cancellation_termination"]["py2027_daily_activity"].items():
        if item["points"]:
            parts.append(bar_chart(f"PY2027 {item['label']} daily activity", PY2027_ONLY,
                                   [(p["date"], p["value"]) for p in item["points"]]))
    return "".join(parts)


CSS = (
    "body{margin:0;background:#f2f5f9;color:#24364b;font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:14px}"
    "header{background:#0e2a47;color:#fff;padding:20px 28px}main{padding:20px 28px;max-width:1300px}"
    "h2{font-size:17px;color:#0e2a47}.card{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:16px}"
    ".grid{border-collapse:collapse;width:100%}.grid th,.grid td{text-align:left;padding:6px 8px;"
    "border-bottom:1px solid #e2e8f0;vertical-align:top}.grid th{font-size:12px;color:#6b7c93}"
    ".muted{color:#6b7c93;font-size:12px}.badge{padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700}"
    ".badge.comparable{background:#dcfce7;color:#166534}.badge.diagnostic{background:#fef3c7;color:#92400e}"
    ".badge.py2027_only,.badge.py2026_only{background:#e0e7ff;color:#3730a3}"
    ".badge.not_comparable{background:#fee2e2;color:#991b1b}.badge.oe_lifecycle_alignment_diagnostic"
    "{background:#fef3c7;color:#92400e}.chart{margin:10px 0 18px}"
)


def _card(title: str, body: str) -> str:
    return f"<section class='card'><h2>{html.escape(title)}</h2>{body}</section>"


def render_html(report: dict) -> str:
    summary = report["executive_summary"]
    population = report["population_comparison"]
    nap = report["new_active_passive"]
    temporal = report["oe_temporal_diagnostic"]
    cancel = report["cancellation_termination"]
    apps = report["applications"]
    value_fmt = lambda value, row: fmt(value, row.get("value_kind", "count"))  # noqa: E731
    diff_fmt = lambda value, row: DASH if value is None else (  # noqa: E731
        f"{value:+.2f} pp" if row.get("difference_unit") == "percentage points" else f"{value:+,}")
    pct_fmt = lambda value, row: DASH if value is None else f"{value:+.2f}%"  # noqa: E731
    sections = [
        _card("1. Executive Summary",
              f"<p>PY2026 source: <b>{_e(summary['py2026_source'])}</b><br>PY2027 source: "
              f"<b>{_e(summary['py2027_source'])}</b><br>PY2027 observed: "
              f"<b>{_e(summary['py2027_observed_at_utc'] or 'N/A')}</b></p><p>{_e(summary['statement'])}</p>"),
        _card("2. Population Comparison", (_table(population["rows"], (
            ("metric", "Metric"), ("py2026_value", "PY2026"), ("py2027_value", "PY2027"),
            ("absolute_difference", "Difference"), ("percent_difference", "% Difference"),
            ("comparison_status", "Status"), ("caveat", "Caveat")),
            {"py2026_value": value_fmt, "py2027_value": value_fmt, "absolute_difference": diff_fmt,
             "percent_difference": pct_fmt}) + f"<p class='muted'>{_e(population.get('note') or population.get('reason'))}</p>")),
        _card("3. PY2027 Current OE Snapshot", _table(report["py2027_current_snapshot"], (
            ("metric", "Metric"), ("value", "Value"), ("retrieved_at_utc", "Observed (UTC)"),
            ("verification", "Verification"), ("evidence_source", "Evidence")),
            {"value": lambda v, r: fmt(v, r["value_kind"])})),
        _card("4. New / Active / Passive",
              f"<p>{_badge(PY2027_ONLY)} New {_e(fmt(nap['py2027']['New']))}, Active "
              f"{_e(fmt(nap['py2027']['Active']))}, Passive {_e(fmt(nap['py2027']['Passive']))}</p>"
              f"<p>{_e(nap['py2026'])}</p><p>Phase 6D: NEW = {NAP_NEW_CLASS}; Activity bridge = {NAP_BRIDGE}.</p>"
              f"<p class='muted'>{_e(nap['partition_note'])}</p>"),
        _card("5. OE Temporal Diagnostic", _temporal_html(temporal)),
        _card("6. Cancellation / Termination", _cancel_html(cancel)),
        _card("7. Applications", _table(apps["rows"], (
            ("metric", "Metric"), ("py2027_value", "PY2027"), ("verification", "Verification"),
            ("py2026_value", "PY2026"), ("comparison_status", "Status")),
            {"py2027_value": lambda v, r: fmt(v)})),
        _card("8. Comparison Quality Matrix", _table(report["comparison_quality_matrix"], (
            ("metric", "Metric"), ("py2026_source", "PY2026 source"), ("py2027_source", "PY2027 source"),
            ("grain", "Grain"), ("semantic_status", "Semantic status"), ("report_treatment", "Report treatment")))),
        _card("9. Key Findings", "<ul>" + "".join(f"<li>{_e(f)}</li>" for f in report["key_findings"]) + "</ul>"
              if report["key_findings"] else "<p>No findings: required evidence unavailable.</p>"),
        _card("10. Management View", _table(report["management_view"], (
            ("metric", "Metric"), ("py2026", "PY2026"), ("py2027", "PY2027"), ("change", "Change"),
            ("status", "Status")))),
        _card("11. Data Quality / Caveats", "<ul>" + "".join(f"<li>{_e(c)}</li>" for c in report["caveats"]) + "</ul>"),
        _card("Visualizations", charts(report)),
    ]
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Georgia Access PY2026 vs PY2027</title>"
            f"<style>{CSS}</style></head><body><header><h1>Georgia Access — PY2026 vs PY2027 Executive "
            f"Comparison</h1><p>Generated {_e(report['generated_at_utc'])}. production_comparable = false.</p>"
            f"</header><main>{''.join(sections)}</main></body></html>")


def _temporal_html(temporal: dict) -> str:
    if not temporal.get("available"):
        return f"<p>{_badge(OE_LABEL)} Unavailable: {_e(temporal['reason'])}</p><p>{_e(ANCHOR_STATEMENT)}</p>"
    parts = [f"<p>{_badge(OE_LABEL)} {_e(temporal['statement'])}</p><p><b>{_e(ANCHOR_STATEMENT)}</b></p>"]
    for name, item in temporal["fields"].items():
        parts.append(f"<h3>{_e(name)}</h3>")
        parts.append(_table(item["monthly"] or [], (("month", "Month"), ("distinct_people", "People"),
                                                    ("distinct_policies", "Policies"), ("period_role", "Role")),
                            {"distinct_people": lambda v, r: fmt(v), "distinct_policies": lambda v, r: fmt(v)})
                     if item["monthly"] is not None else "<p>Monthly results unavailable.</p>")
        parts.append(_table(item["cumulative"] or [], (("through", "Through"), ("distinct_people", "People"),
                                                       ("distinct_policies", "Policies")),
                            {"distinct_people": lambda v, r: fmt(v), "distinct_policies": lambda v, r: fmt(v)})
                     if item["cumulative"] is not None else "<p>Cumulative results unavailable.</p>")
    return "".join(parts)


def _cancel_html(cancel: dict) -> str:
    parts = [f"<p><b>{_e(cancel['statement'])}</b> {_e(cancel['yoy_difference'])}</p><h3>PY2026 latest-state population</h3>"]
    parts.append(_table(cancel["py2026_population"], (("label", "Population"), ("distinct_policies", "Policies"),
                                                      ("distinct_people", "People"), ("comparison_status", "Status")),
                        {"distinct_policies": lambda v, r: fmt(v), "distinct_people": lambda v, r: fmt(v)})
                 if cancel["py2026_population"] else "<p>Unavailable (Phase 6B report not found).</p>")
    parts.append("<h3>PY2027 daily last-update activity</h3>")
    for item in cancel["py2027_daily_activity"].values():
        if item["points"]:
            total = sum(p["value"] for p in item["points"])
            parts.append(f"<p>{_badge(PY2027_ONLY)} {_e(item['label'])}: {len(item['points'])} days stored, "
                         f"{_e(fmt(total))} policy-days in stored series.</p>")
        else:
            parts.append(f"<p>{_e(item['label'])}: stored daily series unavailable.</p>")
    return "".join(parts)


def write_report(report: dict, output_dir: Path = EXECUTIVE_DIR) -> tuple[Path, Path]:
    guard(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def main() -> int:
    report = build_report(collect_evidence(), datetime.now(timezone.utc).isoformat(timespec="seconds"))
    json_path, html_path = write_report(report)
    for row in report["management_view"]:
        print(f"{row['metric']:<40}{row['py2026']:<26}{row['py2027']:<14}{row['change']:<26}{row['status']}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
