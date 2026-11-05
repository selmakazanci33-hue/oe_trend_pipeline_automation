"""Consumes the Phase 1 retrieval outcome: snapshot -> analytics -> HTML report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from analytics.derived import MetricAnalytics
from history.store import RUN_FAILED, RUN_SUCCESS, SnapshotStore, SnapshotWriteResult, utc_now
from metrics.catalog import MetricDefinition
from reporting.html_report import write_report
from reporting.model import RunOutcome, build_report_model
from sisense.models import MetricResult


@dataclass(frozen=True)
class ReportingSummary:
    data_status: str
    snapshot_write: SnapshotWriteResult | None
    stored_snapshot_count: int
    analytics: MetricAnalytics
    report_path: Path


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

    def complete_success(self, run_id: str, result: MetricResult) -> ReportingSummary:
        """Store the validated live result (never overwriting) and regenerate the report."""
        with self._store() as store:
            write = store.record_snapshot(run_id, result)
            store.finish_run(run_id, RUN_SUCCESS)
            snapshots = store.snapshots(self._metric.metric_id, self._metric.plan_year)
        return self._report(RunOutcome(run_id, result, snapshot_write=write), snapshots)

    def complete_failure(self, run_id: str, failure_type: str) -> ReportingSummary:
        """Record the failed run (no snapshot) and regenerate the report from stored history."""
        with self._store() as store:
            store.finish_run(run_id, RUN_FAILED, failure_type)
            snapshots = store.snapshots(self._metric.metric_id, self._metric.plan_year)
        return self._report(RunOutcome(run_id, None, failure_type=failure_type), snapshots)

    def _report(self, outcome: RunOutcome, snapshots) -> ReportingSummary:
        model = build_report_model(self._metric, snapshots, outcome, generated_at=self._clock())
        path = write_report(model, self._reports_dir)
        return ReportingSummary(
            data_status=model.data_status,
            snapshot_write=outcome.snapshot_write,
            stored_snapshot_count=model.analytics.stored_snapshot_count,
            analytics=model.analytics,
            report_path=path,
        )
