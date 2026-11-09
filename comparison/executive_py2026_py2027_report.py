"""Phase 7 / 7B: Georgia Access PY2026 vs PY2027 executive comparison report.

The report builder consumes a current-run data model (``build_model``). The
live runner (``comparison.live_executive_runner``) fills that model with values
retrieved in the same invocation. ``model_from_artifacts`` is the explicit
fallback path: every value it supplies is STALE_FALLBACK, never current.

Freshness (LIVE_THIS_RUN / HISTORICAL_SNAPSHOT / STALE_FALLBACK / UNAVAILABLE)
and comparison status (COMPARABLE / DIAGNOSTIC / ...) are independent: a live
value does not change a semantic status. production_comparable stays false.

Unavailable stays unavailable: it is never shown as zero.

    python -m comparison.executive_py2026_py2027_report   (artifact fallback rebuild only)
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

PY2026_SOURCE = "dbo.Enrollments_TEST (coverage_year = 2026)"
PY2027_SOURCE = "Sisense PY 2027 - OE Executive Dashboard"
SNAPSHOT_SOURCE = "metric_snapshots (validated history)"
VALIDATION_SOURCE = "runtime validation report"
SERIES_STORE_SOURCE = "source_series_points (validated source series)"
SUMMARY_STATEMENT = (
    "PY2026 and PY2027 come from different systems and lifecycle points. Only metrics with defensible semantic "
    "alignment are presented as comparisons. Other metrics are explicitly labelled diagnostic or PY2027-only."
)

LIVE = "LIVE_THIS_RUN"
HISTORICAL = "HISTORICAL_SNAPSHOT"
STALE = "STALE_FALLBACK"
UNAVAILABLE = "UNAVAILABLE"
FRESHNESS = (LIVE, HISTORICAL, STALE, UNAVAILABLE)

COMPARABLE = "COMPARABLE"
DIAGNOSTIC = "DIAGNOSTIC"
PY2027_ONLY = "PY2027_ONLY"
PY2026_ONLY = "PY2026_ONLY"
NOT_COMPARABLE = "NOT_COMPARABLE"
DIFF_STATUSES = (COMPARABLE, DIAGNOSTIC)

PRODUCTION_VERIFIED = "PRODUCTION_VERIFIED"
VALIDATION_ONLY = "VALIDATION_ONLY"
NOT_FROM_ENROLLMENT = "NOT_AVAILABLE_FROM_ENROLLMENT_SOURCE"
NAP_PY2026 = "PY2026 equivalent not defensibly available from the canonical enrollment table."
NAP_NEW_CLASS = "OTHER_LIFECYCLE_CLASS"
NAP_BRIDGE = "NO_ACTIVITY_BRIDGE"
NAP_FOOTER = "Lifecycle measures; no defensible PY2026 equivalent."
OE_LABEL = "OE_LIFECYCLE_ALIGNMENT_DIAGNOSTIC"
ANCHOR_STATEMENT = "Official OE start anchor not proven; calendar-date alignment only."
MONTHLY_FOOTNOTE = "Activity/cohort dates; not historical population snapshots."
CE_CHART_TITLE = "Currently Enrolled — PY2026 Diagnostic Range vs PY2027"
CE_CHART_STATUS = "DIAGNOSTIC — not production comparable"
CANCEL_STATEMENT = ("PY2026 is latest-state population evidence; PY2027 is daily last-update activity. "
                    "They are different semantic families.")
SERIES_UNAVAILABLE = "Daily activity series unavailable for this run"
DASH = "—"

CE = "currently_enrolled_people"
PE = "percent_effectuated"
STARTED = "applications_started"
SUBMITTED = "submitted_applications"
APPLICANTS = "total_applicants"
NEW, ACTIVE, PASSIVE = "new_enrolled", "active_enrolled", "passive_enrolled"
CANCELLED_DAILY, TERMINATED_DAILY = "cancelled_policies_daily", "terminated_policies_daily"
SERIES_IDS = ((CANCELLED_DAILY, "Cancelled Policies"), (TERMINATED_DAILY, "Terminated Policies"))
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
PY2027_IDS = tuple(item[0] for item in PY2027_METRICS)
CE_CAVEAT = ("DB enrollee_id is a person-identifier proxy for Sisense exchg_indiv_identifier (unproven); "
             "person-type and insurance-type filters unavailable in PY2026; benefit window shifted one year; "
             "PY2026 Oct-31-equivalent latest state vs PY2027 live OE.")
AUTH_KEYS = frozenset({"cookie", "cookies", "set-cookie", "authorization", "token", "access_token", "csrf",
                       "x-xsrf-token", "headers", "post_data", "password", "secret", "body", "request_body"})


class ExecutiveOutputError(ValueError):
    """The executive report refused an unsafe key or value."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- artifact readers (history and fallback only) ---------------------------------------

def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def artifact_time(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
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
                out["snapshots"].setdefault(metric_id, []).append(
                    {"value": _number(value), "retrieved_at_utc": retrieved})
        if "source_series_points" in tables:
            for metric_id, day, value, observed in connection.execute(
                    "SELECT metric_id, source_date, value, observed_at_utc FROM source_series_points "
                    "WHERE time_grain = 'days' ORDER BY source_date, id"):
                points = out["series"].setdefault(metric_id, {})
                points[day] = {"date": day, "value": int(value), "observed_at_utc": observed}
    finally:
        connection.close()
    out["series"] = {key: list(points.values()) for key, points in out["series"].items()}
    return out


def _number(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


# --- current-run data model ---------------------------------------------------------------

def entry(value, retrieved_at, source, freshness) -> dict:
    """One metric/result with its provenance. A missing value is always UNAVAILABLE."""
    if value is None:
        return {"value": None, "retrieved_at": None, "source": None, "freshness": UNAVAILABLE}
    return {"value": value, "retrieved_at": retrieved_at, "source": source, "freshness": freshness}


def py2026_live(report: dict, retrieved_at: str) -> dict:
    return {"report": report, "freshness": LIVE, "retrieved_at": retrieved_at, "source": PY2026_SOURCE,
            "note": f"PY2026 DB: {LIVE} / {retrieved_at}"}


def py2026_fallback(report: dict | None, artifact_at: str | None, reason: str) -> dict:
    if report is None:
        return {"report": None, "freshness": UNAVAILABLE, "retrieved_at": None, "source": PY2026_SOURCE,
                "note": f"PY2026 DB unavailable ({reason}) — no fallback artifact"}
    return {"report": report, "freshness": STALE, "retrieved_at": artifact_at,
            "source": f"{PY2026_SOURCE} via Phase 6B artifact",
            "note": f"PY2026 DB unavailable ({reason}) — showing Phase 6B artifact from {artifact_at}"}


def _fallback_metric(metric_id: str, history: dict, validation: dict | None) -> dict:
    points = history.get("snapshots", {}).get(metric_id) or []
    if points:
        return entry(points[-1]["value"], points[-1]["retrieved_at_utc"], SNAPSHOT_SOURCE, STALE)
    for item in (validation or {}).get("metrics") or []:
        if item.get("metric_id") == metric_id and item.get("runtime_value") is not None:
            return entry(item["runtime_value"], validation.get("generated_at_utc"), VALIDATION_SOURCE, STALE)
    return entry(None, None, None, UNAVAILABLE)


def _fallback_series(metric_id: str, history: dict) -> dict:
    points = history.get("series", {}).get(metric_id) or []
    if not points:
        return {"points": None, "retrieved_at": None, "source": None, "freshness": UNAVAILABLE}
    return {"points": [{"date": p["date"], "value": p["value"]} for p in points],
            "retrieved_at": max(p["observed_at_utc"] for p in points), "source": SERIES_STORE_SOURCE,
            "freshness": STALE}


def py2027_model(live_values: dict, live_series: dict, history: dict, validation: dict | None,
                 live_ok: bool, reason: str | None = None) -> dict:
    """Live values win. A metric missing from this run falls back to its last validated value as STALE_FALLBACK."""
    metrics = {}
    for metric_id in PY2027_IDS:
        live = live_values.get(metric_id) if live_ok else None
        if live is not None and live.get("value") is not None:
            metrics[metric_id] = entry(live["value"], live["retrieved_at"], PY2027_SOURCE, LIVE)
        else:
            metrics[metric_id] = _fallback_metric(metric_id, history, validation)
    series = {}
    for metric_id, _ in SERIES_IDS:
        live = live_series.get(metric_id) if live_ok else None
        if live and live.get("points"):
            series[metric_id] = {"points": list(live["points"]), "retrieved_at": live["retrieved_at"],
                                 "source": f"{PY2027_SOURCE} (natural dashboard response)", "freshness": LIVE}
        else:
            series[metric_id] = _fallback_series(metric_id, history)
    ce = metrics[CE]
    if live_ok and ce["freshness"] == LIVE:
        freshness, note = LIVE, f"PY2027 Sisense: {LIVE} / {ce['retrieved_at']}"
    elif ce["freshness"] == STALE:
        freshness = STALE
        note = (f"PY2027 Sisense unavailable{f' ({reason})' if reason else ''} — showing last validated "
                f"snapshot from {ce['retrieved_at']}")
    else:
        freshness = UNAVAILABLE
        note = f"PY2027 Sisense unavailable{f' ({reason})' if reason else ''} — no validated snapshot"
    return {"metrics": metrics, "series": series, "freshness": freshness, "retrieved_at": ce["retrieved_at"],
            "source": PY2027_SOURCE, "note": note}


def build_model(py2026: dict, py2027: dict, history: dict, phase_6d: dict | None, generated_at: str) -> dict:
    return {"generated_at_utc": generated_at, "py2026": py2026, "py2027": py2027, "history": history,
            "phase_6d": phase_6d}


def model_from_artifacts(history_path: Path | None = None, reason: str = "artifact rebuild; no live retrieval") -> dict:
    """Explicit fallback path: nothing here is current."""
    history = read_history(resolve_history_path() if history_path is None else history_path)
    py2026 = py2026_fallback(load_json(PHASE_6B_REPORT), artifact_time(PHASE_6B_REPORT), reason)
    py2027 = py2027_model({}, {}, history, load_json(VALIDATION_REPORT), live_ok=False, reason=reason)
    return build_model(py2026, py2027, history, load_json(PHASE_6D_REPORT), now_utc())


# --- helpers -----------------------------------------------------------------------------

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


def _side(prefix: str, item: dict) -> dict:
    return {f"{prefix}_value": item["value"], f"{prefix}_retrieved_at": item["retrieved_at"],
            f"{prefix}_source": item["source"], f"{prefix}_freshness": item["freshness"]}


def comparison_row(metric, py2026: dict, py2027: dict, status, caveat, ratio=False) -> dict:
    return {"metric": metric, **_side("py2026", py2026), **_side("py2027", py2027),
            **difference(py2026["value"], py2027["value"], status, ratio),
            "comparison_status": status, "caveat": caveat, "value_kind": "ratio" if ratio else "count"}


NONE_ENTRY = {"value": None, "retrieved_at": None, "source": None, "freshness": UNAVAILABLE}


def fmt(value, kind: str = "count") -> str:
    if value is None:
        return "N/A"
    if kind == "ratio":
        return f"{value * 100:.1f}%"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def compact(value) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 10_000:
        return f"{value / 1_000:.0f}K"
    return f"{int(value):,}"


# --- sections ----------------------------------------------------------------------------

def _py26(model: dict, value) -> dict:
    py2026 = model["py2026"]
    return entry(value, py2026["retrieved_at"], py2026["source"], py2026["freshness"])


def py2027_snapshot(model: dict) -> list[dict]:
    rows = []
    for metric_id, label, verification, kind in PY2027_METRICS:
        item = model["py2027"]["metrics"][metric_id]
        rows.append({"metric_id": metric_id, "metric": label, "value_kind": kind, **item,
                     "verification": verification if item["value"] is not None else UNAVAILABLE})
    return rows


def ce_range(model: dict) -> dict:
    report = model["py2026"]["report"] or {}
    scenarios = [{"candidate_id": c.get("candidate_id"), "description": c.get("description"),
                  "distinct_people": c.get("distinct_people")}
                 for c in report.get("currently_enrolled_candidates") or []]
    values = [s["distinct_people"] for s in scenarios if s["distinct_people"] is not None]
    return {"low": min(values) if values else None, "high": max(values) if values else None,
            "scenarios": scenarios, "comparison_status": DIAGNOSTIC,
            "freshness": model["py2026"]["freshness"] if values else UNAVAILABLE,
            "retrieved_at": model["py2026"]["retrieved_at"] if values else None,
            "note": "CE1-CE4 are competing diagnostic scenarios of one PY2026 metric; none is selected or ranked."}


def population_comparison(model: dict) -> dict:
    report = model["py2026"]["report"]
    ce27, pe27 = model["py2027"]["metrics"][CE], model["py2027"]["metrics"][PE]
    if report is None:
        return {"available": False, "reason": model["py2026"]["note"], "rows": [
            comparison_row("Currently Enrolled", NONE_ENTRY, ce27, DIAGNOSTIC, "PY2026 data unavailable."),
            comparison_row("% Effectuated", NONE_ENTRY, pe27, DIAGNOSTIC, "PY2026 data unavailable.", True)]}
    rows = []
    for candidate in report.get("currently_enrolled_candidates") or []:
        cid = candidate.get("candidate_id")
        rows.append(comparison_row(f"Currently Enrolled vs PY2026 {cid}", _py26(model, candidate.get("distinct_people")),
                                   ce27, DIAGNOSTIC, f"{CE_CAVEAT} Scenario {cid}: {candidate.get('description')}"))
    for item in (report.get("percent_effectuated") or {}).get("per_candidate") or []:
        rows.append(comparison_row(f"% Effectuated vs PY2026 {item.get('candidate_id')}", _py26(model, item.get("ratio")),
                                   pe27, DIAGNOSTIC, "Phase 6A numerator/denominator semantics on the PY2026 proxy; "
                                   "not the official PY2026 % Effectuated. " + CE_CAVEAT, ratio=True))
    ident = report.get("identifier_behavior") or {}
    for key, label in (("distinct_people", "people"), ("distinct_policies", "policies")):
        rows.append(comparison_row(f"PY2026 distinct {label} (all statuses)", _py26(model, ident.get(key)), NONE_ENTRY,
                                   PY2026_ONLY, "Whole PY2026 latest-state population; no PY2027 counterpart."))
    return {"available": True, "rows": rows, "note": ce_range(model)["note"]}


def status_populations(model: dict) -> dict:
    report = model["py2026"]["report"] or {}
    out = {}
    for key in ("enrollment_status", "enrollee_status"):
        rows = (report.get("status_distributions") or {}).get(key)
        out[key] = None if rows is None else [
            {"status_value": r.get("status_value"), **{k: r.get(k) for k in ("row_count", "distinct_people",
                                                                               "distinct_policies")},
             "comparison_status": PY2026_ONLY, "freshness": model["py2026"]["freshness"],
             "retrieved_at": model["py2026"]["retrieved_at"], "source": model["py2026"]["source"]}
            for r in rows]
    return out


def nap_section(model: dict) -> dict:
    metrics = model["py2027"]["metrics"]
    six_d = model.get("phase_6d") or {}
    return {
        "comparison_status": PY2027_ONLY,
        "py2027": {label: metrics[mid] for mid, label in ((NEW, "New"), (ACTIVE, "Active"), (PASSIVE, "Passive"))},
        "py2026": NAP_PY2026,
        "phase_6d": {"new_classification": NAP_NEW_CLASS, "activity_bridge": NAP_BRIDGE, "decision": "CLOSED",
                     "capture_artifact_present": bool(six_d),
                     "captured_relationship": (six_d.get("phase_6d_decision") or {}).get("lifecycle_relationship")},
        "proven_partition": False,
        "partition_note": "New + Active + Passive is not treated as a partition of Currently Enrolled; numeric "
                          "closeness is not evidence of lifecycle logic.",
        "footer": NAP_FOOTER,
    }


def temporal_section(model: dict) -> dict:
    report = model["py2026"]["report"] or {}
    oe = report.get("oe_aligned_temporal_diagnostics")
    base = {"label": OE_LABEL, "anchor": ANCHOR_STATEMENT, "footnote": MONTHLY_FOOTNOTE,
            "freshness": model["py2026"]["freshness"] if oe else UNAVAILABLE,
            "retrieved_at": model["py2026"]["retrieved_at"] if oe else None, "source": model["py2026"]["source"]}
    if not oe:
        return {**base, "available": False, "reason": "PY2026 temporal results unavailable."}
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
    return {**base, "available": True, "fields": fields,
            "statement": "Activity/cohort accumulation by date; not historical population state."}


def cancellation_section(model: dict) -> dict:
    report = model["py2026"]["report"] or {}
    populations = [{"label": item.get("label"), "distinct_policies": item.get("distinct_policies"),
                    "distinct_people": item.get("distinct_people"), "comparison_status": PY2026_ONLY,
                    "freshness": model["py2026"]["freshness"], "retrieved_at": model["py2026"]["retrieved_at"],
                    "source": model["py2026"]["source"]}
                   for item in report.get("population_diagnostics") or []]
    activity = {mid: {"label": label, "comparison_status": PY2027_ONLY, **model["py2027"]["series"][mid]}
                for mid, label in SERIES_IDS}
    return {"statement": CANCEL_STATEMENT, "py2026_population": populations or None,
            "py2027_daily_activity": activity, "yoy_difference": "Not computed (different semantic families)."}


def applications_section(model: dict) -> dict:
    metrics = model["py2027"]["metrics"]
    rows = [{"metric": label, "py2027": metrics[mid], "verification": verification,
             "py2026_value": NOT_FROM_ENROLLMENT, "comparison_status": PY2027_ONLY, "grain": grain}
            for mid, label, verification, grain in (
                (STARTED, "Applications Started", PRODUCTION_VERIFIED, "application"),
                (SUBMITTED, "Submitted Applications", PRODUCTION_VERIFIED, "application"),
                (APPLICANTS, "Total Applicants", VALIDATION_ONLY, "applicant"))]
    started, submitted = metrics[STARTED]["value"], metrics[SUBMITTED]["value"]
    funnel = None
    if started is not None and submitted is not None:
        funnel = {"started": started, "submitted": submitted, "not_yet_submitted": started - submitted,
                  "submitted_share_of_started": round(submitted / started * 100, 2) if started else None,
                  "grain": "application"}
    return {"rows": rows, "funnel": funnel, "total_applicants": metrics[APPLICANTS],
            "grain_note": "Total Applicants counts applicants, not applications; it is excluded from funnel arithmetic."}


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
     "PY2027 only; validation-only metric; separate grain from applications."),
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
    values = {k: v["value"] for k, v in nap["py2027"].items()}
    if all(v is not None for v in values.values()) and sum(values.values()):
        total = sum(values.values())
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
        out.append(f"PY2027 Currently Enrolled ({HISTORICAL}) changed from {fmt(first['value'])} "
                   f"({first['retrieved_at_utc']}) to {fmt(last['value'])} ({last['retrieved_at_utc']}), "
                   f"{last['value'] - first['value']:+,} across {len(points)} saved snapshots.")
    if temporal.get("available"):
        for name, item in temporal["fields"].items():
            cumulative = item.get("cumulative") or []
            people = [c.get("distinct_people") for c in cumulative]
            if len(people) >= 2 and None not in (people[0], people[-1]) and people[-1]:
                out.append(f"PY2026 {name} ({OE_LABEL}): {people[0] / people[-1] * 100:.1f}% of people dated on or "
                           f"before {cumulative[-1]['through']} were already dated on or before "
                           f"{cumulative[0]['through']}.")
    return out


def management_view(population: dict, range_: dict, model: dict) -> list[dict]:
    metrics = model["py2027"]["metrics"]

    def row(metric, py26, py27, change, status, freshness):
        return {"metric": metric, "py2026": py26, "py2027": py27, "change": change, "status": status,
                "freshness": freshness}

    def change_span(rows, kind):
        diffs = [r["absolute_difference"] for r in rows if r["absolute_difference"] is not None]
        if not diffs:
            return DASH
        lo, hi = min(diffs), max(diffs)
        one = (lambda d: f"{d:+.2f} pp") if kind == "ratio" else (lambda d: f"{d:+,}")
        return one(lo) if lo == hi else f"{one(lo)} to {one(hi)}"

    ce_rows = [r for r in population["rows"] if r["metric"].startswith("Currently Enrolled")]
    pe_rows = [r for r in population["rows"] if r["metric"].startswith("% Effectuated")]
    pe_values = [r["py2026_value"] for r in pe_rows if r["py2026_value"] is not None]
    ce26 = "N/A" if range_["low"] is None else (
        fmt(range_["low"]) if range_["low"] == range_["high"] else f"{fmt(range_['low'])} – {fmt(range_['high'])}")
    pe26 = "N/A" if not pe_values else (fmt(min(pe_values), "ratio") if min(pe_values) == max(pe_values)
                                         else f"{fmt(min(pe_values), 'ratio')} – {fmt(max(pe_values), 'ratio')}")
    pair = f"PY2026 {model['py2026']['freshness']} / PY2027 {{}}"
    rows = [row("Currently Enrolled (PY2026 CE1-CE4 range)", ce26, fmt(metrics[CE]["value"]),
                change_span(ce_rows, "count"), DIAGNOSTIC, pair.format(metrics[CE]["freshness"])),
            row("% Effectuated (PY2026 CE1-CE4 range)", pe26, fmt(metrics[PE]["value"], "ratio"),
                change_span(pe_rows, "ratio"), DIAGNOSTIC, pair.format(metrics[PE]["freshness"]))]
    for mid, label, verification, kind in PY2027_METRICS[2:]:
        status = PY2027_ONLY if verification != VALIDATION_ONLY else f"{PY2027_ONLY} (validation-only)"
        rows.append(row(label, DASH, fmt(metrics[mid]["value"], kind), DASH, status, metrics[mid]["freshness"]))
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
    "Freshness and comparability are independent: a LIVE_THIS_RUN value keeps its semantic status.",
)


def build_report(model: dict) -> dict:
    population = population_comparison(model)
    range_ = ce_range(model)
    nap = nap_section(model)
    temporal = temporal_section(model)
    cancellation = cancellation_section(model)
    apps = applications_section(model)
    report = {
        "report_kind": "EXECUTIVE_PY2026_PY2027_COMPARISON",
        "phase": "7B",
        "generated_at_utc": model["generated_at_utc"],
        "executive_summary": {
            "data_refreshed": model["generated_at_utc"],
            "py2026": {k: model["py2026"][k] for k in ("freshness", "retrieved_at", "source", "note")},
            "py2027": {k: model["py2027"][k] for k in ("freshness", "retrieved_at", "source", "note")},
            "py2027_observed_at_utc": model["py2027"]["metrics"][CE]["retrieved_at"],
            "statement": SUMMARY_STATEMENT,
        },
        "kpis": [{"metric_id": mid, "label": label, "value_kind": kind, **model["py2027"]["metrics"][mid]}
                 for mid, label, _, kind in PY2027_METRICS if mid in (CE, PE, STARTED, SUBMITTED)],
        "currently_enrolled_range": range_,
        "population_comparison": population,
        "py2026_status_populations": status_populations(model),
        "py2027_current_snapshot": py2027_snapshot(model),
        "new_active_passive": nap,
        "oe_temporal_diagnostic": temporal,
        "cancellation_termination": cancellation,
        "applications": apps,
        "comparison_quality_matrix": quality_matrix(),
        "key_findings": findings(population, nap, apps, temporal, model["history"]),
        "management_view": management_view(population, range_, model),
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
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
    walk(report)


# --- charts (inline SVG; no external dependency) ---------------------------------------------

BLUE, TEAL, AMBER, GREY = "#2563eb", "#0d9488", "#d97706", "#94a3b8"


def _e(value) -> str:
    return html.escape(DASH if value is None else str(value))


def _badge(status: str) -> str:
    return f"<span class='badge {html.escape(status.split(' ')[0].lower())}'>{html.escape(status)}</span>"


def _fresh(freshness: str | None) -> str:
    return f"<span class='fresh {html.escape((freshness or UNAVAILABLE).lower())}'>{_e(freshness or UNAVAILABLE)}</span>"


def unavailable_card(title: str, message: str, status: str | None = None) -> str:
    return (f"<div class='chart unavailable'><h3>{_e(title)}</h3>{_badge(status) if status else ''}"
            f"<p>{_e(message)}</p></div>")


def _chart(title: str, status_html: str, svg: str, footer: str = "") -> str:
    return (f"<div class='chart'><h3>{_e(title)}</h3><div class='status'>{status_html}</div>{svg}"
            + (f"<p class='foot'>{footer}</p>" if footer else "") + "</div>")


def hbar_svg(items: list[tuple[str, float, str]], kind: str = "count", width: int = 720) -> str:
    top = max(value for _, value, _ in items) or 1
    bar_h, gap, label_w, value_w = 30, 14, 190, 120
    height = len(items) * (bar_h + gap) + gap
    parts = []
    for index, (label, value, color) in enumerate(items):
        y = gap + index * (bar_h + gap)
        length = max(2.0, (width - label_w - value_w) * value / top)
        parts.append(f"<text x='{label_w - 10}' y='{y + 20}' text-anchor='end' class='lbl'>{_e(label)}</text>"
                     f"<rect x='{label_w}' y='{y}' width='{length:.1f}' height='{bar_h}' rx='3' fill='{color}'/>"
                     f"<text x='{label_w + length + 8:.1f}' y='{y + 20}' class='val'>{_e(fmt(value, kind))}</text>")
    return f"<svg viewBox='0 0 {width} {height}' width='100%' role='img'>{''.join(parts)}</svg>"


def grouped_vbar_svg(categories: list[str], series: list[tuple[str, str, dict]], width: int = 960,
                     height: int = 320) -> str:
    """Grouped vertical bars. A category missing a value draws no bar (never a zero bar)."""
    left, right, top, bottom = 56, 16, 24, 64
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for _, _, data in series for v in data.values() if v is not None]
    peak = max(values) if values else 1
    group_w = plot_w / max(1, len(categories))
    bar_w = min(34.0, group_w * 0.8 / max(1, len(series)))
    parts = [f"<line x1='{left}' y1='{top + plot_h}' x2='{width - right}' y2='{top + plot_h}' stroke='#cbd5e1'/>"]
    for tick in range(5):
        value = peak * tick / 4
        y = top + plot_h - plot_h * tick / 4
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{width - right}' y2='{y:.1f}' stroke='#f1f5f9'/>"
                     f"<text x='{left - 6}' y='{y + 4:.1f}' text-anchor='end' class='tick'>{_e(compact(value))}</text>")
    for ci, category in enumerate(categories):
        gx = left + ci * group_w + (group_w - bar_w * len(series)) / 2
        for si, (_, color, data) in enumerate(series):
            value = data.get(category)
            if value is None:
                continue
            h = plot_h * value / peak if peak else 0
            x, y = gx + si * bar_w, top + plot_h - h
            parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_w - 2:.1f}' height='{h:.1f}' fill='{color}'/>"
                         f"<text x='{x + bar_w / 2 - 1:.1f}' y='{y - 4:.1f}' text-anchor='middle' class='dl'>"
                         f"{_e(compact(value))}</text>")
        cx = left + ci * group_w + group_w / 2
        parts.append(f"<text x='{cx:.1f}' y='{top + plot_h + 18}' text-anchor='middle' class='tick'>"
                     f"{_e(category)}</text>")
    legend_x = left
    for name, color, _ in series:
        parts.append(f"<rect x='{legend_x}' y='{height - 22}' width='12' height='12' fill='{color}'/>"
                     f"<text x='{legend_x + 18}' y='{height - 12}' class='lbl'>{_e(name)}</text>")
        legend_x += 24 + 8 * len(name)
    return f"<svg viewBox='0 0 {width} {height}' width='100%' role='img'>{''.join(parts)}</svg>"


def ce_chart(report: dict) -> str:
    range_ = report["currently_enrolled_range"]
    py27 = next(k for k in report["kpis"] if k["metric_id"] == CE)
    caveat = f"<b>{_e(CE_CHART_STATUS)}.</b> {_e(CE_CAVEAT)} {_e(range_['note'])}"
    if range_["low"] is None and py27["value"] is None:
        return unavailable_card(CE_CHART_TITLE, "Currently Enrolled data unavailable for this run", CE_CHART_STATUS)
    width, height, base, top = 640, 300, 250, 30
    peak = max(v for v in (range_["high"], py27["value"]) if v is not None) or 1
    scale = (base - top) / peak
    parts = [f"<line x1='40' y1='{base}' x2='{width - 20}' y2='{base}' stroke='#cbd5e1'/>"]
    if range_["low"] is not None:
        low_y, high_y = base - range_["low"] * scale, base - range_["high"] * scale
        parts.append(f"<rect x='120' y='{low_y:.1f}' width='140' height='{base - low_y:.1f}' fill='{GREY}'/>"
                     f"<rect x='120' y='{high_y:.1f}' width='140' height='{low_y - high_y:.1f}' fill='{GREY}' "
                     f"fill-opacity='0.35' stroke='{GREY}' stroke-dasharray='4 3'/>"
                     f"<text x='190' y='{high_y - 8:.1f}' text-anchor='middle' class='val'>"
                     f"{_e(fmt(range_['low']))} – {_e(fmt(range_['high']))}</text>")
    else:
        parts.append(f"<text x='190' y='{base - 10}' text-anchor='middle' class='lbl'>PY2026 unavailable</text>")
    if py27["value"] is not None:
        y = base - py27["value"] * scale
        parts.append(f"<rect x='380' y='{y:.1f}' width='140' height='{base - y:.1f}' fill='{BLUE}'/>"
                     f"<text x='450' y='{y - 8:.1f}' text-anchor='middle' class='val'>{_e(fmt(py27['value']))}</text>")
    else:
        parts.append(f"<text x='450' y='{base - 10}' text-anchor='middle' class='lbl'>PY2027 unavailable</text>")
    parts.append(f"<text x='190' y='{base + 20}' text-anchor='middle' class='lbl'>PY2026 diagnostic range (CE1–CE4)</text>"
                 f"<text x='450' y='{base + 20}' text-anchor='middle' class='lbl'>PY2027 Currently Enrolled</text>")
    svg = f"<svg viewBox='0 0 {width} {height}' width='100%' style='max-width:{width}px' role='img'>{''.join(parts)}</svg>"
    scenarios = "".join(f"<li>{_e(s['candidate_id'])}: {_e(fmt(s['distinct_people']))}</li>" for s in range_["scenarios"])
    status = (f"{_badge(DIAGNOSTIC)} <b>{_e(CE_CHART_STATUS)}</b> · PY2026 {_fresh(range_['freshness'])} · "
              f"PY2027 {_fresh(py27['freshness'])}")
    footer = caveat + (f"<ul class='scen'>{scenarios}</ul>" if scenarios else "")
    return _chart(CE_CHART_TITLE, status, svg, footer)


def nap_chart(report: dict) -> str:
    title = "PY2027 Enrolled People — New / Active / Passive"
    nap = report["new_active_passive"]["py2027"]
    items = [(label, item["value"], color) for (label, item), color in zip(nap.items(), (TEAL, BLUE, GREY))
             if item["value"] is not None]
    if not items:
        return unavailable_card(title, "New / Active / Passive unavailable for this run", PY2027_ONLY)
    missing = [label for label, item in nap.items() if item["value"] is None]
    freshness = sorted({item["freshness"] for item in nap.values() if item["value"] is not None})
    status = f"{_badge(PY2027_ONLY)} " + " ".join(_fresh(f) for f in freshness)
    footer = _e(NAP_FOOTER) + " Not a proven partition of Currently Enrolled." + (
        f" Not available: {_e(', '.join(missing))}." if missing else "")
    return _chart(title, status, hbar_svg(items), footer)


def funnel_chart(report: dict) -> str:
    title = "PY2027 Application Funnel"
    apps = report["applications"]
    funnel = apps["funnel"]
    applicants = apps["total_applicants"]
    extra = "" if applicants["value"] is None else (
        f" Total Applicants (separate grain, validation-only): {_e(fmt(applicants['value']))} "
        f"{_fresh(applicants['freshness'])}.")
    if funnel is None:
        rows = {r["metric"]: r["py2027"] for r in apps["rows"]}
        items = [(k, rows[k]["value"], BLUE) for k in ("Applications Started", "Submitted Applications")
                 if rows[k]["value"] is not None]
        if not items:
            return unavailable_card(title, "Application funnel unavailable for this run", PY2027_ONLY)
        return _chart(title, _badge(PY2027_ONLY), hbar_svg(items), "Funnel incomplete; share not computed." + extra)
    share = funnel["submitted_share_of_started"]
    rows = {r["metric"]: r["py2027"] for r in apps["rows"]}
    freshness = sorted({rows[k]["freshness"] for k in ("Applications Started", "Submitted Applications")})
    status = f"{_badge(PY2027_ONLY)} " + " ".join(_fresh(f) for f in freshness)
    footer = (f"Submitted share of started: <b>{'N/A' if share is None else f'{share:.1f}%'}</b>." + extra)
    return _chart(title, status, hbar_svg([("Applications Started", funnel["started"], BLUE),
                                           ("Submitted Applications", funnel["submitted"], TEAL)]), footer)


def monthly_chart(report: dict) -> str:
    title = "PY2026 Enrollment Lifecycle Activity by Month"
    temporal = report["oe_temporal_diagnostic"]
    if not temporal.get("available"):
        return unavailable_card(title, "Monthly lifecycle activity unavailable for this run", OE_LABEL)
    series = []
    for name, label, color in (("enrollment_create_date", "Enrollment Created — distinct people", BLUE),
                               ("enrollment_confirmation_date", "Enrollment Confirmed — distinct people", AMBER)):
        monthly = temporal["fields"][name]["monthly"] or []
        series.append((label, color, {m["month"]: m.get("distinct_people") for m in monthly}))
    months = sorted({month for _, _, data in series for month in data})
    if not months:
        return unavailable_card(title, "Monthly lifecycle activity unavailable for this run", OE_LABEL)
    status = f"{_badge(OE_LABEL)} {_fresh(temporal['freshness'])}"
    return _chart(title, status, grouped_vbar_svg(months, series),
                  f"{_e(MONTHLY_FOOTNOTE)} {_e(ANCHOR_STATEMENT)}")


def daily_chart(report: dict) -> str:
    title = "PY2027 Cancelled / Terminated Daily Activity"
    activity = report["cancellation_termination"]["py2027_daily_activity"]
    present = [(item["label"], item) for item in activity.values() if item["points"]]
    if not present:
        return unavailable_card(title, SERIES_UNAVAILABLE, PY2027_ONLY)
    colors = (AMBER, "#dc2626")
    series = [(label, color, {p["date"]: p["value"] for p in item["points"]})
              for (label, item), color in zip(present, colors)]
    dates = []
    for _, _, data in series:
        dates += [d for d in data if d not in dates]
    status = f"{_badge(PY2027_ONLY)} " + " ".join(
        f"{_e(label)} {_fresh(item['freshness'])} ({_e(item['retrieved_at'])})" for label, item in present)
    missing = [item["label"] for item in activity.values() if not item["points"]]
    footer = ("Daily last-update activity by source date. Not compared with PY2026 latest-state populations."
              + (f" Not available: {_e(', '.join(missing))}." if missing else ""))
    return _chart(title, status, grouped_vbar_svg(dates, series), footer)


def charts(report: dict) -> str:
    return "".join((ce_chart(report), nap_chart(report), funnel_chart(report), monthly_chart(report),
                    daily_chart(report)))


# --- rendering ----------------------------------------------------------------------------

def _table(rows: list[dict], columns: tuple[tuple[str, str], ...], formats: dict | None = None) -> str:
    formats = formats or {}
    head = "".join(f"<th>{html.escape(title)}</th>" for _, title in columns)
    body = ""
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            if key in formats:
                cells.append(f"<td>{formats[key](value, row)}</td>")
            elif key.endswith("status") and isinstance(value, str):
                cells.append(f"<td>{_badge(value)}</td>")
            elif key.endswith("freshness") and isinstance(value, str):
                cells.append(f"<td>{_fresh(value)}</td>")
            else:
                cells.append(f"<td>{_e(value if value is not None else 'N/A')}</td>")
        body += "<tr>" + "".join(cells) + "</tr>"
    return f"<table class='grid'><tr>{head}</tr>{body}</table>"


CSS = (
    "body{margin:0;background:#f8fafc;color:#1e293b;font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:14px}"
    "header{background:#fff;border-bottom:1px solid #e2e8f0;padding:20px 32px}"
    "header .org{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#64748b}"
    "header h1{margin:4px 0 10px;font-size:24px}.refresh{display:flex;flex-wrap:wrap;gap:8px 24px;font-size:13px}"
    "main{padding:20px 32px;max-width:1200px;margin:0 auto}"
    ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:18px}"
    ".kpi{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px}"
    ".kpi .k{font-size:12px;color:#64748b;text-transform:uppercase}.kpi .v{font-size:28px;font-weight:700;margin:4px 0}"
    ".kpi .m{font-size:11px;color:#64748b}"
    ".chart,.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;margin-bottom:16px}"
    ".chart h3{margin:0 0 6px;font-size:16px}.status{margin-bottom:8px;font-size:12px}"
    ".chart.unavailable{background:#f8fafc;color:#64748b}.foot{font-size:12px;color:#475569;margin:6px 0 0}"
    ".scen{margin:4px 0 0;padding-left:18px;font-size:12px;color:#475569}"
    "svg .lbl{font-size:12px;fill:#334155}svg .val{font-size:13px;font-weight:600;fill:#0f172a}"
    "svg .tick{font-size:11px;fill:#64748b}svg .dl{font-size:10px;fill:#334155}"
    "h2{font-size:17px;margin:22px 0 10px}.grid{border-collapse:collapse;width:100%;background:#fff}"
    ".grid th,.grid td{text-align:left;padding:7px 9px;border-bottom:1px solid #e2e8f0;vertical-align:top}"
    ".grid th{font-size:12px;color:#64748b;background:#f8fafc}"
    ".badge,.fresh{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700}"
    ".badge{background:#e2e8f0;color:#334155}.badge.diagnostic,.badge.oe_lifecycle_alignment_diagnostic"
    "{background:#fef3c7;color:#92400e}.badge.comparable{background:#dcfce7;color:#166534}"
    ".badge.py2027_only,.badge.py2026_only{background:#e0e7ff;color:#3730a3}"
    ".badge.not_comparable{background:#fee2e2;color:#991b1b}"
    ".fresh.live_this_run{background:#dcfce7;color:#166534}.fresh.stale_fallback{background:#fee2e2;color:#991b1b}"
    ".fresh.historical_snapshot{background:#e0f2fe;color:#075985}.fresh.unavailable{background:#f1f5f9;color:#64748b}"
    "details{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 16px;margin-bottom:12px}"
    "summary{cursor:pointer;font-weight:600}.muted{color:#64748b;font-size:12px}"
)


def _kpis(report: dict) -> str:
    cards = []
    for k in report["kpis"]:
        label = f"PY2027 {k['label']}" if k["metric_id"] in (CE, PE) else k["label"]
        cards.append(f"<div class='kpi'><div class='k'>{_e(label)}</div><div class='v'>{_e(fmt(k['value'], k['value_kind']))}"
                     f"</div><div class='m'>{_fresh(k['freshness'])} {_e(k['retrieved_at'] or '')}</div></div>")
    return f"<div class='kpis'>{''.join(cards)}</div>"


def render_html(report: dict) -> str:
    summary = report["executive_summary"]
    population = report["population_comparison"]
    nap = report["new_active_passive"]
    temporal = report["oe_temporal_diagnostic"]
    cancel = report["cancellation_termination"]
    apps = report["applications"]
    value_fmt = lambda value, row: _e(fmt(value, row.get("value_kind", "count")))  # noqa: E731
    diff_fmt = lambda value, row: DASH if value is None else _e(  # noqa: E731
        f"{value:+.2f} pp" if row.get("difference_unit") == "percentage points" else f"{value:+,}")
    pct_fmt = lambda value, row: DASH if value is None else _e(f"{value:+.2f}%")  # noqa: E731
    count_fmt = lambda value, row: _e(fmt(value))  # noqa: E731
    header = (
        "<header><div class='org'>Georgia Access</div><h1>PY2026 vs PY2027 Enrollment Comparison</h1>"
        f"<div class='refresh'><span><b>Data refreshed: {_e(summary['data_refreshed'])}</b></span>"
        f"<span>PY2026 DB: {_fresh(summary['py2026']['freshness'])} / {_e(summary['py2026']['retrieved_at'] or 'N/A')}"
        f"</span><span>PY2027 Sisense: {_fresh(summary['py2027']['freshness'])} / "
        f"{_e(summary['py2027']['retrieved_at'] or 'N/A')}</span></div>"
        + "".join(f"<p class='muted'>{_e(summary[side]['note'])}</p>" for side in ("py2026", "py2027")
                  if summary[side]["freshness"] != LIVE)
        + "</header>")
    management = _table(report["management_view"], (
        ("metric", "Metric"), ("py2026", "PY2026"), ("py2027", "PY2027"), ("change", "Change"),
        ("status", "Status"), ("freshness", "Freshness")))
    details = [
        ("Executive summary", f"<p>PY2026 source: <b>{_e(summary['py2026']['source'])}</b><br>PY2027 source: "
                              f"<b>{_e(summary['py2027']['source'])}</b></p><p>{_e(summary['statement'])}</p>"),
        ("Population comparison", _table(population["rows"], (
            ("metric", "Metric"), ("py2026_value", "PY2026"), ("py2026_freshness", "PY2026 freshness"),
            ("py2027_value", "PY2027"), ("py2027_freshness", "PY2027 freshness"),
            ("absolute_difference", "Difference"), ("percent_difference", "% Difference"),
            ("comparison_status", "Status"), ("caveat", "Caveat")),
            {"py2026_value": value_fmt, "py2027_value": value_fmt, "absolute_difference": diff_fmt,
             "percent_difference": pct_fmt}) + f"<p class='muted'>{_e(population.get('note') or population.get('reason'))}</p>"),
        ("PY2027 current OE snapshot", _table(report["py2027_current_snapshot"], (
            ("metric", "Metric"), ("value", "Value"), ("freshness", "Freshness"), ("retrieved_at", "Retrieved (UTC)"),
            ("verification", "Verification"), ("source", "Source")),
            {"value": lambda v, r: _e(fmt(v, r["value_kind"]))})),
        ("PY2026 status populations", "".join(
            f"<h4>{_e(key)}</h4>" + (_table(rows, (("status_value", "Status"), ("row_count", "Rows"),
                                                   ("distinct_people", "People"), ("distinct_policies", "Policies"),
                                                   ("comparison_status", "Status"), ("freshness", "Freshness")),
                                            {"row_count": count_fmt, "distinct_people": count_fmt,
                                             "distinct_policies": count_fmt}) if rows else "<p>Unavailable.</p>")
            for key, rows in report["py2026_status_populations"].items())),
        ("New / Active / Passive", f"<p>{_e(nap['py2026'])}</p><p>Phase 6D: NEW = {NAP_NEW_CLASS}; "
                                   f"Activity bridge = {NAP_BRIDGE}.</p><p class='muted'>{_e(nap['partition_note'])}</p>"),
        ("OE temporal diagnostic", _temporal_html(temporal, count_fmt)),
        ("Cancellation / termination", _cancel_html(cancel, count_fmt)),
        ("Applications", _table(apps["rows"], (
            ("metric", "Metric"), ("py2027", "PY2027"), ("grain", "Grain"), ("verification", "Verification"),
            ("py2026_value", "PY2026"), ("comparison_status", "Status")),
            {"py2027": lambda v, r: f"{_e(fmt(v['value']))} {_fresh(v['freshness'])}"})
            + f"<p class='muted'>{_e(apps['grain_note'])}</p>"),
        ("Comparison quality matrix", _table(report["comparison_quality_matrix"], (
            ("metric", "Metric"), ("py2026_source", "PY2026 source"), ("py2027_source", "PY2027 source"),
            ("grain", "Grain"), ("semantic_status", "Semantic status"), ("report_treatment", "Report treatment")))),
        ("Key findings", "<ul>" + "".join(f"<li>{_e(f)}</li>" for f in report["key_findings"]) + "</ul>"
         if report["key_findings"] else "<p>No findings: required evidence unavailable.</p>"),
        ("Data quality / caveats", "<ul>" + "".join(f"<li>{_e(c)}</li>" for c in report["caveats"]) + "</ul>"),
    ]
    detail_html = "".join(f"<details><summary>{_e(title)}</summary>{body}</details>" for title, body in details)
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Georgia Access — PY2026 vs PY2027</title><style>{CSS}</style></head><body>{header}<main>"
            f"{_kpis(report)}{charts(report)}<h2>Management comparison</h2>{management}"
            f"<p class='muted'>production_comparable = false. {_e(SUMMARY_STATEMENT)}</p>"
            f"<h2>Detailed diagnostics and caveats</h2>{detail_html}</main></body></html>")


def _temporal_html(temporal: dict, count_fmt) -> str:
    if not temporal.get("available"):
        return f"<p>{_badge(OE_LABEL)} Unavailable: {_e(temporal['reason'])}</p><p>{_e(ANCHOR_STATEMENT)}</p>"
    parts = [f"<p>{_badge(OE_LABEL)} {_fresh(temporal['freshness'])} {_e(temporal['statement'])}</p>"
             f"<p><b>{_e(ANCHOR_STATEMENT)}</b></p>"]
    for name, item in temporal["fields"].items():
        parts.append(f"<h4>{_e(name)}</h4>")
        parts.append(_table(item["monthly"], (("month", "Month"), ("distinct_people", "People"),
                                              ("distinct_policies", "Policies"), ("period_role", "Role")),
                            {"distinct_people": count_fmt, "distinct_policies": count_fmt})
                     if item["monthly"] else "<p>Monthly results unavailable.</p>")
        parts.append(_table(item["cumulative"], (("through", "Through"), ("distinct_people", "People"),
                                                 ("distinct_policies", "Policies")),
                            {"distinct_people": count_fmt, "distinct_policies": count_fmt})
                     if item["cumulative"] else "<p>Cumulative results unavailable.</p>")
    return "".join(parts)


def _cancel_html(cancel: dict, count_fmt) -> str:
    parts = [f"<p><b>{_e(cancel['statement'])}</b> {_e(cancel['yoy_difference'])}</p>"
             "<h4>PY2026 latest-state population</h4>"]
    parts.append(_table(cancel["py2026_population"], (("label", "Population"), ("distinct_policies", "Policies"),
                                                      ("distinct_people", "People"), ("comparison_status", "Status"),
                                                      ("freshness", "Freshness")),
                        {"distinct_policies": count_fmt, "distinct_people": count_fmt})
                 if cancel["py2026_population"] else "<p>Unavailable.</p>")
    parts.append("<h4>PY2027 daily last-update activity</h4>")
    for item in cancel["py2027_daily_activity"].values():
        if item["points"]:
            parts.append(f"<p>{_badge(PY2027_ONLY)} {_fresh(item['freshness'])} {_e(item['label'])}: "
                         f"{len(item['points'])} source dates ({_e(item['retrieved_at'])}).</p>")
        else:
            parts.append(f"<p>{_e(item['label'])}: {_e(SERIES_UNAVAILABLE)}.</p>")
    return "".join(parts)


def write_report(report: dict, output_dir: Path = EXECUTIVE_DIR) -> tuple[Path, Path]:
    guard(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def main() -> int:
    """Artifact-only rebuild. Every value is STALE_FALLBACK; use the live runner for current data."""
    report = build_report(model_from_artifacts())
    json_path, html_path = write_report(report)
    print(f"Artifact fallback report (STALE_FALLBACK): {html_path}")
    print(f"Wrote: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
