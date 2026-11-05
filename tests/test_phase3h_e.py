"""Phase 3H-E: operator observation window and independently timed control semantics.

Fakes only. No network. Invented values are not screenshot evidence.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import main as app
from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE
from main import await_operator_observation, begin_discovery, finalize_discovery
from metrics.catalog import METRICS, PROMOTION_CANDIDATES
from metrics.passive_metrics import PM_EXTRACTED, PassiveMetricOutcome, apply_control
from metrics.runtime_validation import (
    EXACT_VALUE_MATCH,
    IDENTITY_FAIL,
    IDENTITY_PASS,
    LIVE_MOVEMENT,
    RV_PENDING,
    RV_SERIES_EXTRACTED,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    SOURCE_UNAVAILABLE,
    ControlComparison,
)
from reporting.status_explorer import EXPLORER_STATUSES, PENDING_SOURCE
from sisense.discovery import DiscoveryObserver
from sisense.payloads.currently_enrolled import build_currently_enrolled_query
from tests.discovery_fixtures import cell, widget_payload, widget_request
from tests.test_discovery import EXPECTED
from tests.test_phase3h import runtime
from tests.test_phase3h_c import PRODUCTION_IDS, cancelled_request
from tests.test_pipeline_main import PipelineHarness, ok_body
from tests.test_session_context import FakeResponse

PROJECT = Path(__file__).resolve().parent.parent


class Quiet:
    def wait_for_event(self, event, predicate=None, timeout=None):
        raise PlaywrightTimeoutError("quiet")


def _settings(runtime_validation=True):
    return SimpleNamespace(
        timeouts=SimpleNamespace(default=1000, very_slow=5000),
        reports_dir=None,
        runtime_validation=runtime_validation,
    )


def _observer():
    return DiscoveryObserver(
        VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE["id"], ("Cancelled Policies",), EXPECTED.widget_id,
    )


class OperatorWindowTests(unittest.TestCase):
    def test_runtime_validation_enters_the_window_after_the_initial_quiet_load(self):
        observer = _observer()
        observer._on_request_finished(widget_request(
            widget_payload("w-early", "Early", measures=["People"]), [cell(1)],
        ))
        session = SimpleNamespace(context=Quiet())
        begin_discovery(session, observer, _settings())
        self.assertFalse(observer._closed)
        seen = []

        def complete():
            seen.append(observer._closed)
            return ""

        with mock.patch.object(app, "read_operator_completion", complete):
            await_operator_observation(session, observer, _settings())
        self.assertEqual(seen, [False])
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        body = main_text.split("def main(", 1)[1].split("\ndef create_source_observer", 1)[0]
        self.assertLess(body.index("begin_discovery("), body.index("await_operator_observation("))
        self.assertLess(body.index("await_operator_observation("), body.index("retrieve_value("))
        for line in app.OBSERVATION_WINDOW_LINES:
            self.assertIn(line, main_text)

    def test_observer_stays_attached_while_waiting_for_the_operator(self):
        observer = _observer()
        session = SimpleNamespace(context=Quiet())

        def complete():
            self.assertFalse(observer._closed)
            observer._on_request_finished(cancelled_request())
            return ""

        with mock.patch.object(app, "read_operator_completion", complete):
            await_operator_observation(session, observer, _settings())
        self.assertFalse(observer._closed)
        self.assertIsNotNone(observer.entry("w-cancelled-policies"))

    def test_operator_completion_freezes_discovery_before_browser_close(self):
        observer = _observer()
        session = SimpleNamespace(context=Quiet(), closed=False)

        def close():
            self.assertTrue(observer._closed)
            session.closed = True

        session.close = close
        with mock.patch.object(app, "read_operator_completion", return_value=""):
            await_operator_observation(session, observer, _settings())
        self.assertFalse(observer._closed)
        self.assertFalse(session.closed)
        finalize_discovery(session, observer, build_currently_enrolled_query(), _settings())
        session.close()
        self.assertTrue(observer._closed)
        self.assertTrue(session.closed)
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        body = main_text.split("def main(", 1)[1].split("\ndef create_source_observer", 1)[0]
        self.assertLess(body.index("finalize_discovery("), body.index("session.close()"))
        self.assertLess(body.index("session.close()"), body.index("complete_success("))

    def test_naturally_observed_late_jaql_is_in_the_final_inventory(self):
        observer = _observer()
        session = SimpleNamespace(context=Quiet())
        begin_discovery(session, observer, _settings())
        opening = observer.target_requests_seen

        def complete():
            observer._on_request_finished(widget_request(
                widget_payload("w-late", "Later", measures=["People"]), [cell(2)],
            ))
            return ""

        with mock.patch.object(app, "read_operator_completion", complete):
            await_operator_observation(session, observer, _settings())
        outcome = finalize_discovery(session, observer, build_currently_enrolled_query(), _settings())
        self.assertIsNotNone(outcome)
        self.assertGreater(observer.target_requests_seen, opening)
        self.assertIsNotNone(observer.entry("w-late"))
        after = widget_request(widget_payload("w-after", "After close", measures=["People"]), [cell(3)])
        observer._on_request_finished(after)
        self.assertIsNone(observer.entry("w-after"))

    def test_late_cancelled_policies_exact_source_is_discoverable(self):
        observer = _observer()
        session = SimpleNamespace(context=Quiet())

        def complete():
            observer._on_request_finished(cancelled_request())
            return ""

        with mock.patch.object(app, "read_operator_completion", complete):
            await_operator_observation(session, observer, _settings(runtime_validation=True))
        outcome = finalize_discovery(session, observer, build_currently_enrolled_query(), _settings())
        identified = {item.widget_id: item.confidence for item in outcome.identification}
        self.assertEqual(identified.get("w-cancelled-policies"), "EXACT_IDENTITY_CONFIRMED")
        rows = {row.metric_id: row for row in outcome.runtime.rows}
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)
        self.assertEqual(rows["cancelled_policies_daily"].bound_measure_title, "Cancelled Policies")

    def test_no_automatic_scroll_click_or_navigation(self):
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        window = main_text.split("def read_operator_completion", 1)[1].split("def wait_for_dashboard_quiet", 1)[0]
        for forbidden in (".scroll", "click(", "goto(", "mouse", ".request.post", "page."):
            self.assertNotIn(forbidden, window)
        self.assertIn("Does not scroll", window)
        touched = []

        class Context(Quiet):
            def __getattr__(self, name):
                touched.append(name)
                raise AssertionError(name)

        with mock.patch.object(app, "read_operator_completion", return_value=""):
            await_operator_observation(SimpleNamespace(context=Context()), _observer(), _settings())
        self.assertEqual(touched, [])

    def test_no_application_cancellation_jaql_is_generated(self):
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn(".request.post", main_text)
        discovery = (PROJECT / "sisense" / "discovery.py").read_text(encoding="utf-8")
        self.assertNotIn(".request.post", discovery)
        self.assertEqual((PROJECT / "sisense" / "client.py").read_text(encoding="utf-8").count(".request.post("), 1)

    def test_application_generated_jaql_remains_the_one_currently_enrolled_request(self):
        self.assertEqual((PROJECT / "main.py").read_text(encoding="utf-8").count("retrieve_value("), 2)
        self.assertEqual(
            (PROJECT / "sisense" / "payloads" / "currently_enrolled.py").read_text(encoding="utf-8").count(
                "def build_currently_enrolled_query"),
            1,
        )


class OperatorWindowMainTests(PipelineHarness):
    def test_validation_run_prompts_and_sends_one_application_request(self):
        code, out, _, posts = self.run_main([FakeResponse(200, ok_body(12001))], runtime_validation=True)
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(self.operator_prompts, ["enter"])
        self.assertEqual(len(posts), 1)
        for line in app.OBSERVATION_WINDOW_LINES:
            self.assertIn(line, out)

    def test_normal_run_does_not_wait_for_operator_input(self):
        code, out, _, posts = self.run_main([FakeResponse(200, ok_body(12001))])
        self.assertEqual(code, app.EXIT_OK)
        self.assertEqual(self.operator_prompts, [])
        self.assertEqual(len(posts), 1)
        self.assertNotIn("Passive observation window is active.", out)


class ControlSemanticsTests(unittest.TestCase):
    def test_structural_match_with_different_values_is_live_movement(self):
        moved = ControlComparison(12001, 12006, None, passive_observed_at="t0", application_observed_at="t1")
        self.assertEqual(moved.identity_status, IDENTITY_PASS)
        self.assertEqual(moved.status, LIVE_MOVEMENT)
        self.assertNotEqual(moved.status, "FAIL")
        self.assertTrue(moved.identity_passed)
        self.assertEqual(moved.delta, 5)
        self.assertEqual((moved.passive_observed_at, moved.application_observed_at), ("t0", "t1"))

    def test_structural_match_with_equal_values_is_exact_value_match(self):
        matched = ControlComparison(12001, 12001, None)
        self.assertEqual(matched.identity_status, IDENTITY_PASS)
        self.assertEqual(matched.status, EXACT_VALUE_MATCH)
        self.assertEqual(matched.delta, 0)
        self.assertTrue(matched.identity_passed)

    def test_structural_mismatch_is_identity_fail_even_when_values_are_equal(self):
        failed = ControlComparison(12001, 12001, "FAIL (identity/filter verification)")
        self.assertEqual(failed.identity_status, IDENTITY_FAIL)
        self.assertEqual(failed.status, IDENTITY_FAIL)
        self.assertFalse(failed.identity_passed)

    def test_unavailable_source(self):
        missing_widget = ControlComparison(None, 12001, "Verified widget not observed")
        self.assertEqual(missing_widget.status, SOURCE_UNAVAILABLE)
        missing_application = ControlComparison(12001, None, "Application request failed")
        self.assertEqual(missing_application.identity_status, IDENTITY_PASS)
        self.assertEqual(missing_application.status, SOURCE_UNAVAILABLE)
        self.assertFalse(missing_application.identity_passed)

    def test_no_numeric_tolerance(self):
        source = (PROJECT / "metrics" / "runtime_validation.py").read_text(encoding="utf-8")
        body = source.split("class ControlComparison", 1)[1].split("def passive_control_value", 1)[0]
        self.assertIn("not judged by a numeric tolerance", " ".join(body.split()))
        self.assertNotIn("abs(", body)
        status = body.split("def status", 1)[1].split("def identity_passed", 1)[0]
        self.assertNotIn("<=", status)
        self.assertNotIn(">=", status)
        near = ControlComparison(12001, 12002, None)
        self.assertEqual(near.status, LIVE_MOVEMENT)
        far = ControlComparison(12001, 22001, None)
        self.assertEqual(far.status, LIVE_MOVEMENT)

    def test_same_response_canary_stays_strict(self):
        ok = PassiveMetricOutcome("percent_effectuated", PM_EXTRACTED, 0.5, 12001)
        self.assertTrue(apply_control(ok, 12001).succeeded)
        failed = apply_control(ok, 12002)
        self.assertFalse(failed.succeeded)
        self.assertIn("same dashboard response", failed.failure)
        text = (PROJECT / "metrics" / "passive_metrics.py").read_text(encoding="utf-8")
        canary = text.split("def apply_control", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("!=", canary)
        self.assertNotIn("tolerance", canary.lower())

    def test_later_application_value_remains_authoritative(self):
        moved = ControlComparison(12001, 12006, None)
        self.assertEqual(moved.authoritative_value, 12006)
        self.assertNotEqual(moved.authoritative_value, moved.passive_value)

    def test_identity_fail_does_not_gate_snapshot_persistence(self):
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertIn("IDENTITY_FAIL is reported on the validation artifact", main_text)
        self.assertIn("does not block these snapshots", main_text)
        pipeline = (PROJECT / "reporting" / "pipeline.py").read_text(encoding="utf-8")
        complete = pipeline.split("def complete_success", 1)[1].split("def complete_failure", 1)[0]
        self.assertNotIn("ControlComparison", complete)
        self.assertNotIn("IDENTITY_FAIL", complete)


class CancellationBoundaryTests(unittest.TestCase):
    def test_cancellation_stays_unpromoted_and_is_not_inferred(self):
        self.assertEqual(tuple(METRICS), PRODUCTION_IDS)
        self.assertEqual(list(PROMOTION_CANDIDATES), ["total_applicants"])
        for metric_id in (
            "cancelled_policies_daily", "terminated_policies_daily",
            "enrolled_cancelled", "cancelled_not_enrolled", "total_applicants",
        ):
            self.assertNotIn(metric_id, METRICS)
        rows, _ = runtime(cancelled_request())
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_SERIES_EXTRACTED)
        for metric_id in ("terminated_policies_daily", "enrolled_cancelled", "cancelled_not_enrolled"):
            self.assertEqual(rows[metric_id].status, RV_VALIDATION_FAILED)
            self.assertNotEqual(rows[metric_id].status, RV_SERIES_EXTRACTED)
            self.assertNotEqual(rows[metric_id].status, RV_VALUE_EXTRACTED)
        self.assertEqual(rows["terminated"].status, RV_PENDING)
        self.assertIsNone(rows["terminated"].value)
        cancelled = next(status for status in EXPLORER_STATUSES if status.key == "cancelled")
        terminated = next(status for status in EXPLORER_STATUSES if status.key == "terminated")
        pending = next(status for status in EXPLORER_STATUSES if status.key == "pending")
        self.assertEqual(cancelled.metric_ids, ())
        self.assertEqual(cancelled.message, PENDING_SOURCE)
        self.assertEqual(terminated.message, PENDING_SOURCE)
        self.assertEqual(pending.message, "Pending enrollment source verification")

    def test_suite_makes_no_network_calls(self):
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({alias.name.split(".")[0] for alias in node.names} & forbidden_modules)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden_modules)
            if isinstance(node, ast.Attribute) and node.attr in {"post", "urlopen", "create_connection"}:
                self.fail(node.attr)
