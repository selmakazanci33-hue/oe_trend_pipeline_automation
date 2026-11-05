"""Dedicated store for verified Sisense SOURCE time series.

Points are keyed by the source date (the business date dimension), not by
pipeline retrieval time and not by the hourly snapshot slot. This module does
not write ``metric_snapshots`` and does not change first-write-per-hour retention.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from history.store import format_utc, parse_utc, utc_now
from metrics.source_series import DAILY_SOURCE_METRIC_IDS

WRITE_RECORDED = "RECORDED"
WRITE_ALREADY_RECORDED = "ALREADY_RECORDED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_series_points (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id               TEXT    NOT NULL,
    source_date             TEXT    NOT NULL,
    time_grain              TEXT    NOT NULL,
    date_semantics          TEXT    NOT NULL,
    value                   NUMERIC NOT NULL,
    definition_fingerprint  TEXT    NOT NULL,
    observed_at_utc         TEXT    NOT NULL,
    UNIQUE (metric_id, source_date, time_grain, date_semantics, definition_fingerprint)
);
"""


class SourceSeriesRejected(ValueError):
    """The point is not a daily SOURCE series observation."""


@dataclass(frozen=True)
class SourceSeriesPoint:
    id: int
    metric_id: str
    source_date: str
    time_grain: str
    date_semantics: str
    value: int
    definition_fingerprint: str
    observed_at_utc: datetime


@dataclass(frozen=True)
class SourceSeriesWrite:
    status: str
    point: SourceSeriesPoint


class SourceSeriesStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def record(
        self, metric_id: str, source_date: str, time_grain: str, date_semantics: str,
        value: int, definition_fingerprint: str, observed_at: datetime | None = None,
    ) -> SourceSeriesWrite:
        if metric_id not in DAILY_SOURCE_METRIC_IDS:
            raise SourceSeriesRejected(f"{metric_id} is not a daily SOURCE series")
        if time_grain != "days":
            raise SourceSeriesRejected("Daily SOURCE series requires time grain 'days'")
        if not source_date or not date_semantics or not definition_fingerprint:
            raise SourceSeriesRejected("Source date, date semantics, and definition fingerprint are required")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SourceSeriesRejected("Series value must be a nonnegative integer")
        observed = observed_at or utc_now()
        existing = self._find(metric_id, source_date, time_grain, date_semantics, definition_fingerprint)
        if existing is not None:
            return SourceSeriesWrite(WRITE_ALREADY_RECORDED, existing)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO source_series_points (metric_id, source_date, time_grain, date_semantics, value, "
                "definition_fingerprint, observed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (metric_id, source_date, time_grain, date_semantics, value, definition_fingerprint,
                 format_utc(observed)),
            )
        point = self._find_id(cursor.lastrowid)
        assert point is not None
        return SourceSeriesWrite(WRITE_RECORDED, point)

    def points(self, metric_id: str) -> list[SourceSeriesPoint]:
        rows = self._conn.execute(
            "SELECT * FROM source_series_points WHERE metric_id = ? ORDER BY source_date, id",
            (metric_id,),
        ).fetchall()
        return [self._point(row) for row in rows]

    def _find(self, metric_id, source_date, time_grain, date_semantics, fingerprint) -> SourceSeriesPoint | None:
        row = self._conn.execute(
            "SELECT * FROM source_series_points WHERE metric_id = ? AND source_date = ? AND time_grain = ? "
            "AND date_semantics = ? AND definition_fingerprint = ?",
            (metric_id, source_date, time_grain, date_semantics, fingerprint),
        ).fetchone()
        return None if row is None else self._point(row)

    def _find_id(self, row_id) -> SourceSeriesPoint | None:
        row = self._conn.execute("SELECT * FROM source_series_points WHERE id = ?", (row_id,)).fetchone()
        return None if row is None else self._point(row)

    def _point(self, row) -> SourceSeriesPoint:
        return SourceSeriesPoint(
            id=row["id"], metric_id=row["metric_id"], source_date=row["source_date"],
            time_grain=row["time_grain"], date_semantics=row["date_semantics"], value=int(row["value"]),
            definition_fingerprint=row["definition_fingerprint"],
            observed_at_utc=parse_utc(row["observed_at_utc"]),
        )
