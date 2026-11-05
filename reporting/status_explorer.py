"""Enrollment Status Explorer: presentation only.

Values come from the report model. Unverified statuses have no numeric value.
Separate verified measures are never combined into a share of one population
unless an explicit verified partition says they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.derived import INSUFFICIENT_HISTORY, SOURCE
from metrics.catalog import (
    ACTIVE_ENROLLED,
    CANCELLATION_CANDIDATES,
    CURRENTLY_ENROLLED,
    METRICS,
    NEW_ENROLLED,
    PASSIVE_ENROLLED,
    STATUS_ACTIVE,
)
from reporting.formatting import fmt_comparison, fmt_int, fmt_metric_value, fmt_signed, fmt_time
from reporting.model import DATA_NOT_AVAILABLE, ReportModel

PENDING_SOURCE = "Source verification pending"
PENDING_ENROLLMENT = "Pending enrollment source verification"
DISTRIBUTION_NOTE = (
    "Counts are shown separately. Currently Enrolled is a distinct population from New, Active, and Passive."
)

# Same-widget status measures still are not a verified partition of Currently Enrolled,
# or of each other, until a source definition says so.
_POPULATION = {
    CURRENTLY_ENROLLED.metric_id: "currently_enrolled",
    NEW_ENROLLED.metric_id: "enrollment_status",
    ACTIVE_ENROLLED.metric_id: "enrollment_status",
    PASSIVE_ENROLLED.metric_id: "enrollment_status",
}
VERIFIED_PARTITIONS: frozenset[frozenset[str]] = frozenset()

VERIFIED_STATUS_IDS = (
    CURRENTLY_ENROLLED.metric_id,
    NEW_ENROLLED.metric_id,
    ACTIVE_ENROLLED.metric_id,
    PASSIVE_ENROLLED.metric_id,
)


@dataclass(frozen=True)
class PendingGroup:
    heading: str
    items: tuple[str, ...]
    section_id: str


@dataclass(frozen=True)
class ExplorerStatus:
    key: str
    label: str
    metric_ids: tuple[str, ...]
    groups: tuple[PendingGroup, ...] = ()
    message: str | None = None
    series_metric_id: str | None = None

    @property
    def verified(self) -> bool:
        return bool(self.metric_ids) and not self.groups


EXPLORER_STATUSES: tuple[ExplorerStatus, ...] = (
    ExplorerStatus("all", "ALL", VERIFIED_STATUS_IDS),
    ExplorerStatus("enrolled", "ENROLLED", (CURRENTLY_ENROLLED.metric_id,)),
    ExplorerStatus("new", "NEW", (NEW_ENROLLED.metric_id,)),
    ExplorerStatus("active", "ACTIVE", (ACTIVE_ENROLLED.metric_id,)),
    ExplorerStatus("passive", "PASSIVE", (PASSIVE_ENROLLED.metric_id,)),
    ExplorerStatus(
        "cancelled", "CANCELLED", (),
        (
            PendingGroup("Cancellation Overview", (
                "Enrolled & Cancelled", "Cancelled — Not Enrolled",
            ), "cancellation_overview"),
            PendingGroup("Cancellation Activity", (
                "Cancelled Policies — Daily",
            ), "cancellation_activity"),
        ),
        PENDING_SOURCE,
        series_metric_id="cancelled_policies_daily",
    ),
    ExplorerStatus(
        "terminated", "TERMINATED", (),
        (
            PendingGroup("Cancellation Activity", (
                "Terminated Policies — Daily",
            ), "cancellation_activity"),
        ),
        PENDING_SOURCE,
        series_metric_id="terminated_policies_daily",
    ),
    ExplorerStatus("pending", "PENDING", (), message=PENDING_ENROLLMENT),
)


def share_of_whole_allowed(metric_ids: tuple[str, ...]) -> bool:
    """True only for an explicit verified partition of one population."""
    ids = tuple(metric_ids)
    if len(ids) < 2 or len(set(ids)) != len(ids):
        return False
    if any(metric_id not in _POPULATION for metric_id in ids):
        return False
    if len({_POPULATION[metric_id] for metric_id in ids}) != 1:
        return False
    return frozenset(ids) in VERIFIED_PARTITIONS


def _history(panel_points) -> list[dict[str, Any]]:
    return [{"t": point.observed_at.isoformat(), "v": point.value} for point in panel_points]


def _metric_record(metric, analytics, data_status: str) -> dict[str, Any]:
    latest = analytics.latest
    previous = analytics.previous
    value = None if latest is None else latest.value
    return {
        "metric_id": metric.metric_id,
        "display_name": metric.display_name,
        "plan_year": metric.plan_year,
        "population": _POPULATION.get(metric.metric_id),
        "semantic": SOURCE,
        "data_status": data_status,
        "value": value,
        "display_value": None if value is None else fmt_metric_value(value, metric.value_format),
        "retrieved_label": None if latest is None else fmt_time(latest.observed_at),
        "previous_change": fmt_comparison(previous),
        "history": _history(analytics.points),
        "observations": _observation_rows(metric, analytics.points, data_status),
    }


def _observation_rows(metric, points, data_status: str) -> list[dict[str, str]]:
    """One row per stored or live observation. Missing history is an empty list, not a zero."""
    rows = []
    for index, point in enumerate(points):
        previous = points[index - 1] if index else None
        rows.append({
            "observed_at": fmt_time(point.observed_at),
            "metric": metric.display_name,
            "value": fmt_metric_value(point.value, metric.value_format),
            "source": SOURCE,
            "data_status": data_status if index == len(points) - 1 else "Stored observation",
            "previous_value": "—" if previous is None else fmt_metric_value(previous.value, metric.value_format),
            "change": INSUFFICIENT_HISTORY if previous is None else fmt_signed(point.value - previous.value),
        })
    return rows


def explorer_document(model: ReportModel) -> dict[str, Any]:
    """Safe presentation payload. No fingerprints, requests, or invented zeros."""
    metrics: dict[str, dict[str, Any]] = {}
    if model.metric.status == STATUS_ACTIVE and model.metric.metric_id in _POPULATION:
        metrics[model.metric.metric_id] = _metric_record(model.metric, model.analytics, model.data_status)
    for panel in model.panels:
        if panel.metric.status == STATUS_ACTIVE and panel.metric.metric_id in _POPULATION:
            metrics[panel.metric.metric_id] = _metric_record(panel.metric, panel.analytics, panel.data_status)
    return {
        "note": DISTRIBUTION_NOTE,
        "share_of_whole": False,
        "names": {metric_id: METRICS[metric_id].display_name for metric_id in VERIFIED_STATUS_IDS},
        "statuses": [
            {
                "key": status.key,
                "label": status.label,
                "metric_ids": list(status.metric_ids),
                "verified": status.verified,
                "message": status.message,
                "series_metric_id": status.series_metric_id,
                "groups": [
                    {"heading": group.heading, "section_id": group.section_id, "items": list(group.items)}
                    for group in status.groups
                ],
            }
            for status in EXPLORER_STATUSES
        ],
        "metrics": metrics,
        "series": _series_document(model),
    }


def _series_document(model: ReportModel) -> dict[str, list[dict[str, Any]]]:
    """Persisted source-series rows only. No fingerprint and no invented dates."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for point in getattr(model, "source_series", ()):
        label = CANCELLATION_CANDIDATES[point.metric_id].display_name
        grouped.setdefault(point.metric_id, []).append({
            "date": point.source_date,
            "series": label,
            "value": point.value,
            "value_display": fmt_int(point.value),
        })
    return grouped


def distribution_figure(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Absolute counts only. Missing values are omitted, never drawn as zero."""
    ids = tuple(record["metric_id"] for record in records)
    if share_of_whole_allowed(ids):
        return None
    present = [
        record for record in records
        if isinstance(record.get("value"), (int, float)) and not isinstance(record.get("value"), bool)
    ]
    if not present:
        return None
    return {
        "mode": "separate_counts",
        "share_of_whole": False,
        "data": [{
            "type": "bar",
            "orientation": "h",
            "x": [record["value"] for record in present],
            "y": [record["display_name"] for record in present],
            "marker": {"color": "#1d5fa7"},
            "hovertemplate": "%{y}: %{x:,.0f} people<extra></extra>",
        }],
        "layout": {
            "margin": {"l": 180, "r": 16, "t": 8, "b": 36},
            "height": 220,
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "#ffffff",
            "xaxis": {"title": {"text": "People"}, "tickformat": ",.0f", "gridcolor": "#e6ecf3"},
            "yaxis": {"automargin": True},
        },
    }


def _status_document(document: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in document["statuses"] if item["key"] == key)


def _missing_record(document: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "display_name": document["names"][metric_id],
        "population": _POPULATION.get(metric_id),
        "semantic": SOURCE,
        "data_status": DATA_NOT_AVAILABLE,
        "value": None,
        "display_value": None,
        "retrieved_label": None,
        "previous_change": INSUFFICIENT_HISTORY,
        "history": [],
        "observations": [],
    }


def detail_records(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Verified metrics only. A missing production value stays unavailable."""
    status = _status_document(document, key)
    if not status["verified"]:
        return []
    return [
        document["metrics"].get(metric_id) or _missing_record(document, metric_id)
        for metric_id in status["metric_ids"]
    ]


def detail_lines(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Executive detail. A missing value stays unavailable; it is not zero."""
    value = record["display_value"] if record["value"] is not None else "Not available"
    retrieved = record["retrieved_label"] or "—"
    history = "Stored observations available" if len(record["history"]) >= 2 else "Insufficient history"
    return [
        ("Current value", value),
        ("Type", record["semantic"]),
        ("Status", record["data_status"]),
        ("Retrieved", retrieved),
        ("Previous observation", record["previous_change"]),
        ("History", history),
    ]


EXPLORER_SCRIPT = r"""
/* enrollment-status-explorer */
(function () {
  var explorer = __EXPLORER_JSON__;
  var buttons = document.querySelectorAll("[data-status-key]");
  var chart = document.getElementById("status-distribution-chart");
  var chartEmpty = document.getElementById("status-distribution-empty");
  var historyChart = document.getElementById("status-history-chart");
  var detail = document.getElementById("status-detail");
  var plotOpts = {responsive: true, displaylogo: false};

  function statusBy(key) {
    for (var i = 0; i < explorer.statuses.length; i++) {
      if (explorer.statuses[i].key === key) return explorer.statuses[i];
    }
    return explorer.statuses[0];
  }

  function recordsFor(status) {
    if (!status.verified) return [];
    return status.metric_ids.map(function (id) {
      var found = explorer.metrics[id];
      if (found) return found;
      return {
        metric_id: id,
        display_name: explorer.names[id] || id,
        value: null,
        display_value: null,
        semantic: "SOURCE",
        data_status: "NOT AVAILABLE",
        retrieved_label: null,
        previous_change: "Insufficient history",
        history: [],
        observations: []
      };
    });
  }

  function countable(records) {
    if (explorer.share_of_whole === true) return [];
    return records.filter(function (record) { return typeof record.value === "number"; });
  }

  function lines(record) {
    var current = record.value === null || typeof record.value === "undefined"
      ? "Not available" : record.display_value;
    var history = record.history && record.history.length >= 2
      ? "Stored observations available" : "Insufficient history";
    return [
      ["Current value", current],
      ["Type", record.semantic],
      ["Status", record.data_status],
      ["Retrieved", record.retrieved_label || "—"],
      ["Previous observation", record.previous_change],
      ["History", history]
    ];
  }

  function fillDetail(records) {
    if (!detail) return;
    detail.replaceChildren();
    records.forEach(function (record) {
      var article = document.createElement("article");
      article.setAttribute("data-status-detail", record.metric_id);
      var title = document.createElement("h3");
      title.textContent = record.display_name;
      var table = document.createElement("table");
      table.className = "kv";
      lines(record).forEach(function (pair) {
        var row = document.createElement("tr");
        var head = document.createElement("th");
        var cell = document.createElement("td");
        head.textContent = pair[0];
        cell.textContent = pair[1];
        row.appendChild(head);
        row.appendChild(cell);
        table.appendChild(row);
      });
      article.appendChild(title);
      article.appendChild(table);
      var observations = record.observations || [];
      if (observations.length) {
        article.appendChild(observationTable(observations));
      }
      detail.appendChild(article);
    });
  }

  function observationTable(observations) {
    var table = document.createElement("table");
    table.className = "stats status-records";
    var head = document.createElement("tr");
    ["Observation timestamp", "Metric/status", "Value", "Source", "Data status",
      "Historical observation", "Change"].forEach(function (label) {
      var cell = document.createElement("th");
      cell.textContent = label;
      head.appendChild(cell);
    });
    var thead = document.createElement("thead");
    thead.appendChild(head);
    var body = document.createElement("tbody");
    observations.forEach(function (row) {
      var tr = document.createElement("tr");
      [row.observed_at, row.metric, row.value, row.source, row.data_status,
        row.previous_value, row.change].forEach(function (text) {
        var cell = document.createElement("td");
        cell.textContent = text;
        tr.appendChild(cell);
      });
      body.appendChild(tr);
    });
    table.appendChild(thead);
    table.appendChild(body);
    return table;
  }

  function seriesRows(status) {
    if (!status.series_metric_id || !explorer.series) return [];
    return explorer.series[status.series_metric_id] || [];
  }

  function drawSeries(status) {
    var rows = seriesRows(status);
    document.querySelectorAll("[data-series-chart]").forEach(function (node) {
      var mine = node.getAttribute("data-series-chart") === status.series_metric_id;
      node.hidden = !mine || !rows.length;
      if (!mine || !rows.length) return;
      Plotly.newPlot(node, [{
        type: "bar",
        x: rows.map(function (row) { return row.date; }),
        y: rows.map(function (row) { return row.value; })
      }], {
        margin: {l: 64, r: 16, t: 8, b: 48},
        height: 220,
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "#ffffff",
        xaxis: {title: {text: "Source date"}, type: "category"},
        yaxis: {title: {text: "Record count"}, tickformat: ",.0f"}
      }, plotOpts);
    });
  }

  function drawBars(records) {
    var present = countable(records);
    if (!chart) return;
    if (!present.length) {
      chart.hidden = true;
      if (chartEmpty) chartEmpty.hidden = false;
      return;
    }
    if (chartEmpty) chartEmpty.hidden = true;
    chart.hidden = false;
    Plotly.newPlot(chart, [{
      type: "bar",
      orientation: "h",
      x: present.map(function (record) { return record.value; }),
      y: present.map(function (record) { return record.display_name; }),
      marker: {color: "#1d5fa7"},
      hovertemplate: "%{y}: %{x:,.0f} people<extra></extra>"
    }], {
      margin: {l: 180, r: 16, t: 8, b: 36},
      height: 220,
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "#ffffff",
      xaxis: {title: {text: "People"}, tickformat: ",.0f", gridcolor: "#e6ecf3"},
      yaxis: {automargin: true}
    }, plotOpts);
  }

  function drawHistory(records) {
    if (!historyChart) return;
    var series = records.length === 1 ? records[0].history : [];
    if (!series || series.length < 2) {
      historyChart.hidden = true;
      return;
    }
    historyChart.hidden = false;
    Plotly.newPlot(historyChart, [{
      type: "scatter",
      mode: "lines+markers",
      x: series.map(function (point) { return point.t; }),
      y: series.map(function (point) { return point.v; }),
      line: {color: "#1d5fa7"}
    }], {
      margin: {l: 64, r: 16, t: 8, b: 40},
      height: 220,
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "#ffffff",
      xaxis: {title: {text: "Stored observation time"}},
      yaxis: {title: {text: "People"}, tickformat: ",.0f"}
    }, plotOpts);
  }

  function show(key) {
    var status = statusBy(key);
    buttons.forEach(function (button) {
      var selected = button.getAttribute("data-status-key") === key;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    document.querySelectorAll("[data-explorer-panel]").forEach(function (panel) {
      var keys = panel.getAttribute("data-explorer-panel").split(/\s+/);
      var visible = keys.indexOf(key) !== -1 || (status.verified && keys.indexOf("verified") !== -1);
      panel.hidden = !visible;
    });
    document.querySelectorAll("[data-explorer-metric]").forEach(function (slot) {
      slot.hidden = status.metric_ids.indexOf(slot.getAttribute("data-explorer-metric")) === -1;
    });
    document.querySelectorAll("[data-explorer-item]").forEach(function (item) {
      item.hidden = item.getAttribute("data-explorer-item") !== key;
    });
    var records = recordsFor(status);
    fillDetail(records);
    drawBars(records);
    drawHistory(records);
    drawSeries(status);
    if (chart && !chart.hidden && window.Plotly && Plotly.Plots) Plotly.Plots.resize(chart);
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      show(button.getAttribute("data-status-key"));
    });
  });
  show("all");
})();
"""


def explorer_script(document: dict[str, Any], json_text: str) -> str:
    """Self-contained presentation script. The payload replaces the marker."""
    if document.get("share_of_whole"):
        raise RuntimeError("share of whole is not enabled")
    if "__EXPLORER_JSON__" not in EXPLORER_SCRIPT:
        raise RuntimeError("explorer script marker missing")
    return EXPLORER_SCRIPT.replace("__EXPLORER_JSON__", json_text)
