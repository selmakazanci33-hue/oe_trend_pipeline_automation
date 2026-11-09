"""Phase 7D: management-facing HTML for the PY2026 vs PY2027 executive report.

Internal enums (freshness, comparison status) stay in the JSON report. This
renderer translates them into business language; the raw values, timestamps,
definitions and audit notes appear only inside the collapsed Technical details.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone

from comparison import executive_py2026_py2027_report as ex

STALE_WARNING_HOURS_ENV = "EXECUTIVE_STALE_WARNING_HOURS"
DEFAULT_STALE_WARNING_HOURS = 24.0

FRESHNESS_LABELS = {
    ex.LIVE: "Current",
    ex.STALE: "Last validated",
    ex.HISTORICAL: "Historical",
    ex.UNAVAILABLE: "Not available",
}
STATUS_LABELS = {
    ex.COMPARABLE: "Comparable",
    ex.DIAGNOSTIC: "Indicative comparison",
    ex.PY2027_ONLY: "PY2027 only",
    ex.PY2026_ONLY: "PY2026 only",
    ex.NOT_COMPARABLE: "Different definitions — shown separately",
    ex.OE_LABEL: "Enrollment activity trend",
    ex.VALIDATION_ONLY: "Reference only",
    ex.UNAVAILABLE: "Not available",
    ex.MODE_DISABLED: "Not available",
}
COMPARISON_SHORT = {
    ex.COMPARABLE: "Comparable",
    ex.DIAGNOSTIC: "Indicative",
    ex.PY2026_ONLY: "PY2026 only",
    ex.PY2027_ONLY: "PY2027 only",
    ex.NOT_COMPARABLE: "Different definitions",
}
PY2026_COLOR, PY2027_COLOR = "#94a3b8", "#2563eb"
CE_FOOTNOTE = "Shown for context only; definitions are not fully aligned."
NAP_TITLE = "PY2027 Enrollment Mix"
NAP_FOOTNOTE = "Dashboard lifecycle categories; historical PY2026 equivalents are not currently available."
TECHNICAL_ID = "technical"


def _e(value) -> str:
    return html.escape("—" if value is None else str(value))


def stale_warning_hours() -> float:
    try:
        return float(os.environ.get(STALE_WARNING_HOURS_ENV, DEFAULT_STALE_WARNING_HOURS))
    except ValueError:
        return DEFAULT_STALE_WARNING_HOURS


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def friendly_time(iso: str | None, with_year: bool = False) -> str | None:
    """'Nov 9, 11:22 AM UTC' (or 'Nov 9, 2026 · 11:22 AM UTC')."""
    moment = _parse(iso)
    if moment is None:
        return None
    moment = moment.astimezone(timezone.utc)
    clock = f"{moment.strftime('%I').lstrip('0') or '12'}:{moment:%M %p} UTC"
    day = f"{moment:%b} {moment.day}"
    return f"{day}, {moment.year} · {clock}" if with_year else f"{day}, {clock}"


def freshness_note(item: dict, now_iso: str | None, threshold_hours: float | None = None) -> str:
    """Business-language provenance. A last-validated value warns only beyond the age threshold."""
    freshness = item.get("freshness") or ex.UNAVAILABLE
    label = FRESHNESS_LABELS.get(freshness, "Not available")
    when = friendly_time(item.get("retrieved_at"))
    title = f" title='{_e(item.get('retrieved_at'))}'" if item.get("retrieved_at") else ""
    css = "fresh"
    if freshness in (ex.STALE, ex.HISTORICAL):
        text = f"{label} · {when}" if when else label
        threshold = stale_warning_hours() if threshold_hours is None else threshold_hours
        then, now = _parse(item.get("retrieved_at")), _parse(now_iso)
        if then is not None and now is not None and (now - then).total_seconds() > threshold * 3600:
            css += " old"
    else:
        text = label
        if freshness == ex.UNAVAILABLE:
            css += " na"
    return f"<span class='{css}'{title}>{_e(text)}</span>"


def status_pill(status: str | None) -> str:
    return f"<span class='pill'>{_e(STATUS_LABELS.get(status, status or 'Not available'))}</span>"


def _card(title: str, body: str, subtitle: str | None = None, extra_class: str = "") -> str:
    sub = f"<div class='sub'>{subtitle}</div>" if subtitle else ""
    return f"<section class='card {extra_class}'><h3>{_e(title)}</h3>{sub}{body}</section>"


def _note_card(title: str, message: str) -> str:
    return _card(title, f"<p class='muted'>{_e(message)}</p>", extra_class="empty")


# --- header and KPIs ---------------------------------------------------------------------------

def header(report: dict) -> str:
    summary = report["executive_summary"]
    as_of = friendly_time(summary["data_refreshed"], with_year=True) or "—"

    def indicator(name, side):
        item = {"freshness": side["freshness"], "retrieved_at": side["retrieved_at"]}
        return f"<span class='src'>{_e(name)} · {freshness_note(item, summary['data_refreshed'])}</span>"

    return ("<header><div class='org'>Georgia Access</div><h1>PY2026 vs PY2027 Enrollment Comparison</h1>"
            f"<div class='asof' title='{_e(summary['data_refreshed'])}'>Report as of: {_e(as_of)}</div>"
            f"<div class='sources'>{indicator('PY2026 Database', summary['py2026'])}"
            f"{indicator('PY2027 Dashboard', summary['py2027'])}</div></header>")


def kpis(report: dict) -> str:
    now = report["executive_summary"]["data_refreshed"]
    cards = []
    for k in report["kpis"]:
        label = f"PY2027 {k['label']}" if k["metric_id"] in (ex.CE, ex.PE) else k["label"]
        cards.append(f"<div class='kpi'><div class='k'>{_e(label)}</div>"
                     f"<div class='v'>{_e(ex.fmt(k['value'], k['value_kind']))}</div>"
                     f"<div class='m'>{freshness_note(k, now)}</div></div>")
    return f"<div class='kpis'>{''.join(cards)}</div>"


# --- selector charts ---------------------------------------------------------------------------

def _selector(chart_id: str, metrics: dict, default: str | None) -> str:
    buttons = []
    for key, item in metrics.items():
        on = " on" if key == default else ""
        if not item["selector_enabled"]:
            buttons.append(f"<button type='button' disabled title='{_e(ex.SELECTOR_DISABLED_TIP)}' "
                           f"data-metric='{key}'>{_e(item['label'])}</button>")
        else:
            tip = "" if item["yoy_enabled"] else " title='One plan year only; year-over-year view not available yet'"
            buttons.append(f"<button type='button' class='sel{on}' data-metric='{key}'{tip}>{_e(item['label'])}</button>")
    return f"<div class='selector' data-chart='{chart_id}'>{''.join(buttons)}</div>"


def monthly_chart(report: dict) -> str:
    title = "Monthly Enrollment Comparison — PY2026 vs PY2027"
    monthly = report["monthly_yoy"]
    if monthly["default"] is None:
        return _note_card(title, "Monthly data is not available for this report.")
    current = monthly["current_month"]
    current_label = current["month"] if current else None
    labels = {m: (f"{m}*" if m == current_label else m) for m in monthly["months"]}
    panels = []
    for key, item in monthly["metrics"].items():
        if not item["selector_enabled"]:
            continue
        series = []
        if any(v is not None for v in item["py2026"]):
            series.append(("PY2026", PY2026_COLOR, dict(zip(monthly["months"], item["py2026"]))))
        if any(v is not None for v in item["py2027"]):
            series.append(("PY2027", PY2027_COLOR, dict(zip(monthly["months"], item["py2027"]))))
        svg = ex.grouped_vbar_svg(monthly["months"], series, labels=labels)
        notes = []
        if not item["yoy_enabled"]:
            missing = "PY2027" if item["mode"] == ex.MODE_PY2026 else "PY2026"
            notes.append(f"{missing}: comparable definition not available yet — shown for one year only.")
        notes.append(_e(item["definition"]))
        hidden = "" if key == monthly["default"] else " hidden"
        panels.append(f"<div class='panel' data-metric='{key}'{hidden}><div class='sub'>{status_pill(item['comparison_status'])}</div>"
                      f"{svg}<p class='muted'>{' '.join(notes)}</p></div>")
    mtd = ""
    if current:
        through = friendly_time(f"{current['through']}T00:00:00+00:00") or current["through"]
        through_day = through.split(",")[0]
        mtd = (f"<div class='mtd'><b>* {_e(current['month'])}</b>: PY2027 is month to date (through {_e(through_day)}). "
               f"PY2026 {_e(current['month'])} bars show the <b>PY2026 Full Month</b> for context only; no "
               f"change is calculated between a full month and a partial month.<br>"
               f"Same-period comparison through {_e(through_day)}: <b>{_e(current['message'])}</b>.</div>")
    body = (_selector("monthly", monthly["metrics"], monthly["default"])
            + f"<div id='monthly'>{''.join(panels)}</div>" + mtd
            + "<p class='muted'>Future PY2027 months are left blank until data exists.</p>")
    return _card(title, body, extra_class="major")


def daily_chart(report: dict) -> str:
    title = "Daily Enrollment Comparison — PY2026 vs PY2027"
    daily = report["daily_yoy"]
    if daily["default"] is None:
        return _note_card(title, "Daily series not available for this run.")
    panels = []
    for key, item in daily["metrics"].items():
        if not item["selector_enabled"]:
            continue
        cats = item["categories"]
        labels = {c: ex.day_label(c) for c in cats} if item["calendar"] else {}
        series = []
        if any(v is not None for v in item["py2026"]):
            series.append(("PY2026", PY2026_COLOR, dict(zip(cats, item["py2026"]))))
        series.append(("PY2027", PY2027_COLOR, dict(zip(cats, item["py2027"]))))
        svg = ex.line_svg(cats, series, labels)
        notes = []
        if item["note"]:
            notes.append(item["note"])
        if not item["calendar"]:
            notes.append("Shown in observed order; calendar dates were not available.")
        if key in ("cancelled", "terminated"):
            notes.append("Daily dashboard activity; not compared with PY2026 status counts.")
        hidden = "" if key == daily["default"] else " hidden"
        panels.append(f"<div class='panel' data-metric='{key}'{hidden}><div class='sub'>{status_pill(item['comparison_status'])}</div>"
                      f"{svg}<p class='muted'>{_e(' '.join(notes))}</p></div>")
    body = _selector("daily", daily["metrics"], daily["default"]) + f"<div id='daily'>{''.join(panels)}</div>"
    return _card(title, body, extra_class="major")


def trend_chart(report: dict) -> str:
    title = "Currently Enrolled — PY2027 trend"
    trend = report["same_period"]["currently_enrolled_trend"]
    rows = trend["rows"]
    if not rows:
        return _note_card(title, "The PY2027 trend starts after the first validated observation.")
    cats = [r["py2027_date"] for r in rows]
    series = [("PY2027", PY2027_COLOR, {r["py2027_date"]: r["py2027_value"] for r in rows})]
    if trend["py2026_available"]:
        series.append(("PY2026 (same date last year)", PY2026_COLOR, {r["py2027_date"]: r["py2026_value"] for r in rows}))
    latest = FRESHNESS_LABELS.get(rows[-1]["py2027_freshness"], "Not available")
    note = "" if trend["py2026_available"] else " PY2026 history for the same dates is not available."
    body = (ex.line_svg(cats, series, {d: ex.day_label(d) for d in cats})
            + f"<p class='muted'>Latest point: {_e(latest)}.{_e(note)} The trend grows with each successful run.</p>")
    return _card(title, body, subtitle="PY2027 trend")


def activity_chart(report: dict) -> str:
    title = "Enrollment Activity — Cumulative People by Date"
    activity = report["same_period"]["activity"]
    fields = activity["py2026"]
    names = (("enrollment_create_date", "PY2026 Created", PY2026_COLOR),
             ("enrollment_confirmation_date", "PY2026 Confirmed", "#d97706"))
    series = [(label, color, {c["through"]: c["distinct_people"] for c in fields.get(name) or []})
              for name, label, color in names if fields.get(name)]
    cats = sorted({d for _, _, data in series for d in data if d})
    if not cats:
        return _note_card(title, "Enrollment activity is not available for this report.")
    body = (ex.line_svg(cats, series, {d: ex.day_label(d) for d in cats})
            + "<p class='muted'>Cumulative people with an enrollment created / confirmed by each date. "
              "A comparable PY2027 enrollment activity measure is not available yet.</p>")
    return _card(title, body, subtitle=status_pill(ex.OE_LABEL))


# --- section charts ---------------------------------------------------------------------------

def ce_chart(report: dict) -> str:
    range_ = report["currently_enrolled_range"]
    py27 = next(k for k in report["kpis"] if k["metric_id"] == ex.CE)
    title = "Currently Enrolled — PY2026 vs PY2027"
    if range_["low"] is None and py27["value"] is None:
        return _note_card(title, "Currently Enrolled is not available for this report.")
    items = []
    if range_["high"] is not None:
        items.append(("PY2026 (high estimate)", range_["high"], PY2026_COLOR))
        items.append(("PY2026 (low estimate)", range_["low"], PY2026_COLOR))
    if py27["value"] is not None:
        items.append(("PY2027", py27["value"], PY2027_COLOR))
    rng = "—" if range_["low"] is None else f"{ex.fmt(range_['low'])} → {ex.fmt(range_['high'])}"
    lines = [f"<div class='pair'><div><div class='k'>PY2026 estimated diagnostic range</div><div class='v2'>{_e(rng)}</div></div>"
             f"<div><div class='k'>PY2027</div><div class='v2'>{_e(ex.fmt(py27['value']))}</div>"
             f"<div class='m'>{freshness_note(py27, report['executive_summary']['data_refreshed'])}</div></div></div>"]
    closest = report.get("closest_scenario")
    if closest is not None:
        lines.append(f"<p>Closest available PY2026 scenario: <b>{_e(ex.fmt(closest['py2026_value']))}</b><sup>†</sup></p>")
    body = "".join(lines) + ex.hbar_svg(items) + f"<p class='muted'>† {_e(CE_FOOTNOTE)}</p>"
    return _card(title, body, subtitle="Indicative comparison")


def same_date_table(report: dict) -> str:
    now = report["executive_summary"]["data_refreshed"]
    metrics = {r["metric"]: r for r in report["py2027_current_snapshot"]}
    head = "<tr><th>Metric</th><th>PY2026</th><th>PY2027</th><th>Difference</th><th>Comparison</th></tr>"
    body = ""
    for row in report["same_date_table"]:
        item = metrics.get(row["metric"])
        note = f"<div class='m'>{freshness_note(item, now)}</div>" if item else ""
        diff = row["difference"].split(" (")[0] if row["difference"] != ex.DASH else ex.DASH
        body += (f"<tr><td>{_e(row['metric'])}</td><td>{_e(row['py2026'])}</td><td>{_e(row['py2027'])}{note}</td>"
                 f"<td>{_e(diff)}</td><td>{_e(COMPARISON_SHORT.get(row['status'], row['status']))}</td></tr>")
    return _card("Same-Date Comparison", f"<table class='grid'>{head}{body}</table>"
                 "<p class='muted'>Currently Enrolled difference uses the closest available PY2026 scenario, "
                 "for context only.</p>")


def status_population(report: dict) -> str:
    data = report["py2026_status_population_chart"]
    title = "PY2026 Latest-State Enrollment Status"
    main = [b for b in data["bars"] if b["status"] in ex.STATUS_ORDER and b["distinct_people"] is not None]
    other = [b for b in data["bars"] if b["status"] not in ex.STATUS_ORDER and b["distinct_people"] is not None]
    if not main:
        return _note_card(title, "PY2026 status counts are not available for this report.")
    colors = {"Enrolled": "#0d9488", "Cancelled": "#d97706", "Terminated": "#dc2626", "Pending": PY2026_COLOR}
    body = ex.hbar_svg([(b["status"], b["distinct_people"], colors[b["status"]]) for b in main])
    if other:
        body += ("<p class='muted'>Other / pending states: "
                 + ", ".join(f"{_e(b['status'])} {_e(ex.fmt(b['distinct_people']))}" for b in other) + "</p>")
    body += f"<p class='muted'>Distinct people. {_e(ex.PY2027_STATUS_POP_UNAVAILABLE)}</p>"
    return _card(title, body, subtitle=status_pill(ex.PY2026_ONLY))


def nap_chart(report: dict) -> str:
    nap = report["new_active_passive"]["py2027"]
    items = [(label, item["value"], color) for (label, item), color in
             zip(nap.items(), ("#0d9488", PY2027_COLOR, PY2026_COLOR)) if item["value"] is not None]
    if not items:
        return _note_card(NAP_TITLE, "The PY2027 enrollment mix is not available for this report.")
    now = report["executive_summary"]["data_refreshed"]
    notes = " ".join(f"{_e(label)}: {freshness_note(item, now)}" for label, item in nap.items())
    return _card(NAP_TITLE, ex.hbar_svg(items) + f"<p class='muted'>{notes}</p><p class='muted'>{_e(NAP_FOOTNOTE)}</p>",
                 subtitle=status_pill(ex.PY2027_ONLY))


def funnel_chart(report: dict) -> str:
    title = "PY2027 Application Funnel"
    apps = report["applications"]
    now = report["executive_summary"]["data_refreshed"]
    rows = {r["metric"]: r["py2027"] for r in apps["rows"]}
    started, submitted = rows["Applications Started"], rows["Submitted Applications"]
    items = [(k, rows[k]["value"], c) for k, c in (("Applications Started", PY2027_COLOR),
                                                   ("Submitted Applications", "#0d9488")) if rows[k]["value"] is not None]
    if not items:
        return _note_card(title, "Application counts are not available for this report.")
    stats = [f"<div><div class='k'>Applications Started</div><div class='v2'>{_e(ex.fmt(started['value']))}</div>"
             f"<div class='m'>{freshness_note(started, now)}</div></div>",
             f"<div><div class='k'>Submitted Applications</div><div class='v2'>{_e(ex.fmt(submitted['value']))}</div>"
             f"<div class='m'>{freshness_note(submitted, now)}</div></div>"]
    funnel = apps["funnel"]
    if funnel is not None:
        share = funnel["submitted_share_of_started"]
        stats.append(f"<div><div class='k'>Not yet submitted</div><div class='v2'>{_e(ex.fmt(funnel['not_yet_submitted']))}</div>"
                     f"<div class='m'>Submitted share: {_e('—' if share is None else f'{share:.1f}%')}</div></div>")
    applicants = apps["total_applicants"]
    if applicants["value"] is not None:
        stats.append(f"<div class='ref'><div class='k'>Total Applicants</div><div class='v2'>{_e(ex.fmt(applicants['value']))}</div>"
                     f"<div class='m'>{freshness_note(applicants, now)}</div>"
                     f"<div class='m'>Reference metric — applicant grain</div></div>")
    body = f"<div class='pair'>{''.join(stats)}</div>" + ex.hbar_svg(items)
    return _card(title, body, subtitle=status_pill(ex.PY2027_ONLY))


def lifecycle_chart(report: dict) -> str:
    title = "PY2026 Enrollment Lifecycle Activity by Month"
    temporal = report["oe_temporal_diagnostic"]
    if not temporal.get("available"):
        return _note_card(title, "Monthly enrollment activity is not available for this report.")

    def series_for(measure, suffix):
        out = []
        for name, label, color in (("enrollment_create_date", f"Created {suffix}", PY2027_COLOR),
                                   ("enrollment_confirmation_date", f"Confirmed {suffix}", "#d97706")):
            monthly = temporal["fields"][name]["monthly"] or []
            out.append((label, color, {m["month"]: m.get(measure) for m in monthly}))
        return out

    people = series_for("distinct_people", "People")
    months = sorted({m for _, _, data in people for m in data})
    if not months:
        return _note_card(title, "Monthly enrollment activity is not available for this report.")
    labels = {m: ex.month_label(m) for m in months}
    text = "Open enrollment: November 2025 onward"
    svg = ex.grouped_vbar_svg(months, people, labels=labels, highlight_from=ex.OE_EMPHASIS_FROM, highlight_text=text)
    policies = ex.grouped_vbar_svg(months, series_for("distinct_policies", "Policies"), labels=labels,
                                   highlight_from=ex.OE_EMPHASIS_FROM, highlight_text=text)
    body = (svg + "<p class='muted'>People by the month their enrollment was created or confirmed. "
                  "These are activity dates, not month-end enrollment counts.</p>"
            f"<details class='inner'><summary>Policies view</summary>{policies}</details>")
    return _card(title, body, subtitle=status_pill(ex.OE_LABEL))


def cancellation_chart(report: dict) -> str:
    title = "PY2027 Cancellation / Termination Daily Activity"
    activity = report["cancellation_termination"]["py2027_daily_activity"]
    present = [(item["label"], item) for item in activity.values() if item.get("points")]
    if not present:
        return _note_card(title, "Daily cancellation and termination activity is not available for this run.")
    calendar = all(ex.daily_axis(item["points"])[1] for _, item in present)
    series, colors = [], ("#d97706", "#dc2626")
    for (label, item), color in zip(present, colors):
        if calendar:
            series.append((label, color, {p["date"]: p["value"] for p in item["points"]}))
        else:
            series.append((label, color, {f"Day {i}": p["value"] for i, p in enumerate(item["points"], 1)}))
    if calendar:
        cats = sorted({d for _, _, data in series for d in data})
        labels = {d: ex.day_label(d) for d in cats}
    else:
        cats = [f"Day {i}" for i in range(1, max(len(item["points"]) for _, item in present) + 1)]
        labels = {}
    missing = [item["label"] for item in activity.values() if not item.get("points")]
    note = "Policies updated to each status per day on the dashboard. Not compared with PY2026 status counts."
    if not calendar:
        note += " Shown in observed order; calendar dates were not available."
    if missing:
        note += f" Not available this run: {', '.join(missing)}."
    return _card(title, ex.grouped_vbar_svg(cats, series, labels=labels) + f"<p class='muted'>{_e(note)}</p>",
                 subtitle=status_pill(ex.PY2027_ONLY))


def management_table(report: dict) -> str:
    now = report["executive_summary"]["data_refreshed"]
    snapshot = {r["metric"]: r for r in report["py2027_current_snapshot"]}
    head = "<tr><th>Metric</th><th>PY2026</th><th>PY2027</th><th>Change</th><th>Comparison</th></tr>"
    body = ""
    for row in report["management_view"]:
        name = row["metric"].split(" (")[0]
        item = snapshot.get(name)
        note = ""
        if item is not None and item["freshness"] != ex.LIVE:
            note = f"<div class='m'>{freshness_note(item, now)}</div>"
        status = row["status"].split(" ")[0]
        label = COMPARISON_SHORT.get(status, status)
        if "validation-only" in row["status"]:
            label += " · Reference"
        body += (f"<tr><td>{_e(name)}</td><td>{_e(row['py2026'])}</td><td>{_e(row['py2027'])}{note}</td>"
                 f"<td>{_e(row['change'])}</td><td>{_e(label)}</td></tr>")
    return _card("Management View", f"<table class='grid'>{head}{body}</table>")


# --- technical details -------------------------------------------------------------------------

TECHNICAL_KEYS = ("executive_summary", "py2027_current_snapshot", "currently_enrolled_range", "closest_scenario",
                  "population_comparison", "py2026_status_populations", "new_active_passive", "applications",
                  "cancellation_termination", "same_period", "monthly_yoy", "daily_yoy", "comparison_quality_matrix",
                  "history_summary", "key_findings", "caveats", "production_comparable", "report_kind", "phase")


def technical_details(report: dict) -> str:
    rows = "".join(
        f"<tr><td>{_e(r['metric'])}</td><td>{_e(r['freshness'])}</td><td>{_e(r['retrieved_at'])}</td>"
        f"<td>{_e(r['source'])}</td><td>{_e(r['verification'])}</td></tr>" for r in report["py2027_current_snapshot"])
    scenarios = "".join(f"<li>{_e(s['candidate_id'])}: {_e(ex.fmt(s['distinct_people']))} — {_e(s['description'])}</li>"
                        for s in report["currently_enrolled_range"]["scenarios"])
    dump = json.dumps({k: report.get(k) for k in TECHNICAL_KEYS}, indent=2, default=str)
    return (f"<details id='{TECHNICAL_ID}' class='tech'><summary>Technical details</summary>"
            "<h4>PY2027 metric provenance</h4><table class='grid'><tr><th>Metric</th><th>Freshness</th>"
            f"<th>Retrieved (UTC)</th><th>Source</th><th>Verification</th></tr>{rows}</table>"
            f"<h4>PY2026 Currently Enrolled scenarios (CE1–CE4)</h4><ul>{scenarios or '<li>None</li>'}</ul>"
            f"<p>production_comparable = {_e(report['production_comparable'])}</p>"
            f"<h4>Caveats</h4><ul>{''.join(f'<li>{_e(c)}</li>' for c in report['caveats'])}</ul>"
            f"<h4>Report data (JSON)</h4><pre>{_e(dump)}</pre></details>")


# --- page -------------------------------------------------------------------------------------

CSS = (
    "body{margin:0;background:#f8fafc;color:#1e293b;font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:14px}"
    "header{background:#fff;border-bottom:1px solid #e2e8f0;padding:20px 32px}"
    "header .org{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#64748b}"
    "header h1{margin:4px 0 6px;font-size:24px}.asof{font-size:14px;color:#334155}"
    ".sources{display:flex;gap:18px;margin-top:8px;font-size:12px;color:#475569}"
    "main{padding:20px 32px;max-width:1200px;margin:0 auto}"
    ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:18px}"
    ".kpi{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px}"
    ".k{font-size:12px;color:#64748b;text-transform:uppercase}.kpi .v{font-size:28px;font-weight:700;margin:4px 0}"
    ".v2{font-size:22px;font-weight:700;margin:2px 0}.m{font-size:11px;color:#64748b}"
    ".card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;margin-bottom:16px}"
    ".card h3{margin:0 0 4px;font-size:16px}.card.major h3{font-size:18px}.card.empty{background:#f8fafc}"
    ".sub{font-size:12px;color:#475569;margin-bottom:8px}.muted{color:#64748b;font-size:12px}"
    ".pill{display:inline-block;padding:1px 9px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:11px;"
    "font-weight:600}.fresh{color:#166534}.fresh.na{color:#64748b}.fresh.old{color:#b45309;font-weight:600}"
    ".pair{display:flex;flex-wrap:wrap;gap:28px;margin:6px 0 10px}.ref{border-left:3px solid #e2e8f0;padding-left:12px}"
    "h2{font-size:20px;margin:26px 0 10px}h2.major{border-left:4px solid #2563eb;padding-left:10px}"
    ".selector{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}"
    ".selector button{border:1px solid #cbd5e1;background:#fff;border-radius:6px;padding:4px 12px;cursor:pointer;"
    "font-size:13px}.selector button.on{background:#2563eb;color:#fff;border-color:#2563eb}"
    ".selector button:disabled{color:#94a3b8;background:#f1f5f9;cursor:not-allowed}"
    ".mtd{background:#f8fafc;border-radius:8px;padding:8px 12px;font-size:12px;color:#334155;margin-top:6px}"
    "svg .lbl{font-size:12px;fill:#334155}svg .val{font-size:13px;font-weight:600;fill:#0f172a}"
    "svg .tick{font-size:11px;fill:#64748b}svg .dl{font-size:10px;fill:#334155}"
    ".grid{border-collapse:collapse;width:100%}.grid th,.grid td{text-align:left;padding:7px 9px;"
    "border-bottom:1px solid #e2e8f0;vertical-align:top}.grid th{font-size:12px;color:#64748b;background:#f8fafc}"
    "details.tech{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 16px;margin:24px 0}"
    "details summary{cursor:pointer;font-weight:600}details.inner{margin-top:6px}"
    "pre{white-space:pre-wrap;font-size:11px;background:#f8fafc;padding:10px;border-radius:6px;max-height:480px;"
    "overflow:auto}"
)

SELECTOR_JS = (
    "document.querySelectorAll('.selector').forEach(function(group){"
    "var root=document.getElementById(group.getAttribute('data-chart'));if(!root){return;}"
    "group.querySelectorAll('button.sel').forEach(function(btn){btn.addEventListener('click',function(){"
    "root.querySelectorAll('.panel').forEach(function(p){p.hidden=p.getAttribute('data-metric')!==btn.getAttribute('data-metric');});"
    "group.querySelectorAll('button.sel').forEach(function(b){b.classList.toggle('on',b===btn);});});});});"
)


def executive_body(report: dict) -> str:
    return "".join((
        kpis(report),
        f"<h2 class='major'>{_e(ex.SAME_PERIOD_TITLE)}</h2>",
        monthly_chart(report), daily_chart(report), trend_chart(report), activity_chart(report),
        "<h2>Currently Enrolled comparison</h2>", ce_chart(report), same_date_table(report),
        "<h2>PY2026 Status Population</h2>", status_population(report),
        "<h2>PY2027 Enrollment Mix</h2>", nap_chart(report),
        "<h2>PY2027 Application Funnel</h2>", funnel_chart(report),
        "<h2>PY2026 Monthly Enrollment Lifecycle Activity</h2>", lifecycle_chart(report),
        "<h2>PY2027 Cancellation / Termination Daily Activity</h2>", cancellation_chart(report),
        "<h2>Management View</h2>", management_table(report),
    ))


def render_executive_html(report: dict) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Georgia Access — PY2026 vs PY2027</title><style>{CSS}</style></head><body>"
            f"{header(report)}<main>{executive_body(report)}{technical_details(report)}</main>"
            f"<script>{SELECTOR_JS}</script></body></html>")
