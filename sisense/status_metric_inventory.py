"""Passive inventory of PY2027 Pending / Cancelled / Terminated evidence.

Classifies catalog metrics, the verified Currently Enrolled payload, stored
snapshots, mocked or previously captured natural requests, and existing
discovery artifacts. This module does not send JAQL, does not open a browser,
and does not change production metrics.

Natural dashboard capture remains the existing discovery flag:

    SISENSE_DISCOVERY=true python main.py

Then run:

    python -m sisense.status_metric_inventory
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, interpret_payload
from comparison.lifecycle import resolve_history_path
from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DASHBOARD_TITLE
from metrics.catalog import METRICS, PROMOTION_CANDIDATES, STATUS_ACTIVE
from sisense.discovery import normalize_label
from sisense.payloads.currently_enrolled import (
    METRIC_NAME,
    PLAN_YEAR,
    VERIFIED_MEASURE_FIELD,
    WIDGET_TITLE,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "reports" / "discovery"
JSON_NAME = "py2027_status_metric_inventory.json"
HTML_NAME = "py2027_status_metric_inventory.html"
DISCOVERY_ARTIFACT = PROJECT_ROOT / "reports" / "discovery" / "wave1_metric_discovery.json"

VERIFIED = "VERIFIED_EXISTING_METRIC"
PASSIVE = "PASSIVE_EVIDENCE_FOUND_NEEDS_VALIDATION"
FILTER_ONLY = "FILTER_MEMBER_ONLY"
TEXT_ONLY = "TEXT_ONLY"
NOT_OBSERVED = "NOT_OBSERVED"

READY = "READY"
PY2027_NOT_VERIFIED = "PY2027_NOT_VERIFIED"
PY2026_MISSING = "PY2026_DEFINITION_MISSING"
MISMATCH = "DEFINITION_MISMATCH"
NEEDS_VALIDATION = "NEEDS_VALIDATION"

PY2026_LATEST_STATE = "LATEST_STATE_POPULATION"

_SESSION_KEYS = {
    "cookie", "cookies", "set-cookie", "password", "passwd", "token",
    "access-token", "refresh-token", "headers", "raw-headers", "cookie-header",
    "storage-state", "session", "secret", "api-key",
}

KEYWORD_ALIASES = {
    "pending": "pending",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "cancellation": "cancelled",
    "cancellations": "cancelled",
    "terminated": "terminated",
    "termination": "terminated",
    "terminations": "terminated",
}

ENROLLED_TARGETS = (
    {"id": "currently_enrolled_people", "label": "Currently Enrolled", "catalog_id": "currently_enrolled_people"},
    {"id": "new_enrolled", "label": "New Enrolled", "catalog_id": "new_enrolled"},
    {"id": "active_enrolled", "label": "Active Enrolled", "catalog_id": "active_enrolled"},
    {"id": "passive_enrolled", "label": "Passive Enrolled", "catalog_id": "passive_enrolled"},
)
APPLICATION_TARGETS = (
    {"id": "applications_started", "label": "Applications Started", "catalog_id": "applications_started"},
    {"id": "submitted_applications", "label": "Submitted Applications", "catalog_id": "submitted_applications"},
    {"id": "application_total", "label": "Application Total", "catalog_id": "total_applicants"},
)

CAPTURE_COMMAND = (
    r'$env:SISENSE_DISCOVERY="true"; .\.venv\Scripts\python.exe main.py'
)
INVENTORY_COMMAND = r".\.venv\Scripts\python.exe -m sisense.status_metric_inventory"

CURRENTLY_ENROLLED_WIDGET_ID = "6a98048ccef60720fe7f9b47"


def canonical_keyword(text: str | None) -> str | None:
    """Map Canceled/Cancellation/Termination variants onto inventory keys."""
    key = normalize_label(text)
    if key is None:
        return None
    return KEYWORD_ALIASES.get(key)


def keyword_in_text(text: str | None, canonical: str) -> bool:
    key = normalize_label(text)
    if not key:
        return False
    tokens = key.replace("&", " ").replace("/", " ").replace("-", " ").replace("—", " ").split()
    return any(KEYWORD_ALIASES.get(token) == canonical for token in tokens)


def _is_session_key(key) -> bool:
    folded = str(key).casefold().replace("_", "-")
    if folded in _SESSION_KEYS:
        return True
    if folded.startswith("auth") or folded.endswith("srf"):
        return True
    return any(part in folded for part in ("cookie", "password", "secret", "header", "token", "session"))


def sanitize_observation(value):
    """Drop cookies, header bags, tokens, and other session material."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _is_session_key(key):
                continue
            cleaned[key] = sanitize_observation(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_observation(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_observation(item) for item in value]
    return value


def meets_verified_requirements(row: dict) -> bool:
    """Promote only when identity, aggregation, identifier, year, filters, value, and source are all present."""
    filters = row.get("filters")
    has_filters = bool(filters)
    return all((
        row.get("widget"),
        row.get("widget_id"),
        row.get("aggregation"),
        row.get("identifier"),
        row.get("plan_year") is not None,
        has_filters,
        row.get("value") is not None,
        row.get("natural_widget_request") is True,
    ))


def filter_members_from_payload(definition: dict) -> list[dict]:
    """Status members from the verified Currently Enrolled request. turnedOff is not interpreted."""
    rows = []
    for item in definition.get("scope_filters") or []:
        column = item.get("column")
        if column not in {"enrollment_status_description", "enrollee_status_description"}:
            continue
        nested = item.get("nested_filter")
        for member in item.get("members") or []:
            rows.append({
                "column": column,
                "member": member,
                "canonical": canonical_keyword(str(member)),
                "nested_filter_present": nested is not None,
                "turned_off_semantics": "unresolved",
            })
    return rows


def classify_status_target(
    canonical: str,
    definition: dict,
    discovery: dict | None = None,
    events: list[dict] | None = None,
) -> dict:
    """Classify Pending / Cancelled / Terminated. Filter members are never promoted on their own."""
    members = [
        row for row in filter_members_from_payload(definition)
        if row["canonical"] == canonical
    ]
    exact_hits: list[dict] = []
    text_hits: list[dict] = []
    for widget in _iter_observed_widgets(discovery, events):
        titles = _widget_titles(widget)
        safe = _safe_request(widget)
        if any(canonical_keyword(title) == canonical for title in titles["identity"]):
            exact_hits.append(safe)
        elif any(keyword_in_text(title, canonical) for title in titles["all"]):
            text_hits.append(safe)
        for label in _category_labels(widget):
            if canonical_keyword(label) == canonical:
                text_hits.append(safe | {"match_source": "category label"})
            elif keyword_in_text(label, canonical):
                text_hits.append(safe | {"match_source": "category label"})
        for member in widget.get("filter_member_labels") or widget.get("filter_members") or []:
            label = member if isinstance(member, str) else (member or {}).get("member") or (member or {}).get("label")
            if canonical_keyword(label) == canonical and not any(
                canonical_keyword(title) == canonical for title in titles["identity"]
            ):
                members.append({
                    "column": None if not isinstance(member, dict) else member.get("column"),
                    "member": label,
                    "canonical": canonical,
                    "source": "observed_request",
                })

    if exact_hits:
        best = _best_exact_hit(exact_hits, canonical)
        row = _status_row(
            canonical,
            PASSIVE,
            widget=best.get("widget_title") or best.get("widget"),
            widget_id=best.get("widget_id"),
            aggregation=best.get("aggregation"),
            identifier=best.get("identifier"),
            value=best.get("numeric_result", best.get("value")),
            filters=best.get("filters") or [],
            plan_year=best.get("plan_year", PLAN_YEAR),
            natural_widget_request=True,
            evidence="Exact widget or measure title in a naturally observed dashboard request.",
            observed_requests=exact_hits,
            filter_members=members,
        )
        if meets_verified_requirements(row):
            row["classification"] = VERIFIED
            row["evidence"] = "Natural widget request meets verified-metric requirements."
        return row
    if members:
        return _status_row(
            canonical,
            FILTER_ONLY,
            widget=WIDGET_TITLE,
            widget_id=CURRENTLY_ENROLLED_WIDGET_ID,
            evidence=(
                f"{canonical} appears as a filter member of the verified Currently Enrolled People "
                "request. turnedOff is not interpreted. A filter member is not a metric."
            ),
            filter_members=members,
            natural_widget_request=False,
        )
    if text_hits:
        return _status_row(
            canonical,
            TEXT_ONLY,
            widget=text_hits[0].get("widget_title"),
            widget_id=text_hits[0].get("widget_id"),
            evidence="The word appears in a longer title or category label, not as its own metric.",
            observed_requests=text_hits,
        )
    return _status_row(
        canonical,
        NOT_OBSERVED,
        evidence="No natural widget, measure, or filter-member evidence.",
    )


def classify_catalog_metric(metric_id: str, snapshots: dict[str, dict], definition: dict | None = None) -> dict:
    metric = METRICS.get(metric_id) or PROMOTION_CANDIDATES.get(metric_id)
    if metric is None:
        return _catalog_row(metric_id, NOT_OBSERVED, evidence="Not in the catalog.")
    snapshot = snapshots.get(metric_id)
    source = metric.passive_source
    widget = metric.display_name if source is None else source.widget_title
    widget_id = CURRENTLY_ENROLLED_WIDGET_ID if source is None else source.widget_id
    identifier = source.measure_title if source is not None else None
    aggregation = None
    filters: list[dict] = []
    natural = source is not None or metric_id == METRIC_NAME
    if metric_id == METRIC_NAME and definition is not None:
        measure = definition.get("measure") or {}
        aggregation = measure.get("aggregation")
        fields = measure.get("fields") or []
        if fields:
            identifier = f"{fields[0].get('table')}.{fields[0].get('column')}"
        filters = [
            {
                "column": item.get("column"),
                "members": item.get("members") or [],
                "from": item.get("from"),
                "to": item.get("to"),
            }
            for item in definition.get("scope_filters") or []
        ]
        widget = WIDGET_TITLE
        widget_id = CURRENTLY_ENROLLED_WIDGET_ID
    row = _catalog_row(
        metric.metric_id,
        VERIFIED if metric.status == STATUS_ACTIVE else PASSIVE,
        widget=widget,
        widget_id=widget_id,
        aggregation=aggregation,
        identifier=identifier,
        value=None if snapshot is None else snapshot.get("metric_value"),
        plan_year=metric.plan_year,
        filters=filters,
        natural_widget_request=natural,
        evidence=(
            "Catalog STATUS_ACTIVE metric. Production stabilization is unchanged."
            if metric.status == STATUS_ACTIVE else
            "Catalog candidate only. Not a verified production metric."
        ),
        retrieved_at_utc=None if snapshot is None else snapshot.get("retrieved_at_utc"),
        label=metric.display_name,
    )
    if metric.status == STATUS_ACTIVE and metric_id == METRIC_NAME and not meets_verified_requirements(row):
        if snapshot is None:
            row["evidence"] = (
                "Catalog STATUS_ACTIVE Currently Enrolled People metric. "
                "Stored numeric snapshot was not available in this run."
            )
    return row


def readiness_row(metric_label: str, py2026: str | None, sisense_class: str) -> dict:
    if sisense_class == VERIFIED and py2026 == PY2026_LATEST_STATE:
        comparison = NEEDS_VALIDATION
        note = (
            "Both sides have a people count, but identifier and filter equivalence are not proven. "
            "PY2026 uses LATEST_STATE_POPULATION; PY2027 is the Sisense widget. "
            "turnedOff remains unresolved."
        )
    elif sisense_class == VERIFIED and py2026 is None:
        comparison = PY2026_MISSING
        note = "PY2027 Sisense is verified. No PY2026 latest-state analog is defined."
    elif sisense_class in {FILTER_ONLY, NOT_OBSERVED, TEXT_ONLY} and py2026 == PY2026_LATEST_STATE:
        comparison = PY2027_NOT_VERIFIED
        note = "PY2026 latest-state exists. PY2027 Sisense is not a verified status metric."
    elif sisense_class == PASSIVE:
        comparison = NEEDS_VALIDATION
        note = "Natural dashboard evidence exists and is not production-verified."
    else:
        comparison = PY2027_NOT_VERIFIED
        note = "No verified PY2027 Sisense metric."
    return {
        "metric": metric_label,
        "py2026_db": py2026 or PY2026_MISSING,
        "py2027_sisense": sisense_class,
        "comparison_readiness": comparison,
        "note": note,
    }


def build_inventory(
    definition: dict,
    *,
    discovery: dict | None = None,
    snapshots: dict[str, dict] | None = None,
    events: list[dict] | None = None,
) -> dict:
    snapshots = snapshots or {}
    events = [sanitize_observation(event) for event in (events or [])]
    pending = classify_status_target("pending", definition, discovery, events)
    cancelled = classify_status_target("cancelled", definition, discovery, events)
    terminated = classify_status_target("terminated", definition, discovery, events)
    enrolled = {
        item["id"]: classify_catalog_metric(item["catalog_id"], snapshots, definition)
        for item in ENROLLED_TARGETS
    }
    applications = {
        item["id"]: classify_catalog_metric(item["catalog_id"], snapshots, definition)
        for item in APPLICATION_TARGETS
    }
    if applications["application_total"]["classification"] == VERIFIED:
        applications["application_total"]["classification"] = PASSIVE
        applications["application_total"]["evidence"] = (
            "Application Total is not a distinct verified metric. Total Applicants remains a candidate."
        )
    applications["application_total"]["catalog_id"] = applications["application_total"].get("metric_id")
    applications["application_total"]["metric_id"] = "application_total"
    applications["application_total"]["label"] = "Application Total"
    for item in ENROLLED_TARGETS:
        enrolled[item["id"]]["label"] = item["label"]
    for item in APPLICATION_TARGETS:
        applications[item["id"]]["label"] = item["label"]
    currently = enrolled["currently_enrolled_people"]
    currently["classification"] = VERIFIED
    currently["widget"] = WIDGET_TITLE
    currently["widget_id"] = CURRENTLY_ENROLLED_WIDGET_ID
    currently["aggregation"] = currently.get("aggregation") or "Count"
    currently["identifier"] = currently.get("identifier") or f"enrollee.{VERIFIED_MEASURE_FIELD}"
    currently["plan_year"] = PLAN_YEAR
    currently["natural_widget_request"] = True
    matrix = [
        readiness_row("Currently Enrolled", PY2026_LATEST_STATE, currently["classification"]),
        readiness_row("Pending", PY2026_LATEST_STATE, pending["classification"]),
        readiness_row("Cancelled", PY2026_LATEST_STATE, cancelled["classification"]),
        readiness_row("Terminated", PY2026_LATEST_STATE, terminated["classification"]),
        readiness_row("New Enrolled", None, enrolled["new_enrolled"]["classification"]),
        readiness_row("Active Enrolled", None, enrolled["active_enrolled"]["classification"]),
        readiness_row("Passive Enrolled", None, enrolled["passive_enrolled"]["classification"]),
        readiness_row("Applications Started", None, applications["applications_started"]["classification"]),
        readiness_row("Submitted Applications", None, applications["submitted_applications"]["classification"]),
    ]
    inventory = {
        "report_kind": "PY2027_STATUS_METRIC_INVENTORY",
        "dashboard_id": VERIFIED_DASHBOARD_ID,
        "dashboard_title": VERIFIED_DASHBOARD_TITLE,
        "capture_note": (
            "This command does not send JAQL. Natural requests are those already captured by "
            f"{CAPTURE_COMMAND} and written to wave1_metric_discovery.json, plus any sanitized "
            "events passed into the classifier."
        ),
        "inventory_command": INVENTORY_COMMAND,
        "turned_off_semantics": "unresolved",
        "py2026_status_series": PY2026_LATEST_STATE,
        "concepts_preserved": [
            "SNAPSHOT POPULATION",
            "MONTHLY ENROLLMENT ACTIVITY",
            "LATEST STATE POPULATION",
        ],
        "target_status_metrics": [pending, cancelled, terminated],
        "enrolled_metrics": [enrolled[item["id"]] for item in ENROLLED_TARGETS],
        "application_metrics": [applications[item["id"]] for item in APPLICATION_TARGETS],
        "readiness_matrix": matrix,
        "observed_widgets": [_safe_request(widget) for widget in _iter_observed_widgets(discovery, events)],
        "observed_requests": events,
        "py2027_current_reference": snapshots.get(METRIC_NAME),
        "discovery_artifact_present": discovery is not None,
        "no_experimental_jaql": True,
        "verified_payload_unmodified": True,
        "visualization_created": False,
    }
    return sanitize_observation(inventory)


def load_discovery_artifact(path: Path = DISCOVERY_ARTIFACT) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_validated_metrics(path: Path | None = None) -> dict[str, dict]:
    history = path or resolve_history_path()
    if not history.is_file():
        return {}
    uri = f"{history.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    found = {}
    try:
        for metric_id in list(METRICS) + list(PROMOTION_CANDIDATES):
            row = connection.execute(
                """
                SELECT retrieved_at_utc, metric_value
                FROM metric_snapshots
                WHERE metric_id = ? AND plan_year = ? AND retrieval_status = 'VALIDATED'
                ORDER BY retrieved_at_utc DESC
                LIMIT 1
                """,
                (metric_id, PLAN_YEAR),
            ).fetchone()
            if row is None:
                continue
            retrieved = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            found[metric_id] = {
                "metric_id": metric_id,
                "retrieved_at_utc": row[0],
                "observation_date": retrieved.date().isoformat(),
                "metric_value": _as_int(row[1]),
                "plan_year": PLAN_YEAR,
                "source": "metric_snapshots.retrieved_at_utc",
            }
    finally:
        connection.close()
    return found


def write_report(inventory: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(inventory), encoding="utf-8")
    return json_path, html_path


def render_html(inventory: dict) -> str:
    def table(title: str, rows: list[dict]) -> str:
        body = "".join(
            "<tr>"
            f"<td>{_e(item.get('label') or item.get('canonical') or item.get('metric_id'))}</td>"
            f"<td>{_e(item.get('classification'))}</td>"
            f"<td>{_e(item.get('widget'))}</td>"
            f"<td>{_e(item.get('aggregation'))}</td>"
            f"<td>{_e(item.get('identifier'))}</td>"
            f"<td>{_e(item.get('value'))}</td>"
            f"<td>{_e(item.get('evidence'))}</td>"
            "</tr>"
            for item in rows
        )
        return (
            f"<h2>{html.escape(title)}</h2>"
            "<table><thead><tr><th>Metric</th><th>Classification</th><th>Widget</th>"
            "<th>Aggregation</th><th>Identifier</th><th>Value</th><th>Evidence</th>"
            f"</tr></thead><tbody>{body}</tbody></table>"
        )

    matrix = "".join(
        "<tr>"
        f"<td>{_e(item['metric'])}</td>"
        f"<td>{_e(item['py2026_db'])}</td>"
        f"<td>{_e(item['py2027_sisense'])}</td>"
        f"<td>{_e(item['comparison_readiness'])}</td>"
        "</tr>"
        for item in inventory.get("readiness_matrix") or []
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2027 status metric inventory</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:1100px}"
        "table{border-collapse:collapse;width:100%;margin-bottom:2rem}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#f4f7fb}</style></head><body>"
        "<h1>PY2027 status metric inventory</h1>"
        "<p>Passive evidence only. Numbers and text tables. No visualization.</p>"
        + table("Target status metrics", inventory.get("target_status_metrics") or [])
        + table("Enrolled metrics", inventory.get("enrolled_metrics") or [])
        + table("Application metrics", inventory.get("application_metrics") or [])
        + "<h2>Final dashboard readiness</h2>"
        "<table><thead><tr><th>Metric</th><th>PY2026 DB</th><th>PY2027 Sisense</th>"
        "<th>Comparison readiness</th></tr></thead>"
        f"<tbody>{matrix}</tbody></table>"
        f"<p>{_e(inventory.get('capture_note'))}</p>"
        "</body></html>"
    )


def print_table(inventory: dict) -> None:
    print("PY2027 STATUS METRIC INVENTORY — passive; no JAQL sent")
    print(f"{'Metric':<24}{'Classification':<42}{'Widget':<36}{'Value':>12}")
    for group in ("target_status_metrics", "enrolled_metrics", "application_metrics"):
        for item in inventory.get(group) or []:
            label = item.get("label") or item.get("canonical") or item.get("metric_id")
            print(
                f"{str(label):<24}{item.get('classification', ''):<42}"
                f"{str(item.get('widget') or ''):<36}{_num(item.get('value')):>12}"
            )
    print("")
    print("READINESS")
    print(f"{'Metric':<24}{'PY2026':<28}{'PY2027':<42}{'Ready'}")
    for item in inventory.get("readiness_matrix") or []:
        print(
            f"{item['metric']:<24}{str(item['py2026_db']):<28}"
            f"{item['py2027_sisense']:<42}{item['comparison_readiness']}"
        )


def main() -> int:
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    definition = interpret_payload(payload)
    discovery = load_discovery_artifact()
    snapshots = read_validated_metrics()
    inventory = build_inventory(definition, discovery=discovery, snapshots=snapshots)
    json_path, html_path = write_report(inventory)
    print_table(inventory)
    if discovery is None:
        print(f"No discovery artifact at {DISCOVERY_ARTIFACT}")
        print(f"Capture natural requests with: {CAPTURE_COMMAND}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


def _iter_observed_widgets(discovery: dict | None, events: list[dict] | None) -> list[dict]:
    widgets: list[dict] = []
    if discovery:
        widgets.extend(discovery.get("widgets") or [])
        widgets.extend(discovery.get("widget_inventory") or [])
        for metric in discovery.get("metrics") or []:
            if metric.get("state") == "DISCOVERED":
                widgets.append({
                    "widget_id": (metric.get("widget_ids") or [None])[0],
                    "widget_title": metric.get("label"),
                    "measures": [metric.get("label")],
                    "metric_id": metric.get("metric_id"),
                })
        for candidate in discovery.get("cancellation_source_identification") or []:
            widgets.append({
                "widget_id": candidate.get("widget_id"),
                "widget_title": candidate.get("widget_title"),
                "measures": candidate.get("measure_titles") or [],
                "dimensions": candidate.get("dimension_titles") or [],
                "datasource": candidate.get("datasource"),
            })
    widgets.extend(events or [])
    return widgets


def _widget_titles(widget: dict) -> dict[str, list[str]]:
    identity = [widget.get("widget_title"), widget.get("metric_title")]
    measures = widget.get("measures") or []
    if measures and isinstance(measures[0], dict):
        identity.extend(item.get("title") for item in measures)
    else:
        identity.extend(measures)
    details = widget.get("measure_details") or []
    identity.extend(item.get("title") for item in details if isinstance(item, dict))
    all_titles = list(identity)
    for dim in widget.get("dimensions") or []:
        if isinstance(dim, dict):
            all_titles.append(dim.get("title"))
        else:
            all_titles.append(dim)
    all_titles.extend(widget.get("dimension_names") or [])
    return {"identity": [title for title in identity if title], "all": [title for title in all_titles if title]}


def _category_labels(widget: dict) -> list[str]:
    response = widget.get("response") or {}
    labels = list(response.get("matched_category_labels") or [])
    labels.extend(widget.get("matched_category_labels") or [])
    return [label for label in labels if label]


def _safe_request(widget: dict) -> dict:
    jaql = widget.get("jaql_structure") or widget.get("sanitized_jaql")
    return sanitize_observation({
        "sequence": widget.get("sequence"),
        "endpoint": widget.get("endpoint") or widget.get("path") or "/api/datasources/MAIN/jaql",
        "method": widget.get("method") or "POST",
        "widget_id": widget.get("widget_id"),
        "widget_title": widget.get("widget_title") or widget.get("widget"),
        "widget_type": widget.get("widget_type"),
        "metric_title": widget.get("metric_title"),
        "datasource": widget.get("datasource") or widget.get("datasource_title"),
        "measures": widget.get("measures"),
        "aggregation": widget.get("aggregation") or _first(widget.get("aggregation_names")),
        "identifier": widget.get("identifier"),
        "filters": widget.get("filters"),
        "filter_member_labels": widget.get("filter_member_labels"),
        "response_shape": widget.get("response_shape") or (widget.get("response") or {}).get("shape"),
        "numeric_result": widget.get("numeric_result"),
        "value": widget.get("value"),
        "plan_year": widget.get("plan_year"),
        "natural_widget_request": widget.get("natural_widget_request", True),
        "jaql_structure": jaql,
        "dimension_names": widget.get("dimension_names"),
        "aggregation_names": widget.get("aggregation_names"),
    })


def _best_exact_hit(hits: list[dict], canonical: str) -> dict:
    complete = [hit for hit in hits if meets_verified_requirements(_status_row(canonical, VERIFIED, **{
        "widget": hit.get("widget_title"),
        "widget_id": hit.get("widget_id"),
        "aggregation": hit.get("aggregation"),
        "identifier": hit.get("identifier"),
        "value": hit.get("numeric_result", hit.get("value")),
        "filters": hit.get("filters") or [],
        "natural_widget_request": True,
        "plan_year": hit.get("plan_year", PLAN_YEAR),
    }))]
    return complete[0] if complete else hits[0]


def _status_row(canonical: str, classification: str, **fields) -> dict:
    labels = {"pending": "Pending", "cancelled": "Cancelled", "terminated": "Terminated"}
    row = {
        "metric_id": canonical,
        "canonical": canonical,
        "label": labels.get(canonical, canonical),
        "classification": classification,
        "widget": None,
        "widget_id": None,
        "aggregation": None,
        "identifier": None,
        "value": None,
        "plan_year": PLAN_YEAR,
        "filters": [],
        "evidence": None,
        "filter_members": [],
        "observed_requests": [],
        "natural_widget_request": False,
    }
    row.update(fields)
    return row


def _catalog_row(metric_id: str, classification: str, **fields) -> dict:
    row = {
        "metric_id": metric_id,
        "canonical": None,
        "label": metric_id,
        "classification": classification,
        "widget": None,
        "widget_id": None,
        "aggregation": None,
        "identifier": None,
        "value": None,
        "plan_year": PLAN_YEAR,
        "filters": [],
        "evidence": None,
        "filter_members": [],
        "observed_requests": [],
        "natural_widget_request": False,
    }
    row.update(fields)
    return row


def _first(values):
    if not values:
        return None
    return values[0]


def _as_int(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value)


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _num(value) -> str:
    return "n/a" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
