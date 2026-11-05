"""Phase 3H-G: retain every distinct widget definition, not only the latest request.

Fakes only. No network. Invented series values are not screenshot evidence.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from history.source_series_store import SourceSeriesStore
from history.store import SnapshotRejected, SnapshotStore
from metrics.catalog import METRICS, PROMOTION_CANDIDATES
from metrics.passive_metrics import PM_EXTRACTED, PassiveMetricOutcome, apply_control
from metrics.runtime_validation import (
    LIVE_MOVEMENT,
    RV_PENDING,
    RV_SERIES_EXTRACTED,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    ControlComparison,
)
from metrics.source_series import extract_source_points, may_persist_in_metric_snapshots, source_series_figure
from reporting.discovery_report import write_discovery_report
from sisense.discovery import evaluate_discovery, widget_structure
from sisense.models import MetricResult
from sisense.passive import definition_fingerprint
from tests.discovery_fixtures import FILTER_SENTINEL, cell, widget_payload, widget_request
from tests.reporting_fixtures import T0
from tests.test_discovery import EXPECTED
from tests.test_phase3h import observe_all, people_payload, runtime
from tests.test_phase3h_c import PRODUCTION_IDS, cancelled_request

PROJECT = Path(__file__).resolve().parent.parent
WIDGET = "w-cancelled-policies"
DATE = ("Date", "datetime", "days", "enrollment", "last_update_timestamp")


def _series(measure, *extra, level="days", column="last_update_timestamp", guid="g"):
    measures = (measure, *extra)
    body = widget_payload(
        WIDGET, "Policy status", measures=list(measures),
        dimensions=[("Date", "datetime", level, "enrollment", column)],
        widget_type="chart/line", query_guid=guid,
    )
    rows = []
    for index, day in enumerate(("2026-03-01", "2026-03-02")):
        row = [cell(day, day)]
        if len(measures) == 1:
            row.append(cell((1201, 1302)[index]))
        else:
            row.append(cell(4))
            row.append(cell((1201, 1302)[index]))
        rows.append(row)
    return widget_request(body, rows)


def _body(request):
    return json.loads(request.post_data)


def _unselected(guid="unselected"):
    return _series("Policies", guid=guid)


class RetentionTests(unittest.TestCase):
    def test_same_fingerprint_is_one_definition(self):
        first = _series("Cancelled Policies", guid="one")
        second = _series("Cancelled Policies", guid="two")
        self.assertEqual(definition_fingerprint(_body(first)), definition_fingerprint(_body(second)))
        observer = observe_all(first, second)
        (entry,) = observer.entries()
        self.assertEqual(len(entry.definitions), 1)
        definition = next(iter(entry.definitions.values()))
        self.assertEqual(definition.observations, 2)
        self.assertIs(definition.latest_request, second)

    def test_different_fingerprints_both_remain(self):
        observer = observe_all(_unselected(), _series("Cancelled Policies"))
        (entry,) = observer.entries()
        self.assertEqual(len(entry.definitions), 2)
        titles = {item.structure.measures[0].title for item in entry.definitions.values()}
        self.assertEqual(titles, {"Policies", "Cancelled Policies"})

    def test_return_to_unselected_keeps_the_selected_definition(self):
        selected = _series("Cancelled Policies", guid="selected")
        observer = observe_all(_unselected("a1"), selected, _unselected("a2"))
        (entry,) = observer.entries()
        self.assertEqual(len(entry.definitions), 2)
        self.assertEqual(entry.structure.measures[0].title, "Policies")
        by_title = {item.structure.measures[0].title: item for item in entry.definitions.values()}
        self.assertEqual(by_title["Policies"].observations, 2)
        self.assertIs(
            by_title["Policies"].latest_request,
            entry.definitions[definition_fingerprint(_body(_unselected("a2")))].latest_request,
        )
        self.assertEqual(by_title["Cancelled Policies"].observations, 1)
        self.assertIs(by_title["Cancelled Policies"].latest_request, selected)
        rows, _ = runtime(_unselected("a1"), selected, _unselected("a2"))
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)
        self.assertEqual(rows["cancelled_policies_daily"].bound_measure_title, "Cancelled Policies")
        self.assertEqual(rows["cancelled_policies_daily"].date_column, "last_update_timestamp")
        self.assertEqual(rows["cancelled_policies_daily"].time_grain, "days")

    def test_resolve_targets_sees_every_definition(self):
        rows, observer = runtime(_unselected(), _series("Cancelled Policies"), _unselected("again"))
        result = evaluate_discovery(observer, EXPECTED)
        self.assertEqual(result.widget_count, 1)
        self.assertEqual(result.definition_count, 2)
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)

    def test_terminated_is_independent(self):
        both, _ = runtime(_series("Cancelled Policies"), _series("Terminated Policies", guid="term"))
        self.assertEqual(both["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)
        self.assertEqual(both["terminated_policies_daily"].status, RV_SERIES_EXTRACTED)
        self.assertEqual(both["terminated_policies_daily"].bound_measure_title, "Terminated Policies")
        cancelled_only, _ = runtime(_series("Cancelled Policies"), _unselected())
        self.assertEqual(cancelled_only["terminated_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertNotEqual(cancelled_only["terminated_policies_daily"].status, RV_SERIES_EXTRACTED)

    def test_people_level_and_pending_stay_independent(self):
        rows, _ = runtime(
            _series("Cancelled Policies"), _unselected(),
            widget_request(people_payload(), [cell(1201), cell(1302)]),
        )
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALUE_EXTRACTED)
        self.assertEqual(rows["enrolled_cancelled"].bound_measure_title, "Enrolled & Cancelled")
        self.assertEqual(rows["cancelled_not_enrolled"].status, RV_VALUE_EXTRACTED)
        self.assertEqual(rows["cancelled_not_enrolled"].bound_measure_title, "Cancelled - Not Enrolled")
        alone, _ = runtime(_series("Cancelled Policies"))
        self.assertEqual(alone["enrolled_cancelled"].status, RV_VALIDATION_FAILED)
        self.assertEqual(alone["cancelled_not_enrolled"].status, RV_VALIDATION_FAILED)
        self.assertEqual(rows["cancelled"].status, RV_PENDING)
        self.assertEqual(rows["terminated"].status, RV_PENDING)
        self.assertIsNone(rows["cancelled"].value)
        self.assertIsNone(rows["terminated"].value)

    def test_daily_identity_column_grain_and_exact_title(self):
        wrong_column, _ = runtime(cancelled_request(column="updated_at"))
        self.assertEqual(wrong_column["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("date dimension", wrong_column["cancelled_policies_daily"].reason)
        wrong_grain, _ = runtime(cancelled_request(level="months"))
        self.assertEqual(wrong_grain["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("days", wrong_grain["cancelled_policies_daily"].reason)
        selected = _series("Other Count", "Cancelled Policies", guid="pos")
        rows, _ = runtime(_unselected(), selected, _unselected("after"))
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)
        self.assertEqual(rows["cancelled_policies_daily"].binding, "exact measure title")
        points = extract_source_points(
            widget_structure(_body(selected)), selected._response._text, "Cancelled Policies",
        )
        self.assertEqual(points, [("2026-03-01", 1201), ("2026-03-02", 1302)])
        self.assertNotIn(4, [value for _, value in points])
        figure = source_series_figure(points)
        self.assertEqual(figure["data"][0]["x"], ["2026-03-01", "2026-03-02"])
        self.assertNotIn(T0.isoformat(), str(figure))

    def test_daily_points_use_source_series_not_metric_snapshots(self):
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
                series.record(
                    "cancelled_policies_daily", "2026-03-01", "days",
                    "enrollment.last_update_timestamp", 1201, "sha256:test", T0,
                )
            finally:
                series.close()

    def test_report_shows_both_definitions_without_sensitive_fields(self):
        selected = _series("Cancelled Policies", guid="GUID-SECRET")
        observer = observe_all(_unselected("a"), selected, _unselected("b"))
        result = evaluate_discovery(observer, EXPECTED)
        from metrics.discovery import resolve_targets
        with tempfile.TemporaryDirectory() as tmp:
            path = write_discovery_report(
                resolve_targets(result), result, Path(tmp), T0,
            )
            html = path.read_text(encoding="utf-8")
            document = (path.parent / "wave1_metric_discovery.json").read_text(encoding="utf-8")
        self.assertIn("Cancelled Policies", html)
        self.assertIn("Policies", html)
        self.assertIn("2 distinct definitions", html)
        self.assertIn("last_update_timestamp", document)
        self.assertIn("sha256:", document)
        for secret in (FILTER_SENTINEL, "Count([", "GUID-SECRET", "queryGuid", "cookie", "xsrf", "authorization"):
            self.assertNotIn(secret.lower(), html.lower())
            self.assertNotIn(secret.lower(), document.lower())


class InvariantTests(unittest.TestCase):
    def test_catalog_control_and_request_count_stay_unchanged(self):
        self.assertEqual(tuple(METRICS), PRODUCTION_IDS)
        self.assertEqual(list(PROMOTION_CANDIDATES), ["total_applicants"])
        self.assertNotIn("cancelled_policies_daily", METRICS)
        moved = ControlComparison(12001, 12006, None)
        self.assertEqual(moved.status, LIVE_MOVEMENT)
        self.assertEqual(moved.authoritative_value, 12006)
        failed = apply_control(PassiveMetricOutcome("percent_effectuated", PM_EXTRACTED, 0.5, 12001), 12002)
        self.assertFalse(failed.succeeded)
        self.assertIn("same dashboard response", failed.failure)
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(main_text.count("retrieve_value("), 2)
        self.assertEqual((PROJECT / "sisense" / "client.py").read_text(encoding="utf-8").count(".request.post("), 1)
        discovery = (PROJECT / "sisense" / "discovery.py").read_text(encoding="utf-8")
        for forbidden in ("click(", "goto(", ".scroll", "mouse"):
            self.assertNotIn(forbidden, discovery)
        body = main_text.split("def main(", 1)[1].split("\ndef create_source_observer", 1)[0]
        self.assertLess(body.index("await_operator_observation("), body.index("retrieve_value("))

    def test_suite_makes_no_network_calls(self):
        forbidden = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({alias.name.split(".")[0] for alias in node.names} & forbidden)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden)
