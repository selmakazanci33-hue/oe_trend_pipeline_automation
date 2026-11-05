"""Self-contained executive HTML report (inline CSS/JS, Plotly embedded for offline use).

The report renders only fields of ReportModel, which carries business values
and pipeline status. It has no access to session, cookie, header or payload data.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from html import escape
from pathlib import Path

from plotly.offline import get_plotlyjs

from analytics.derived import DERIVED, SNAPSHOT, SOURCE, Comparison, DerivedValue
from metrics.catalog import (
    PENDING_MESSAGE,
    SECTION_CURRENT_ENROLLMENT,
    SECTION_ENROLLMENT_METRICS,
    STATUS_ACTIVE,
    MetricDefinition,
    report_sections,
    roadmap_for,
)
from reporting.formatting import (
    ARROWS,
    direction,
    fmt_comparison,
    fmt_derived,
    fmt_int,
    fmt_metric_change,
    fmt_metric_value,
    fmt_pct,
    fmt_signed,
    fmt_time,
)
from reporting.model import DATA_LIVE, DATA_NOT_AVAILABLE, DATA_PARTIAL, DATA_STALE, MetricPanel, ReportModel

LATEST_REPORT = Path("latest") / "index.html"

BADGE_CLASSES = {
    "LIVE": "badge-live",
    "PASS": "badge-pass",
    "STALE": "badge-stale",
    "NOT AVAILABLE": "badge-error",
    "PARTIAL": "badge-stale",
    "ERROR": "badge-error",
    "SAVED": "badge-pass",
    "DUPLICATE_SLOT": "badge-stale",
    "DUPLICATE_RUN": "badge-stale",
    SOURCE: "badge-source",
    DERIVED: "badge-derived",
    SNAPSHOT: "badge-snapshot",
}


def _badge(text: str) -> str:
    return f'<span class="badge {BADGE_CLASSES.get(text, "badge-none")}">{escape(text)}</span>'


def _script_json(data: object) -> str:
    return json.dumps(data, separators=(",", ":")).replace("</", "<\\/")


def chart_figure(model: ReportModel) -> dict:
    """Plotly figure built strictly from observed points; returns no traces when there are none."""
    points = model.analytics.points
    stored = [p for p in points if p.stored]
    unstored = [p for p in points if not p.stored]
    traces: list[dict] = []
    hover = "<b>%{y:,.0f}</b> people<br>%{x|%b %d, %Y %H:%M} UTC<extra>%{fullData.name}</extra>"

    if stored:
        traces.append({
            "type": "scatter", "mode": "lines+markers", "name": "Snapshot",
            "x": [p.observed_at.isoformat() for p in stored], "y": [p.value for p in stored],
            "line": {"color": "#1d5fa7", "width": 3, "shape": "linear"},
            "marker": {"size": 7, "color": "#1d5fa7"},
            "hovertemplate": hover,
        })
    if unstored:
        traces.append({
            "type": "scatter", "mode": "markers", "name": "Current live value (snapshot slot already recorded)",
            "x": [p.observed_at.isoformat() for p in unstored], "y": [p.value for p in unstored],
            "marker": {"size": 12, "symbol": "diamond", "color": "#f4b33f", "line": {"color": "#0e2a47", "width": 1}},
            "hovertemplate": hover,
        })

    latest = model.analytics.latest
    annotations = []
    if latest is not None:
        traces.append({
            "type": "scatter", "mode": "markers", "name": "Latest",
            "x": [latest.observed_at.isoformat()], "y": [latest.value],
            "marker": {"size": 16, "color": "rgba(15,139,141,0.25)", "line": {"color": "#0f8b8d", "width": 2}},
            "hovertemplate": hover, "showlegend": False,
        })
        annotations.append({
            "x": latest.observed_at.isoformat(), "y": latest.value, "text": f"Latest: {fmt_int(latest.value)}",
            "showarrow": True, "arrowhead": 0, "ax": 0, "ay": -36,
            "font": {"color": "#0e2a47", "size": 12}, "bgcolor": "#ffffff", "bordercolor": "#0f8b8d",
        })

    xaxis: dict = {
        "type": "date", "title": {"text": "Snapshot time (UTC)"}, "gridcolor": "#e6ecf3",
        "rangeselector": {"buttons": [
            {"count": 1, "label": "1D", "step": "day", "stepmode": "backward"},
            {"count": 7, "label": "7D", "step": "day", "stepmode": "backward"},
            {"count": 1, "label": "1M", "step": "month", "stepmode": "backward"},
            {"step": "all", "label": "All"},
        ]},
    }
    if len(points) >= 2:
        xaxis["rangeslider"] = {"visible": True, "thickness": 0.08}
    elif len(points) == 1:
        only = points[0].observed_at
        xaxis["range"] = [(only - timedelta(hours=12)).isoformat(), (only + timedelta(hours=12)).isoformat()]

    layout = {
        "autosize": True, "height": 420, "margin": {"l": 70, "r": 24, "t": 24, "b": 40},
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "#ffffff",
        "font": {"family": "Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif", "color": "#24364b"},
        "hovermode": "closest", "separators": ".,",
        "xaxis": xaxis,
        "yaxis": {"title": {"text": "Currently Enrolled People"}, "tickformat": ",.0f", "gridcolor": "#e6ecf3",
                  "rangemode": "normal"},
        "legend": {"orientation": "h", "y": -0.25},
        "annotations": annotations,
    }
    return {"data": traces, "layout": layout}


def _comparison_card(comparison: Comparison) -> str:
    if not comparison.available:
        body = f'<div class="move-value muted">{escape(comparison.change.reason or "Insufficient history")}</div>'
        basis = ""
    else:
        dir_ = direction(comparison.change.value)
        pct = f' <span class="move-pct">{escape(fmt_pct(comparison.change_pct.value))}</span>' \
            if comparison.change_pct.available else \
            f' <span class="move-pct muted">{escape(comparison.change_pct.reason or "")}</span>'
        body = (f'<div class="move-value {dir_}">{ARROWS[dir_]} {escape(fmt_signed(comparison.change.value))}'
                f'{pct}</div>')
        ref = comparison.change.reference
        basis = (f'<div class="move-basis">vs {escape(fmt_int(ref.value))} at {escape(fmt_time(ref.observed_at))}</div>'
                 if ref else "")
    return (f'<div class="card move-card"><div class="card-label">{escape(comparison.label)} '
            f'{_badge(DERIVED)}</div>{body}{basis}</div>')


def _kpi_section(model: ReportModel) -> str:
    latest = model.kpi
    metric = model.metric
    if latest is None:
        return (f'<section class="card kpi"><div class="card-label">{escape(metric.display_name.upper())} '
                f'{_badge(model.data_status)}</div><div class="kpi-value muted">No data</div>'
                f'<div class="kpi-note">No validated value has been retrieved or stored yet.</div></section>')

    if model.data_status == DATA_LIVE:
        note = f"Live value retrieved {fmt_time(latest.observed_at)}"
    else:
        note = f"Historical value — last successful snapshot {fmt_time(latest.observed_at)}"

    previous = model.analytics.previous
    if previous.available:
        dir_ = direction(previous.change.value)
        pct = fmt_pct(previous.change_pct.value) if previous.change_pct.available else previous.change_pct.reason
        trend = (f'<div class="kpi-trend {dir_}"><span>{ARROWS[dir_]} {escape(fmt_signed(previous.change.value))}'
                 f'</span><span>{ARROWS[dir_]} {escape(pct or "")}</span>'
                 f'<span class="kpi-trend-basis">Since previous snapshot '
                 f'({escape(fmt_time(previous.change.reference.observed_at))})</span></div>')
    else:
        trend = '<div class="kpi-trend muted"><span>Trend:</span><span>Insufficient historical data</span></div>'

    return (f'<section class="card kpi"><div class="card-label">{escape(metric.display_name.upper())} '
            f'{_badge(model.data_status)} {_badge(SOURCE)}</div>'
            f'<div class="kpi-value" data-testid="kpi-value">{escape(fmt_int(latest.value))}</div>'
            f'<div class="kpi-note">{escape(note)}</div>{trend}</section>')


def _trend_section(model: ReportModel, chart_id: str = "trend-chart-overview") -> str:
    empty_id = f"{chart_id}-empty"
    return ('<section class="card" data-section="enrollment_trend"><div class="section-head"><h2>Enrollment Trend</h2>'
            f'<span class="section-sub">{escape(model.metric.display_name)} — built only from stored validated '
            'snapshots</span></div>'
            f'<div id="{escape(chart_id)}" class="chart"></div><div id="{escape(empty_id)}" class="empty" hidden>'
            'No snapshot history yet. The trend appears after the first successful retrieval.</div></section>')


def _movement_section(model: ReportModel) -> str:
    cards = "".join(_comparison_card(c) for c in model.analytics.comparisons)
    return (f'<section data-section="currently_enrolled_recent_change"><div class="section-head">'
            f'<h2>Currently Enrolled — Recent Change</h2>'
            f'<span class="section-sub">{escape(model.metric.display_name)} — derived from actual snapshot history '
            f'only</span></div>'
            f'<div class="grid grid-4">{cards}</div></section>')


def _stats_rows(model: ReportModel) -> str:
    a = model.analytics
    rows: list[tuple[str, str, str]] = []
    if a.latest is not None:
        rows.append((model.metric.display_name, fmt_int(a.latest.value), SOURCE))
    stats: list[DerivedValue] = [a.historical_min, a.historical_max, a.average_daily_change, a.latest_snapshot_time]
    rows += [(d.label, fmt_derived(d), d.kind) for d in stats]
    rows += [(c.label, fmt_comparison(c), DERIVED) for c in a.comparisons]
    return "".join(
        f"<tr><td>{escape(label)}</td><td class=\"num\">{escape(value)}</td><td>{_badge(kind)}</td></tr>"
        for label, value, kind in rows
    )


def _history_section(model: ReportModel) -> str:
    return (f'<section class="card" data-section="currently_enrolled_history"><div class="section-head">'
            f'<h2>Currently Enrolled — History &amp; Trends</h2>'
            f'<span class="section-sub">{escape(model.metric.display_name)} — calculated from stored snapshot history'
            f'</span></div>'
            f'<table class="stats"><thead><tr><th>Measure</th><th class="num">Value</th><th>Type</th></tr></thead>'
            f'<tbody>{_stats_rows(model)}</tbody></table></section>')


def _quality_section(model: ReportModel) -> str:
    metric = model.metric
    rows = [
        ("Source", escape(metric.source)),
        ("Dashboard", escape(metric.dashboard)),
        ("Plan Year", escape(str(metric.plan_year))),
        ("Data Status", _badge(model.data_status)),
        ("Retrieval Status", _badge(model.retrieval_status)
         + (f' <span class="muted">({escape(model.failure_type)})</span>' if model.failure_type else "")),
        ("Last Successful Retrieval", escape(fmt_time(model.last_successful_retrieval))),
        ("Latest Snapshot", escape(fmt_derived(model.analytics.latest_snapshot_time))),
        ("Snapshot This Run", _badge(model.snapshot_write_status) if model.snapshot_write_status
         else '<span class="muted">Not written</span>'),
        ("Historical Snapshot Count", escape(str(model.analytics.stored_snapshot_count))),
    ]
    body = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
    return (f'<section class="card"><div class="section-head"><h2>Data Freshness &amp; Quality</h2>'
            f'<span class="section-sub">{escape(metric.display_name)}</span></div>'
            f'<table class="kv">{body}</table></section>')


def _banner(model: ReportModel) -> str:
    status = model.overall_data_status
    if status == DATA_PARTIAL:
        return ('<div class="banner banner-partial"><strong>Partially refreshed.</strong> Some metrics could not be '
                'refreshed in the latest run; each metric shows its own status.</div>')
    if status == DATA_STALE:
        return ('<div class="banner banner-stale"><strong>Historical data.</strong> The latest retrieval did not '
                'complete; values shown are from the last successful snapshot.</div>')
    if status == DATA_NOT_AVAILABLE:
        return ('<div class="banner banner-error"><strong>No data.</strong> No validated value has been '
                'retrieved or stored yet.</div>')
    return ""


def _roadmap_chips(items) -> str:
    return "".join(f'<li><span>{escape(i.name)}</span><span class="badge badge-none">PENDING</span></li>'
                   for i in items)


def _pending_view(module_id: str, title: str) -> str:
    items = roadmap_for(module_id)
    roadmap = f'<ul class="roadmap">{_roadmap_chips(items)}</ul>' if items else ""
    return (f'<section class="card pending"><h2>{escape(title)}</h2><p class="muted">{escape(PENDING_MESSAGE)}</p>'
            f'{roadmap}</section>')


def _panel_card(panel: MetricPanel) -> str:
    metric = panel.metric
    latest = panel.latest
    head = (f'<div class="card-label">{escape(metric.display_name.upper())} {_badge(panel.data_status)} '
            f'{_badge(SOURCE)}</div>')
    if latest is None:
        reason = f" ({panel.failure})" if panel.failure else ""
        return (f'<div class="card metric-card" data-metric="{escape(metric.metric_id)}">{head}'
                f'<div class="metric-value muted">Not available</div>'
                f'<div class="kpi-note">No validated value has been retrieved or stored yet{escape(reason)}.</div>'
                f'{_panel_snapshot(panel)}</div>')
    if panel.data_status == DATA_LIVE:
        note = f"Live value retrieved {fmt_time(latest.observed_at)}"
    else:
        note = f"Historical value — last successful snapshot {fmt_time(latest.observed_at)}"
        if panel.failure:
            note += f" (this run: {panel.failure})"
    previous = panel.analytics.previous
    if previous.available:
        dir_ = direction(previous.change.value)
        change = (f'<div class="metric-change {dir_}">{ARROWS[dir_]} '
                  f'{escape(fmt_metric_change(previous.change.value, metric.value_format))} '
                  f'<span class="muted">since previous snapshot ({escape(fmt_time(previous.change.reference.observed_at))})'
                  f'</span> {_badge(DERIVED)}</div>')
    else:
        change = f'<div class="metric-change muted">{escape(previous.change.reason or "Insufficient history")}</div>'
    return (f'<div class="card metric-card" data-metric="{escape(metric.metric_id)}">{head}'
            f'<div class="metric-value">{escape(fmt_metric_value(latest.value, metric.value_format))}</div>'
            f'<div class="kpi-note">{escape(note)}</div>{change}{_panel_snapshot(panel)}</div>')


def _panel_snapshot(panel: MetricPanel) -> str:
    state = _badge(panel.snapshot_write_status) if panel.snapshot_write_status else '<span class="muted">Not written</span>'
    return (f'<div class="metric-snapshot">Snapshot this run: {state} '
            f'<span class="muted">({panel.analytics.stored_snapshot_count} stored)</span></div>')


def _panels_section(panels, title: str, section_id: str | None = None) -> str:
    if not panels:
        return ""
    cards = "".join(_panel_card(p) for p in panels)
    attr = f' data-section="{escape(section_id)}"' if section_id else ""
    return (f'<section{attr}><div class="section-head"><h2>{escape(title)}</h2>'
            f'<span class="section-sub">Each metric carries its own status, timestamp and snapshot history</span></div>'
            f'<div class="grid grid-4">{cards}</div></section>')


def _pending_items(model: ReportModel, module_id: str):
    verified = {p.metric.display_name for p in model.panels} | {model.metric.display_name}
    return [i for i in roadmap_for(module_id) if i.name not in verified]


def _panels_in(model: ReportModel, section_id: str) -> tuple[MetricPanel, ...]:
    return tuple(
        p for p in model.panels_for("enrollment")
        if (p.metric.section_id or SECTION_ENROLLMENT_METRICS) == section_id
    )


def _definition_fields(metric: MetricDefinition) -> list[tuple[str, str]]:
    fields = [
        ("Metric", f"{escape(metric.display_name)} {_badge(SOURCE)}"),
        ("Category", escape(metric.category) if metric.category else ""),
        ("Unit", escape(metric.unit) if metric.unit else ""),
        ("Source", escape(metric.source) if metric.source else ""),
        ("Description", escape(metric.description) if metric.description else ""),
    ]
    return [(label, value) for label, value in fields if value]


def _definition_block(metric: MetricDefinition) -> str:
    body = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in _definition_fields(metric))
    return (f'<article class="definition" data-definition="{escape(metric.metric_id)}">'
            f"<h3>{escape(metric.display_name)}</h3><table class=\"kv\">{body}</table></article>")


def _production_enrollment_metrics(model: ReportModel) -> tuple[MetricDefinition, ...]:
    ordered: list[MetricDefinition] = []
    if model.metric.module_id == "enrollment":
        ordered.append(model.metric)
    seen = {m.metric_id for m in ordered}
    for panel in model.panels_for("enrollment"):
        if panel.metric.metric_id not in seen:
            ordered.append(panel.metric)
            seen.add(panel.metric.metric_id)
    return tuple(ordered)


def _definitions_section(model: ReportModel) -> str:
    metrics = _production_enrollment_metrics(model)
    if not metrics:
        return ""
    blocks = "".join(_definition_block(metric) for metric in metrics)
    return (f'<section class="card" data-section="metric_definitions"><div class="section-head">'
            f"<h2>Metric Definitions</h2></div>"
            f'<div class="definitions">{blocks}</div></section>')


def _grouped_section(model: ReportModel, section) -> str:
    panels = _panels_in(model, section.section_id)
    pending = [item for item in _pending_items(model, section.module_id) if item.section_id == section.section_id]
    if not panels and not pending:
        return ""
    sub = f'<span class="section-sub">{escape(section.subtitle)}</span>' if section.subtitle else ""
    cards = f'<div class="grid grid-4">{"".join(_panel_card(p) for p in panels)}</div>' if panels else ""
    chips = f'<ul class="roadmap compact">{_roadmap_chips(pending)}</ul>' if pending else ""
    kind = "pending-group" if not panels else "production-group"
    return (f'<section class="{kind}" data-section="{escape(section.section_id)}">'
            f'<div class="section-head"><h2>{escape(section.heading)}</h2>{sub}</div>{cards}{chips}</section>')


def _module_view(model: ReportModel, module_id: str, title: str) -> str:
    panels = model.panels_for(module_id)
    if not panels:
        return _pending_view(module_id, title)
    items = _pending_items(model, module_id)
    pending = (f'<section class="card pending"><h2>Additional {escape(title)} Metrics</h2>'
               f'<p class="muted">{escape(PENDING_MESSAGE)}</p><ul class="roadmap">{_roadmap_chips(items)}</ul></section>'
               if items else "")
    return _panels_section(panels, f"{title} Metrics") + pending


def _applications_view(model: ReportModel) -> str:
    panels = model.panels_for("applications")
    pending = _pending_items(model, "applications")
    cards = "".join(_panel_card(p) for p in panels)
    chips = f'<ul class="roadmap compact">{_roadmap_chips(pending)}</ul>' if pending else ""
    body = f'<div class="grid grid-4">{cards}</div>' if panels else ""
    return (f'<section data-section="applications_flow"><div class="section-head"><h2>Applications</h2></div>'
            f"{body}{chips}</section>")


def _enrollment_view(model: ReportModel) -> str:
    grouped = "".join(
        _grouped_section(model, section)
        for section in report_sections("enrollment")
        if section.section_id not in (SECTION_CURRENT_ENROLLMENT, SECTION_ENROLLMENT_METRICS)
    )
    return (
        f'<section data-section="{SECTION_CURRENT_ENROLLMENT}"><div class="section-head">'
        f"<h2>Current Enrollment</h2></div>{_kpi_section(model)}</section>"
        + _panels_section(_panels_in(model, SECTION_ENROLLMENT_METRICS), "Enrollment Metrics",
                          SECTION_ENROLLMENT_METRICS)
        + grouped
        + _history_section(model)
        + _trend_section(model, "trend-chart-enrollment")
        + _definitions_section(model)
    )


def render_html(model: ReportModel) -> str:
    nav = []
    views = []
    for module in model.modules:
        active = module.status == STATUS_ACTIVE
        tag = "" if active else '<span class="nav-tag">Pending</span>'
        nav.append(f'<button class="nav-item{" is-active" if module.module_id == "overview" else ""}" '
                   f'data-view="{escape(module.module_id)}">{escape(module.title)}{tag}</button>')
        if module.module_id == "overview":
            content = (_kpi_section(model) + _panels_section(model.panels, "Key Metrics")
                       + _movement_section(model) + _trend_section(model, "trend-chart-overview")
                       + _quality_section(model))
        elif module.module_id == "enrollment":
            content = _enrollment_view(model)
        elif module.module_id == "applications":
            content = _applications_view(model)
        else:
            content = _module_view(model, module.module_id, module.title)
        hidden = "" if module.module_id == "overview" else " hidden"
        views.append(f'<div class="view" id="view-{escape(module.module_id)}"{hidden}>'
                     f'<h1 class="view-title">{escape(module.title)}</h1>{content}</div>')

    figure = _script_json(chart_figure(model))
    pipeline_badge = _badge(model.pipeline_status)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Georgia Access — OE Executive Intelligence — PY{escape(str(model.metric.plan_year))}</title>
<style>{CSS}</style>
<script>{get_plotlyjs()}</script>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">GA</div>
    <div>
      <div class="brand-name">Georgia Access</div>
      <div class="brand-product">OE Executive Intelligence</div>
      <div class="brand-year">Plan Year {escape(str(model.metric.plan_year))}</div>
    </div>
  </div>
  <dl class="meta">
    <div><dt>Last Successful Refresh</dt><dd>{escape(fmt_time(model.last_successful_refresh))}</dd></div>
    <div><dt>Data Source</dt><dd>{escape(model.metric.source)}</dd></div>
    <div><dt>Data Status</dt><dd>{_badge(model.overall_data_status)}</dd></div>
    <div><dt>Pipeline Status</dt><dd>{pipeline_badge}</dd></div>
  </dl>
</header>
<div class="layout">
  <nav class="sidebar" aria-label="Dashboard modules">{"".join(nav)}</nav>
  <main>
    {_banner(model)}
    {"".join(views)}
    <footer>Generated {escape(fmt_time(model.generated_at))} · {_badge(SOURCE)} — values come from validated Sisense retrievals · {_badge(DERIVED)} — values are calculated from stored production snapshot history · {_badge(SNAPSHOT)} — metadata associated with a stored validated observation.</footer>
  </main>
</div>
<script>
(function () {{
  var figure = {figure};
  var chartIds = ["trend-chart-overview", "trend-chart-enrollment"];
  var plotOpts = {{responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"]}};
  chartIds.forEach(function (id) {{
    var chart = document.getElementById(id);
    if (!chart) return;
    if (figure.data.length) {{
      Plotly.newPlot(chart, figure.data, figure.layout, plotOpts);
    }} else {{
      chart.hidden = true;
      var empty = document.getElementById(id + "-empty");
      if (empty) empty.hidden = false;
    }}
  }});
  document.querySelectorAll(".nav-item").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      document.querySelectorAll(".nav-item").forEach(function (b) {{ b.classList.remove("is-active"); }});
      btn.classList.add("is-active");
      document.querySelectorAll(".view").forEach(function (v) {{ v.hidden = true; }});
      document.getElementById("view-" + btn.dataset.view).hidden = false;
      var visibleChart = document.getElementById("trend-chart-" + btn.dataset.view);
      if (visibleChart && figure.data.length) {{ Plotly.Plots.resize(visibleChart); }}
    }});
  }});
}})();
</script>
</body>
</html>
"""


def write_report(model: ReportModel, reports_dir: Path) -> Path:
    """Write reports/latest/index.html atomically and return its path."""
    target = Path(reports_dir) / LATEST_REPORT
    target.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(model)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".index.", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(html)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


CSS = """
:root{--navy:#0e2a47;--blue:#1d5fa7;--teal:#0f8b8d;--gold:#f4b33f;--bg:#f2f5f9;--card:#fff;--text:#24364b;
--muted:#6b7c93;--line:#e2e8f0;--up:#1a7f4b;--down:#b42318}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:15px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:24px;flex-wrap:wrap;
padding:18px 28px;background:linear-gradient(120deg,var(--navy),#16406b 60%,var(--blue));color:#fff}
.brand{display:flex;gap:14px;align-items:center}.brand-mark{width:48px;height:48px;border-radius:12px;
background:var(--gold);color:var(--navy);font-weight:800;display:grid;place-items:center;font-size:18px}
.brand-name{font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.85}
.brand-product{font-size:22px;font-weight:700}.brand-year{font-size:13px;opacity:.85}
.meta{display:flex;gap:24px;flex-wrap:wrap;margin:0}.meta dt{font-size:11px;text-transform:uppercase;
letter-spacing:.08em;opacity:.75}.meta dd{margin:4px 0 0;font-weight:600}
.layout{display:grid;grid-template-columns:230px 1fr;min-height:calc(100vh - 90px)}
.sidebar{background:#fff;border-right:1px solid var(--line);padding:16px 10px;display:flex;flex-direction:column;gap:2px}
.nav-item{all:unset;cursor:pointer;padding:10px 14px;border-radius:8px;display:flex;justify-content:space-between;gap:10px;
align-items:center;color:var(--text);font-weight:500}.nav-item:hover{background:#eef3f9}
.nav-item.is-active{background:var(--navy);color:#fff}.nav-tag{font-size:10px;text-transform:uppercase;
letter-spacing:.06em;color:var(--muted)}.nav-item.is-active .nav-tag{color:#cfd8e3}
main{padding:24px 28px 40px;display:flex;flex-direction:column;gap:20px;min-width:0}
.view{display:flex;flex-direction:column;gap:20px}.view[hidden]{display:none}
.view-title{margin:0;font-size:20px;color:var(--navy)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;
box-shadow:0 1px 2px rgba(14,42,71,.05)}
.card-label{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;
display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.kpi-value{font-size:56px;font-weight:800;color:var(--navy);line-height:1.1;margin-top:8px;font-variant-numeric:tabular-nums}
.kpi-note{color:var(--muted);margin-top:4px}.kpi-trend{display:flex;gap:16px;flex-wrap:wrap;margin-top:14px;
font-weight:700;font-size:17px;align-items:baseline}.kpi-trend-basis{font-weight:400;font-size:13px;color:var(--muted)}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}.muted{color:var(--muted);font-weight:400}
.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.section-head h2,.pending h2{margin:0;font-size:17px;color:var(--navy)}.section-sub{font-size:13px;color:var(--muted)}
.pending{padding:14px 16px}.pending .muted{margin:6px 0 0;font-size:13px}
.pending-group{padding:2px 0}.pending-group .section-head{margin-bottom:6px}
.pending-group h2{font-size:15px;font-weight:600;color:var(--muted)}
.roadmap.compact{margin:0;gap:6px;grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}
.roadmap.compact li{padding:6px 10px;font-size:13px}
.definitions{display:grid;gap:16px}.definition h3{margin:0 0 8px;font-size:15px;color:var(--navy)}
.grid{display:grid;gap:16px}.grid-4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.move-value{font-size:24px;font-weight:700;margin-top:10px;font-variant-numeric:tabular-nums}
.move-value.muted{font-size:16px}.move-pct{font-size:15px;margin-left:6px}
.move-basis{font-size:12px;color:var(--muted);margin-top:6px}
.chart{width:100%;min-height:420px}.empty{padding:40px;text-align:center;color:var(--muted);
border:1px dashed var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse}.kv th{text-align:left;width:38%;color:var(--muted);font-weight:500}
.kv th,.kv td,.stats th,.stats td{padding:10px 8px;border-bottom:1px solid var(--line)}
.stats thead th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.06em}
.badge-live{background:#dcfce7;color:#166534}.badge-pass{background:#dbeafe;color:#1e40af}
.badge-stale{background:#fef3c7;color:#92400e}.badge-error{background:#fee2e2;color:#991b1b}
.badge-none{background:#e5e7eb;color:#374151}.badge-source{background:#e0f2f1;color:#0f766e}
.badge-derived{background:#ede9fe;color:#5b21b6}.badge-snapshot{background:#e8eef5;color:#1e3a5f}
.banner{padding:12px 16px;border-radius:10px}.banner-stale{background:#fff7e6;border:1px solid #f6d48a}
.banner-error{background:#fdecec;border:1px solid #f5b5b5}.banner-partial{background:#fff7e6;border:1px solid #f6d48a}
.roadmap{list-style:none;padding:0;margin:14px 0 0;display:grid;gap:8px;
grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}.roadmap li{display:flex;justify-content:space-between;
align-items:center;gap:8px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fafbfd}
.metric-snapshot{margin-top:8px;font-size:12px;color:var(--muted)}
.metric-value{font-size:34px;font-weight:800;color:var(--navy);line-height:1.15;margin-top:8px;font-variant-numeric:tabular-nums}
.metric-value.muted{font-size:20px;color:var(--muted)}.metric-change{margin-top:10px;font-weight:700;font-size:14px}
footer{font-size:12px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
@media (max-width:860px){.layout{grid-template-columns:1fr}.sidebar{flex-direction:row;overflow-x:auto;
border-right:0;border-bottom:1px solid var(--line)}.nav-item{white-space:nowrap}.kpi-value{font-size:42px}}
"""
