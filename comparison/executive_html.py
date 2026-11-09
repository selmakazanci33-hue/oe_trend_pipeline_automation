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
    ex.DIAGNOSTIC: "Definition alignment in progress",
    ex.ALIGNMENT: "Definition alignment in progress",
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
    ex.DIAGNOSTIC: "Definition alignment in progress",
    ex.ALIGNMENT: "Definition alignment in progress",
    ex.PY2026_ONLY: "PY2026 only",
    ex.PY2027_ONLY: "PY2027 only",
    ex.NOT_COMPARABLE: "Different definitions",
}
PY2026_COLOR, PY2027_COLOR = "#94a3b8", "#2563eb"
CREATED_COLOR, CONFIRMED_COLOR = "#2563eb", "#d97706"
CE_FOOTNOTE = ex.CE_FOOTNOTE_EXECUTIVE
NAP_TITLE = "PY2027 Enrollment Mix"
NAP_FOOTNOTE = "PY2027 lifecycle classification"
TECHNICAL_ID = "technical"
ALIGNMENT_TEXT = "Definition alignment in progress"
CE_CONTEXT = "PY2026 equivalent definition not yet confirmed"
POP_CONTEXT = "Distinct people · latest database state"
NOT_COMPARABLE_TEXT = "Not directly comparable"
DATABASE_METRIC = "PY2026 database metric"
DASHBOARD_METRIC = "PY2027 dashboard metric"
ACTIVITY_TITLE = "PY2026 Enrollment Activity by Month"
ACTIVITY_NOTE = "PY2026 created and confirmed activity."
POPULATION_TREND_TITLE = "PY2027 Enrollment Population Trend"
POPULATION_TREND_SUBTITLE = "Validated Currently Enrolled snapshots by report date"
POPULATION_TREND_NOTE = "PY2026 equivalent historical population series is not available."
DAILY_POLICY_TITLE = "PY2027 Daily Policy Activity"
DAILY_POLICY_SUBTITLE = "Cancelled policies by enrollment last-update date"
APPLICANT_REFERENCE = "Applicant-level reference"
OVERVIEW_TITLE = "Plan Year Overview"
SNAPSHOT_TITLE = "Plan Year Operational Snapshot"
SNAPSHOT_NOTE = ("Plan-year metrics reflect their respective operational definitions; direct YoY differences "
                 "are shown only where definitions align.")
COMPOSITION_TITLE = "PY2026 Enrollment Status Composition"
EVOLUTION_TITLE = "PY2026 Enrollment Status Evolution"
EVOLUTION_SUBTITLE = "Historical latest-state enrollment population"
OBSERVED_CHANGE = "Change in Observed Population"
STATUS_COLORS = {"Enrolled": "#0d9488", "Cancelled": "#d97706", "Terminated": "#dc2626", "Pending": "#64748b"}


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

    return ("<header><div class='org'>Georgia Access</div><h1>Enrollment Dashboard</h1>"
            f"<div class='asof' title='{_e(summary['data_refreshed'])}'>Report as of: {_e(as_of)}</div>"
            f"<div class='sources'>{indicator('PY2026 Database', summary['py2026'])}"
            f"{indicator('PY2027 Dashboard', summary['py2027'])}</div></header>")


def kpis(report: dict) -> str:
    now = report["executive_summary"]["data_refreshed"]
    cards = []
    for k in report["kpis"]:
        secondary = freshness_note(k, now) if k["freshness"] != ex.LIVE else "<span class='fresh'>Current</span>"
        value = ex.DASH if k["value"] is None else ex.fmt(k["value"], k["value_kind"])
        cards.append(f"<div class='kpi'><div class='k'>{_e(k['label'])}</div>"
                     f"<div class='v'>{_e(value)}</div>{secondary}</div>")
    return f"<div class='kpis'>{''.join(cards)}</div>"


def _strip(cards: list[tuple[str, str, str]]) -> str:
    body = "".join(f"<div class='mini'><div class='k'>{_e(label)}</div><div class='v'>{_e(value)}</div>"
                   f"<div class='m'>{_e(note)}</div></div>" for label, value, note in cards)
    return f"<div class='strip'>{body}</div>"


def _stat_rows(rows: list[tuple[str, str]]) -> str:
    return "".join(f"<div class='stat'><span>{_e(label)}</span><b>{_e(value)}</b></div>" for label, value in rows)


def _panel(heading: str, subtitle: str, rows: list[tuple[str, str]], footnote: str = "") -> str:
    foot = f"<p class='muted'>{_e(footnote)}</p>" if footnote else ""
    return (f"<div class='year'><h4>{_e(heading)}</h4><div class='sub'>{_e(subtitle)}</div>"
            f"{_stat_rows(rows)}{foot}</div>")


def plan_year_snapshot(report: dict) -> str:
    bars = {b["status"]: b.get("distinct_people") for b in report["py2026_status_population_chart"]["bars"]}
    snapshot = {r["metric"]: r for r in report["py2027_current_snapshot"]}

    def db_count(status: str) -> str:
        people = bars.get(status)
        return ex.DASH if people is None else ex.fmt(people)

    def dashboard_value(metric: str, kind: str = "count") -> str:
        item = snapshot.get(metric)
        if item is None or item.get("value") is None:
            return ex.DASH
        return ex.fmt(item["value"], kind)

    left = _panel("PY2026", "Latest Database State",
                  [(status, db_count(status)) for status in ex.STATUS_ORDER])
    right = _panel("PY2027", "Current OE Dashboard", [
        ("Currently Enrolled", dashboard_value("Currently Enrolled")),
        ("% Effectuated", dashboard_value("% Effectuated", "ratio")),
        ("New", dashboard_value("New")),
        ("Active", dashboard_value("Active")),
        ("Passive", dashboard_value("Passive")),
    ])
    return _card(SNAPSHOT_TITLE, f"<div class='split'>{left}{right}</div><p class='muted'>{_e(SNAPSHOT_NOTE)}</p>",
                 extra_class="major")


def status_composition(report: dict) -> str:
    data = report["py2026_status_population_chart"]
    main = [b for b in data["bars"] if b["status"] in ex.STATUS_ORDER and b["distinct_people"] is not None]
    if not main:
        return _note_card(COMPOSITION_TITLE, "PY2026 status counts are not available for this report.")
    body = ex.hbar_svg([(b["status"], b["distinct_people"], STATUS_COLORS[b["status"]]) for b in main])
    return _card(COMPOSITION_TITLE, body + "<p class='muted'>Exact database count.</p>",
                 subtitle="Latest database state")


def status_evolution_chart(report: dict) -> str:
    data = report.get("py2026_status_evolution") or {}
    points = data.get("points") or []
    if not data.get("available") or not points:
        return ""
    cats = [point["date"] for point in points]
    series = []
    for label in ex.EVOLUTION_LABELS:
        values = {point["date"]: point.get(label) for point in points}
        if any(value is not None for value in values.values()):
            series.append((label, STATUS_COLORS[label], values))
    if not series:
        return ""
    note = "<p class='muted'>An all-zero snapshot was left blank.</p>" if data.get("omitted_dates") else ""
    svg = ex.line_svg(cats, series, {day: ex.day_label(day) for day in cats})
    return _card(EVOLUTION_TITLE, svg + note, subtitle=EVOLUTION_SUBTITLE, extra_class="major")


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


def _year_panel(chart_id: str, key: str, item: dict, svg: str, extra_notes: list[str], default: str | None) -> str:
    title = ex.year_chart_title(item.get("chart_base") or item["label"], item["mode"])
    notes = [n for n in (ex.year_chart_note(item["mode"]), *extra_notes) if n]
    hidden = "" if key == default else " hidden"
    yoy = " data-yoy='1'" if item["mode"] == ex.MODE_YOY else ""
    sub = "YoY Comparison" if item["mode"] == ex.MODE_YOY else status_pill(item["comparison_status"])
    return (f"<div class='panel' data-metric='{key}' data-mode='{item['mode']}'{yoy}{hidden}>"
            f"<h4>{_e(title)}</h4><div class='sub'>{sub}</div>{svg}"
            f"<p class='muted'>{' '.join(_e(n) for n in notes)}</p></div>")


def monthly_chart(report: dict) -> str:
    title = "Monthly Enrollment"
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
        if item["mode"] in (ex.MODE_YOY, ex.MODE_PY2026) and any(v is not None for v in item["py2026"]):
            series.append(("PY2026", PY2026_COLOR, dict(zip(monthly["months"], item["py2026"]))))
        if item["mode"] in (ex.MODE_YOY, ex.MODE_PY2027) and any(v is not None for v in item["py2027"]):
            series.append(("PY2027", PY2027_COLOR, dict(zip(monthly["months"], item["py2027"]))))
        svg = ex.grouped_vbar_svg(monthly["months"], series, labels=labels)
        panels.append(_year_panel("monthly", key, item, svg, [item["definition"]], monthly["default"]))
    mtd = ""
    if current:
        through = friendly_time(f"{current['through']}T00:00:00+00:00") or current["through"]
        through_day = through.split(",")[0]
        mtd = (f"<div class='mtd'><b>* {_e(current['month'])}</b>: PY2027 is month to date (through {_e(through_day)}). "
               f"PY2026 {_e(current['month'])} bars, when shown, are the <b>PY2026 Full Month</b> for context only; no "
               f"change is calculated between a full month and a partial month.<br>"
               f"Same-period comparison through {_e(through_day)}: <b>{_e(current['message'])}</b>.</div>")
    body = (_selector("monthly", monthly["metrics"], monthly["default"])
            + f"<div id='monthly'>{''.join(panels)}</div>" + mtd
            + "<p class='muted'>Future PY2027 months are left blank until data exists.</p>")
    return _card(title, body, extra_class="major")


def _line_panel(item: dict) -> str:
    cats = item["categories"]
    labels = {c: ex.day_label(c) for c in cats} if item["calendar"] else {}
    series = []
    if item["mode"] in (ex.MODE_YOY, ex.MODE_PY2026) and any(v is not None for v in item["py2026"]):
        series.append(("PY2026", PY2026_COLOR, dict(zip(cats, item["py2026"]))))
    if item["mode"] in (ex.MODE_YOY, ex.MODE_PY2027) and any(v is not None for v in item["py2027"]):
        series.append(("PY2027", PY2027_COLOR, dict(zip(cats, item["py2027"]))))
    return ex.line_svg(cats, series, labels)


def plan_year_overview(report: dict) -> str:
    """Comparability legend only. Counts stay in the snapshot and charts."""
    rows = (
        ("Currently Enrolled", ex.DASH, DASHBOARD_METRIC),
        ("% Effectuated", ex.DASH, DASHBOARD_METRIC),
        ("Enrollment status", DATABASE_METRIC, ex.DASH),
        ("Created and Confirmed", DATABASE_METRIC, ex.DASH),
        ("Enrollment mix", ex.DASH, DASHBOARD_METRIC),
        ("Applications", ex.DASH, DASHBOARD_METRIC),
        ("Daily policy activity", ex.DASH, DASHBOARD_METRIC),
    )
    body = "<table class='grid'><tr><th>Metric</th><th>PY2026</th><th>PY2027</th><th>Comparison</th></tr>"
    for metric, py26, py27 in rows:
        body += (f"<tr><td>{_e(metric)}</td><td>{_e(py26)}</td><td>{_e(py27)}</td>"
                 f"<td class='muted'>{_e(NOT_COMPARABLE_TEXT)}</td></tr>")
    return _card(OVERVIEW_TITLE, body + "</table>", subtitle="Where each metric is shown")


def py2026_activity_chart(report: dict) -> str:
    temporal = report["oe_temporal_diagnostic"]
    if not temporal.get("available"):
        return _note_card(ACTIVITY_TITLE, "Monthly enrollment activity is not available for this report.")
    series, months = [], set()
    for name, label, color in (("enrollment_create_date", "Created", CREATED_COLOR),
                               ("enrollment_confirmation_date", "Confirmed", CONFIRMED_COLOR)):
        monthly = ((temporal.get("fields") or {}).get(name) or {}).get("monthly") or []
        data = {row["month"]: row.get("distinct_people") for row in monthly if row.get("month")}
        if any(v is not None for v in data.values()):
            series.append((label, color, data))
            months.update(m for m, v in data.items() if v is not None)
    axis = sorted(months)
    if not axis or not series:
        return _note_card(ACTIVITY_TITLE, "Monthly enrollment activity is not available for this report.")
    by_name = {label: data for label, _, data in series}
    cards = []
    for label in ("Created", "Confirmed"):
        present = [(month, value) for month, value in by_name.get(label, {}).items() if value is not None]
        if present:
            month, value = max(present, key=lambda item: (item[1], item[0]))
            cards.append((f"Peak {label} Month", ex.month_label(month), ex.fmt(value)))
    cards.append(("Latest Available Month", ex.month_label(axis[-1]), ""))
    insight = _activity_insight(by_name)
    svg = ex.grouped_vbar_svg(axis, series, labels={m: ex.month_label(m) for m in axis})
    fields = (report.get("same_period") or {}).get("activity", {}).get("py2026") or {}
    cumul = []
    for name, label, color in (("enrollment_create_date", "Created", CREATED_COLOR),
                               ("enrollment_confirmation_date", "Confirmed", CONFIRMED_COLOR)):
        points = {c["through"]: c["distinct_people"] for c in fields.get(name) or [] if c.get("through")}
        if points:
            cumul.append((label, color, points))
    extra = ""
    if cumul:
        cats = sorted({d for _, _, data in cumul for d in data})
        extra = (f"<details class='inner'><summary>Cumulative activity by date</summary>"
                 f"{ex.line_svg(cats, cumul, {d: ex.day_label(d) for d in cats})}</details>")
    insight_html = f"<p class='insight'>{_e(insight)}</p>" if insight else ""
    return _card(ACTIVITY_TITLE, _strip(cards) + svg + insight_html + f"<p class='muted'>{_e(ACTIVITY_NOTE)}</p>" + extra,
                 extra_class="major")


def _activity_insight(by_name: dict) -> str | None:
    created, confirmed = by_name.get("Created") or {}, by_name.get("Confirmed") or {}
    gaps = []
    for month in set(created) & set(confirmed):
        left, right = created.get(month), confirmed.get(month)
        if left is None or right is None:
            continue
        gaps.append((abs(left - right), month, left, right))
    if not gaps:
        return None
    gap, month, left, right = max(gaps)
    higher = "Created" if left >= right else "Confirmed"
    return (f"Largest Created–Confirmed gap is {ex.month_label(month)}: {higher} is higher by {ex.fmt(gap)}.")


def population_trend_chart(report: dict) -> str:
    item = (report.get("daily_yoy") or {}).get("metrics", {}).get("enrolled")
    if not item or not item.get("selector_enabled"):
        return _note_card(POPULATION_TREND_TITLE, "No validated Currently Enrolled snapshots yet.")
    title = "Enrollment Population Trend" if item["mode"] == ex.MODE_YOY else POPULATION_TREND_TITLE
    note = "" if item["calendar"] else "<p class='muted'>Shown in observed order; calendar dates were not available.</p>"
    return _card(title, _population_strip(item) + _line_panel(item) + note, subtitle=POPULATION_TREND_SUBTITLE,
                 extra_class="major")


def _population_strip(item: dict) -> str:
    pairs = [(category, value) for category, value in zip(item["categories"], item["py2027"]) if value is not None]
    if not pairs:
        return ""
    label = ex.day_label if item.get("calendar") else str
    first_day, first_value = pairs[0]
    latest_day, latest_value = pairs[-1]
    cards = [("Latest Currently Enrolled", ex.fmt(latest_value), label(latest_day)),
             ("First Observed", ex.fmt(first_value), label(first_day))]
    if len(pairs) >= 2:
        cards.append((OBSERVED_CHANGE, f"{latest_value - first_value:+,}", "First snapshot to latest snapshot"))
    return _strip(cards)


def daily_chart(report: dict) -> str:
    daily = report["daily_yoy"]
    present = [(key, item) for key, item in daily["metrics"].items()
               if key in ("cancelled", "terminated") and item.get("selector_enabled")]
    if not present:
        return _note_card(DAILY_POLICY_TITLE, "Daily policy activity is not available for this run.")
    summary = _policy_strip(present)
    if len(present) == 1:
        svg = _line_panel(present[0][1])
    else:
        cats = sorted({c for _, item in present for c in item["categories"]})
        series = []
        colors = {"cancelled": "#d97706", "terminated": "#dc2626"}
        for key, item in present:
            series.append((item["label"], colors[key], dict(zip(item["categories"], item["py2027"]))))
        labels = {c: ex.day_label(c) for c in cats} if all(item["calendar"] for _, item in present) else {}
        svg = ex.line_svg(cats, series, labels)
    missing = [item["label"] for key, item in daily["metrics"].items()
               if key in ("cancelled", "terminated") and not item.get("selector_enabled")]
    names = [item["label"] for _, item in present]
    subtitle = DAILY_POLICY_SUBTITLE if names == ["Cancelled"] else "Policies by enrollment last-update date"
    note = "Separate from enrollment population."
    if missing:
        note += " " + " and ".join(missing) + " is not available this run."
    return _card(DAILY_POLICY_TITLE, summary + svg + f"<p class='muted'>{_e(note)}</p>", subtitle=subtitle,
                 extra_class="major")


def _policy_strip(present: list) -> str:
    cards = []
    for _, item in present:
        pairs = [(category, value) for category, value in zip(item["categories"], item.get("py2027") or [])
                 if value is not None]
        if not pairs:
            continue
        label = ex.day_label if item.get("calendar") else str
        latest_day, latest_value = pairs[-1]
        peak_day, peak_value = max(pairs, key=lambda pair: (pair[1], pair[0]))
        name = item["label"]
        prefix = f"{name} " if len(present) > 1 else ""
        cards.extend((
            (f"{prefix}Latest observed day", ex.fmt(latest_value), label(latest_day)),
            (f"{prefix}Observed period total", ex.fmt(sum(value for _, value in pairs)), f"{len(pairs)} days"),
            (f"{prefix}Peak day", ex.fmt(peak_value), label(peak_day)),
        ))
    return _strip(cards) if cards else ""


def trend_chart(report: dict) -> str:
    """Same snapshot family as the population-trend card; kept for tests that call it directly."""
    return population_trend_chart(report)


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
    title = ex.CE_YOY_TITLE
    if range_["low"] is None and py27["value"] is None:
        return _note_card(title, "Currently Enrolled is not available for this report.")
    rng = "—" if range_["low"] is None else f"{ex.fmt(range_['low'])} → {ex.fmt(range_['high'])}"
    body = (f"<div class='pair'><div><div class='k'>PY2026 database scenarios</div>"
            f"<div class='v2'>{_e(rng)}</div><div class='m'>Exact database count</div></div>"
            f"<div><div class='k'>PY2027 dashboard</div><div class='v2'>{_e(ex.fmt(py27['value']))}</div>"
            f"<div class='m'>{freshness_note(py27, report['executive_summary']['data_refreshed'])}</div>"
            f"<div class='m'>Dashboard count</div></div></div>"
            f"<p class='muted'>{_e(CE_FOOTNOTE)}</p>")
    return _card(title, body, subtitle=ex.CE_ALIGNMENT_SUBTITLE)


def same_date_table(report: dict) -> str:
    now = report["executive_summary"]["data_refreshed"]
    metrics = {r["metric"]: r for r in report["py2027_current_snapshot"]}
    head = "<tr><th>Metric</th><th>PY2026</th><th>PY2027</th><th>Difference</th><th>Comparison</th></tr>"
    body = ""
    for row in report["same_date_table"]:
        item = metrics.get(row["metric"])
        note = f"<div class='m'>{freshness_note(item, now)}</div>" if item else ""
        body += (f"<tr><td>{_e(row['metric'])}</td><td>{_e(row['py2026'])}</td><td>{_e(row['py2027'])}{note}</td>"
                 f"<td>{_e(row['difference'])}</td><td>{_e(COMPARISON_SHORT.get(row['status'], row['status']))}</td></tr>")
    return _card("Same-Date Comparison", f"<table class='grid'>{head}{body}</table>"
                 f"<p class='muted'>{_e(ex.CE_ALIGNMENT_SUBTITLE)}. Difference is blank until a PY2026 definition "
                 "is confirmed as equivalent to the dashboard metric.</p>")


def status_population(report: dict) -> str:
    data = report["py2026_status_population_chart"]
    title = ex.STATUS_POP_TITLE
    main = [b for b in data["bars"] if b["status"] in ex.STATUS_ORDER and b["distinct_people"] is not None]
    other = [b for b in data["bars"] if b["status"] not in ex.STATUS_ORDER and b["distinct_people"] is not None]
    if not main:
        return _note_card(title, "PY2026 status counts are not available for this report.")
    colors = {"Enrolled": "#0d9488", "Cancelled": "#d97706", "Terminated": "#dc2626", "Pending": PY2026_COLOR}
    body = ex.hbar_svg([(b["status"], b["distinct_people"], colors[b["status"]]) for b in main])
    if other:
        body += ("<p class='muted'>Other / pending states: "
                 + ", ".join(f"{_e(b['status'])} {_e(ex.fmt(b['distinct_people']))}" for b in other) + "</p>")
    body += (f"<p class='muted'>Exact database count for the stated latest-state definition. "
             f"{_e(ex.PY2027_STATUS_POP_UNAVAILABLE)}</p>")
    return _card(title, body, subtitle=_e(ex.STATUS_POP_SUBTITLE))


def nap_chart(report: dict) -> str:
    nap = report["new_active_passive"]["py2027"]
    items = [(label, item["value"], color) for (label, item), color in
             zip(nap.items(), ("#0d9488", PY2027_COLOR, PY2026_COLOR)) if item["value"] is not None]
    if not items:
        return _note_card(NAP_TITLE, "The PY2027 enrollment mix is not available for this report.")
    now = report["executive_summary"]["data_refreshed"]
    values = [item["value"] for _, item in nap.items()]
    total = sum(values) if values and all(value is not None for value in values) else None
    labelled = []
    for (label, item), color in zip(nap.items(), ("#0d9488", PY2027_COLOR, PY2026_COLOR)):
        if item["value"] is None:
            continue
        text = label if not total else f"{label} · {item['value'] / total * 100:.1f}%"
        labelled.append((text, item["value"], color))
    notes = " ".join(f"{_e(label)}: {freshness_note(item, now)}"
                     for label, item in nap.items() if item["freshness"] != ex.LIVE)
    note = f"<p class='muted'>{notes}</p>" if notes else ""
    return _card(NAP_TITLE, ex.hbar_svg(labelled) + note, subtitle=_e(NAP_FOOTNOTE))


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
    def _metric_note(item: dict) -> str:
        return f"<div class='m'>{freshness_note(item, now)}</div>" if item["freshness"] != ex.LIVE else ""

    stats = [f"<div><div class='k'>Applications Started</div><div class='v2'>{_e(ex.fmt(started['value']))}</div>"
             f"{_metric_note(started)}</div>",
             f"<div><div class='k'>Submitted Applications</div><div class='v2'>{_e(ex.fmt(submitted['value']))}</div>"
             f"{_metric_note(submitted)}</div>"]
    funnel = apps["funnel"]
    if funnel is not None:
        share = funnel["submitted_share_of_started"]
        stats.append(f"<div><div class='k'>Not yet submitted</div><div class='v2'>{_e(ex.fmt(funnel['not_yet_submitted']))}</div>"
                     f"<div class='m'>Submitted share: {_e('—' if share is None else f'{share:.1f}%')}</div></div>")
    applicants = apps["total_applicants"]
    if applicants["value"] is not None:
        stats.append(f"<div class='ref'><div class='k'>Total Applicants</div><div class='v2'>{_e(ex.fmt(applicants['value']))}</div>"
                     f"{_metric_note(applicants)}"
                     f"<div class='m'>{_e(APPLICANT_REFERENCE)}</div></div>")
    body = f"<div class='pair'>{''.join(stats)}</div>" + ex.hbar_svg(items)
    return _card(title, body)


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
                  "population_comparison", "py2026_status_populations", "py2026_status_evolution", "phase_8b_evidence",
                  "new_active_passive", "applications",
                  "cancellation_termination", "same_period", "monthly_yoy", "daily_yoy", "comparison_quality_matrix",
                  "history_summary", "key_findings", "caveats", "production_comparable", "report_kind", "phase")


def technical_details(report: dict) -> str:
    rows = "".join(
        f"<tr><td>{_e(r['metric'])}</td><td>{_e(r['freshness'])}</td><td>{_e(r['retrieved_at'])}</td>"
        f"<td>{_e(r['source'])}</td><td>{_e(r['verification'])}</td></tr>" for r in report["py2027_current_snapshot"])
    scenarios = "".join(f"<li>{_e(s['candidate_id'])}: {_e(ex.fmt(s['distinct_people']))} — {_e(s['description'])}</li>"
                        for s in report["currently_enrolled_range"]["scenarios"])
    dump = json.dumps({k: report.get(k) for k in TECHNICAL_KEYS}, indent=2, default=str)
    evidence = report.get("phase_8b_evidence") or {}
    evidence_html = ""
    if evidence:
        evidence_html = ("<h4>Phase 8B enrollment activity evidence</h4>"
                         f"<p>{_e(evidence.get('final_decision'))} · {_e(evidence.get('summary'))}</p>")
    return (f"<details id='{TECHNICAL_ID}' class='tech'><summary>Technical details</summary>"
            "<h4>PY2027 metric provenance</h4><table class='grid'><tr><th>Metric</th><th>Freshness</th>"
            f"<th>Retrieved (UTC)</th><th>Source</th><th>Verification</th></tr>{rows}</table>"
            f"<h4>PY2026 Currently Enrolled scenarios (CE1–CE4)</h4><p>{_e(ex.CE_TECHNICAL_NOTE)}</p>"
            f"<ul>{scenarios or '<li>None</li>'}</ul>"
            f"{evidence_html}"
            f"<p>production_comparable = {_e(report['production_comparable'])}</p>"
            f"<h4>Caveats</h4><ul>{''.join(f'<li>{_e(c)}</li>' for c in report['caveats'])}</ul>"
            f"<h4>Report data (JSON)</h4><pre>{_e(dump)}</pre></details>")


# --- page -------------------------------------------------------------------------------------

CSS = (
    "body{margin:0;background:#f8fafc;color:#1e293b;font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:14px}"
    "header{background:#fff;border-bottom:1px solid #e2e8f0;padding:16px 32px}"
    "header .org{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#64748b}"
    "header h1{margin:4px 0 6px;font-size:24px}.asof{font-size:14px;color:#334155}"
    ".sources{display:flex;gap:18px;margin-top:8px;font-size:12px;color:#475569}"
    "main{padding:16px 32px 28px;max-width:1280px;margin:0 auto}"
    ".split{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:800px){.split{grid-template-columns:1fr}}"
    ".year{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}"
    ".year h4{margin:0 0 2px;font-size:15px}"
    ".stat{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid #e2e8f0}"
    ".stat b{font-size:16px}"
    ".strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:4px 0 10px}"
    ".mini{background:#f8fafc;border-radius:8px;padding:8px 10px}.mini .v{font-size:16px;font-weight:700;margin:2px 0}"
    ".insight{margin:8px 0;font-size:13px;color:#334155}"
    ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0 0 12px}"
    ".kpi{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px}"
    ".k{font-size:12px;color:#64748b;text-transform:uppercase}.kpi .v{font-size:28px;font-weight:700;margin:4px 0}"
    ".v2{font-size:22px;font-weight:700;margin:2px 0}.m{font-size:11px;color:#64748b}"
    ".card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;margin-bottom:12px}"
    ".card h3{margin:0 0 4px;font-size:16px}.card.major h3{font-size:18px}.card.empty{background:#f8fafc}"
    ".panel h4{margin:0 0 4px;font-size:15px}"
    ".sub{font-size:12px;color:#475569;margin-bottom:8px}.muted{color:#64748b;font-size:12px}"
    ".pill{display:inline-block;padding:1px 9px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:11px;"
    "font-weight:600}.fresh{color:#166534}.fresh.na{color:#64748b}.fresh.old{color:#b45309;font-weight:600}"
    ".pair{display:flex;flex-wrap:wrap;gap:28px;margin:6px 0 10px}.ref{border-left:3px solid #e2e8f0;padding-left:12px}"
    "h2.group{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#64748b;margin:20px 0 8px;font-weight:700}"
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


def _group(title: str) -> str:
    return f"<h2 class='group'>{_e(title)}</h2>"


def executive_body(report: dict) -> str:
    return "".join((
        _group("Executive Snapshot"),
        kpis(report),
        plan_year_snapshot(report),
        _group("Plan-Year Operational View"),
        plan_year_overview(report),
        _group("PY2026 Historical View"),
        status_composition(report),
        status_evolution_chart(report),
        _group("PY2027 Current OE View"),
        nap_chart(report),
        funnel_chart(report),
        _group("Trends & Activity"),
        py2026_activity_chart(report),
        population_trend_chart(report),
        daily_chart(report),
    ))


def render_executive_html(report: dict) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Georgia Access — Enrollment Dashboard</title><style>{CSS}</style></head><body>"
            f"{header(report)}<main>{executive_body(report)}{technical_details(report)}</main>"
            f"<script>{SELECTOR_JS}</script></body></html>")
