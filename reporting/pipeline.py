"""Consumes the Phase 1 retrieval outcome: snapshot -> analytics -> HTML report."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from analytics.derived import MetricAnalytics
from history.store import RUN_FAILED, RUN_SUCCESS, SnapshotStore, SnapshotWriteResult, utc_now
from metrics.catalog import MetricDefinition
from reporting.html_report import write_report
from reporting.model import MetricPanel, RunOutcome, build_metric_panel, build_report_model
from sisense.models import MetricResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PassiveRunValue:
    """This run's outcome for one additional production metric: a validated result or a failure reason."""

    metric: MetricDefinition
    result: MetricResult | None
    failure: str | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.failure is None):
            raise ValueError("A passive run value is either a validated result or a failure")


@dataclass(frozen=True)
class ReportingSummary:
    data_status: str
    snapshot_write: SnapshotWriteResult | None
    stored_snapshot_count: int
    analytics: MetricAnalytics
    report_path: Path
    panels: tuple[MetricPanel, ...] = ()
    overall_data_status: str | None = None


class ReportingPipeline:
    def __init__(
        self,
        metric: MetricDefinition,
        history_db_path: Path,
        reports_dir: Path,
        granularity: str = "hour",
        clock: Callable[[], datetime] = utc_now,
    ):
        self._metric = metric
        self._history_db_path = Path(history_db_path)
        self._reports_dir = Path(reports_dir)
        self._granularity = granularity
        self._clock = clock

    def _store(self) -> SnapshotStore:
        return SnapshotStore(self._history_db_path, self._granularity, clock=self._clock)

    def begin_run(self, run_id: str) -> None:
        with self._store() as store:
            store.start_run(run_id)

    def complete_success(
        self, run_id: str, result: MetricResult, passive: Sequence[PassiveRunValue] = ()
    ) -> ReportingSummary:
        """Store validated live results (never overwriting) and regenerate the report."""
        with self._store() as store:
            write = store.record_snapshot(run_id, result)
            panels = tuple(self._panel(store, run_id, item) for item in passive)
            store.finish_run(run_id, RUN_SUCCESS)
            snapshots = store.snapshots(self._metric.metric_id, self._metric.plan_year)
        return self._report(RunOutcome(run_id, result, snapshot_write=write), snapshots, panels)

    def complete_failure(
        self, run_id: str, failure_type: str, passive_metrics: Sequence[MetricDefinition] = ()
    ) -> ReportingSummary:
        """Record the failed run (no snapshot) and regenerate the report from stored history."""
        with self._store() as store:
            store.finish_run(run_id, RUN_FAILED, failure_type)
            snapshots = store.snapshots(self._metric.metric_id, self._metric.plan_year)
            panels = tuple(
                build_metric_panel(m, store.snapshots(m.metric_id, m.plan_year), None, None,
                                   "Currently Enrolled control request failed")
                for m in passive_metrics
            )
        return self._report(RunOutcome(run_id, None, failure_type=failure_type), snapshots, panels)

    @staticmethod
    def _panel(store: SnapshotStore, run_id: str, item: PassiveRunValue) -> MetricPanel:
        metric = item.metric
        write, result, failure = None, item.result, item.failure
        if result is not None:
            try:
                write = store.record_snapshot(run_id, result)
            except Exception as exc:
                logger.error("Snapshot rejected for %s: %s", metric.metric_id, type(exc).__name__)
                result, failure = None, f"Snapshot rejected ({type(exc).__name__})"
        return build_metric_panel(metric, store.snapshots(metric.metric_id, metric.plan_year), result, write, failure)

    def _report(self, outcome: RunOutcome, snapshots, panels: tuple[MetricPanel, ...] = ()) -> ReportingSummary:
        model = build_report_model(self._metric, snapshots, outcome, generated_at=self._clock(), panels=panels)
        path = write_report(model, self._reports_dir)
        return ReportingSummary(
            data_status=model.data_status,
            snapshot_write=outcome.snapshot_write,
            stored_snapshot_count=model.analytics.stored_snapshot_count,
            analytics=model.analytics,
            report_path=path,
            panels=panels,
            overall_data_status=model.overall_data_status,
        )
