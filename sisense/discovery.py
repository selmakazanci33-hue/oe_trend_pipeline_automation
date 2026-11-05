"""Passive discovery of the dashboard's own JAQL widgets (structure only).

Scope: completed POSTs to the verified JAQL URL in the attached context whose
body names the target dashboard and datasource. Request bodies (already held
by Playwright) are reduced to safe structural metadata as each request
completes; raw bodies are never retained, logged or persisted. A bounded
number of Request references is kept so responses the browser already
received can be inspected later, in memory, from the main flow.

Nothing here sends a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from playwright.sync_api import BrowserContext, Request

from config.settings import VERIFIED_JAQL_URL
from sisense.passive import ExpectedWidgetQuery, _request_payload, check_candidate, split_reference
from validation.validators import extract_count_measure_value, parse_json_body

logger = logging.getLogger(__name__)

MAX_WIDGETS = 80
MAX_RETAINED = 30

SHAPE_SINGLE = "SINGLE_INDICATOR"
SHAPE_MULTI_MEASURE = "MULTI_MEASURE"
SHAPE_CATEGORICAL = "CATEGORICAL"
SHAPE_TIME_SERIES = "TIME_SERIES"
SHAPE_EMPTY = "EMPTY"
SHAPE_UNPARSEABLE = "UNPARSEABLE"
SHAPE_UNAVAILABLE = "RESPONSE_UNAVAILABLE"
SHAPE_NOT_SUCCESSFUL = "NOT_SUCCESSFUL"

SOURCE_WIDGET_TITLE = "widget title"
SOURCE_MEASURE_TITLE = "measure title"
SOURCE_DIMENSION_TITLE = "dimension title"
SOURCE_CATEGORY_LABEL = "category label"

CONTROL_PASS = "PASS"


def normalize_label(text: Any) -> str | None:
    """Case- and whitespace-insensitive form; no other normalization (no substrings, no fuzzing)."""
    if not isinstance(text, str):
        return None
    normalized = " ".join(text.split()).casefold()
    return normalized or None


@dataclass(frozen=True)
class Column:
    title: str | None
    kind: str  # "measure" | "dimension"
    datatype: str | None
    level: str | None

    @property
    def is_date(self) -> bool:
        return self.kind == "dimension" and (self.datatype == "datetime" or self.level is not None)


@dataclass(frozen=True)
class WidgetStructure:
    """Safe structural metadata from a dashboard request body; no filter values, no formulas."""

    dashboard_id: str
    widget_id: str
    widget_title: str
    widget_type: str | None
    datasource_id: str | None
    datasource_title: str | None
    columns: tuple[Column, ...]
    filter_count: int
    dashboard_filter_count: int

    @property
    def measures(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "measure")

    @property
    def dimensions(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "dimension")


def widget_structure(payload: Mapping[str, Any] | None) -> WidgetStructure | None:
    if not isinstance(payload, Mapping):
        return None
    dashboard = split_reference(payload.get("dashboard"))
    widget = split_reference(payload.get("widget"))
    if dashboard is None or widget is None:
        return None
    metadata = payload.get("metadata")
    items = [item for item in metadata if isinstance(item, Mapping)] if isinstance(metadata, list) else []
    columns = []
    filters = 0
    dashboard_filters = 0
    for item in items:
        jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
        if item.get("panel") == "scope":
            filters += 1
            dashboard_filters += jaql.get("isDashboardFilter") is True
            continue
        title = jaql.get("title") if isinstance(jaql.get("title"), str) else None
        kind = "measure" if jaql.get("type") == "measure" or "formula" in jaql else "dimension"
        datatype = jaql.get("datatype") if isinstance(jaql.get("datatype"), str) else None
        level = jaql.get("level") if isinstance(jaql.get("level"), str) else None
        columns.append(Column(title, kind, datatype, level))
    datasource = payload.get("datasource") if isinstance(payload.get("datasource"), Mapping) else {}
    widget_type = payload.get("widgetType")
    return WidgetStructure(
        dashboard_id=dashboard[0],
        widget_id=widget[0],
        widget_title=widget[1],
        widget_type=widget_type if isinstance(widget_type, str) else None,
        datasource_id=datasource.get("id") if isinstance(datasource.get("id"), str) else None,
        datasource_title=datasource.get("title") if isinstance(datasource.get("title"), str) else None,
        columns=tuple(columns),
        filter_count=filters,
        dashboard_filter_count=dashboard_filters,
    )


def title_matches(structure: WidgetStructure, targets: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Exact (normalized) matches of target labels against widget, measure and dimension titles."""
    wanted = {normalize_label(t) for t in targets} - {None}
    found: dict[str, list[str]] = {}

    def add(title: str | None, source: str) -> None:
        key = normalize_label(title)
        if key in wanted and source not in found.setdefault(key, []):
            found[key].append(source)

    add(structure.widget_title, SOURCE_WIDGET_TITLE)
    for column in structure.columns:
        add(column.title, SOURCE_MEASURE_TITLE if column.kind == "measure" else SOURCE_DIMENSION_TITLE)
    return {key: tuple(sources) for key, sources in found.items()}


@dataclass
class _WidgetEntry:
    structure: WidgetStructure
    observations: int = 0
    structure_changed: bool = False
    latest_request: Request | None = None
    retention_skipped: bool = False


class DiscoveryObserver:
    def __init__(
        self,
        dashboard_id: str,
        datasource_id: str,
        target_labels: Iterable[str],
        verified_widget_id: str | None = None,
        url: str = VERIFIED_JAQL_URL,
        max_widgets: int = MAX_WIDGETS,
        max_retained: int = MAX_RETAINED,
    ):
        self._dashboard_id = dashboard_id
        self._datasource_id = datasource_id
        self.target_labels = tuple(target_labels)
        self._verified_widget_id = verified_widget_id
        self._url = url
        self._max_widgets = max_widgets
        self._max_retained = max_retained
        self._context: BrowserContext | None = None
        self._widgets: dict[str, _WidgetEntry] = {}
        self.target_requests_seen = 0
        self.out_of_scope = 0
        self.widgets_dropped = 0

    def attach(self, context: BrowserContext) -> None:
        self._context = context
        self._widgets = {}
        self.target_requests_seen = 0
        self.out_of_scope = 0
        self.widgets_dropped = 0

        def on_request_finished(request: Request) -> None:
            if context is self._context:
                self._on_request_finished(request)

        context.on("requestfinished", on_request_finished)

    def stop(self) -> None:
        self._context = None

    def is_target(self, request: Request) -> bool:
        return request.method == "POST" and request.url == self._url

    @property
    def retained_count(self) -> int:
        return sum(entry.latest_request is not None for entry in self._widgets.values())

    def _relevant(self, structure: WidgetStructure) -> bool:
        return (
            structure.widget_id == self._verified_widget_id
            or bool(title_matches(structure, self.target_labels))
            or bool(structure.dimensions)
        )

    def _on_request_finished(self, request: Request) -> None:
        if not self.is_target(request):
            return
        if any(entry.latest_request is request for entry in self._widgets.values()):
            return
        self.target_requests_seen += 1
        structure = widget_structure(_request_payload(request))
        if (structure is None or structure.dashboard_id != self._dashboard_id
                or structure.datasource_id != self._datasource_id):
            self.out_of_scope += 1
            return
        entry = self._widgets.get(structure.widget_id)
        if entry is None:
            if len(self._widgets) >= self._max_widgets:
                self.widgets_dropped += 1
                return
            entry = self._widgets[structure.widget_id] = _WidgetEntry(structure)
        elif entry.structure != structure:
            entry.structure_changed = True
            entry.structure = structure
        entry.observations += 1
        if not self._relevant(structure):
            return
        if entry.latest_request is None and self.retained_count >= self._max_retained:
            entry.retention_skipped = True
            return
        entry.latest_request = request  # latest completed observation of this widget

    def entries(self) -> list[_WidgetEntry]:
        return list(self._widgets.values())

    def entry(self, widget_id: str) -> _WidgetEntry | None:
        return self._widgets.get(widget_id)


@dataclass(frozen=True)
class ResponseSummary:
    http_status: int | None
    available: bool
    shape: str
    rows: int = 0
    value_cells: int = 0
    matched_category_labels: tuple[str, ...] = ()
    time_points: int = 0
    first_period: str | None = None
    last_period: str | None = None
    failure: str | None = None


def _rows(values: list[Any]) -> list[list[Any]] | None:
    if all(isinstance(item, Mapping) for item in values):
        return [values]
    if all(isinstance(item, list) for item in values):
        return values
    return None


def _cell_label(cell: Any) -> str | None:
    if not isinstance(cell, Mapping):
        return None
    text = cell.get("text", cell.get("data"))
    return text if isinstance(text, str) else None


def summarize_response(
    status: int | None, body_text: str | None, structure: WidgetStructure, targets: Iterable[str]
) -> ResponseSummary:
    """Classify the response shape in memory. Metric values are not kept."""
    if status != 200:
        return ResponseSummary(status, False, SHAPE_NOT_SUCCESSFUL)
    if body_text is None:
        return ResponseSummary(status, False, SHAPE_UNAVAILABLE, failure="Response body unavailable")
    try:
        body = parse_json_body(body_text)
    except Exception as exc:
        return ResponseSummary(status, True, SHAPE_UNPARSEABLE, failure=type(exc).__name__)
    if body.get("error"):
        return ResponseSummary(status, True, SHAPE_UNPARSEABLE, failure="Response reports an error")
    values = body.get("values")
    if not isinstance(values, list):
        return ResponseSummary(status, True, SHAPE_UNPARSEABLE, failure="Missing 'values' list")
    if not values:
        return ResponseSummary(status, True, SHAPE_EMPTY)
    rows = _rows(values)
    width = len(structure.columns)
    if rows is None or any(len(row) != width or not all(isinstance(c, Mapping) for c in row) for row in rows):
        return ResponseSummary(status, True, SHAPE_UNPARSEABLE, failure="Rows do not match request columns")
    cells = sum(len(row) for row in rows)

    date_index = next((i for i, c in enumerate(structure.columns) if c.is_date), None)
    category_indexes = [i for i, c in enumerate(structure.columns) if c.kind == "dimension" and not c.is_date]
    if date_index is not None:
        periods = [_cell_label(row[date_index]) for row in rows]
        labeled = [p for p in periods if p]
        return ResponseSummary(status, True, SHAPE_TIME_SERIES, len(rows), cells, time_points=len(rows),
                               first_period=labeled[0] if labeled else None,
                               last_period=labeled[-1] if labeled else None)
    if category_indexes:
        wanted = {normalize_label(t): t for t in targets if normalize_label(t)}
        seen = {normalize_label(_cell_label(row[i])) for row in rows for i in category_indexes}
        matched = tuple(wanted[key] for key in wanted if key in seen)
        return ResponseSummary(status, True, SHAPE_CATEGORICAL, len(rows), cells, matched_category_labels=matched)
    shape = SHAPE_MULTI_MEASURE if len(structure.measures) > 1 else SHAPE_SINGLE
    return ResponseSummary(status, True, shape, len(rows), cells)


@dataclass(frozen=True)
class WidgetDiscovery:
    structure: WidgetStructure
    observations: int
    structure_changed: bool
    title_matches: Mapping[str, tuple[str, ...]]
    response: ResponseSummary | None
    control: str | None = None

    @property
    def matches(self) -> dict[str, tuple[str, ...]]:
        merged = {key: list(sources) for key, sources in self.title_matches.items()}
        if self.response is not None:
            for label in self.response.matched_category_labels:
                merged.setdefault(normalize_label(label), []).append(SOURCE_CATEGORY_LABEL)
        return {key: tuple(sources) for key, sources in merged.items()}


@dataclass(frozen=True)
class DiscoveryResult:
    widgets: tuple[WidgetDiscovery, ...]
    target_requests_seen: int
    out_of_scope: int
    widgets_dropped: int
    retention_skipped: int = 0


def _read_response(request: Request) -> tuple[int | None, str | None]:
    """(status, body text) of the response the browser already received; body only for HTTP 200."""
    try:
        response = request.response()
    except Exception:
        return None, None
    if response is None:
        return None, None
    status = response.status
    if status != 200:
        return status, None
    try:
        return status, response.body().decode("utf-8")
    except Exception:
        return status, None


def verified_widget_value(
    request: Request, status: int | None, text: str | None, expected: ExpectedWidgetQuery
) -> tuple[str, int | None]:
    """(control result, passive value) for the verified widget, with the strict Phase 3B checks."""
    payload = _request_payload(request)
    if payload is None or not check_candidate(payload, expected).accepted:
        return "FAIL (identity/filter verification)", None
    if status != 200:
        return f"FAIL (HTTP {status})", None
    if text is None:
        return "FAIL (response body unavailable)", None
    try:
        value = extract_count_measure_value(parse_json_body(text), payload, expected.primary_measure_title)
    except Exception as exc:
        return f"FAIL (parse: {type(exc).__name__})", None
    return CONTROL_PASS, value


def _control_check(request: Request, status: int | None, text: str | None, expected: ExpectedWidgetQuery) -> str:
    """The verified widget keeps its strict Phase 3B checks; discovery never relaxes them (and keeps no value)."""
    return verified_widget_value(request, status, text, expected)[0]


def evaluate_discovery(observer: DiscoveryObserver, expected: ExpectedWidgetQuery | None) -> DiscoveryResult:
    """Inspect retained responses in memory (main flow only; driver reads, no network)."""
    widgets = []
    for entry in observer.entries():
        matches = title_matches(entry.structure, observer.target_labels)
        response = None
        control = None
        if entry.latest_request is not None:
            status, text = _read_response(entry.latest_request)
            response = summarize_response(status, text, entry.structure, observer.target_labels)
            if expected is not None and entry.structure.widget_id == expected.widget_id:
                control = _control_check(entry.latest_request, status, text, expected)
        widgets.append(WidgetDiscovery(entry.structure, entry.observations, entry.structure_changed,
                                       matches, response, control))
    return DiscoveryResult(
        widgets=tuple(widgets),
        target_requests_seen=observer.target_requests_seen,
        out_of_scope=observer.out_of_scope,
        widgets_dropped=observer.widgets_dropped,
        retention_skipped=sum(entry.retention_skipped for entry in observer.entries()),
    )
