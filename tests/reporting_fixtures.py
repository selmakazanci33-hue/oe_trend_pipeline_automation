from datetime import datetime, timedelta, timezone

from config.settings import SOURCE_LABEL, VERIFIED_DASHBOARD_TITLE
from history.store import Snapshot, snapshot_slot
from sisense.models import MetricResult

METRIC_ID = "currently_enrolled_people"
T0 = datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)


def result(value=812_000, at=T0, metric=METRIC_ID, plan_year=2027):
    return MetricResult(
        metric=metric,
        plan_year=plan_year,
        value=value,
        retrieved_at=at.isoformat(timespec="seconds"),
        source=SOURCE_LABEL,
        dashboard=VERIFIED_DASHBOARD_TITLE,
    )


def snapshot(value, at, id_=None):
    return Snapshot(
        id=id_ if id_ is not None else int(at.timestamp()),
        run_id=f"run-{at.timestamp()}",
        metric_id=METRIC_ID,
        metric_value=value,
        plan_year=2027,
        retrieved_at_utc=at,
        snapshot_slot_utc=snapshot_slot(at, "hour"),
        source=SOURCE_LABEL,
        dashboard=VERIFIED_DASHBOARD_TITLE,
        retrieval_status="VALIDATED",
        created_at_utc=at,
    )


def series(*pairs):
    """pairs of (hours_before_T0, value) -> snapshots in time order."""
    return [snapshot(value, T0 - timedelta(hours=h), id_=i + 1) for i, (h, value) in enumerate(sorted(pairs, reverse=True))]
