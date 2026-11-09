"""Phase 3H-C: late dashboard capture and the observed Cancelled Policies identity.

Fakes only. No network. Invented series values are not screenshot evidence.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE
from history.source_series_store import (
    WRITE_ALREADY_RECORDED,
    WRITE_RECORDED,
    SourceSeriesPoint,
    SourceSeriesStore,
)
from history.store import SnapshotRejected, SnapshotStore
from main import begin_discovery, finalize_discovery
from metrics.catalog import CANCELLATION_CANDIDATES, METRICS, PROMOTION_CANDIDATES
from metrics.runtime_validation import RV_SERIES_EXTRACTED, RV_VALIDATION_FAILED
from metrics.source_series import (
    extract_source_points,
    may_persist_in_metric_snapshots,
    source_series_figure,
)
from reporting.html_report import render_html
from reporting.status_explorer import EXPLORER_SCRIPT
from sisense.discovery import DiscoveryObserver, widget_structure
from sisense.models import MetricResult
from sisense.payloads.currently_enrolled import build_currently_enrolled_query
from tests.discovery_fixtures import widget_payload, widget_request, cell
from tests.reporting_fixtures import T0
from tests.test_discovery import EXPECTED
from tests.test_phase3h import observe_all, runtime
from tests.test_phase3i import explorer_model
from tests.test_report_consistency import visible

PROJECT = Path(__file__).resolve().parent.parent
PRODUCTION_IDS = (
    "currently_enrolled_people", "percent_effectuated", "applications_started",
    "submitted_applications", "new_enrolled", "active_enrolled", "passive_enrolled",
)


def cancelled_body(level="days", column="last_update_timestamp", table="enrollment",
                   measures=None, datasource=None):
    return widget_payload(
        "w-cancelled-policies", "Policy status",
        measures=list(measures if measures is not None else ("Cancelled Policies",)),
        dimensions=[("Date", "datetime", level, table, column)],
        widget_type="chart/line",
        datasource=datasource,
    )


def cancelled_request(counts=(1201, 1302), dates=("2026-03-01", "2026-03-02"), measures=None, **kw):
    measures = list(measures or ("Cancelled Policies",))
    body = cancelled_body(measures=measures, **kw)
    rows = []
    for index, day in enumerate(dates):
        row = [cell(day, day)]
        if len(measures) == 1:
            row.append(cell(counts[index]))
        else:
            row.append(cell(4))
            row.append(cell(counts[index]))
        rows.append(row)
    return widget_request(body, rows)


class ObservedIdentityTests(unittest.TestCase):
    def test_cancelled_policies_exact_daily_identity(self):
        rows, _ = runtime(cancelled_request())
        cancelled = rows["cancelled_policies_daily"]
        self.assertEqual(cancelled.status, RV_SERIES_EXTRACTED)
        self.assertEqual(cancelled.bound_measure_title, "Cancelled Policies")
        self.assertEqual(cancelled.binding, "exact measure title")
        self.assertEqual(cancelled.date_table, "enrollment")
        self.assertEqual(cancelled.date_column, "last_update_timestamp")
        self.assertEqual(cancelled.time_grain, "days")
        self.assertEqual(cancelled.http_status, 200)
        self.assertEqual(cancelled.datasource, "MAIN")
        self.assertIsNone(cancelled.value)
        self.assertEqual(
            CANCELLATION_CANDIDATES["cancelled_policies_daily"].source_date_column,
            "last_update_timestamp",
        )

    def test_date_column_and_grain_are_required(self):
        wrong_column, _ = runtime(cancelled_request(column="updated_at"))
        self.assertEqual(wrong_column["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("observed source column", wrong_column["cancelled_policies_daily"].reason)
        wrong_grain, _ = runtime(cancelled_request(level="weeks"))
        self.assertEqual(wrong_grain["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("days", wrong_grain["cancelled_policies_daily"].reason)

    def test_no_positional_fallback_and_cancelled_does_not_imply_terminated(self):
        request = cancelled_request(measures=("Other Count", "Cancelled Policies"))
        rows, _ = runtime(request)
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)
        self.assertEqual(rows["cancelled_policies_daily"].bound_measure_title, "Cancelled Policies")
        points = extract_source_points(
            widget_structure(cancelled_body(measures=("Other Count", "Cancelled Policies"))),
            request._response._text, "Cancelled Policies",
        )
        self.assertEqual(points, [("2026-03-01", 1201), ("2026-03-02", 1302)])
        self.assertNotIn(4, [value for _, value in points])
        alone, _ = runtime(cancelled_request())
        self.assertEqual(alone["terminated_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIsNone(alone["terminated_policies_daily"].value)
        self.assertIsNone(alone["enrolled_cancelled"].value)
        self.assertIsNone(alone["cancelled_not_enrolled"].value)
        self.assertIsNone(CANCELLATION_CANDIDATES["terminated_policies_daily"].source_date_column)

    def test_daily_points_do_not_enter_metric_snapshots(self):
        self.assertFalse(may_persist_in_metric_snapshots("cancelled_policies_daily"))
        self.assertFalse(may_persist_in_metric_snapshots("terminated_policies_daily"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oe.sqlite3"
            snapshots = SnapshotStore(path, "hour", clock=lambda: T0)
            try:
                snapshots.start_run("r1")
                with self.assertRaises(SnapshotRejected):
                    snapshots.record_snapshot("r1", MetricResult(
                        metric="cancelled_policies_daily", plan_year=2027, value=1201,
                        retrieved_at=T0.isoformat(), source="Sisense MAIN", dashboard="dashboard",
                    ))
            finally:
                snapshots.close()
            series = SourceSeriesStore(path)
            try:
                first = series.record(
                    "cancelled_policies_daily", "2026-03-01", "days",
                    "enrollment.last_update_timestamp", 1201, "sha256:test", T0,
                )
                again = series.record(
                    "cancelled_policies_daily", "2026-03-01", "days",
                    "enrollment.last_update_timestamp", 9999, "sha256:test", T0,
                )
            finally:
                series.close()
            self.assertEqual(first.status, WRITE_RECORDED)
            self.assertEqual(again.status, WRITE_ALREADY_RECORDED)
            self.assertEqual(again.point.value, 1201)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_series_points").fetchone()[0], 1)
        figure = source_series_figure([("2026-03-01", 1201), ("2026-03-02", 1302)])
        self.assertEqual(figure["data"][0]["x"], ["2026-03-01", "2026-03-02"])
        self.assertEqual(figure["layout"]["xaxis"]["title"]["text"], "Source date")
        self.assertNotIn(T0.isoformat(), str(figure))


class CaptureWindowTests(unittest.TestCase):
    def test_late_dashboard_traffic_is_kept_until_the_session_ends(self):
        class Quiet:
            def wait_for_event(self, event, predicate=None, timeout=None):
                raise PlaywrightTimeoutError("quiet")

        observer = DiscoveryObserver(
            VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE["id"], ("Cancelled Policies",), EXPECTED.widget_id,
        )
        early = widget_request(widget_payload("w-early", "Early", measures=["People"]), [cell(1)])
        observer._on_request_finished(early)
        session = SimpleNamespace(context=Quiet())
        settings = SimpleNamespace(
            timeouts=SimpleNamespace(default=1000, very_slow=5000),
            reports_dir=None, runtime_validation=False,
        )
        begin_discovery(session, observer, settings)
        self.assertFalse(observer._closed)
        late = cancelled_request()
        self.assertEqual(late.response_calls, 0)
        observer._on_request_finished(late)
        self.assertEqual(late.response_calls, 0)
        self.assertIsNotNone(observer.entry("w-cancelled-policies"))
        outcome = finalize_discovery(session, observer, build_currently_enrolled_query(), settings)
        self.assertTrue(observer._closed)
        self.assertIsNotNone(outcome)
        identified = {item.widget_id for item in outcome.identification}
        self.assertIn("w-cancelled-policies", identified)
        after = widget_request(widget_payload(
            "w-after-stop", "Later", measures=["Cancelled Policies"],
            dimensions=[("Date", "datetime", "days", "enrollment", "last_update_timestamp")],
        ), [[cell("2026-03-03", "2026-03-03"), cell(1)]])
        observer._on_request_finished(after)
        self.assertIsNone(observer.entry("w-after-stop"))
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        begin = main_text.split("def begin_discovery", 1)[1].split("def finalize_discovery", 1)[0]
        finalize = main_text.split("def finalize_discovery", 1)[1].split("def capture_runtime_values", 1)[0]
        self.assertNotIn("observer.stop()", begin)
        self.assertIn("observer.stop()", finalize)
        self.assertEqual(main_text.count("retrieve_value("), 2)
        self.assertNotIn(".request.post", (PROJECT / "sisense/discovery.py").read_text(encoding="utf-8"))
        self.assertEqual((PROJECT / "sisense/client.py").read_text(encoding="utf-8").count(".request.post("), 1)

    def test_catalog_and_explorer_stay_closed(self):
        self.assertEqual(list(METRICS), list(PRODUCTION_IDS))
        self.assertEqual(list(PROMOTION_CANDIDATES), ["total_applicants"])
        self.assertNotIn("cancelled_policies_daily", METRICS)
        for marker in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "http://", "https://"):
            self.assertNotIn(marker, EXPLORER_SCRIPT)
        self.assertIn("return row.date", EXPLORER_SCRIPT)
        self.assertIn("Source date", EXPLORER_SCRIPT)
        html = render_html(explorer_model())
        activity = html.split('data-section="cancellation_activity"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Source verification pending", activity)
        self.assertNotIn("data-source-series", activity)
        self.assertNotRegex(activity, r">\s*0\s*<")
        point = SourceSeriesPoint(
            1, "cancelled_policies_daily", "2026-03-01", "days",
            "enrollment.last_update_timestamp", 1201, "sha256:test", T0,
        )
        from dataclasses import replace
        shown = visible(render_html(replace(explorer_model(), source_series=(point,))))
        self.assertIn("2026-03-01", shown)
        self.assertIn("1,201", shown)
        self.assertIn("Cancelled Policies — Daily", shown)
        self.assertNotIn("sha256:test", shown)
        self.assertNotIn("fingerprint", shown)
        self.assertNotIn("DUPLICATE_SLOT", shown)
        detail = shown.split('id="status-detail"', 1)[1].split("Cancellation Overview", 1)[0]
        self.assertIn("Observation timestamp", detail)
        self.assertIn("12,001", detail)
        self.assertIn("11,951", detail)
        self.assertIn(">SOURCE<", detail.replace(" ", ""))


class RequestCountTests(unittest.TestCase):
    def test_observer_reads_no_response_while_retaining(self):
        request = cancelled_request()
        observer = observe_all(request)
        self.assertEqual(request.response_calls, 0)
        self.assertIsNotNone(observer.entry("w-cancelled-policies"))
        self.assertFalse(hasattr(observer, "post"))
