"""Phase 3D: runtime values of DISCOVERED Wave 1 metrics, for comparison with the Sisense UI only.

Values come from responses the dashboard already received (driver reads, no
network). Each value is bound to the request's own measure definition and read
with the existing parser; anything that cannot be bound deterministically
fails closed. Results are never stored in history, never shown in the
executive report, and never promote a metric: production status is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from metrics.catalog import (
    CANCELLATION_CANDIDATES,
    CANCELLATION_VALIDATION_TARGETS,
    DISCOVERY_TARGETS,
    PROMOTION_CANDIDATES,
    RUNTIME_VALIDATION_TARGETS,
    STATE_DISCOVERED,
    STATE_PENDING,
    STATE_VERIFIED,
    VALUE_COUNT,
    WAVE1_TARGETS,
    RuntimeValidationTarget,
    catalog_state,
)
from metrics.discovery import MetricDiscovery
from sisense.discovery import (
    CONTROL_PASS,
    DiscoveryObserver,
    _read_response,
    normalize_label,
    verified_widget_value,
)
from sisense.models import InvalidJaqlPayloadError, ResponseValidationError
from sisense.passive import ExpectedWidgetQuery, _request_payload, definition_fingerprint, split_reference
from validation.validators import extract_measure_value, parse_json_body, result_measure_titles

RV_VALUE_EXTRACTED = "VALUE_EXTRACTED"
RV_SERIES_EXTRACTED = "SERIES_EXTRACTED"
RV_NOT_AVAILABLE = "NOT_AVAILABLE"
RV_VALIDATION_FAILED = "VALIDATION_FAILED"
RV_PENDING = "PENDING"

BINDING_MEASURE_TITLE = "exact measure title"
BINDING_SINGLE_MEASURE_WIDGET = "widget title of a single-measure widget"

_LABELS = {t.metric_id: t.label for t in DISCOVERY_TARGETS}
_RUNTIME_TARGET_IDS = frozenset(t.metric_id for t in RUNTIME_VALIDATION_TARGETS)


class BindingError(Exception):
    pass


@dataclass(frozen=True)
class MeasureBinding:
    title: str
    column: int
    basis: str
    percent_format: bool


@dataclass(frozen=True)
class RuntimeValue:
    metric_id: str
    label: str
    status: str
    value: int | float | None = None
    value_kind: str = VALUE_COUNT
    percent_format: bool = False
    widget_id: str | None = None
    widget_title: str | None = None
    shape: str | None = None
    http_status: int | None = None
    binding: str | None = None
    reason: str | None = None
    definition_fingerprint: str | None = None
    datasource: str | None = None
    measure_titles: tuple[str, ...] = ()
    bound_measure_title: str | None = None
    date_dimension_title: str | None = None
    date_table: str | None = None
    date_column: str | None = None
    time_grain: str | None = None
    date_semantics: str | None = None
    series_point_count: int | None = None

    @property
    def production_status(self) -> str:
        """Catalog status, unchanged by runtime validation."""
        if catalog_state(self.metric_id) != STATE_PENDING:
            return catalog_state(self.metric_id)
        if self.metric_id in PROMOTION_CANDIDATES:
            return "VALIDATION ONLY — awaiting verified filter fingerprint"
        if self.metric_id in CANCELLATION_CANDIDATES:
            return "VALIDATION ONLY — awaiting verified source definition"
        if self.metric_id in _RUNTIME_TARGET_IDS:
            return f"{STATE_DISCOVERED} — awaiting UI verification"
        return STATE_PENDING


def bind_measure(payload: Mapping[str, Any], label: str) -> MeasureBinding:
    """Bind ``label`` to exactly one measure column of the request definition, or raise BindingError."""
    try:
        titles = result_measure_titles(payload)
    except InvalidJaqlPayloadError as exc:
        raise BindingError(f"Request definition is not a measures-only query ({exc})") from None
    key = normalize_label(label)
    hits = [i for i, title in enumerate(titles) if normalize_label(title) == key]
    if len(hits) > 1:
        raise BindingError(f"Ambiguous: {len(hits)} measures carry the label")
    if hits:
        column, basis = hits[0], BINDING_MEASURE_TITLE
    else:
        widget = split_reference(payload.get("widget"))
        if widget is None or normalize_label(widget[1]) != key or len(titles) != 1:
            raise BindingError("No measure in the request definition is bound to the label")
        column, basis = 0, BINDING_SINGLE_MEASURE_WIDGET
    measures = [m for m in payload["metadata"] if m.get("panel") != "scope"]
    fmt = measures[column].get("format")
    mask = fmt.get("mask") if isinstance(fmt, Mapping) else None
    percent = isinstance(mask, Mapping) and mask.get("percent") is True
    return MeasureBinding(titles[column], column, basis, percent)


def _datasource_title(payload: Mapping[str, Any]) -> str | None:
    datasource = payload.get("datasource")
    title = datasource.get("title") if isinstance(datasource, Mapping) else None
    return title if isinstance(title, str) else None


def _observed_measure_titles(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Exact measure titles of a measures-only request; empty when any measure lacks a title (fail closed)."""
    try:
        return tuple(result_measure_titles(payload))
    except InvalidJaqlPayloadError:
        return ()


def _null_cell(body: Mapping[str, Any], width: int, column: int) -> bool:
    values = body.get("values")
    if not isinstance(values, list):
        return False
    if all(isinstance(item, Mapping) for item in values):
        row = values
    elif len(values) == 1 and isinstance(values[0], list):
        row = values[0]
    else:
        return False
    return len(row) == width and isinstance(row[column], Mapping) and "data" in row[column] \
        and row[column]["data"] is None


def extract_runtime_value(
    payload: Mapping[str, Any], body_text: str, label: str, value_kind: str
) -> tuple[str, int | float | None, MeasureBinding | None, str | None]:
    """(status, value, binding, reason) for one metric from one already-returned response."""
    try:
        binding = bind_measure(payload, label)
    except BindingError as exc:
        return RV_VALIDATION_FAILED, None, None, str(exc)
    try:
        body = parse_json_body(body_text)
    except ResponseValidationError as exc:
        return RV_VALIDATION_FAILED, None, binding, str(exc)
    if body.get("error"):
        return RV_VALIDATION_FAILED, None, binding, "Response reports an error"
    if body.get("values") == []:
        return RV_NOT_AVAILABLE, None, binding, "Dashboard response contains no result"
    try:
        value = extract_measure_value(body, payload, binding.title)
    except ResponseValidationError as exc:
        width = len(result_measure_titles(payload))
        if _null_cell(body, width, binding.column):
            return RV_NOT_AVAILABLE, None, binding, "Dashboard response value is null"
        return RV_VALIDATION_FAILED, None, binding, str(exc)
    except InvalidJaqlPayloadError as exc:
        return RV_VALIDATION_FAILED, None, binding, str(exc)
    if value_kind == VALUE_COUNT and not isinstance(value, int):
        return RV_VALIDATION_FAILED, None, binding, "Count is not an integer"
    return RV_VALUE_EXTRACTED, value, binding, None


def validate_runtime_values(
    observer: DiscoveryObserver,
    resolved: tuple[MetricDiscovery, ...],
    targets: tuple[RuntimeValidationTarget, ...] = RUNTIME_VALIDATION_TARGETS,
) -> tuple[RuntimeValue, ...]:
    """Rows for the seven Phase 3D targets plus the Wave 1 metrics that stay PENDING (never extracted)."""
    by_id = {m.metric_id: m for m in resolved}
    rows = []
    for target in targets:
        metric = by_id.get(target.metric_id)
        label = _LABELS[target.metric_id]
        if metric is None or metric.state not in (STATE_DISCOVERED, STATE_VERIFIED) or metric.widget is None:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, value_kind=target.value_kind,
                                     reason=f"Not DISCOVERED in this run: {metric.reason if metric else 'not observed'}"))
            continue
        widget = metric.widget
        common = dict(value_kind=target.value_kind, widget_id=widget.structure.widget_id,
                      widget_title=widget.structure.widget_title,
                      shape=widget.response.shape if widget.response else None,
                      http_status=widget.response.http_status if widget.response else None)
        if widget.control is not None and widget.control != CONTROL_PASS:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **common,
                                     reason=f"Verified widget failed its strict control check: {widget.control}"))
            continue
        entry = observer.entry(widget.structure.widget_id)
        request = entry.latest_request if entry else None
        payload = _request_payload(request) if request is not None else None
        if payload is None:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **common,
                                     reason="Dashboard request not retained"))
            continue
        common["definition_fingerprint"] = definition_fingerprint(payload)
        common["datasource"] = _datasource_title(payload)
        common["measure_titles"] = _observed_measure_titles(payload)
        status, text = _read_response(request)
        if status != 200:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **{**common, "http_status": status},
                                     reason=f"Dashboard response HTTP {status}"))
            continue
        if text is None:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **common,
                                     reason="Response body unavailable"))
            continue
        result, value, binding, reason = extract_runtime_value(payload, text, label, target.value_kind)
        rows.append(RuntimeValue(
            target.metric_id, label, result, value, percent_format=bool(binding and binding.percent_format),
            binding=binding.basis if binding else None, reason=reason,
            bound_measure_title=binding.title if binding else None, **common,
        ))
    from metrics.cancellation import extract_cancellation
    for target in CANCELLATION_VALIDATION_TARGETS:
        candidate = CANCELLATION_CANDIDATES[target.metric_id]
        metric = by_id.get(target.metric_id)
        label = candidate.discovery_label
        if metric is None or metric.state not in (STATE_DISCOVERED, STATE_VERIFIED) or metric.widget is None:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, value_kind=target.value_kind,
                                     reason=f"Not DISCOVERED in this run: {metric.reason if metric else 'not observed'}"))
            continue
        widget = metric.widget
        common = dict(value_kind=target.value_kind, widget_id=widget.structure.widget_id,
                      widget_title=widget.structure.widget_title,
                      shape=widget.response.shape if widget.response else None,
                      http_status=widget.response.http_status if widget.response else None)
        entry = observer.entry(widget.structure.widget_id)
        request = entry.latest_request if entry else None
        payload = _request_payload(request) if request is not None else None
        if payload is None:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **common,
                                     reason="Dashboard request not retained"))
            continue
        common["definition_fingerprint"] = definition_fingerprint(payload)
        common["datasource"] = _datasource_title(payload)
        common["measure_titles"] = _observed_measure_titles(payload)
        status, text = _read_response(request)
        if status != 200:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **{**common, "http_status": status},
                                     reason=f"Dashboard response HTTP {status}"))
            continue
        if text is None:
            rows.append(RuntimeValue(target.metric_id, label, RV_VALIDATION_FAILED, **common,
                                     reason="Response body unavailable"))
            continue
        rows.append(extract_cancellation(candidate, payload, text, common))
    in_scope = {t.metric_id for t in targets} | {t.metric_id for t in CANCELLATION_VALIDATION_TARGETS}
    for target in WAVE1_TARGETS:
        if target.metric_id not in in_scope and catalog_state(target.metric_id) == STATE_PENDING:
            metric = by_id.get(target.metric_id)
            reason = metric.reason if metric and metric.state == STATE_PENDING else "Outside Phase 3D scope"
            rows.append(RuntimeValue(target.metric_id, target.label, RV_PENDING, reason=reason))
    return tuple(rows)


IDENTITY_PASS = "IDENTITY_PASS"
IDENTITY_FAIL = "IDENTITY_FAIL"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
LIVE_MOVEMENT = "LIVE_MOVEMENT"
EXACT_VALUE_MATCH = "EXACT_VALUE_MATCH"


def _classify_identity(passive_value: int | None, reason: str | None) -> str:
    """Structural result of the passive read. Value equality is not part of it."""
    if reason and reason.startswith("FAIL"):
        return IDENTITY_FAIL
    if isinstance(passive_value, int) and not isinstance(passive_value, bool):
        return IDENTITY_PASS
    return SOURCE_UNAVAILABLE


@dataclass(frozen=True)
class ControlComparison:
    """Independently timed Currently Enrolled reads.

    Identity is the verified dashboard, widget, datasource, measure, and filter
    definition plus a successful parse. A later live value may differ. That
    difference is movement, not an identity failure, and it is not judged by a
    numeric tolerance.
    """

    passive_value: int | None
    application_value: int | None
    reason: str | None
    passive_observed_at: str | None = None
    application_observed_at: str | None = None
    identity_status: str | None = None

    def __post_init__(self) -> None:
        if self.identity_status is None:
            object.__setattr__(self, "identity_status", _classify_identity(self.passive_value, self.reason))

    @property
    def delta(self) -> int | None:
        """Application minus passive. Positive means the later application read is higher."""
        if not isinstance(self.passive_value, int) or isinstance(self.passive_value, bool):
            return None
        if not isinstance(self.application_value, int) or isinstance(self.application_value, bool):
            return None
        return self.application_value - self.passive_value

    @property
    def difference(self) -> int | None:
        return self.delta

    @property
    def status(self) -> str:
        if self.identity_status != IDENTITY_PASS:
            return self.identity_status or SOURCE_UNAVAILABLE
        if self.delta is None:
            return SOURCE_UNAVAILABLE
        if self.passive_value == self.application_value:
            return EXACT_VALUE_MATCH
        return LIVE_MOVEMENT

    @property
    def identity_passed(self) -> bool:
        """True for EXACT_VALUE_MATCH and LIVE_MOVEMENT. Movement is not a failure."""
        return self.status in (EXACT_VALUE_MATCH, LIVE_MOVEMENT)

    @property
    def authoritative_value(self) -> int | None:
        """The application retrieval. A moved passive read does not replace it."""
        return self.application_value


def passive_control_value(
    observer: DiscoveryObserver, expected: ExpectedWidgetQuery | None
) -> tuple[int | None, str | None]:
    """Passive Currently Enrolled from the verified widget, with the unchanged strict checks."""
    if expected is None:
        return None, "Verified definition unavailable"
    entry = observer.entry(expected.widget_id)
    if entry is None or entry.latest_request is None:
        return None, "Verified widget not observed"
    status, text = _read_response(entry.latest_request)
    result, value = verified_widget_value(entry.latest_request, status, text, expected)
    return value, None if result == CONTROL_PASS else result
