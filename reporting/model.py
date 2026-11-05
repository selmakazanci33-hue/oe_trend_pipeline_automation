"""Executive reporting model: business values only, with explicit SOURCE/DERIVED labels.

Status rules:
- LIVE: this run retrieved and validated a live value.
- STALE: this run did not produce a live value, but stored snapshots exist;
  the KPI shows the latest stored snapshot, labelled as historical.
- NO HISTORY: no live value and no stored snapshots.
Additional production metrics (panels) carry the same rules per metric, with
NOT AVAILABLE in place of NO HISTORY.
Pipeline status is PASS only when this run's retrieval succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from analytics.derived import DataPoint, MetricAnalytics, compute_analytics
from history.store import Snapshot, SnapshotWriteResult, parse_utc
from metrics.catalog import MODULES, ROADMAP, MetricDefinition, Module, RoadmapItem
from sisense.models import MetricResult

DATA_LIVE = "LIVE"
DATA_STALE = "STALE"
DATA_NO_HISTORY = "NO HISTORY"
DATA_NOT_AVAILABLE = "NOT AVAILABLE"
PIPELINE_PASS = "PASS"
PIPELINE_ERROR = "ERROR"


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    live_result: MetricResult | None
    failure_type: str | None = None
    snapshot_write: SnapshotWriteResult | None = None

    def __post_init__(self) -> None:
        if (self.live_result is None) == (self.failure_type is None):
            raise ValueError("A run outcome is either a live result or a failure, not both or neither")

    @property
    def succeeded(self) -> bool:
        return self.live_result is not None


@dataclass(frozen=True)
class ReportModel:
    metric: MetricDefinition
    analytics: MetricAnalytics
    data_status: str
    pipeline_status: str
    retrieval_status: str
    failure_type: str | None
    snapshot_write_status: str | None
    last_successful_refresh: datetime | None
    generated_at: datetime
    modules: tuple[Module, ...]
    roadmap: tuple[RoadmapItem, ...]
    panels: tuple["MetricPanel", ...] = ()

    @property
    def kpi(self) -> DataPoint | None:
        return self.analytics.latest

    def panels_for(self, module_id: str) -> tuple["MetricPanel", ...]:
        return tuple(p for p in self.panels if p.metric.module_id == module_id)


@dataclass(frozen=True)
class MetricPanel:
    """One additional production metric with its own status (LIVE / STALE / NOT AVAILABLE)."""

    metric: MetricDefinition
    analytics: MetricAnalytics
    data_status: str
    snapshot_write_status: str | None
    failure: str | None

    @property
    def latest(self) -> DataPoint | None:
        return self.analytics.latest


def build_metric_panel(
    metric: MetricDefinition,
    snapshots: list[Snapshot],
    live_result: MetricResult | None,
    snapshot_write: SnapshotWriteResult | None,
    failure: str | None,
) -> MetricPanel:
    """LIVE only for a value validated in this run; otherwise the last stored snapshot (STALE) or nothing."""
    current = None
    if live_result is not None:
        stored_id = snapshot_write.snapshot.id if snapshot_write is not None and snapshot_write.saved else None
        current = DataPoint(live_result.value, parse_utc(live_result.retrieved_at), stored=stored_id is not None,
                            snapshot_id=stored_id)
    analytics = compute_analytics(metric.metric_id, snapshots, current)
    if current is not None:
        status = DATA_LIVE
    elif analytics.stored_snapshot_count:
        status = DATA_STALE
    else:
        status = DATA_NOT_AVAILABLE
    return MetricPanel(metric, analytics, status, snapshot_write.status if snapshot_write else None,
                       None if current is not None else failure)


def build_report_model(
    metric: MetricDefinition,
    snapshots: list[Snapshot],
    outcome: RunOutcome,
    generated_at: datetime,
    panels: tuple[MetricPanel, ...] = (),
) -> ReportModel:
    current: DataPoint | None = None
    if outcome.succeeded:
        result = outcome.live_result
        write = outcome.snapshot_write
        stored_id = write.snapshot.id if write is not None and write.saved else None
        current = DataPoint(
            value=result.value,
            observed_at=parse_utc(result.retrieved_at),
            stored=stored_id is not None,
            snapshot_id=stored_id,
        )

    analytics = compute_analytics(metric.metric_id, snapshots, current)

    if outcome.succeeded:
        data_status = DATA_LIVE
    elif analytics.stored_snapshot_count:
        data_status = DATA_STALE
    else:
        data_status = DATA_NO_HISTORY

    refresh_times = [s.retrieved_at_utc for s in snapshots]
    if current is not None:
        refresh_times.append(current.observed_at)

    return ReportModel(
        metric=metric,
        analytics=analytics,
        data_status=data_status,
        pipeline_status=PIPELINE_PASS if outcome.succeeded else PIPELINE_ERROR,
        retrieval_status=PIPELINE_PASS if outcome.succeeded else PIPELINE_ERROR,
        failure_type=outcome.failure_type,
        snapshot_write_status=outcome.snapshot_write.status if outcome.snapshot_write else None,
        last_successful_refresh=max(refresh_times) if refresh_times else None,
        generated_at=generated_at,
        modules=MODULES,
        roadmap=ROADMAP,
        panels=tuple(panels),
    )
