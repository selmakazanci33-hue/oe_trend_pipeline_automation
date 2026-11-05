"""Developer-facing Wave 1 runtime validation artifact (not the executive dashboard).

Shows runtime values of DISCOVERED metrics so they can be compared with the
Sisense UI. Contains no request/response bodies, formulas, filter contents,
headers, cookies or session information.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from metrics.catalog import VALUE_PERCENT
from metrics.runtime_validation import (
    RV_NOT_AVAILABLE,
    RV_PENDING,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    ControlComparison,
    RuntimeValue,
)
from reporting.discovery_report import _atomic_write
from reporting.formatting import fmt_int, fmt_time

VALIDATION_DIR = Path("validation")
HTML_NAME = "wave1_runtime_validation.html"
JSON_NAME = "wave1_runtime_validation.json"

_STATUS_CLASS = {RV_VALUE_EXTRACTED: "ok", RV_NOT_AVAILABLE: "na", RV_VALIDATION_FAILED: "fail", RV_PENDING: "pending"}


def format_runtime_value(row: RuntimeValue) -> str:
    if row.value is None:
        return "—"
    if row.value_kind == VALUE_PERCENT:
        if row.percent_format:
            return f"{row.value * 100:.2f}% (raw {row.value:g})"
        return f"{row.value:g}"
    return fmt_int(row.value)


def _fmt_optional_int(value: int | None) -> str:
    return "—" if value is None else fmt_int(value)


def validation_document(
    rows: tuple[RuntimeValue, ...], control: ControlComparison, generated_at: datetime
) -> dict[str, Any]:
    return {
        "generated_at_utc": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "Runtime values from the dashboard's own responses; validation only, not production data",
        "metrics": [
            {
                "metric_id": r.metric_id,
                "label": r.label,
                "widget_id": r.widget_id,
                "widget_title": r.widget_title,
                "shape": r.shape,
                "http_status": r.http_status,
                "runtime_value": r.value,
                "display_value": format_runtime_value(r),
                "value_kind": r.value_kind,
                "binding": r.binding,
                "extraction_status": r.status,
                "production_status": r.production_status,
                "reason": r.reason,
            }
            for r in rows
        ],
        "currently_enrolled_control": {
            "passive_value": control.passive_value,
            "application_value": control.application_value,
            "difference": control.difference,
            "status": control.status,
            "reason": control.reason,
        },
    }


def _e(value: Any) -> str:
    return escape("—" if value is None or value == "" else str(value))


def render_validation_html(
    rows: tuple[RuntimeValue, ...], control: ControlComparison, generated_at: datetime
) -> str:
    body = "".join(
        f"<tr><td>{_e(r.label)}</td><td>{_e(r.widget_title)}<div class=\"muted\">{_e(r.widget_id)}</div></td>"
        f"<td>{_e(r.shape)}</td><td>{_e(r.http_status)}</td><td class=\"num\">{_e(format_runtime_value(r))}</td>"
        f"<td><span class=\"badge {_STATUS_CLASS.get(r.status, 'pending')}\">{_e(r.status)}</span></td>"
        f"<td>{_e(r.production_status)}</td><td>{_e(r.binding)}<div class=\"muted\">{_e(r.reason) if r.reason else ''}</div></td></tr>"
        for r in rows
    )
    difference = control.difference
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wave 1 Runtime Validation — Developer Review</title><style>{CSS}</style></head>
<body><header><div class="eyebrow">Developer review — not the executive dashboard</div>
<h1>Wave 1 Runtime Validation</h1>
<p>Generated {_e(fmt_time(generated_at))}. Values read from the dashboard's own already-returned responses
for comparison with the Sisense UI. Not stored in history; not shown in the executive dashboard.</p></header>
<main>
<section class="card"><h2>Currently Enrolled control</h2>
<table class="kv">
<tr><th>Passive Currently Enrolled</th><td>{_e(_fmt_optional_int(control.passive_value))}</td></tr>
<tr><th>Application Currently Enrolled</th><td>{_e(_fmt_optional_int(control.application_value))}</td></tr>
<tr><th>Difference</th><td>{_e('—' if difference is None else (f'{difference:+,}' if difference else '0'))}</td></tr>
<tr><th>Result</th><td><span class="badge {'ok' if control.status == 'PASS' else 'fail'}">{_e(control.status)}</span>
{f'<span class="muted">{_e(control.reason)}</span>' if control.reason else ''}</td></tr>
</table></section>
<section class="card"><h2>Discovered metrics</h2>
<table class="grid"><thead><tr><th>Metric</th><th>Widget</th><th>Shape</th><th>HTTP</th><th>Runtime Value</th>
<th>Extraction Status</th><th>Production Status</th><th>Binding / Reason</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="muted">VALUE_EXTRACTED means the value was bound to its exact measure definition and passed type checks.
It does not make a metric VERIFIED; promotion requires explicit approval after UI comparison.</p></section>
</main></body></html>
"""


def write_validation_report(
    rows: tuple[RuntimeValue, ...], control: ControlComparison, reports_dir: Path, generated_at: datetime
) -> Path:
    directory = Path(reports_dir) / VALIDATION_DIR
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / JSON_NAME, json.dumps(validation_document(rows, control, generated_at), indent=2))
    html_path = directory / HTML_NAME
    _atomic_write(html_path, render_validation_html(rows, control, generated_at))
    return html_path


CSS = """
body{margin:0;background:#f2f5f9;color:#24364b;font-family:"Segoe UI",Roboto,Arial,sans-serif;font-size:14px}
header{background:#0e2a47;color:#fff;padding:20px 28px}header h1{margin:4px 0 6px;font-size:22px}
header p{margin:0;color:#cfd8e3;max-width:900px}.eyebrow{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f4b33f}
main{padding:20px 28px;max-width:1300px}h2{font-size:17px;color:#0e2a47;margin:0 0 10px}
.card{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(14,42,71,.08)}
table{border-collapse:collapse;width:100%}.grid th,.grid td{text-align:left;padding:7px 8px;border-bottom:1px solid #e2e8f0;
vertical-align:top}.grid th{font-size:12px;text-transform:uppercase;color:#6b7c93}.num{font-variant-numeric:tabular-nums;font-weight:600}
.kv th{width:260px;text-align:left;font-weight:600;padding:5px 8px 5px 0;color:#6b7c93}.kv td{padding:5px 0}
.muted{color:#6b7c93;font-size:12px}.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700}
.badge.ok{background:#dcfce7;color:#166534}.badge.na{background:#fef3c7;color:#92400e}.badge.fail{background:#fee2e2;color:#991b1b}
.badge.pending{background:#f1f5f9;color:#475569}
"""
