"""SQLite snapshot history for validated metric values.

Idempotency:
- ``run_id`` identifies one execution; a run can store at most one snapshot per metric.
- ``snapshot_slot_utc`` buckets snapshots by granularity (UTC hour by default, or
  UTC day). The first validated value in a slot is kept; later runs in the same
  slot are reported as DUPLICATE_SLOT and never overwrite it.

Only business values are stored: no credentials, cookies, headers, tokens or
JAQL payloads.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from metrics.catalog import STATUS_ACTIVE, get_metric
from sisense.models import MetricResult

SNAPSHOT_GRANULARITIES = {
    "hour": "%Y-%m-%dT%H:00:00Z",
    "day": "%Y-%m-%dT00:00:00Z",
}
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

RUN_RUNNING = "RUNNING"
RUN_SUCCESS = "SUCCESS"
RUN_FAILED = "FAILED"

WRITE_SAVED = "SAVED"
WRITE_DUPLICATE_SLOT = "DUPLICATE_SLOT"
WRITE_DUPLICATE_RUN = "DUPLICATE_RUN"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    started_at_utc  TEXT NOT NULL,
    finished_at_utc TEXT,
    status          TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    failure_type    TEXT
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT    NOT NULL REFERENCES pipeline_runs (run_id),
    metric_id         TEXT    NOT NULL,
    metric_value      NUMERIC NOT NULL,
    plan_year         INTEGER NOT NULL,
    retrieved_at_utc  TEXT    NOT NULL,
    snapshot_slot_utc TEXT    NOT NULL,
    source            TEXT    NOT NULL,
    dashboard         TEXT    NOT NULL,
    retrieval_status  TEXT    NOT NULL CHECK (retrieval_status = 'VALIDATED'),
    created_at_utc    TEXT    NOT NULL,
    UNIQUE (run_id, metric_id),
    UNIQUE (metric_id, plan_year, snapshot_slot_utc)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_metric_time
    ON metric_snapshots (metric_id, plan_year, retrieved_at_utc);
CREATE INDEX IF NOT EXISTS idx_runs_started
    ON pipeline_runs (started_at_utc);
"""


class SnapshotRejected(ValueError):
    """The value is not a validated, catalogued, storable metric result."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_utc(text: str) -> datetime:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"Timestamp {text!r} has no timezone")
    return moment.astimezone(timezone.utc)


def snapshot_slot(moment: datetime, granularity: str) -> str:
    try:
        pattern = SNAPSHOT_GRANULARITIES[granularity]
    except KeyError:
        raise ValueError(f"Unsupported snapshot granularity {granularity!r}") from None
    return moment.astimezone(timezone.utc).strftime(pattern)


@dataclass(frozen=True)
class Snapshot:
    id: int
    run_id: str
    metric_id: str
    metric_value: int | float
    plan_year: int
    retrieved_at_utc: datetime
    snapshot_slot_utc: str
    source: str
    dashboard: str
    retrieval_status: str
    created_at_utc: datetime


@dataclass(frozen=True)
class SnapshotWriteResult:
    status: str
    snapshot: Snapshot

    @property
    def saved(self) -> bool:
        return self.status == WRITE_SAVED


@dataclass(frozen=True)
class PipelineRun:
    run_id: str
    started_at_utc: datetime
    finished_at_utc: datetime | None
    status: str
    failure_type: str | None


def _number(value: object) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value  # type: ignore[return-value]


class SnapshotStore:
    def __init__(self, path: Path, granularity: str = "hour", clock: Callable[[], datetime] = utc_now):
        if granularity not in SNAPSHOT_GRANULARITIES:
            raise ValueError(f"Unsupported snapshot granularity {granularity!r}")
        self.path = Path(path)
        self.granularity = granularity
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # Runs -----------------------------------------------------------------

    def start_run(self, run_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO pipeline_runs (run_id, started_at_utc, status) VALUES (?, ?, ?)",
                (run_id, format_utc(self._clock()), RUN_RUNNING),
            )

    def finish_run(self, run_id: str, status: str, failure_type: str | None = None) -> None:
        if status not in (RUN_SUCCESS, RUN_FAILED):
            raise ValueError(f"Invalid final run status {status!r}")
        with self._conn:
            updated = self._conn.execute(
                "UPDATE pipeline_runs SET status = ?, finished_at_utc = ?, failure_type = ? "
                "WHERE run_id = ? AND status = ?",
                (status, format_utc(self._clock()), failure_type, run_id, RUN_RUNNING),
            ).rowcount
        if updated != 1:
            raise ValueError(f"Run {run_id!r} is not running")

    def get_run(self, run_id: str) -> PipelineRun | None:
        row = self._conn.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return PipelineRun(
            run_id=row["run_id"],
            started_at_utc=parse_utc(row["started_at_utc"]),
            finished_at_utc=parse_utc(row["finished_at_utc"]) if row["finished_at_utc"] else None,
            status=row["status"],
            failure_type=row["failure_type"],
        )

    # Snapshots --------------------------------------------------------------

    def record_snapshot(self, run_id: str, result: MetricResult) -> SnapshotWriteResult:
        """Store a validated live result; never overwrites an existing snapshot."""
        metric = self._validate(result)
        run = self.get_run(run_id)
        if run is None or run.status != RUN_RUNNING:
            raise SnapshotRejected(f"Run {run_id!r} is not an active run")

        retrieved_at = parse_utc(result.retrieved_at)
        slot = snapshot_slot(retrieved_at, self.granularity)

        existing = self._find("run_id = ? AND metric_id = ?", (run_id, metric.metric_id))
        if existing is not None:
            return SnapshotWriteResult(WRITE_DUPLICATE_RUN, existing)
        existing = self._find(
            "metric_id = ? AND plan_year = ? AND snapshot_slot_utc = ?",
            (metric.metric_id, result.plan_year, slot),
        )
        if existing is not None:
            return SnapshotWriteResult(WRITE_DUPLICATE_SLOT, existing)

        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO metric_snapshots (run_id, metric_id, metric_value, plan_year, retrieved_at_utc, "
                "snapshot_slot_utc, source, dashboard, retrieval_status, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'VALIDATED', ?)",
                (
                    run_id, metric.metric_id, result.value, result.plan_year, format_utc(retrieved_at),
                    slot, result.source, result.dashboard, format_utc(self._clock()),
                ),
            )
        snapshot = self._find("id = ?", (cursor.lastrowid,))
        assert snapshot is not None
        return SnapshotWriteResult(WRITE_SAVED, snapshot)

    def snapshots(self, metric_id: str, plan_year: int) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM metric_snapshots WHERE metric_id = ? AND plan_year = ? "
            "ORDER BY retrieved_at_utc, id",
            (metric_id, plan_year),
        ).fetchall()
        return [self._snapshot(row) for row in rows]

    def snapshot_count(self, metric_id: str, plan_year: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM metric_snapshots WHERE metric_id = ? AND plan_year = ?",
            (metric_id, plan_year),
        ).fetchone()[0]

    def _validate(self, result: MetricResult):
        if not isinstance(result, MetricResult):
            raise SnapshotRejected("Only validated MetricResult objects can be stored")
        try:
            metric = get_metric(result.metric)
        except KeyError as exc:
            raise SnapshotRejected(str(exc)) from None
        if metric.status != STATUS_ACTIVE or not metric.history_enabled:
            raise SnapshotRejected(f"Metric {metric.metric_id!r} is not enabled for history")
        if result.plan_year != metric.plan_year:
            raise SnapshotRejected(f"Plan year {result.plan_year} does not match catalog {metric.plan_year}")
        value = result.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise SnapshotRejected("Metric value must be a finite, non-negative number")
        if metric.value_format == "integer" and not isinstance(value, int):
            raise SnapshotRejected("Integer metric received a non-integer value")
        try:
            parse_utc(result.retrieved_at)
        except (TypeError, ValueError) as exc:
            raise SnapshotRejected(f"Invalid retrieved_at: {exc}") from None
        return metric

    def _find(self, where: str, params: tuple) -> Snapshot | None:
        row = self._conn.execute(f"SELECT * FROM metric_snapshots WHERE {where}", params).fetchone()
        return self._snapshot(row) if row is not None else None

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            id=row["id"],
            run_id=row["run_id"],
            metric_id=row["metric_id"],
            metric_value=_number(row["metric_value"]),
            plan_year=row["plan_year"],
            retrieved_at_utc=parse_utc(row["retrieved_at_utc"]),
            snapshot_slot_utc=row["snapshot_slot_utc"],
            source=row["source"],
            dashboard=row["dashboard"],
            retrieval_status=row["retrieval_status"],
            created_at_utc=parse_utc(row["created_at_utc"]),
        )
