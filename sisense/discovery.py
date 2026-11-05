"""Passive discovery of the dashboard's own JAQL widgets (structure only).

Scope: completed POSTs to the verified JAQL URL in the attached context whose
body names the target dashboard and datasource. Request bodies (already held
by Playwright) are reduced to safe structural metadata as each request
completes; raw bodies are never retained, logged or persisted. Each widget
keeps one request per distinct definition fingerprint. A repeated fingerprint
updates only that definition. A bounded number of Request references is kept
so responses the browser already received can be inspected later, in memory,
from the main flow.

Nothing here sends a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from playwright.sync_api import BrowserContext, Request

from config.settings import VERIFIED_JAQL_URL
from sisense.passive import (
    ExpectedWidgetQuery,
    _request_payload,
    check_candidate,
    definition_fingerprint,
    split_reference,
)
from validation.validators import extract_count_measure_value, parse_json_body

logger = logging.getLogger(__name__)

MAX_WIDGETS = 80
# Inventory fingerprints and response shapes need the already-received Request.
# Bound to the same cap as widget tracking so a full dashboard load can be inventoried.
MAX_RETAINED = MAX_WIDGETS

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
class FieldIdentity:
    """A referenced table and column. No filter members and no formula text."""

    table: str | None
    column: str | None


@dataclass(frozen=True)
class Column:
    title: str | None
    kind: str  # "measure" | "dimension"
    datatype: str | None
    level: str | None
    table: str | None = None
    column: str | None = None
    column_title: str | None = None
    context_fields: tuple[FieldIdentity, ...] = ()

    @property
    def is_date(self) -> bool:
        return self.kind == "dimension" and (self.datatype == "datetime" or self.level is not None)


@dataclass(frozen=True)
class MetadataRole:
    """Safe role of one metadata item. No filter members and no formula text."""

    panel: str
    kind: str
    jaql_type: str | None
    title: str | None
    date_level: str | None
    column_title: str | None = None
    table: str | None = None
    column: str | None = None
    context_fields: tuple[FieldIdentity, ...] = ()


@dataclass(frozen=True)
class WidgetStructure:
    """Safe structural metadata from a dashboard request body.

    ``columnTitle``, table, column, and context field names are kept.
    Filter member values and formula text are not.
    """

    dashboard_id: str
    widget_id: str
    widget_title: str
    widget_type: str | None
    datasource_id: str | None
    datasource_title: str | None
    columns: tuple[Column, ...]
    filter_count: int
    dashboard_filter_count: int
    metadata_item_count: int = 0
    metadata_roles: tuple[MetadataRole, ...] = ()

    @property
    def measures(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "measure")

    @property
    def dimensions(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "dimension")

    @property
    def date_grain(self) -> str | None:
        dates = [c for c in self.dimensions if c.is_date]
        if len(dates) != 1:
            return None
        return dates[0].level


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _column_title(item: Mapping[str, Any], jaql: Mapping[str, Any]) -> str | None:
    """``columnTitle`` is a separate label from ``jaql.title``. Either location is accepted."""
    for source in (jaql, item):
        title = _text(source.get("columnTitle"))
        if title is not None:
            return title
    return None


def _context_fields(jaql: Mapping[str, Any]) -> tuple[FieldIdentity, ...]:
    """Table and column names referenced by a measure context. Literals and members are dropped."""
    context = jaql.get("context")
    if not isinstance(context, Mapping):
        return ()
    found: list[FieldIdentity] = []
    for value in context.values():
        if not isinstance(value, Mapping):
            continue
        identity = FieldIdentity(_text(value.get("table")), _text(value.get("column")))
        if identity.table is None and identity.column is None:
            continue
        if identity not in found:
            found.append(identity)
    return tuple(sorted(found, key=lambda item: (item.table or "", item.column or "")))


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
    roles = []
    filters = 0
    dashboard_filters = 0
    for item in items:
        jaql = item.get("jaql") if isinstance(item.get("jaql"), Mapping) else {}
        title = _text(jaql.get("title"))
        jaql_type = _text(jaql.get("type"))
        level = _text(jaql.get("level"))
        table = _text(jaql.get("table"))
        column_name = _text(jaql.get("column"))
        column_title = _column_title(item, jaql)
        context_fields = _context_fields(jaql)
        if item.get("panel") == "scope":
            filters += 1
            dashboard_filters += jaql.get("isDashboardFilter") is True
            roles.append(MetadataRole(
                "scope", "filter", jaql_type, title, level, column_title, table, column_name, context_fields,
            ))
            continue
        kind = "measure" if jaql.get("type") == "measure" or "formula" in jaql else "dimension"
        datatype = _text(jaql.get("datatype"))
        columns.append(Column(title, kind, datatype, level, table, column_name, column_title, context_fields))
        roles.append(MetadataRole(
            "result", kind, jaql_type, title, level, column_title, table, column_name, context_fields,
        ))
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
        metadata_item_count=len(items),
        metadata_roles=tuple(roles),
    )


def title_matches(structure: WidgetStructure, targets: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Exact matches against widget, measure, and dimension ``jaql.title`` values.

    ``columnTitle``, table, column, and context field names are not match sources.
    Production discovery stays on the existing title fields.
    """
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
class _DefinitionRetention:
    """One distinct metric definition for a widget. Repeats update only this record."""

    fingerprint: str
    structure: WidgetStructure
    observations: int = 0
    latest_request: Request | None = None
    sequence: int = 0


@dataclass
class _WidgetEntry:
    """Widget identity plus every distinct definition observed for it.

    The key is widget id + definition fingerprint. A later request replaces only
    the record with the same fingerprint. Returning to an earlier definition
    does not delete the others.
    """

    structure: WidgetStructure
    definitions: dict[str, _DefinitionRetention] = field(default_factory=dict)
    retention_skipped: bool = False

    @property
    def observations(self) -> int:
        return sum(item.observations for item in self.definitions.values())

    @property
    def structure_changed(self) -> bool:
        return len(self.definitions) > 1

    @property
    def latest_request(self) -> Request | None:
        if not self.definitions:
            return None
        return max(self.definitions.values(), key=lambda item: item.sequence).latest_request


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
        self._closed = False
        self._sequence = 0

    def attach(self, context: BrowserContext) -> None:
        self._context = context
        self._closed = False
        self._widgets = {}
        self.target_requests_seen = 0
        self.out_of_scope = 0
        self.widgets_dropped = 0
        self._sequence = 0

        def on_request_finished(request: Request) -> None:
            if context is self._context:
                self._on_request_finished(request)

        context.on("requestfinished", on_request_finished)

    def stop(self) -> None:
        """Stop retaining dashboard traffic. Later requests, including this application's, are ignored."""
        self._closed = True
        self._context = None

    def is_target(self, request: Request) -> bool:
        return request.method == "POST" and request.url == self._url

    @property
    def retained_count(self) -> int:
        return sum(len(entry.definitions) for entry in self._widgets.values())

    def _is_retained(self, request: Request) -> bool:
        return any(
            definition.latest_request is request
            for entry in self._widgets.values()
            for definition in entry.definitions.values()
        )

    def _relevant(self, structure: WidgetStructure) -> bool:
        """Retain every in-scope widget so discovery can inventory structure without extra HTTP.

        Production binding still uses exact measure titles. Label match is not required
        to keep a naturally observed request for the safe inventory.
        """
        return structure is not None

    def _on_request_finished(self, request: Request) -> None:
        if self._closed:
            return
        if not self.is_target(request):
            return
        if self._is_retained(request):
            return
        self.target_requests_seen += 1
        payload = _request_payload(request)
        structure = widget_structure(payload)
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
        if not self._relevant(structure):
            return
        fingerprint = definition_fingerprint(payload)
        definition = entry.definitions.get(fingerprint)
        if definition is None and self.retained_count >= self._max_retained:
            entry.retention_skipped = True
            return
        if definition is None:
            definition = entry.definitions[fingerprint] = _DefinitionRetention(fingerprint, structure)
        self._sequence += 1
        definition.observations += 1
        definition.sequence = self._sequence
        definition.structure = structure
        definition.latest_request = request
        entry.structure = structure

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
    definition_fingerprint: str | None = None

    @property
    def matches(self) -> dict[str, tuple[str, ...]]:
        merged = {key: list(sources) for key, sources in self.title_matches.items()}
        if self.response is not None:
            for label in self.response.matched_category_labels:
                merged.setdefault(normalize_label(label), []).append(SOURCE_CATEGORY_LABEL)
        return {key: tuple(sources) for key, sources in merged.items()}


HIDDEN_STATE_METADATA = "Fingerprint differs only in non-exposed state metadata."


@dataclass(frozen=True)
class FingerprintStateDifference:
    """Which exposed safe fields differ between two fingerprints of one widget."""

    widget_id: str
    widget_title: str
    fingerprint_a: str
    fingerprint_b: str
    column_title_changed: bool
    table_column_changed: bool
    context_fields_changed: bool
    scope_structure_changed: bool
    jaql_title_changed: bool
    note: str | None


def _role_column_titles(structure: WidgetStructure) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((role.panel, role.column_title or "") for role in structure.metadata_roles))


def _role_tables(structure: WidgetStructure) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (role.panel, role.table or "", role.column or "") for role in structure.metadata_roles
    ))


def _role_context(structure: WidgetStructure) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (role.panel, field.table or "", field.column or "")
        for role in structure.metadata_roles
        for field in role.context_fields
    ))


def _scope_structure(structure: WidgetStructure) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(
        (
            role.kind,
            role.jaql_type or "",
            role.title or "",
            role.column_title or "",
            role.table or "",
            role.column or "",
            role.date_level or "",
            tuple((field.table or "", field.column or "") for field in role.context_fields),
        )
        for role in structure.metadata_roles
        if role.panel == "scope"
    ))


def _jaql_titles(structure: WidgetStructure) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (role.panel, role.kind, role.title or "") for role in structure.metadata_roles
    ))


def explain_fingerprint_differences(
    widgets: Iterable[WidgetDiscovery],
) -> tuple[FingerprintStateDifference, ...]:
    """Pairwise safe comparison of definitions that share a widget id.

    Member values are not compared. When every exposed safe field matches,
    the note is ``HIDDEN_STATE_METADATA``.
    """
    grouped: dict[str, list[WidgetDiscovery]] = {}
    for widget in widgets:
        if not widget.definition_fingerprint:
            continue
        grouped.setdefault(widget.structure.widget_id, []).append(widget)
    differences = []
    for widget_id in sorted(grouped):
        group = sorted(grouped[widget_id], key=lambda item: item.definition_fingerprint or "")
        if len(group) < 2:
            continue
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                structure_a = left.structure
                structure_b = right.structure
                column_title_changed = _role_column_titles(structure_a) != _role_column_titles(structure_b)
                table_column_changed = _role_tables(structure_a) != _role_tables(structure_b)
                context_fields_changed = _role_context(structure_a) != _role_context(structure_b)
                scope_structure_changed = _scope_structure(structure_a) != _scope_structure(structure_b)
                jaql_title_changed = _jaql_titles(structure_a) != _jaql_titles(structure_b)
                hidden = not any((
                    column_title_changed,
                    table_column_changed,
                    context_fields_changed,
                    scope_structure_changed,
                    jaql_title_changed,
                ))
                differences.append(FingerprintStateDifference(
                    widget_id=widget_id,
                    widget_title=structure_a.widget_title,
                    fingerprint_a=left.definition_fingerprint or "",
                    fingerprint_b=right.definition_fingerprint or "",
                    column_title_changed=column_title_changed,
                    table_column_changed=table_column_changed,
                    context_fields_changed=context_fields_changed,
                    scope_structure_changed=scope_structure_changed,
                    jaql_title_changed=jaql_title_changed,
                    note=HIDDEN_STATE_METADATA if hidden else None,
                ))
    return tuple(differences)


@dataclass(frozen=True)
class DiscoveryResult:
    widgets: tuple[WidgetDiscovery, ...]
    target_requests_seen: int
    out_of_scope: int
    widgets_dropped: int
    retention_skipped: int = 0

    @property
    def widget_count(self) -> int:
        return len({widget.structure.widget_id for widget in self.widgets})

    @property
    def definition_count(self) -> int:
        return sum(1 for widget in self.widgets if widget.definition_fingerprint)


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
    """Inspect every retained definition in memory (main flow only; driver reads, no network)."""
    widgets = []
    for entry in observer.entries():
        if not entry.definitions:
            matches = title_matches(entry.structure, observer.target_labels)
            widgets.append(WidgetDiscovery(entry.structure, 0, False, matches, None, None, None))
            continue
        multiple = len(entry.definitions) > 1
        for definition in entry.definitions.values():
            matches = title_matches(definition.structure, observer.target_labels)
            response = None
            control = None
            if definition.latest_request is not None:
                status, text = _read_response(definition.latest_request)
                response = summarize_response(
                    status, text, definition.structure, observer.target_labels,
                )
                if expected is not None and definition.structure.widget_id == expected.widget_id:
                    control = _control_check(definition.latest_request, status, text, expected)
            widgets.append(WidgetDiscovery(
                definition.structure, definition.observations, multiple, matches, response, control,
                definition.fingerprint,
            ))
    return DiscoveryResult(
        widgets=tuple(widgets),
        target_requests_seen=observer.target_requests_seen,
        out_of_scope=observer.out_of_scope,
        widgets_dropped=observer.widgets_dropped,
        retention_skipped=sum(entry.retention_skipped for entry in observer.entries()),
    )
