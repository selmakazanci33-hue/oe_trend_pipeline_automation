"""Derived analytics computed only from actual observed values.

SOURCE values come from validated Sisense retrievals (stored snapshots, plus
the current run's live value). DERIVED values are calculated from them.
SNAPSHOT is stored observation metadata (for example the latest snapshot
timestamp), not a calculated business value. When history is insufficient a
DerivedValue carries ``value=None`` and a reason; it is never zero-filled and
never substituted with a manual baseline.

Window comparisons (daily / 7-day / 30-day) use the most recent earlier point
taken at or before ``latest - window`` and no older than ``latest - window -
tolerance``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from history.store import Snapshot

SOURCE = "SOURCE"
DERIVED = "DERIVED"
SNAPSHOT = "SNAPSHOT"
INSUFFICIENT_HISTORY = "Insufficient history"


@dataclass(frozen=True)
class DataPoint:
    value: int | float
    observed_at: datetime
    stored: bool
    snapshot_id: int | None = None
    kind: str = SOURCE


@dataclass(frozen=True)
class DerivedValue:
    key: str
    label: str
    value: int | float | datetime | None
    unit: str
    reason: str | None = None
    reference: DataPoint | None = None
    kind: str = DERIVED

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Comparison:
    key: str
    label: str
    change: DerivedValue
    change_pct: DerivedValue

    @property
    def available(self) -> bool:
        return self.change.available


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    span: timedelta
    tolerance: timedelta


WINDOWS = (
    Window("daily", "Daily Change", timedelta(days=1), timedelta(hours=12)),
    Window("7d", "7-Day Change", timedelta(days=7), timedelta(days=1)),
    Window("30d", "30-Day Change", timedelta(days=30), timedelta(days=2)),
)


@dataclass(frozen=True)
class MetricAnalytics:
    metric_id: str
    latest: DataPoint | None
    points: tuple[DataPoint, ...]
    stored_snapshot_count: int
    previous: Comparison
    windows: tuple[Comparison, ...]
    historical_min: DerivedValue
    historical_max: DerivedValue
    average_daily_change: DerivedValue
    latest_snapshot_time: DerivedValue

    @property
    def comparisons(self) -> tuple[Comparison, ...]:
        return (self.previous, *self.windows)

    def comparison(self, key: str) -> Comparison:
        for comparison in self.comparisons:
            if comparison.key == key:
                return comparison
        raise KeyError(key)


def points_from_snapshots(snapshots: Iterable[Snapshot]) -> list[DataPoint]:
    return sorted(
        (DataPoint(s.metric_value, s.retrieved_at_utc, stored=True, snapshot_id=s.id) for s in snapshots),
        key=lambda p: (p.observed_at, p.snapshot_id or 0),
    )


def _unavailable(key: str, label: str, unit: str, reason: str, kind: str = DERIVED) -> DerivedValue:
    return DerivedValue(key, label, None, unit, reason=reason, kind=kind)


def _comparison(
    key: str, label: str, latest: DataPoint | None, reference: DataPoint | None, reason: str
) -> Comparison:
    if latest is None or reference is None:
        return Comparison(
            key, label,
            _unavailable(f"{key}_change", label, "people", reason),
            _unavailable(f"{key}_change_pct", f"{label} %", "percent", reason),
        )
    change = latest.value - reference.value
    change_value = DerivedValue(f"{key}_change", label, change, "people", reference=reference)
    if reference.value == 0:
        pct = _unavailable(f"{key}_change_pct", f"{label} %", "percent", "Undefined (reference value is zero)")
    else:
        pct = DerivedValue(
            f"{key}_change_pct", f"{label} %", change / reference.value * 100, "percent", reference=reference
        )
    return Comparison(key, label, change_value, pct)


def _window_reference(latest: DataPoint, earlier: list[DataPoint], window: Window) -> DataPoint | None:
    target = latest.observed_at - window.span
    floor = target - window.tolerance
    candidates = [p for p in earlier if floor <= p.observed_at <= target]
    return candidates[-1] if candidates else None


def compute_analytics(metric_id: str, snapshots: Iterable[Snapshot], current: DataPoint | None = None) -> MetricAnalytics:
    """``current`` is this run's live value when it was not stored (e.g. duplicate slot)."""
    stored = points_from_snapshots(snapshots)
    points = list(stored)
    if current is not None and not current.stored:
        points.append(current)
        points.sort(key=lambda p: (p.observed_at, p.snapshot_id or 0))

    latest = current if current is not None else (stored[-1] if stored else None)
    latest_snapshot_time = (
        DerivedValue("latest_snapshot_time", "Latest Snapshot Time", stored[-1].observed_at, "timestamp",
                     reference=stored[-1], kind=SNAPSHOT)
        if stored else _unavailable("latest_snapshot_time", "Latest Snapshot Time", "timestamp", "No snapshots",
                                    SNAPSHOT)
    )

    if latest is None:
        empty = "No data"
        return MetricAnalytics(
            metric_id=metric_id,
            latest=None,
            points=tuple(points),
            stored_snapshot_count=len(stored),
            previous=_comparison("previous", "Previous Snapshot Change", None, None, empty),
            windows=tuple(_comparison(w.key, w.label, None, None, empty) for w in WINDOWS),
            historical_min=_unavailable("historical_min", "Historical Minimum", "people", empty),
            historical_max=_unavailable("historical_max", "Historical Maximum", "people", empty),
            average_daily_change=_unavailable("average_daily_change", "Average Daily Change", "people_per_day", empty),
            latest_snapshot_time=latest_snapshot_time,
        )

    earlier = [
        p for p in points
        if p.observed_at < latest.observed_at and not (p.stored and p.snapshot_id == latest.snapshot_id)
    ]

    previous = _comparison(
        "previous", "Previous Snapshot Change", latest, earlier[-1] if earlier else None, INSUFFICIENT_HISTORY
    )
    windows = tuple(
        _comparison(w.key, w.label, latest, _window_reference(latest, earlier, w), INSUFFICIENT_HISTORY)
        for w in WINDOWS
    )

    if len(points) >= 2:
        low = min(points, key=lambda p: p.value)
        high = max(points, key=lambda p: p.value)
        historical_min = DerivedValue("historical_min", "Historical Minimum", low.value, "people", reference=low)
        historical_max = DerivedValue("historical_max", "Historical Maximum", high.value, "people", reference=high)
    else:
        historical_min = _unavailable("historical_min", "Historical Minimum", "people", INSUFFICIENT_HISTORY)
        historical_max = _unavailable("historical_max", "Historical Maximum", "people", INSUFFICIENT_HISTORY)

    first = points[0]
    span_days = (latest.observed_at - first.observed_at).total_seconds() / 86400
    if span_days >= 1:
        average_daily_change = DerivedValue(
            "average_daily_change", "Average Daily Change",
            (latest.value - first.value) / span_days, "people_per_day", reference=first,
        )
    else:
        average_daily_change = _unavailable(
            "average_daily_change", "Average Daily Change", "people_per_day", INSUFFICIENT_HISTORY
        )

    return MetricAnalytics(
        metric_id=metric_id,
        latest=latest,
        points=tuple(points),
        stored_snapshot_count=len(stored),
        previous=previous,
        windows=windows,
        historical_min=historical_min,
        historical_max=historical_max,
        average_daily_change=average_daily_change,
        latest_snapshot_time=latest_snapshot_time,
    )
