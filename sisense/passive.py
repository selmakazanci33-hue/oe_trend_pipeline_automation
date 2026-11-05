"""Phase 3B proof: validate the dashboard's own JAQL response for one verified widget.

Validation only; the application request stays authoritative. Nothing here
sends a request: it reads Request/Response objects the browser already
produced in the authenticated context. Request and response bodies are
inspected in memory only and are never logged or persisted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

from playwright.sync_api import BrowserContext, Request

from config.settings import VERIFIED_JAQL_URL
from sisense.models import JaqlQuery
from validation.validators import extract_count_measure_value, parse_json_body

logger = logging.getLogger(__name__)

PASS = "PASS"
FAIL = "FAIL"
MAX_CANDIDATES = 5

# Business-significant keys; everything else (instanceid, queryGuid, format,
# titles of filter columns, locale, collapsed, ...) is presentation or volatile.
_FILTER_JAQL_KEYS = ("table", "column", "dim", "datatype", "level", "filter")
_MEASURE_JAQL_KEYS = ("type", "title", "formula", "context")
_CONTEXT_KEY = re.compile(r"\[[^\[\]]+\]")


def split_reference(value: Any) -> tuple[str, str] | None:
    """``"<id>;<title>"`` (dashboard/widget fields) -> (id, title)."""
    if not isinstance(value, str) or ";" not in value:
        return None
    ident, title = value.split(";", 1)
    return ident, title


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in items):
            return sorted(items, key=lambda item: json.dumps(item))
        return items
    return value


def _fingerprint(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"))


def _filter_definition(jaql: Mapping[str, Any]) -> dict[str, Any]:
    return {key: jaql[key] for key in _FILTER_JAQL_KEYS if key in jaql}


def _measure_definition(item: Mapping[str, Any]) -> str:
    """Measure semantics with formula context keys replaced by ordered placeholders."""
    jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
    definition = {key: jaql[key] for key in _MEASURE_JAQL_KEYS if key in jaql}
    formula = definition.get("formula")
    context = definition.get("context")
    if isinstance(formula, str) and isinstance(context, Mapping):
        order: list[str] = []
        for key in _CONTEXT_KEY.findall(formula):
            if key in context and key not in order:
                order.append(key)
        order += sorted(key for key in context if key not in order)
        for index, key in enumerate(order):
            formula = formula.replace(key, f"{{{index}}}")
        definition["formula"] = formula.strip()
        definition["context"] = [
            _filter_definition(context[key]) if isinstance(context[key], Mapping) else context[key] for key in order
        ]
    definition["source"] = item.get("source")
    definition["disabled"] = item.get("disabled") is True
    return _fingerprint(definition)


def _scope_filter(item: Mapping[str, Any]) -> str:
    jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
    return _fingerprint({**_filter_definition(jaql), "disabled": item.get("disabled") is True})


@dataclass(frozen=True)
class QueryShape:
    dashboard: tuple[str, str] | None
    widget: tuple[str, str] | None
    datasource: tuple[Any, Any]
    measures: tuple[str, ...]
    measure_titles: tuple[Any, ...]
    scope_filters: tuple[str, ...]


def query_shape(payload: Mapping[str, Any]) -> QueryShape:
    metadata = payload.get("metadata")
    items = [item for item in metadata if isinstance(item, Mapping)] if isinstance(metadata, list) else []
    measures = [item for item in items if item.get("panel") != "scope"]
    datasource = payload.get("datasource") if isinstance(payload.get("datasource"), Mapping) else {}
    return QueryShape(
        dashboard=split_reference(payload.get("dashboard")),
        widget=split_reference(payload.get("widget")),
        datasource=(datasource.get("id"), datasource.get("title")),
        measures=tuple(_measure_definition(item) for item in measures),
        measure_titles=tuple((item.get("jaql") or {}).get("title") for item in measures),
        scope_filters=tuple(sorted(_scope_filter(item) for item in items if item.get("panel") == "scope")),
    )


def definition_fingerprint(payload: Mapping[str, Any]) -> str:
    """One-way hash of a request's business definition, safe to persist and compare.

    Covers dashboard id, widget id, datasource, every measure definition (in
    order) and the scope-filter multiset, normalized exactly as ``check_candidate``
    compares them; volatile fields (queryGuid, instanceid, formatting) are
    excluded. Raw filter values never leave memory.
    """
    shape = query_shape(payload)
    material = {
        "dashboard": shape.dashboard[0] if shape.dashboard else None,
        "widget": shape.widget[0] if shape.widget else None,
        "datasource": list(shape.datasource),
        "measures": list(shape.measures),
        "scope_filters": list(shape.scope_filters),
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True)
class ExpectedWidgetQuery:
    """Expected dashboard request, derived from the verified payload."""

    metric: str
    primary_measure_title: str
    shape: QueryShape

    @classmethod
    def from_verified_query(cls, query: JaqlQuery) -> "ExpectedWidgetQuery":
        shape = query_shape(query.body)
        if shape.dashboard is None or shape.widget is None:
            raise ValueError("Verified payload does not identify its dashboard and widget")
        if query.primary_measure_title not in shape.measure_titles:
            raise ValueError("Verified payload does not contain the primary measure")
        return cls(query.metric, query.primary_measure_title, shape)

    @property
    def widget_id(self) -> str:
        return self.shape.widget[0]

    def primary_measure(self, shape: QueryShape) -> str | None:
        if shape.measure_titles.count(self.primary_measure_title) != 1:
            return None
        return shape.measures[shape.measure_titles.index(self.primary_measure_title)]


@dataclass(frozen=True)
class CandidateCheck:
    dashboard: bool
    widget: bool
    datasource: bool
    primary_measure: bool
    filters: bool
    scope_filters_expected: int
    scope_filters_missing: int
    scope_filters_unexpected: int
    measure_set: bool

    @property
    def accepted(self) -> bool:
        return self.dashboard and self.widget and self.datasource and self.primary_measure and self.filters


def check_candidate(payload: Mapping[str, Any], expected: ExpectedWidgetQuery) -> CandidateCheck:
    shape = query_shape(payload)
    want = expected.shape
    actual_primary = expected.primary_measure(shape)
    missing = list(want.scope_filters)
    unexpected = []
    for item in shape.scope_filters:
        if item in missing:
            missing.remove(item)
        else:
            unexpected.append(item)
    measure_set = shape.measures == want.measures
    return CandidateCheck(
        dashboard=shape.dashboard is not None and shape.dashboard[0] == want.dashboard[0],
        widget=shape.widget == want.widget,
        datasource=shape.datasource == want.datasource,
        primary_measure=actual_primary is not None and actual_primary == expected.primary_measure(want),
        filters=not missing and not unexpected and measure_set,
        scope_filters_expected=len(want.scope_filters),
        scope_filters_missing=len(missing),
        scope_filters_unexpected=len(unexpected),
        measure_set=measure_set,
    )


def _request_payload(request: Request) -> Mapping[str, Any] | None:
    try:
        text = request.post_data
        payload = json.loads(text) if text else None
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


class PassiveWidgetObserver:
    """Keeps at most ``max_candidates`` dashboard JAQL POSTs naming the verified widget.

    Scope: POST to the verified JAQL URL in the attached context only. The
    request body (already held by Playwright) is read in memory to decide
    whether a request is a candidate; non-candidates are not retained. Only the
    most recently completed candidates are kept.
    """

    def __init__(self, expected: ExpectedWidgetQuery, url: str = VERIFIED_JAQL_URL,
                 max_candidates: int = MAX_CANDIDATES):
        self.expected = expected
        self._url = url
        self._context: BrowserContext | None = None
        self._candidates: deque[Request] = deque(maxlen=max_candidates)
        self.target_requests_seen = 0
        self.widget_requests_seen = 0

    def attach(self, context: BrowserContext) -> None:
        self._context = context
        self._candidates.clear()
        self.target_requests_seen = 0
        self.widget_requests_seen = 0

        def on_request_finished(request: Request) -> None:
            if context is self._context:
                self._on_request_finished(request)

        context.on("requestfinished", on_request_finished)

    def stop(self) -> None:
        """Stop collecting, so requests sent later (e.g. by this application) can never become candidates."""
        self._context = None

    def is_target(self, request: Request) -> bool:
        return request.method == "POST" and request.url == self._url

    def is_widget_request(self, request: Request) -> bool:
        if not self.is_target(request):
            return False
        payload = _request_payload(request)
        reference = split_reference(payload.get("widget")) if payload else None
        return reference is not None and reference[0] == self.expected.widget_id

    def _on_request_finished(self, request: Request) -> None:
        if not self.is_target(request):
            return
        self.target_requests_seen += 1
        if self.is_widget_request(request) and not any(request is kept for kept in self._candidates):
            self.widget_requests_seen += 1
            self._candidates.append(request)

    def candidates(self) -> list[Request]:
        """Retained candidates, oldest to most recently completed."""
        return list(self._candidates)


@dataclass(frozen=True)
class PassiveObservation:
    candidates_seen: int
    check: CandidateCheck | None
    http_status: int | None
    parsed: bool
    value: int | None
    failure: str | None

    @property
    def found(self) -> bool:
        return self.candidates_seen > 0


def _status(request: Request) -> int | None:
    try:
        response = request.response()
    except Exception:
        return None
    return response.status if response is not None else None


def observe_passive_value(observer: PassiveWidgetObserver) -> PassiveObservation:
    """Select the latest completed HTTP 200 candidate passing every check, then parse its existing response.

    Call from the main flow (not an event handler): ``response()``/``body()``
    are driver calls that return what the browser already received.
    """
    expected = observer.expected
    evaluated = []
    for request in observer.candidates():
        payload = _request_payload(request)
        if payload is None:
            continue
        evaluated.append((request, payload, check_candidate(payload, expected), _status(request)))

    seen = observer.widget_requests_seen
    if not evaluated:
        return PassiveObservation(seen, None, None, False, None, "No dashboard request for the verified widget")

    accepted = [entry for entry in evaluated if entry[2].accepted and entry[3] == 200]
    if not accepted:
        _, _, check, status = evaluated[-1]
        reason = "Candidate failed verification" if not check.accepted else f"Candidate HTTP status {status}"
        return PassiveObservation(seen, check, status, False, None, reason)

    request, payload, check, status = accepted[-1]
    try:
        response = request.response()
        if response is None:
            raise LookupError("no response")
        text = response.body().decode("utf-8")
    except Exception as exc:
        return PassiveObservation(seen, check, status, False, None, f"Response body unavailable ({type(exc).__name__})")
    try:
        value = extract_count_measure_value(parse_json_body(text), payload, expected.primary_measure_title)
    except Exception as exc:
        return PassiveObservation(seen, check, status, False, None, f"Parse failed ({type(exc).__name__})")
    return PassiveObservation(seen, check, status, True, value, None)


@dataclass(frozen=True)
class PassiveComparison:
    observation: PassiveObservation
    application_value: int

    @property
    def passive_value(self) -> int | None:
        return self.observation.value

    @property
    def difference(self) -> int | None:
        return None if self.passive_value is None else self.passive_value - self.application_value

    @property
    def status(self) -> str:
        return PASS if self.passive_value is not None and self.difference == 0 else FAIL


def _mark(ok: bool, yes: str = "MATCH", no: str = "MISMATCH") -> str:
    return yes if ok else no


def comparison_lines(comparison: PassiveComparison) -> list[str]:
    obs = comparison.observation
    check = obs.check
    lines = ["Passive observation:", f"  Candidate: {_mark(obs.found, 'FOUND', 'NOT FOUND')}"]
    if check is not None:
        lines += [
            f"  Dashboard: {_mark(check.dashboard)}",
            f"  Widget: {_mark(check.widget)}",
            f"  Datasource: {_mark(check.datasource)}",
            f"  Primary Measure: {_mark(check.primary_measure)}",
            f"  Filters: {_mark(check.filters)}"
            + ("" if check.filters else
               f" (scope filters expected {check.scope_filters_expected}, missing {check.scope_filters_missing}, "
               f"unexpected {check.scope_filters_unexpected}; measure set {_mark(check.measure_set)})"),
        ]
    lines.append(f"  HTTP Status: {obs.http_status if obs.http_status is not None else 'n/a'}")
    lines.append(f"  Parse: {PASS if obs.parsed else FAIL}")
    if obs.failure:
        lines.append(f"  Reason: {obs.failure}")
    lines += [
        "",
        f"Passive Value: {comparison.passive_value:,}" if comparison.passive_value is not None else "Passive Value: n/a",
        f"Application Value: {comparison.application_value:,}",
        "Difference: n/a" if comparison.difference is None
        else f"Difference: {comparison.difference:+,}" if comparison.difference else "Difference: 0",
        f"Passive Validation: {comparison.status}",
    ]
    return lines
