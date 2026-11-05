"""Developer-facing Wave 1 discovery artifact (not the executive dashboard).

Contains structural metadata only: widget ids/titles/types, column titles,
filter counts, HTTP statuses, response shapes. No metric values, filter
values, request/response bodies, headers or cookies.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from metrics.catalog import STATE_DISCOVERED, STATE_PENDING, STATE_VERIFIED, WAVE1_TARGETS
from metrics.discovery import MetricDiscovery
from reporting.formatting import fmt_time
from sisense.discovery import DiscoveryResult, WidgetDiscovery, normalize_label

DISCOVERY_DIR = Path("discovery")
HTML_NAME = "wave1_metric_discovery.html"
JSON_NAME = "wave1_metric_discovery.json"

_STATE_CLASS = {STATE_VERIFIED: "verified", STATE_DISCOVERED: "discovered", STATE_PENDING: "pending"}
_TARGET_LABELS = {normalize_label(t.label): t.label for t in WAVE1_TARGETS}


def _widget_dict(widget: WidgetDiscovery) -> dict[str, Any]:
    s = widget.structure
    r = widget.response
    return {
        "widget_id": s.widget_id,
        "widget_title": s.widget_title,
        "widget_type": s.widget_type,
        "datasource": s.datasource_title,
        "measures": [c.title for c in s.measures],
        "dimensions": [{"title": c.title, "date": c.is_date} for c in s.dimensions],
        "filter_count": s.filter_count,
        "dashboard_filter_count": s.dashboard_filter_count,
        "observations": widget.observations,
        "structure_changed": widget.structure_changed,
        "matches": {label: list(sources) for label, sources in widget.matches.items()},
        "control": widget.control,
        "response": None if r is None else {
            "http_status": r.http_status,
            "available": r.available,
            "shape": r.shape,
            "rows": r.rows,
            "value_cells": r.value_cells,
            "time_points": r.time_points,
            "first_period": r.first_period,
            "last_period": r.last_period,
            "matched_category_labels": list(r.matched_category_labels),
            "failure": r.failure,
        },
    }


def discovery_document(
    metrics: tuple[MetricDiscovery, ...], result: DiscoveryResult, generated_at: datetime
) -> dict[str, Any]:
    return {
        "generated_at_utc": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "Naturally occurring dashboard JAQL requests only; structure only, no values",
        "observed": {
            "jaql_posts": result.target_requests_seen,
            "out_of_scope": result.out_of_scope,
            "widgets": len(result.widgets),
            "widgets_dropped": result.widgets_dropped,
            "retention_skipped": result.retention_skipped,
        },
        "metrics": [
            {
                "metric_id": m.metric_id,
                "label": m.label,
                "module": m.module_id,
                "state": m.state,
                "widget_ids": [w.structure.widget_id for w in m.widgets],
                "match_sources": list(m.match_sources),
                "reason": m.reason,
                "control": m.control,
            }
            for m in metrics
        ],
        "widgets": [_widget_dict(w) for w in result.widgets],
    }


def _e(value: Any) -> str:
    return escape("—" if value is None or value == "" else str(value))


def _state_badge(state: str) -> str:
    return f'<span class="badge {_STATE_CLASS.get(state, "pending")}">{_e(state)}</span>'


def _summary_rows(metrics: tuple[MetricDiscovery, ...]) -> str:
    rows = []
    for m in metrics:
        widget = m.widget
        shape = http = None
        if widget is not None:
            widget_html = f"{_e(widget.structure.widget_title)}<div class=\"muted\">{_e(widget.structure.widget_id)}</div>"
            shape = widget.response.shape if widget.response else "NOT INSPECTED"
            http = widget.response.http_status if widget.response else None
        elif m.widgets:
            widget_html = _e(f"{len(m.widgets)} widgets")
        else:
            widget_html = _e(None)
        rows.append(
            f"<tr><td>{_e(m.label)}</td><td>{widget_html}</td><td>{_e(shape)}</td><td>{_e(http)}</td>"
            f"<td>{_state_badge(m.state)}</td><td>{_e(m.reason)}</td></tr>"
        )
    return "".join(rows)


def _widget_card(widget: WidgetDiscovery, states: list[tuple[str, str]]) -> str:
    s = widget.structure
    r = widget.response
    measures = "".join(f"<li>{_e(c.title)}</li>" for c in s.measures) or "<li>—</li>"
    dimensions = "".join(
        f"<li>{_e(c.title)}{' <span class=muted>(date)</span>' if c.is_date else ''}</li>" for c in s.dimensions
    ) or "<li>—</li>"
    matches = "".join(
        f"<li>{_e(_TARGET_LABELS.get(key, key))} <span class=\"muted\">via {_e(', '.join(sources))}</span></li>"
        for key, sources in widget.matches.items()
    ) or "<li>—</li>"
    rows = [
        ("Widget ID", _e(s.widget_id)),
        ("Type", _e(s.widget_type)),
        ("Datasource", _e(s.datasource_title)),
        ("Filters", f"{s.filter_count} filters ({s.dashboard_filter_count} dashboard-level)"),
        ("Observations", f"{widget.observations}{' (structure changed between observations)' if widget.structure_changed else ''}"),
        ("HTTP", _e(r.http_status if r else None)),
        ("Response Available", "Yes" if r and r.available else ("No" if r else "Not inspected")),
        ("Response Shape", _e(r.shape if r else None)),
    ]
    if r is not None and r.available:
        rows.append(("Rows / Value Cells", f"{r.rows} / {r.value_cells}"))
    if r is not None and r.time_points:
        rows.append(("Time Points", f"{r.time_points} ({_e(r.first_period)} → {_e(r.last_period)})"))
    if r is not None and r.failure:
        rows.append(("Response Note", _e(r.failure)))
    if widget.control is not None:
        rows.append(("Verified Control Check", _e(widget.control)))
    rows.append(("Discovery Status", " ".join(f"{_e(label)} {_state_badge(state)}" for label, state in states)))
    table = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
    return (
        f'<section class="card"><h3>{_e(s.widget_title)}</h3><table class="kv">{table}</table>'
        f'<div class="cols"><div><h4>Measures</h4><ul>{measures}</ul></div>'
        f'<div><h4>Dimensions</h4><ul>{dimensions}</ul></div>'
        f'<div><h4>Exact label matches</h4><ul>{matches}</ul></div></div></section>'
    )


def render_discovery_html(
    metrics: tuple[MetricDiscovery, ...], result: DiscoveryResult, generated_at: datetime
) -> str:
    by_widget: dict[str, list[tuple[str, str]]] = {}
    for m in metrics:
        for w in m.widgets:
            by_widget.setdefault(w.structure.widget_id, []).append((m.label, m.state))
    relevant = [w for w in result.widgets if w.structure.widget_id in by_widget]
    others = [w for w in result.widgets if w.structure.widget_id not in by_widget]
    cards = "".join(_widget_card(w, by_widget[w.structure.widget_id]) for w in relevant) or (
        '<p class="muted">No dashboard widget carried an exact Wave 1 label.</p>')
    other_rows = "".join(
        f"<tr><td>{_e(w.structure.widget_title)}</td><td>{_e(w.structure.widget_id)}</td>"
        f"<td>{_e(w.structure.widget_type)}</td><td>{len(w.structure.measures)}</td>"
        f"<td>{len(w.structure.dimensions)}</td><td>{w.structure.filter_count}</td>"
        f"<td>{_e(w.response.shape if w.response else 'NOT INSPECTED')}</td></tr>"
        for w in others
    )
    counts = {state: sum(m.state == state for m in metrics) for state in (STATE_VERIFIED, STATE_DISCOVERED, STATE_PENDING)}
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wave 1 Metric Discovery — Developer Review</title><style>{CSS}</style></head>
<body><header><div class="eyebrow">Developer review — not the executive dashboard</div>
<h1>Executive Core Discovery Summary</h1>
<p>Generated {_e(fmt_time(generated_at))} from the dashboard's own JAQL requests only.
Structure only: no metric values, filter values, payloads or credentials.</p>
<div class="stats"><span>{result.target_requests_seen} JAQL POSTs observed</span>
<span>{len(result.widgets)} dashboard widgets</span><span>{result.out_of_scope} out of scope</span>
<span>{_state_badge(STATE_VERIFIED)} {counts[STATE_VERIFIED]}</span>
<span>{_state_badge(STATE_DISCOVERED)} {counts[STATE_DISCOVERED]}</span>
<span>{_state_badge(STATE_PENDING)} {counts[STATE_PENDING]}</span></div></header>
<main>
<section class="card"><h2>Wave 1 targets</h2>
<table class="grid"><thead><tr><th>Metric</th><th>Widget</th><th>Shape</th><th>HTTP</th><th>Status</th><th>Reason</th></tr></thead>
<tbody>{_summary_rows(metrics)}</tbody></table>
<p class="muted">DISCOVERED is not VERIFIED: discovered widgets are not stored in history or shown in the executive dashboard.</p></section>
<h2>Matched widgets</h2>{cards}
<section class="card"><h2>Other observed widgets ({len(others)})</h2>
<table class="grid"><thead><tr><th>Title</th><th>Widget ID</th><th>Type</th><th>Measures</th><th>Dimensions</th><th>Filters</th><th>Shape</th></tr></thead>
<tbody>{other_rows or '<tr><td colspan="7" class="muted">None</td></tr>'}</tbody></table></section>
</main></body></html>
"""


def _atomic_write(target: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}.", suffix=target.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_discovery_report(
    metrics: tuple[MetricDiscovery, ...], result: DiscoveryResult, reports_dir: Path, generated_at: datetime
) -> Path:
    """Write the HTML (returned) and a JSON twin under reports/discovery/."""
    directory = Path(reports_dir) / DISCOVERY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / JSON_NAME, json.dumps(discovery_document(metrics, result, generated_at), indent=2))
    html_path = directory / HTML_NAME
    _atomic_write(html_path, render_discovery_html(metrics, result, generated_at))
    return html_path


CSS = """
body{margin:0;background:#f2f5f9;color:#24364b;font-family:"Segoe UI",Roboto,Arial,sans-serif;font-size:14px}
header{background:#0e2a47;color:#fff;padding:20px 28px}header h1{margin:4px 0 6px;font-size:22px}
header p{margin:0 0 10px;color:#cfd8e3}.eyebrow{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f4b33f}
.stats{display:flex;flex-wrap:wrap;gap:14px;font-size:13px}main{padding:20px 28px;max-width:1200px}
h2{font-size:17px;color:#0e2a47}.card{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:16px;
box-shadow:0 1px 3px rgba(14,42,71,.08)}.card h3{margin:0 0 10px;color:#0e2a47}
table{border-collapse:collapse;width:100%}.grid th,.grid td{text-align:left;padding:7px 8px;border-bottom:1px solid #e2e8f0;
vertical-align:top}.grid th{font-size:12px;text-transform:uppercase;color:#6b7c93}
.kv th{width:200px;text-align:left;font-weight:600;padding:4px 8px 4px 0;color:#6b7c93}.kv td{padding:4px 0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:10px}
h4{margin:6px 0;font-size:13px}ul{margin:0;padding-left:18px}.muted{color:#6b7c93;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700}
.badge.verified{background:#dcfce7;color:#166534}.badge.discovered{background:#dbeafe;color:#1e40af}
.badge.pending{background:#f1f5f9;color:#475569}
"""
