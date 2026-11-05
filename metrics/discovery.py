"""Resolve Wave 1 discovery targets against passively observed dashboard widgets.

Results carry structural metadata only (ids, titles, shapes, statuses); no
metric values, filter values or request/response bodies. DISCOVERED results
are never MetricDefinitions, so they cannot reach snapshot history or the
executive report.
"""

from __future__ import annotations

from dataclasses import dataclass

from metrics.catalog import (
    CANCELLATION_CANDIDATES,
    DISCOVERY_TARGETS,
    GRAIN_DAYS,
    STATE_DISCOVERED,
    STATE_PENDING,
    STATE_VERIFIED,
    DiscoveryTarget,
    catalog_state,
)
from sisense.discovery import (
    CONTROL_PASS,
    SHAPE_NOT_SUCCESSFUL,
    SHAPE_UNAVAILABLE,
    SHAPE_UNPARSEABLE,
    DiscoveryResult,
    WidgetDiscovery,
    normalize_label,
)

_UNUSABLE_SHAPES = {SHAPE_NOT_SUCCESSFUL: "Matched widget did not return HTTP 200",
                    SHAPE_UNAVAILABLE: "Matched widget's response body was unavailable",
                    SHAPE_UNPARSEABLE: "Matched widget's response could not be parsed structurally"}


@dataclass(frozen=True)
class MetricDiscovery:
    metric_id: str
    label: str
    module_id: str
    state: str
    widgets: tuple[WidgetDiscovery, ...]
    match_sources: tuple[str, ...]
    reason: str
    control: str | None = None

    @property
    def widget(self) -> WidgetDiscovery | None:
        return self.widgets[0] if len(self.widgets) == 1 else None


def _verified(target: DiscoveryTarget, matched: list[WidgetDiscovery]) -> MetricDiscovery:
    """Catalog-verified metrics stay VERIFIED and bind to the widget that matched their own label."""
    key = normalize_label(target.label)
    if not matched:
        return MetricDiscovery(target.metric_id, target.label, target.module_id, STATE_VERIFIED, (), (),
                               "Verified catalog metric; its verified widget was not observed in this run",
                               "NOT OBSERVED")
    ids = {w.structure.widget_id for w in matched}
    if len(ids) == 1:
        passing = [w for w in matched if w.control == CONTROL_PASS]
        widgets = (passing[0] if passing else _choose_definition(matched),)
    else:
        widgets = tuple(matched)
    widget = widgets[0] if len(widgets) == 1 else None
    control = widget.control if widget is not None else None
    if control is None:
        reason = "Verified catalog metric; exact label match in the dashboard's own requests"
    elif control == CONTROL_PASS:
        reason = "Verified catalog metric; strict control check passed"
    else:
        reason = f"Verified catalog metric; strict control check {control}"
    sources = tuple(s for w in widgets for s in w.matches.get(key, ()))
    return MetricDiscovery(target.metric_id, target.label, target.module_id, STATE_VERIFIED, widgets, sources,
                           reason, control)


def _choose_definition(matched: list[WidgetDiscovery], target: DiscoveryTarget | None = None) -> WidgetDiscovery:
    """Pick one retained definition of a single widget. Exact title match already applied.

    A daily candidate with an observed date column prefers that column and grain
    over another retained definition of the same widget that only shares the title.
    """
    usable = [widget for widget in matched
              if widget.response is not None and widget.response.shape not in _UNUSABLE_SHAPES]
    pool = usable or list(matched)
    candidate = CANCELLATION_CANDIDATES.get(target.metric_id) if target is not None else None
    if candidate is not None and candidate.source_date_column:
        exact = []
        for widget in pool:
            dates = [column for column in widget.structure.dimensions if column.is_date]
            if (len(dates) == 1 and dates[0].table == candidate.source_table
                    and dates[0].column == candidate.source_date_column and dates[0].level == GRAIN_DAYS):
                exact.append(widget)
        if exact:
            return exact[0]
    return pool[0]


def resolve_targets(
    result: DiscoveryResult, targets: tuple[DiscoveryTarget, ...] = DISCOVERY_TARGETS
) -> tuple[MetricDiscovery, ...]:
    resolved = []
    for target in targets:
        key = normalize_label(target.label)
        matched = [w for w in result.widgets if key in w.matches]
        if catalog_state(target.metric_id) == STATE_VERIFIED:
            resolved.append(_verified(target, matched))
            continue

        def pending(reason: str, widgets=tuple(matched)) -> MetricDiscovery:
            sources = tuple(s for w in widgets for s in w.matches.get(key, ()))
            return MetricDiscovery(target.metric_id, target.label, target.module_id, STATE_PENDING,
                                   tuple(widgets), sources, reason)

        if not matched:
            resolved.append(pending("No exact label match in the dashboard's own requests"))
        elif len({w.structure.widget_id for w in matched}) > 1:
            resolved.append(pending(f"Ambiguous: exact label found in {len(matched)} widgets"))
        else:
            widget = _choose_definition(matched, target)
            response = widget.response
            if response is None:
                resolved.append(pending("Matched widget's response was not inspected (retention limit)"))
            elif response.shape in _UNUSABLE_SHAPES:
                resolved.append(pending(_UNUSABLE_SHAPES[response.shape]))
            else:
                resolved.append(MetricDiscovery(
                    target.metric_id, target.label, target.module_id, STATE_DISCOVERED, (widget,),
                    widget.matches[key], "Exact label match in one widget; not approved for production",
                ))
    return tuple(resolved)
