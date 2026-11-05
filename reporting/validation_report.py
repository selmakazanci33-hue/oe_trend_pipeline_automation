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

from metrics.cancellation_identification import CancellationSourceCandidate, candidate_document
from metrics.catalog import VALUE_PERCENT
from metrics.fingerprint_evidence import CandidateEvidence, EvidenceOutcome
from metrics.runtime_validation import (
    EXACT_VALUE_MATCH,
    IDENTITY_FAIL,
    IDENTITY_PASS,
    LIVE_MOVEMENT,
    RV_NOT_AVAILABLE,
    RV_PENDING,
    RV_SERIES_EXTRACTED,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    SOURCE_UNAVAILABLE,
    ControlComparison,
    RuntimeValue,
)
from reporting.discovery_report import _atomic_write
from reporting.formatting import fmt_int, fmt_time

VALIDATION_DIR = Path("validation")
HTML_NAME = "wave1_runtime_validation.html"
JSON_NAME = "wave1_runtime_validation.json"

_STATUS_CLASS = {RV_VALUE_EXTRACTED: "ok", RV_SERIES_EXTRACTED: "ok", RV_NOT_AVAILABLE: "na",
                 RV_VALIDATION_FAILED: "fail", RV_PENDING: "pending"}
_CONTROL_CLASS = {EXACT_VALUE_MATCH: "ok", IDENTITY_PASS: "ok", LIVE_MOVEMENT: "move",
                  IDENTITY_FAIL: "fail", SOURCE_UNAVAILABLE: "pending"}


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


def _evidence_value(e: CandidateEvidence) -> str:
    return "—" if e.value is None else fmt_int(e.value)


def evidence_document(outcome: EvidenceOutcome) -> dict[str, Any]:
    return {
        "note": "Evidence for human review only; nothing here promotes a metric",
        "observation_recorded": outcome.observation_recorded,
        "observation_note": outcome.observation_note,
        "fingerprint_comparison": {
            "observation_a_observed_at_utc": outcome.comparison.baseline_observed_at_utc,
            "results": outcome.comparison.results,
        },
        "candidates": [
            {
                "metric_id": e.metric_id,
                "label": e.label,
                "widget_id": e.widget_id,
                "widget_title": e.widget_title,
                "datasource": e.datasource,
                "measure_titles": list(e.measure_titles),
                "measure_count": e.measure_count,
                "bound_measure_title": e.bound_measure_title,
                "binding": e.binding,
                "definition_fingerprint": e.fingerprint,
                "extraction_status": e.extraction_status,
                "runtime_value": e.value,
                "http_status": e.http_status,
                "observed_at_utc": e.observed_at_utc,
                "eligible_for_future_promotion": e.eligible,
                "eligibility_reason": e.eligibility_reason,
                "fingerprint_stability": outcome.comparison.results.get(e.metric_id),
            }
            for e in outcome.evidence
        ],
        "cancellation_candidates": [
            {
                "metric_id": e.metric_id,
                "label": e.label,
                "widget_id": e.widget_id,
                "widget_title": e.widget_title,
                "datasource": e.datasource,
                "measure_titles": list(e.measure_titles),
                "measure_count": e.measure_count,
                "bound_measure_title": e.bound_measure_title,
                "date_dimension_title": e.date_dimension_title,
                "time_grain": e.time_grain,
                "date_semantics": e.date_semantics,
                "series_point_count": e.series_point_count,
                "binding": e.binding,
                "definition_fingerprint": e.fingerprint,
                "extraction_status": e.extraction_status,
                "runtime_value": e.value,
                "http_status": e.http_status,
                "observed_at_utc": e.observed_at_utc,
                "eligible_for_future_promotion": e.eligible,
                "eligibility_reason": e.eligibility_reason,
                "fingerprint_stability": outcome.comparison.results.get(e.metric_id),
            }
            for e in outcome.cancellation
        ],
        "cancellation_shared_widgets": [
            {
                "widget_id": s.widget_id,
                "metric_ids": list(s.metric_ids),
                "same_fingerprint": s.same_fingerprint,
                "distinct_exact_measures": s.distinct_measures,
                "exact_measure_binding": s.exact_binding,
                "all_eligible": s.all_eligible,
                "structurally_eligible_together": s.eligible_together,
            }
            for s in outcome.cancellation_shared
        ],
        "shared_widgets": [
            {
                "widget_id": s.widget_id,
                "metric_ids": list(s.metric_ids),
                "same_fingerprint": s.same_fingerprint,
                "distinct_exact_measures": s.distinct_measures,
                "exact_measure_binding": s.exact_binding,
                "all_eligible": s.all_eligible,
                "structurally_eligible_together": s.eligible_together,
            }
            for s in outcome.shared
        ],
    }


def _identification_html(candidates: tuple[CancellationSourceCandidate, ...] | None) -> str:
    if not candidates:
        return ""
    rows = "".join(
        f"<tr><td>{_e(c.widget_title)}<div class=\"muted\">{_e(c.widget_id)}</div></td>"
        f"<td>{_e(c.datasource)}</td><td>{_e(' | '.join(t or '—' for t in c.measure_titles))}</td>"
        f"<td>{_e(' | '.join(t or '—' for t in c.dimension_titles))}</td><td>{_e(c.date_grain)}</td>"
        f"<td>{_e(c.response_shape)}</td><td>{_e(c.semantic_role)}</td><td>{_e(c.confidence)}</td>"
        f"<td class=\"fp\">{_e(c.fingerprint)}</td><td>{_e(c.reason)}</td></tr>"
        for c in candidates
    )
    return f"""<section class="card"><h2>Cancellation Source Identification</h2>
<p class="muted">Structural nomination from naturally observed JAQL. Display labels are not assumed
to equal measure titles. STRUCTURAL_CANDIDATE is not production-verified. No values.</p>
<table class="grid"><thead><tr><th>Widget</th><th>Datasource</th><th>Measure titles</th><th>Dimension titles</th>
<th>Date grain</th><th>Shape</th><th>Role</th><th>Confidence</th><th>Fingerprint</th><th>Reason</th></tr></thead>
<tbody>{rows}</tbody></table></section>"""


def _evidence_html(outcome: EvidenceOutcome | None) -> str:
    if outcome is None:
        return ""
    rows = "".join(
        f"<tr><td>{_e(e.label)}<div class=\"muted\">{_e(e.metric_id)}</div></td>"
        f"<td>{_e(e.widget_title)}<div class=\"muted\">{_e(e.widget_id)}</div></td><td>{_e(e.datasource)}</td>"
        f"<td>{_e(' | '.join(e.measure_titles))}<div class=\"muted\">{e.measure_count} measure(s)</div></td>"
        f"<td>{_e(e.bound_measure_title)}</td><td class=\"num\">{_e(_evidence_value(e))}</td>"
        f"<td>{_e(e.extraction_status)}</td><td>{_e(e.http_status)}</td>"
        f"<td><span class=\"badge {'ok' if e.eligible else 'fail'}\">{'ELIGIBLE' if e.eligible else 'NOT ELIGIBLE'}</span>"
        f"<div class=\"muted\">{_e(e.eligibility_reason) if e.eligibility_reason else ''}</div></td>"
        f"<td>{_e(outcome.comparison.results.get(e.metric_id))}</td><td class=\"fp\">{_e(e.fingerprint)}</td></tr>"
        for e in outcome.evidence
    )
    shared = "".join(
        f"<tr><td>{_e(s.widget_id)}</td><td>{_e(', '.join(s.metric_ids))}</td>"
        f"<td>{'YES' if s.same_fingerprint else 'NO'}</td><td>{'YES' if s.distinct_measures else 'NO'}</td>"
        f"<td>{'YES' if s.exact_binding else 'NO'}</td>"
        f"<td><span class=\"badge {'ok' if s.eligible_together else 'fail'}\">"
        f"{'ELIGIBLE TOGETHER' if s.eligible_together else 'NOT ELIGIBLE'}</span></td></tr>"
        for s in outcome.shared
    )
    cancellation_rows = "".join(
        f"<tr><td>{_e(e.label)}<div class=\"muted\">{_e(e.metric_id)}</div></td>"
        f"<td>{_e(e.widget_title)}<div class=\"muted\">{_e(e.widget_id)}</div></td><td>{_e(e.datasource)}</td>"
        f"<td>{_e(' | '.join(e.measure_titles))}<div class=\"muted\">{e.measure_count} measure(s)</div></td>"
        f"<td>{_e(e.bound_measure_title)}</td><td>{_e(e.date_dimension_title)}</td><td>{_e(e.time_grain)}</td>"
        f"<td>{_e(e.date_semantics)}<div class=\"muted\">{_e(e.series_point_count)}</div></td>"
        f"<td>{_e(e.extraction_status)}</td><td>{_e(e.http_status)}</td>"
        f"<td><span class=\"badge {'ok' if e.eligible else 'fail'}\">{'ELIGIBLE' if e.eligible else 'NOT ELIGIBLE'}</span>"
        f"<div class=\"muted\">{_e(e.eligibility_reason) if e.eligibility_reason else ''}</div></td>"
        f"<td>{_e(outcome.comparison.results.get(e.metric_id))}</td><td class=\"fp\">{_e(e.fingerprint)}</td></tr>"
        for e in outcome.cancellation
    )
    cancellation_shared = "".join(
        f"<tr><td>{_e(s.widget_id)}</td><td>{_e(', '.join(s.metric_ids))}</td>"
        f"<td>{'YES' if s.same_fingerprint else 'NO'}</td><td>{'YES' if s.distinct_measures else 'NO'}</td>"
        f"<td>{'YES' if s.exact_binding else 'NO'}</td>"
        f"<td><span class=\"badge {'ok' if s.eligible_together else 'fail'}\">"
        f"{'ELIGIBLE TOGETHER' if s.eligible_together else 'NOT ELIGIBLE'}</span></td></tr>"
        for s in outcome.cancellation_shared
    )
    recorded = ("Recorded as a fingerprint observation." if outcome.observation_recorded
                else f"Not recorded as a fingerprint observation ({outcome.observation_note}).")
    baseline = outcome.comparison.baseline_observed_at_utc
    return f"""<section class="card"><h2>Promotion evidence — validation-only candidates</h2>
<p class="muted">Observed {_e(outcome.evidence[0].observed_at_utc if outcome.evidence else None)}. {_e(recorded)}
Stability compares this run (Observation B) with the previous recorded observation (Observation A:
{_e(baseline)}). STABLE / CHANGED / MISSING / NO BASELINE never approve a metric; promotion is a human code change.</p>
<table class="grid"><thead><tr><th>Metric</th><th>Widget</th><th>Datasource</th><th>Exact measure titles</th>
<th>Bound measure</th><th>Value</th><th>Extraction</th><th>HTTP</th><th>Future promotion</th><th>A/B</th>
<th>Definition Fingerprint</th></tr></thead><tbody>{rows}</tbody></table></section>
<section class="card"><h2>Shared-widget check</h2>
<table class="grid"><thead><tr><th>Widget</th><th>Metrics</th><th>Same fingerprint</th><th>Distinct exact measures</th>
<th>Exact binding</th><th>Result</th></tr></thead><tbody>{shared}</tbody></table></section>
<section class="card"><h2>Cancellation / termination evidence — validation only</h2>
<p class="muted">People-level aggregates and daily policy series are independent. Exact measure-title binding only.
No positional fallback. Daily series require a verified date dimension, grain, and date semantics.
Nothing here is shown on the executive dashboard.</p>
<table class="grid"><thead><tr><th>Metric</th><th>Widget</th><th>Datasource</th><th>Exact measure titles</th>
<th>Bound measure</th><th>Date dimension</th><th>Grain</th><th>Date semantics</th>
<th>Extraction</th><th>HTTP</th><th>Future promotion</th><th>A/B</th>
<th>Definition Fingerprint</th></tr></thead><tbody>{cancellation_rows}</tbody></table></section>
<section class="card"><h2>Cancellation shared-widget check</h2>
<table class="grid"><thead><tr><th>Widget</th><th>Metrics</th><th>Same fingerprint</th><th>Distinct exact measures</th>
<th>Exact binding</th><th>Result</th></tr></thead><tbody>{cancellation_shared}</tbody></table></section>"""


def validation_document(
    rows: tuple[RuntimeValue, ...], control: ControlComparison, generated_at: datetime,
    evidence: EvidenceOutcome | None = None,
    identification: tuple[CancellationSourceCandidate, ...] | None = None,
) -> dict[str, Any]:
    document = {
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
                "definition_fingerprint": r.definition_fingerprint,
                "datasource": r.datasource,
                "measure_titles": list(r.measure_titles),
                "bound_measure_title": r.bound_measure_title,
                "date_dimension_title": r.date_dimension_title,
                "time_grain": r.time_grain,
                "date_semantics": r.date_semantics,
                "series_point_count": r.series_point_count,
            }
            for r in rows
        ],
        "currently_enrolled_control": {
            "passive_value": control.passive_value,
            "passive_observed_at": control.passive_observed_at,
            "application_value": control.application_value,
            "application_observed_at": control.application_observed_at,
            "difference": control.difference,
            "delta": control.delta,
            "identity_status": control.identity_status,
            "status": control.status,
            "reason": control.reason,
            "application_value_authoritative": control.authoritative_value,
            "numeric_tolerance": None,
        },
    }
    if evidence is not None:
        document["promotion_evidence"] = evidence_document(evidence)
    if identification is not None:
        document["cancellation_source_identification"] = candidate_document(identification)
    return document


def _e(value: Any) -> str:
    return escape("—" if value is None or value == "" else str(value))


def render_validation_html(
    rows: tuple[RuntimeValue, ...], control: ControlComparison, generated_at: datetime,
    evidence: EvidenceOutcome | None = None,
    identification: tuple[CancellationSourceCandidate, ...] | None = None,
) -> str:
    body = "".join(
        f"<tr><td>{_e(r.label)}</td><td>{_e(r.widget_title)}<div class=\"muted\">{_e(r.widget_id)}</div></td>"
        f"<td>{_e(r.shape)}</td><td>{_e(r.http_status)}</td><td class=\"num\">{_e(format_runtime_value(r))}</td>"
        f"<td><span class=\"badge {_STATUS_CLASS.get(r.status, 'pending')}\">{_e(r.status)}</span></td>"
        f"<td>{_e(r.production_status)}</td><td>{_e(r.binding)}<div class=\"muted\">{_e(r.reason) if r.reason else ''}</div></td>"
        f"<td class=\"fp\">{_e(r.definition_fingerprint)}</td></tr>"
        for r in rows
    )
    delta = control.delta
    delta_text = "—" if delta is None else ("0" if delta == 0 else f"{delta:+,}")
    identity_badge = _CONTROL_CLASS.get(control.identity_status or "", "pending")
    result_badge = _CONTROL_CLASS.get(control.status, "pending")
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
<tr><th>Passive read time</th><td>{_e(control.passive_observed_at)}</td></tr>
<tr><th>Application Currently Enrolled</th><td>{_e(_fmt_optional_int(control.application_value))}</td></tr>
<tr><th>Application read time</th><td>{_e(control.application_observed_at)}</td></tr>
<tr><th>Delta (application minus passive)</th><td>{_e(delta_text)}</td></tr>
<tr><th>Identity</th><td><span class="badge {identity_badge}">{_e(control.identity_status)}</span></td></tr>
<tr><th>Result</th><td><span class="badge {result_badge}">{_e(control.status)}</span>
{f'<span class="muted">{_e(control.reason)}</span>' if control.reason else ''}</td></tr>
<tr><th>Authoritative value</th><td>{_e(_fmt_optional_int(control.authoritative_value))} (application retrieval)</td></tr>
</table>
<p class="muted">IDENTITY_PASS means the passive read matched the verified definition and parsed.
LIVE_MOVEMENT means that read and the later application read both passed that identity and the integers differ.
Movement is not an identity failure. No numeric tolerance is applied. EXACT_VALUE_MATCH means the two
independently timed values happened to be equal. The application retrieval stays authoritative.</p></section>
<section class="card"><h2>Wave 1 metrics</h2>
<table class="grid"><thead><tr><th>Metric</th><th>Widget</th><th>Shape</th><th>HTTP</th><th>Runtime Value</th>
<th>Extraction Status</th><th>Production Status</th><th>Binding / Reason</th><th>Definition Fingerprint</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="muted">VALUE_EXTRACTED means the value was bound to its exact measure definition and passed type checks.
It does not make a metric VERIFIED; promotion requires explicit approval after UI comparison. The definition
fingerprint is a one-way hash of the widget request's dashboard, widget, datasource, measure and filter
definitions (no filter values are shown); a fingerprint from a run whose values matched the UI is the
evidence required to promote a validation-only metric.</p></section>
{_evidence_html(evidence)}
{_identification_html(identification)}
</main></body></html>
"""


def write_validation_report(
    rows: tuple[RuntimeValue, ...], control: ControlComparison, reports_dir: Path, generated_at: datetime,
    evidence: EvidenceOutcome | None = None,
    identification: tuple[CancellationSourceCandidate, ...] | None = None,
) -> Path:
    directory = Path(reports_dir) / VALIDATION_DIR
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / JSON_NAME,
                  json.dumps(validation_document(rows, control, generated_at, evidence, identification), indent=2))
    html_path = directory / HTML_NAME
    _atomic_write(html_path, render_validation_html(rows, control, generated_at, evidence, identification))
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
.badge.ok{background:#dcfce7;color:#166534}.badge.move{background:#e0e7ff;color:#3730a3}
.badge.na{background:#fef3c7;color:#92400e}.badge.fail{background:#fee2e2;color:#991b1b}
.badge.pending{background:#f1f5f9;color:#475569}.fp{font-family:Consolas,Menlo,monospace;font-size:11px;word-break:break-all;
max-width:220px}
"""
