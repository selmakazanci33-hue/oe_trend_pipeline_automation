"""Production passive metrics: values read from the dashboard's own JAQL responses.

Only production-VERIFIED catalog metrics with a ``passive_source`` are read.
Nothing here sends a request: the observer keeps a bounded number of Request
references for the source widgets as the dashboard's own POSTs complete, and
values are read later, in the main flow, from responses the browser already
received (driver reads, no network). The observer is stopped before this
application sends its own request, so that request can never become a source.

A value is accepted only from the most recently completed HTTP 200 request
whose whole definition (dashboard, widget, datasource, measures, scope
filters) matches the verified source definition, bound to its exact measure
title. Anything else fails closed for that metric only. Request and response
bodies stay in memory and are never logged or persisted.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from playwright.sync_api import BrowserContext, Request

from config.settings import VERIFIED_JAQL_URL
from metrics.catalog import MetricDefinition, PassiveSource
from metrics.runtime_validation import _null_cell
from sisense.discovery import _read_response
from sisense.models import InvalidJaqlPayloadError, ResponseValidationError
from sisense.passive import (
    ExpectedWidgetQuery,
    _request_payload,
    check_candidate,
    definition_fingerprint,
    split_reference,
)
from validation.validators import extract_measure_value, parse_json_body, result_measure_titles

logger = logging.getLogger(__name__)

MAX_CANDIDATES_PER_WIDGET = 5

PM_EXTRACTED = "EXTRACTED"
PM_NOT_AVAILABLE = "NOT_AVAILABLE"
PM_FAILED = "FAILED"


class SourceWidgetObserver:
    """Keeps at most ``max_candidates`` completed dashboard JAQL POSTs per source widget."""

    def __init__(self, widget_ids: Iterable[str], url: str = VERIFIED_JAQL_URL,
                 max_candidates: int = MAX_CANDIDATES_PER_WIDGET):
        self.widget_ids = frozenset(widget_ids)
        self._url = url
        self._max = max_candidates
        self._context: BrowserContext | None = None
        self._candidates: dict[str, deque[Request]] = {}
        self.target_requests_seen = 0

    def attach(self, context: BrowserContext) -> None:
        self._context = context
        self._candidates = {widget_id: deque(maxlen=self._max) for widget_id in self.widget_ids}
        self.target_requests_seen = 0

        def on_request_finished(request: Request) -> None:
            if context is self._context:
                self._on_request_finished(request)

        context.on("requestfinished", on_request_finished)

    def stop(self) -> None:
        self._context = None

    def is_target(self, request: Request) -> bool:
        return request.method == "POST" and request.url == self._url

    def source_widget(self, request: Request) -> str | None:
        if not self.is_target(request):
            return None
        payload = _request_payload(request)
        reference = split_reference(payload.get("widget")) if payload else None
        return reference[0] if reference is not None and reference[0] in self.widget_ids else None

    def is_source_request(self, request: Request) -> bool:
        return self.source_widget(request) is not None

    def _on_request_finished(self, request: Request) -> None:
        if not self.is_target(request):
            return
        self.target_requests_seen += 1
        widget_id = self.source_widget(request)
        if widget_id is None:
            return
        kept = self._candidates.setdefault(widget_id, deque(maxlen=self._max))
        if not any(request is item for item in kept):
            kept.append(request)

    def candidates(self, widget_id: str) -> list[Request]:
        """Retained requests for one widget, oldest to most recently completed."""
        return list(self._candidates.get(widget_id, ()))

    def missing(self) -> set[str]:
        return {widget_id for widget_id in self.widget_ids if not self._candidates.get(widget_id)}


@dataclass(frozen=True)
class PassiveMetricOutcome:
    metric_id: str
    status: str
    value: int | float | None = None
    control_value: int | None = None
    failure: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == PM_EXTRACTED

    def failed(self, reason: str) -> "PassiveMetricOutcome":
        return PassiveMetricOutcome(self.metric_id, PM_FAILED, failure=reason)


def source_check(source: PassiveSource, payload: Mapping[str, Any]) -> str | None:
    """None when the request definition is exactly the verified source definition, else the reason."""
    widget = split_reference(payload.get("widget"))
    if widget is None or widget[0] != source.widget_id:
        return "Source widget identity mismatch"
    if source.expected_definition is not None:
        expected = ExpectedWidgetQuery.from_verified_query(source.expected_definition())
        if expected.widget_id != source.widget_id:
            return "Verified definition belongs to a different widget"
        check = check_candidate(payload, expected)
        if not check.accepted:
            failed = [name for name in ("dashboard", "widget", "datasource", "primary_measure", "filters")
                      if not getattr(check, name)]
            return f"Request definition mismatch ({', '.join(failed)})"
        return None
    if source.expected_fingerprint is not None:
        if definition_fingerprint(payload) != source.expected_fingerprint:
            return "Request definition mismatch"
        return None
    return "No verified source definition"


def _bound_measure(source: PassiveSource, payload: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """(exact measure title, its metadata item); raises InvalidJaqlPayloadError when not exactly one."""
    titles = result_measure_titles(payload)
    if source.measure_title is None:
        widget = split_reference(payload.get("widget"))
        if len(titles) != 1 or widget is None or widget[1] != source.widget_title:
            raise InvalidJaqlPayloadError("Source is not a single-measure widget with the expected title")
        title = titles[0]
    else:
        if titles.count(source.measure_title) != 1:
            raise InvalidJaqlPayloadError("Measure title is not present exactly once")
        title = source.measure_title
    measures = [m for m in payload["metadata"] if m.get("panel") != "scope"]
    return title, measures[titles.index(title)]


def _is_percent_measure(item: Mapping[str, Any]) -> bool:
    fmt = item.get("format")
    mask = fmt.get("mask") if isinstance(fmt, Mapping) else None
    return isinstance(mask, Mapping) and mask.get("percent") is True


def _value_from(metric: MetricDefinition, payload: Mapping[str, Any], text: str) -> PassiveMetricOutcome:
    source = metric.passive_source
    try:
        title, item = _bound_measure(source, payload)
    except InvalidJaqlPayloadError as exc:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure=f"Measure binding failed ({exc})")
    if metric.value_format == "percent" and not _is_percent_measure(item):
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Measure is not a percentage measure")
    try:
        body = parse_json_body(text)
    except ResponseValidationError:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Response is not valid JSON")
    if body.get("error"):
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Response reports an error")
    if body.get("values") == []:
        return PassiveMetricOutcome(metric.metric_id, PM_NOT_AVAILABLE, failure="Dashboard response contains no result")
    try:
        value = extract_measure_value(body, payload, title)
    except ResponseValidationError as exc:
        titles = result_measure_titles(payload)
        if _null_cell(body, len(titles), titles.index(title)):
            return PassiveMetricOutcome(metric.metric_id, PM_NOT_AVAILABLE, failure="Dashboard response value is null")
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure=f"Parse failed ({exc})")
    except InvalidJaqlPayloadError as exc:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure=f"Parse failed ({exc})")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Value is not a finite non-negative number")
    if metric.value_format == "integer" and not isinstance(value, int):
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Count is not an integer")
    return PassiveMetricOutcome(metric.metric_id, PM_EXTRACTED, value=value,
                                control_value=_control_value(source, payload, body))


def _control_value(source: PassiveSource, payload: Mapping[str, Any], body: Mapping[str, Any]) -> int | None:
    """Primary (application-verified) measure from the same response, when the source carries one."""
    if source.expected_definition is None:
        return None
    query = source.expected_definition()
    try:
        value = extract_measure_value(body, payload, query.primary_measure_title)
    except (ResponseValidationError, InvalidJaqlPayloadError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def extract_passive_metric(metric: MetricDefinition, observer: SourceWidgetObserver) -> PassiveMetricOutcome:
    """Call from the main flow after ``observer.stop()``; never raises."""
    try:
        return _extract(metric, observer)
    except Exception as exc:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure=f"Extraction error ({type(exc).__name__})")


def _extract(metric: MetricDefinition, observer: SourceWidgetObserver) -> PassiveMetricOutcome:
    source = metric.passive_source
    if source is None or not source.verifiable:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="No verified passive source")
    if source.approved_from_observations and source.expected_fingerprint is None:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED,
                                    failure="Approved source definition is not available")
    evaluated = []
    for request in observer.candidates(source.widget_id):
        payload = _request_payload(request)
        if payload is not None:
            evaluated.append((request, payload, source_check(source, payload)))
    if not evaluated:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Source widget request not observed")
    verified = [entry for entry in evaluated if entry[2] is None]
    if not verified:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure=evaluated[-1][2])
    # Latest completed verified request; an older response never stands in for a failed newer one.
    request, payload, _ = verified[-1]
    status, text = _read_response(request)
    if status != 200:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure=f"Dashboard response HTTP {status}")
    if text is None:
        return PassiveMetricOutcome(metric.metric_id, PM_FAILED, failure="Response body unavailable")
    return _value_from(metric, payload, text)


def apply_control(outcome: PassiveMetricOutcome, application_value: int | None) -> PassiveMetricOutcome:
    """Same-response canary: an embedded Currently Enrolled measure from this response must match exactly."""
    if not outcome.succeeded or outcome.control_value is None:
        return outcome
    if application_value is None:
        return outcome.failed("Application control request failed")
    if outcome.control_value != application_value:
        return outcome.failed("Currently Enrolled control mismatch in the same dashboard response")
    return outcome
