"""Phase 6D: targeted natural semantic capture for the New / Active / Passive widget.

Reads only the dashboard's own, already-completed JAQL requests that the existing
discovery observer retained for widget 6a98048ccef60720fe7f9b6e. Nothing here sends,
replays, alters or builds a request. Request bodies are reduced in memory to an
allowlisted semantic structure:

- field identities (table, column, datatype, level) and the measure aggregation;
- formula structure with context keys as ordered placeholders, literals removed and
  unknown functions masked;
- ISO date bounds and relative-date counts;
- member values only on status/type fields, and only plain categorical tokens.

Anything else is reported as REDACTED_OR_UNAVAILABLE. Headers, cookies, tokens, raw
bodies and identifier values are never read into the report.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sisense.discovery import filter_kind, normalize_label
from sisense.passive import _CONTEXT_KEY, _request_payload, split_reference

logger = logging.getLogger(__name__)

NAP_WIDGET_ID = "6a98048ccef60720fe7f9b6e"
MEASURES = ("NEW", "ACTIVE", "PASSIVE")
JSON_NAME = "nap_semantic_capture.json"
HTML_NAME = "nap_semantic_capture.html"
REDACTED = "REDACTED_OR_UNAVAILABLE"

TARGET_OBSERVED = "TARGET_OBSERVED"
TARGET_NOT_OBSERVED = "TARGET_NOT_OBSERVED"

ENROLLMENT_ACTIVITY = "ENROLLMENT_ACTIVITY_COHORT"
APPLICATION_ACTIVITY = "APPLICATION_ACTIVITY_COHORT"
CURRENT_POPULATION = "CURRENT_POPULATION_LIFECYCLE_CLASS"
OTHER_LIFECYCLE = "OTHER_LIFECYCLE_CLASS"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
NEW_CLASSES = (ENROLLMENT_ACTIVITY, APPLICATION_ACTIVITY, CURRENT_POPULATION, OTHER_LIFECYCLE, INSUFFICIENT)

CREATE_BRIDGE = "POTENTIAL_ENROLLMENT_CREATE_BRIDGE"
CONFIRM_BRIDGE = "POTENTIAL_ENROLLMENT_CONFIRMATION_BRIDGE"
NO_BRIDGE = "NO_ACTIVITY_BRIDGE"
BRIDGE_DECISIONS = (CREATE_BRIDGE, CONFIRM_BRIDGE, NO_BRIDGE, INSUFFICIENT)

MUTUALLY_EXCLUSIVE = "MUTUALLY_EXCLUSIVE_LIFECYCLE_CLASSES"
OVERLAPPING = "OVERLAPPING_POPULATIONS"
INDEPENDENT = "INDEPENDENT_MEASURES"
UNRESOLVED = "UNRESOLVED_LOGIC"

PERSON, POLICY, APPLICATION, APPLICANT, UNKNOWN = "PERSON", "POLICY", "APPLICATION", "APPLICANT", "UNKNOWN"

APPLICATION_CREATED = "application created"
APPLICATION_SUBMITTED = "application submitted"
APPLICATION_OTHER = "application date (other)"
ENROLLMENT_CREATED = "enrollment created"
ENROLLMENT_CONFIRMED = "enrollment confirmed"
ENROLLMENT_OTHER = "enrollment date (other)"
OTHER_DATE = "other date"

MEASURE_CONTEXT = "measure_context"
WIDGET_SCOPE = "widget_scope_filter"
DASHBOARD_SCOPE = "dashboard_scope_filter"
RESULT_DIMENSION = "result_dimension"

OBSERVATIONAL_CONTEXT = {
    "values": {"NEW": "about 4.4K", "ACTIVE": "about 58.8K", "PASSIVE": "about 734.5K"},
    "note": "Earlier runtime observations. Context only; never used to infer semantics or a partition.",
}

SAFE_FUNCTIONS = frozenset({
    "count", "dupcount", "countduplicates", "sum", "avg", "min", "max", "median", "stdev", "stdevp", "var",
    "varp", "if", "isnull", "and", "or", "not", "all", "case", "when", "then", "else", "end", "contribution",
    "rank", "now", "ddiff", "mdiff", "ydiff", "hdiff", "mndiff", "previous", "next", "rsum", "ravg",
})
IDENTIFIER_HINTS = ("id", "identifier", "ssn", "name", "dob", "birth", "email", "phone", "address", "zip")
SAFE_MEMBER_COLUMNS = frozenset({"status", "coverage_year", "application_type", "enrollment_type", "plan_year"})
SAFE_MEMBER = re.compile(r"[A-Za-z][A-Za-z0-9 /&()_.,'-]{0,48}")
SAFE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,7})?)?)?(Z|[+-]\d{2}:?\d{2})?")
FORMULA_TOKEN = re.compile(r"\{\d+\}|<literal>|[A-Za-z_][A-Za-z0-9_]*|\d+(\.\d+)?|[(),+\-*/<>=!\s.]")
DIM = re.compile(r"\[([A-Za-z0-9_]+)\.([A-Za-z0-9_ ]+?)(\s*\([^)]*\))?\]")
FORBIDDEN_KEYS = frozenset({
    "cookie", "cookies", "set-cookie", "authorization", "token", "access_token", "csrf", "x-xsrf-token",
    "headers", "post_data", "body", "raw", "enrollee_id", "enrollment_id", "exchg_indiv_identifier", "ssn",
})


class CaptureOutputError(ValueError):
    """The Phase 6D report refused an unsafe key or value."""


# --- sanitization -----------------------------------------------------------------

def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _identity(jaql: Mapping[str, Any]) -> tuple[str | None, str | None]:
    table, column = _text(jaql.get("table")), _text(jaql.get("column"))
    if (table is None or column is None) and isinstance(jaql.get("dim"), str):
        match = DIM.fullmatch(jaql["dim"].strip())
        if match:
            table, column = table or match.group(1), column or match.group(2).strip()
    return table, column


def member_values_allowed(column: str | None) -> bool:
    name = (column or "").lower()
    if not name:
        return False
    if name in SAFE_MEMBER_COLUMNS or name.endswith("_status_description") or name.endswith("_type_description"):
        return True
    return False


def _safe_member(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 1900 <= value <= 2100
    return isinstance(value, str) and SAFE_MEMBER.fullmatch(value) is not None


def _safe_date(value: Any) -> str:
    return value if isinstance(value, str) and SAFE_DATE.fullmatch(value.strip()) else REDACTED


def sanitize_filter(value: Any, column: str | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return {"kind": REDACTED}
    allowed = member_values_allowed(column)
    out: dict[str, Any] = {"kind": filter_kind(value) or "other"}
    if isinstance(value.get("members"), list):
        members = value["members"]
        out["member_count"] = len(members)
        out["members"] = list(members) if allowed and all(_safe_member(m) for m in members) else REDACTED
    if "startsWith" in value:
        prefix = value["startsWith"]
        out["startsWith"] = prefix if allowed and _safe_member(prefix) else REDACTED
    for bound in ("from", "to"):
        if bound in value:
            out[bound] = _safe_date(value[bound])
    for relative in ("last", "next"):
        if isinstance(value.get(relative), Mapping):
            spec = value[relative]
            out[relative] = {key: spec[key] for key in ("count", "offset")
                             if isinstance(spec.get(key), int) and not isinstance(spec.get(key), bool)}
            if "anchor" in spec:
                out[relative]["anchor"] = _safe_date(spec["anchor"])
    if isinstance(value.get("exclude"), Mapping):
        out["exclude"] = sanitize_filter(value["exclude"], column)
    if isinstance(value.get("filter"), Mapping):
        out["nested"] = sanitize_filter(value["filter"], column)
    for flag in ("all", "turnedOff", "explicit", "multiSelection"):
        if isinstance(value.get(flag), bool):
            out[flag] = value[flag]
    known = {"members", "startsWith", "from", "to", "last", "next", "exclude", "filter", "all", "turnedOff",
             "explicit", "multiSelection"}
    other = sorted(str(key) for key in value if key not in known)
    if other:
        out["other_keys"] = [key if re.fullmatch(r"[A-Za-z]{1,30}", key) else REDACTED for key in other]
    return out


def _field(jaql: Mapping[str, Any]) -> dict:
    table, column = _identity(jaql)
    return {"table": table, "column": column, "datatype": _text(jaql.get("datatype")), "level": _text(jaql.get("level")),
            "filter": sanitize_filter(jaql.get("filter"), column)}


def sanitize_formula(formula: Any, context: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Formula with context keys as {n} placeholders. Literals removed; unknown functions masked as fn."""
    if not isinstance(formula, str):
        return REDACTED, []
    order: list[str] = []
    for key in _CONTEXT_KEY.findall(formula):
        if key in context and key not in order:
            order.append(key)
    order += sorted(key for key in context if key not in order)
    text = formula
    for index, key in enumerate(order):
        text = text.replace(key, f"{{{index}}}")
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<literal>", text)
    tokens, position = [], 0
    while position < len(text):
        match = FORMULA_TOKEN.match(text, position)
        if match is None:
            return REDACTED, order
        token = match.group(0)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            token = token.upper() if token.lower() in SAFE_FUNCTIONS else "fn"
        tokens.append(token)
        position = match.end()
    return " ".join("".join(tokens).split()), order


def sanitize_measure(item: Mapping[str, Any]) -> dict:
    jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
    context = jaql.get("context") if isinstance(jaql.get("context"), Mapping) else {}
    structure, order = sanitize_formula(jaql.get("formula"), context) if "formula" in jaql else (None, [])
    counted_indexes = set()
    aggregation = []
    if structure and structure != REDACTED:
        for function, index in re.findall(r"([A-Z]+)\s*\(\s*\{(\d+)\}\s*\)", structure):
            counted_indexes.add(int(index))
            if function.lower() not in aggregation:
                aggregation.append(function.lower())
    contexts = []
    for index, key in enumerate(order):
        value = context.get(key)
        if not isinstance(value, Mapping):
            contexts.append({"placeholder": f"{{{index}}}", "role": REDACTED})
            continue
        entry = _field(value)
        entry["placeholder"] = f"{{{index}}}"
        entry["role"] = "counted_field" if index in counted_indexes else "filter_context"
        contexts.append(entry)
    counted = [c for c in contexts if c.get("role") == "counted_field"]
    if "formula" not in jaql:
        table, column = _identity(jaql)
        agg = _text(jaql.get("agg"))
        if agg:
            aggregation.append(agg.lower())
        if table or column:
            counted = [{"table": table, "column": column, "datatype": _text(jaql.get("datatype")),
                        "level": _text(jaql.get("level")), "filter": sanitize_filter(jaql.get("filter"), column),
                        "role": "counted_field"}]
    return {
        "title": _text(jaql.get("title")),
        "aggregation": aggregation or [REDACTED],
        "formula_structure": structure if "formula" in jaql else None,
        "counted_fields": [{"table": c["table"], "column": c["column"]} for c in counted] or REDACTED,
        "contexts": contexts,
        "disabled": item.get("disabled") is True,
    }


def sanitize_payload(payload: Mapping[str, Any] | None) -> dict | None:
    """Allowlisted semantic structure of one natural widget request; None if it is not a widget request."""
    if not isinstance(payload, Mapping):
        return None
    widget, dashboard = split_reference(payload.get("widget")), split_reference(payload.get("dashboard"))
    if widget is None:
        return None
    datasource = payload.get("datasource") if isinstance(payload.get("datasource"), Mapping) else {}
    measures, scope, dimensions = [], [], []
    for item in payload.get("metadata") or []:
        if not isinstance(item, Mapping):
            continue
        jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
        if item.get("panel") == "scope":
            entry = _field(jaql)
            entry.update(is_dashboard_filter=jaql.get("isDashboardFilter") is True,
                         disabled=item.get("disabled") is True)
            entry["location"] = DASHBOARD_SCOPE if entry["is_dashboard_filter"] else WIDGET_SCOPE
            scope.append(entry)
        elif jaql.get("type") == "measure" or "formula" in jaql:
            measures.append(sanitize_measure(item))
        else:
            entry = _field(jaql)
            entry["location"] = RESULT_DIMENSION
            dimensions.append(entry)
    return {
        "widget_oid": widget[0],
        "widget_title": widget[1],
        "widget_type": payload.get("widgetType") if isinstance(payload.get("widgetType"), str) else None,
        "dashboard_oid": dashboard[0] if dashboard else None,
        "dashboard_title": dashboard[1] if dashboard else None,
        "datasource_id": datasource.get("id") if isinstance(datasource.get("id"), str) else None,
        "datasource_title": datasource.get("title") if isinstance(datasource.get("title"), str) else None,
        "measures": measures,
        "scope_filters": scope,
        "dimensions": dimensions,
    }


# --- semantics --------------------------------------------------------------------

def date_event(table: str | None, column: str | None) -> str:
    """Event family of the exact captured date field identity (not of any title)."""
    t, c = (table or "").lower(), (column or "").lower()
    if t.startswith("ssap_application") or t.startswith("application") or "application" in c:
        if "creat" in c:
            return APPLICATION_CREATED
        if "submit" in c:
            return APPLICATION_SUBMITTED
        return APPLICATION_OTHER
    if t.startswith("enrollment"):
        if "creat" in c:
            return ENROLLMENT_CREATED
        if "confirm" in c or "effectuat" in c:
            return ENROLLMENT_CONFIRMED
        return ENROLLMENT_OTHER
    return OTHER_DATE


def grain_of(field: Mapping[str, Any]) -> str:
    t, c = (field.get("table") or "").lower(), (field.get("column") or "").lower()
    if t.startswith("applicant"):
        return APPLICANT
    if t.startswith("ssap_application") or t.startswith("application"):
        return APPLICATION
    if t == "enrollment" and c in ("id", "enrollment_id"):
        return POLICY
    if t == "enrollee" and c in ("id", "enrollee_id", "exchg_indiv_identifier"):
        return PERSON
    return UNKNOWN


def _is_date(entry: Mapping[str, Any]) -> bool:
    kind = (entry.get("filter") or {}).get("kind")
    return entry.get("datatype") == "datetime" or entry.get("level") is not None or kind == "range" or any(
        key in (entry.get("filter") or {}) for key in ("last", "next"))


def _is_status(entry: Mapping[str, Any]) -> bool:
    column = (entry.get("column") or "").lower()
    return column.endswith("status_description") or column == "status"


def _restricts(entry: Mapping[str, Any]) -> bool | None:
    """True if a status filter restricts members; None if its members are redacted."""
    flt = entry.get("filter") or {}
    if not flt or flt.get("all") is True or flt.get("kind") == "all" or flt.get("turnedOff") is True:
        return False
    texts = json.dumps(flt)
    return None if REDACTED in texts else True


def applied_filters(definition: Mapping[str, Any], measure: Mapping[str, Any]) -> list[dict]:
    out = [dict(c, location=MEASURE_CONTEXT) for c in measure.get("contexts", []) if c.get("role") == "filter_context"]
    out += [dict(s) for s in definition.get("scope_filters", []) if not s.get("disabled")]
    return out


def find_measure(definition: Mapping[str, Any] | None, title: str) -> dict | None:
    if not definition:
        return None
    wanted = normalize_label(title)
    found = [m for m in definition.get("measures", []) if normalize_label(m.get("title")) == wanted]
    return found[0] if len(found) == 1 else None


def describe_measure(definition: Mapping[str, Any] | None, title: str) -> dict:
    measure = find_measure(definition, title)
    if measure is None:
        return {"title": title, "captured": False, "reason": "Measure not present exactly once in the captured request."}
    filters = applied_filters(definition, measure)
    dates = [{**_date_view(f), "event_family": date_event(f.get("table"), f.get("column")),
              "event_basis": "CAPTURED_FIELD_IDENTITY"} for f in filters if _is_date(f)]
    statuses = [{"field": f"{f.get('table')}.{f.get('column')}", "location": f["location"], "filter": f.get("filter")}
                for f in filters if _is_status(f)]
    counted = measure["counted_fields"]
    return {
        "title": measure["title"],
        "captured": True,
        "aggregation": measure["aggregation"],
        "formula_structure": measure["formula_structure"],
        "counted_fields": counted,
        "grain": UNKNOWN if counted == REDACTED else (grain_of(counted[0]) if len(counted) == 1 else UNKNOWN),
        "context_fields": [f"{c.get('table')}.{c.get('column')}" for c in measure["contexts"]],
        "date_filters": dates,
        "status_filters": statuses,
        "other_filters": [{"field": f"{f.get('table')}.{f.get('column')}", "location": f["location"],
                           "filter": f.get("filter")} for f in filters if not _is_date(f) and not _is_status(f)],
        "disabled": measure["disabled"],
    }


def _date_view(entry: Mapping[str, Any]) -> dict:
    flt = entry.get("filter") or {}
    return {"field": f"{entry.get('table')}.{entry.get('column')}", "location": entry["location"],
            "date_grain": entry.get("level") or REDACTED, "from": flt.get("from"), "to": flt.get("to"),
            "relative": {key: flt[key] for key in ("last", "next") if key in flt} or None,
            "window_kind": flt.get("kind")}


def classify_new(described: Mapping[str, Any]) -> dict:
    if not described.get("captured"):
        return {"classification": INSUFFICIENT, "reason": described.get("reason")}
    if described["counted_fields"] == REDACTED or not described["counted_fields"]:
        return {"classification": INSUFFICIENT, "reason": "Counted field not captured."}
    statuses = [_restricts(item) for item in described["status_filters"]]
    if None in statuses:
        return {"classification": INSUFFICIENT, "reason": "Status filter members are redacted or unavailable."}
    restricted = any(statuses)
    measure_dates = [d for d in described["date_filters"] if d["location"] == MEASURE_CONTEXT]
    dates = measure_dates or described["date_filters"]
    if not dates:
        if described["other_filters"] or restricted:
            return {"classification": OTHER_LIFECYCLE, "reason": "Classified without any captured date filter."}
        return {"classification": INSUFFICIENT, "reason": "No captured date or classifying context."}
    events = {d["event_family"] for d in dates}
    if len(events) != 1:
        return {"classification": OTHER_LIFECYCLE, "reason": f"Several date event families: {sorted(events)}."}
    event = events.pop()
    if event in (APPLICATION_CREATED, APPLICATION_SUBMITTED, APPLICATION_OTHER):
        cls = CURRENT_POPULATION if restricted else APPLICATION_ACTIVITY
    elif event in (ENROLLMENT_CREATED, ENROLLMENT_CONFIRMED):
        cls = CURRENT_POPULATION if restricted else ENROLLMENT_ACTIVITY
    else:
        cls = OTHER_LIFECYCLE
    return {
        "classification": cls,
        "date_event_family": event,
        "status_restricted": restricted,
        "reason": ("Current status restriction applies, so the measure is a current population classified "
                   f"by {event} date." if restricted and cls == CURRENT_POPULATION else
                   f"Counts {described['grain']} records by {event} date without a status restriction."
                   if cls in (ENROLLMENT_ACTIVITY, APPLICATION_ACTIVITY) else
                   f"Date field event family '{event}' is neither application nor enrollment create/confirm."),
    }


def assess_bridge(described: Mapping[str, Any], classified: Mapping[str, Any]) -> dict:
    cls = classified["classification"]
    blockers = ["OE_START_ANCHOR_UNPROVEN: no proven official PY2026/PY2027 OE start date.",
                "POTENTIAL_RENEWAL_MIGRATION_OR_INITIAL_LOAD_EFFECT in PY2026 pre-November create/confirm dates."]
    if cls == INSUFFICIENT:
        decision = INSUFFICIENT
    elif cls != ENROLLMENT_ACTIVITY:
        decision = NO_BRIDGE
    elif described["grain"] in (APPLICATION, APPLICANT):
        decision = NO_BRIDGE
    elif described["grain"] == UNKNOWN:
        decision = INSUFFICIENT
    else:
        decision = CREATE_BRIDGE if classified["date_event_family"] == ENROLLMENT_CREATED else CONFIRM_BRIDGE
    if decision in (CREATE_BRIDGE, CONFIRM_BRIDGE):
        counted = described["counted_fields"][0]
        field = f"{counted['table']}.{counted['column']}"
        if described["grain"] == PERSON:
            blockers.insert(0, f"IDENTIFIER_EQUIVALENCE_UNPROVEN: Sisense {field} vs PY2026 enrollee_id.")
        else:
            blockers.insert(0, f"IDENTIFIER_EQUIVALENCE_UNPROVEN: Sisense {field} vs PY2026 enrollment_id.")
        blockers.append("Field-level business meaning of the captured date must be confirmed equal to the "
                        "PY2026 column meaning; matching field names are not proof.")
        windows = [d for d in described["date_filters"] if d["from"] == REDACTED or d["to"] == REDACTED]
        if windows:
            blockers.append("Date window bounds redacted; OE cutoff alignment cannot be checked.")
    return {
        "decision": decision,
        "py2026_candidate": {CREATE_BRIDGE: "enrollment_create_date",
                             CONFIRM_BRIDGE: "enrollment_confirmation_date"}.get(decision),
        "new_classification": cls,
        "blockers": blockers,
        "production_comparable": False,
    }


def _signature(described: Mapping[str, Any]) -> dict[str, Any]:
    items = described.get("date_filters", []) + described.get("status_filters", []) + described.get("other_filters", [])
    return {item["field"]: json.dumps(item.get("filter", {key: item.get(key) for key in ("from", "to", "relative")}),
                                       sort_keys=True) for item in items}


def _members(described: Mapping[str, Any], field: str):
    for item in described.get("status_filters", []) + described.get("other_filters", []):
        if item["field"] == field:
            members = (item.get("filter") or {}).get("members")
            return set(members) if isinstance(members, list) else None
    return None


def _range(described: Mapping[str, Any], field: str):
    for item in described.get("date_filters", []):
        if item["field"] == field and REDACTED not in (item["from"], item["to"]) and item["from"] and item["to"]:
            return item["from"][:10], item["to"][:10]
    return None


def lifecycle_relationship(described: Mapping[str, Mapping[str, Any]]) -> dict:
    """Relationship from captured definitions only. Observed values never enter this decision."""
    caveat = ("Record-level disjoint definitions do not guarantee person-level disjointness when distinct people "
              "are counted across several records.")
    items = [described.get(title) or {} for title in MEASURES]
    if not all(item.get("captured") and item.get("counted_fields") not in (REDACTED, []) for item in items):
        return {"relationship": UNRESOLVED, "reason": "At least one measure is not fully captured.", "caveat": caveat}
    counted = {json.dumps(item["counted_fields"], sort_keys=True) for item in items}
    if len(counted) > 1:
        return {"relationship": INDEPENDENT, "reason": "Measures count different fields.", "caveat": caveat}
    signatures = [_signature(item) for item in items]
    fields = set().union(*signatures)
    differing = sorted(f for f in fields if len({sig.get(f) for sig in signatures}) > 1)
    if len(differing) != 1:
        return {"relationship": UNRESOLVED, "differing_fields": differing,
                "reason": "Discriminating logic is not a single shared field.", "caveat": caveat}
    field = differing[0]
    members = [_members(item, field) for item in items]
    ranges = [_range(item, field) for item in items]
    if all(m is not None for m in members):
        overlap = any(members[i] & members[j] for i in range(3) for j in range(i + 1, 3))
        rel = OVERLAPPING if overlap else MUTUALLY_EXCLUSIVE
    elif all(r is not None for r in ranges):
        overlap = any(ranges[i][0] <= ranges[j][1] and ranges[j][0] <= ranges[i][1]
                      for i in range(3) for j in range(i + 1, 3))
        rel = OVERLAPPING if overlap else MUTUALLY_EXCLUSIVE
    else:
        return {"relationship": UNRESOLVED, "differing_fields": differing,
                "reason": "Discriminating values redacted or not comparable.", "caveat": caveat}
    return {"relationship": rel, "differing_fields": differing,
            "reason": f"Measures differ only on {field}.", "caveat": caveat}


# --- capture and report -----------------------------------------------------------

def _status(request) -> int | None:
    try:
        response = request.response()
    except Exception:
        return None
    return getattr(response, "status", None) if response is not None else None


def capture_from_observer(observer, widget_id: str = NAP_WIDGET_ID) -> dict:
    """Target widget only. Reads the requests the observer already retained; sends nothing."""
    entry = observer.entry(widget_id) if observer is not None else None
    definitions = []
    for item in sorted((entry.definitions.values() if entry else ()), key=lambda d: d.sequence):
        request = item.latest_request
        payload = _request_payload(request) if request is not None else None
        sanitized = sanitize_payload(payload)
        if sanitized is None or sanitized["widget_oid"] != widget_id:
            continue
        definitions.append({"definition_fingerprint": item.fingerprint, "observations": item.observations,
                            "http_status": _status(request), "definition": sanitized})
    return {"dashboard_requests_seen": getattr(observer, "target_requests_seen", None), "definitions": definitions}


def build_report(capture: Mapping[str, Any], captured_at: str | None = None) -> dict:
    definitions = capture.get("definitions") or []
    latest = definitions[-1]["definition"] if definitions else None
    described = {title: describe_measure(latest, title) for title in MEASURES}
    new_class = classify_new(described["NEW"])
    report = {
        "report_kind": "NAP_SEMANTIC_CAPTURE",
        "phase": "6D",
        "captured_at_utc": captured_at,
        "target_widget": {
            "widget_oid": NAP_WIDGET_ID,
            "status": TARGET_OBSERVED if latest else TARGET_NOT_OBSERVED,
            "widget_title": latest["widget_title"] if latest else None,
            "widget_type": latest["widget_type"] if latest else None,
            "dashboard_title": latest["dashboard_title"] if latest else None,
            "datasource_id": latest["datasource_id"] if latest else None,
            "datasource_title": latest["datasource_title"] if latest else None,
        },
        "natural_request_evidence": {
            "origin": "Dashboard-generated requests observed passively; none sent, replayed or altered.",
            "dashboard_requests_seen": capture.get("dashboard_requests_seen"),
            "definitions": [{key: item[key] for key in ("definition_fingerprint", "observations", "http_status")}
                            for item in definitions],
            "definition_used": definitions[-1]["definition_fingerprint"] if definitions else None,
            "multiple_definitions": len(definitions) > 1,
            "sanitization": "Allowlisted semantics only; unsafe detail reported as " + REDACTED,
        },
        "new_definition": {**described["NEW"], "classification": new_class},
        "active_definition": described["ACTIVE"],
        "passive_definition": described["PASSIVE"],
        "shared_context": {
            "scope_filters": latest["scope_filters"] if latest else None,
            "dimensions": latest["dimensions"] if latest else None,
            "note": "Scope filters apply to every measure in the request unless disabled; measure contexts apply "
                    "only to their own measure.",
        },
        "lifecycle_relationship": {**lifecycle_relationship(described), "observational_context": OBSERVATIONAL_CONTEXT},
        "py2026_bridge_assessment": assess_bridge(described["NEW"], new_class) if described["NEW"].get("captured")
        else {"decision": INSUFFICIENT, "new_classification": INSUFFICIENT,
              "blockers": ["NEW measure not captured from the natural request."], "production_comparable": False},
        "remaining_blockers": [],
        "phase_6d_decision": {},
        "production_comparable": False,
    }
    bridge = report["py2026_bridge_assessment"]
    report["remaining_blockers"] = list(bridge["blockers"])
    if not latest:
        report["remaining_blockers"].insert(0, "Target widget request not observed; open and load the widget.")
    report["phase_6d_decision"] = {
        "new_classification": new_class["classification"],
        "bridge_decision": bridge["decision"],
        "lifecycle_relationship": report["lifecycle_relationship"]["relationship"],
        "promotion": "None. No metric is promoted in Phase 6D.",
        "production_comparable": False,
    }
    guard_output(report)
    return report


def guard_output(value: Any, path: str = "report") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise CaptureOutputError(f"Phase 6D output refused key {key} at {path}")
            guard_output(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            guard_output(item, path)
    elif isinstance(value, (bytes, bytearray)):
        raise CaptureOutputError(f"Phase 6D output refused a binary value at {path}")


def _e(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (list, tuple)):
        return "<br>".join(_e(item) for item in value) or "none"
    if isinstance(value, dict):
        return "<br>".join(f"{html.escape(str(k))}: {_e(v)}" for k, v in value.items()) or "none"
    return html.escape(str(value))


def render_html(report: Mapping[str, Any]) -> str:
    sections = (
        ("Target Widget", report["target_widget"]),
        ("Natural Request Evidence", report["natural_request_evidence"]),
        ("NEW Definition", report["new_definition"]),
        ("ACTIVE Definition", report["active_definition"]),
        ("PASSIVE Definition", report["passive_definition"]),
        ("Shared Context", report["shared_context"]),
        ("Lifecycle Relationship", report["lifecycle_relationship"]),
        ("PY2026 Bridge Assessment", report["py2026_bridge_assessment"]),
        ("Remaining Blockers", report["remaining_blockers"]),
        ("Phase 6D Decision", report["phase_6d_decision"]),
    )
    body = "".join(f"<h2>{html.escape(title)}</h2><p>{_e(value)}</p>" for title, value in sections)
    return ("<!doctype html><html><head><meta charset='utf-8'><title>NAP semantic capture</title></head><body>"
            "<h1>New / Active / Passive Semantic Capture (Phase 6D)</h1>"
            f"<p><b>production_comparable = false</b></p>{body}</body></html>")


def write_report(report: Mapping[str, Any], output_dir: Path) -> tuple[Path, Path]:
    guard_output(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, html_path = output_dir / JSON_NAME, output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def write_nap_capture(observer, reports_dir: Path | None) -> Path | None:
    """Called from the existing discovery finalization. Never raises and never sends a request."""
    if reports_dir is None:
        return None
    try:
        report = build_report(capture_from_observer(observer),
                              datetime.now(timezone.utc).isoformat(timespec="seconds"))
        json_path, _ = write_report(report, reports_dir / "comparison")
        decision = report["phase_6d_decision"]
        logger.info("Phase 6D NAP capture: %s; NEW %s; bridge %s; report %s", report["target_widget"]["status"],
                    decision["new_classification"], decision["bridge_decision"], json_path)
        return json_path
    except Exception as exc:
        logger.error("Phase 6D NAP capture failed: %s", type(exc).__name__)
        return None
