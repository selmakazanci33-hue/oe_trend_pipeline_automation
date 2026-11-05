"""Production hotfix: keep the approved source request when later requests differ.

Fakes only. No network. Fingerprint equality stays exact.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from metrics.catalog import (
    CANCELLATION_CANDIDATES,
    METRICS,
    PERCENT_EFFECTUATED,
    PROMOTION_CANDIDATES,
)
from metrics.passive_metrics import (
    PM_EXTRACTED,
    PM_FAILED,
    PassiveMetricOutcome,
    SourceWidgetObserver,
    apply_control,
    approved_fingerprints_by_widget,
    extract_passive_metric,
    source_check,
)
from sisense.passive import definition_fingerprint
from tests.discovery_fixtures import cell, widget_request
from tests.passive_fixtures import PassiveRequest, payload, response_text
from tests.test_passive_metrics import candidate_payload, nap_payload, promoted
from tests.test_phase3h_c import PRODUCTION_IDS

PROJECT = Path(__file__).resolve().parent.parent
NAP_VALUES = (11011, 22022, 33033)
OTHER_VALUES = (7, 8, 9)


def _nap_metrics(body):
    return tuple(promoted(metric_id, body) for metric_id in
                 ("new_enrolled", "active_enrolled", "passive_enrolled"))


def _mismatch(index):
    return widget_request(
        nap_payload(filters=4 + index, query_guid=f"other-{index}"),
        [cell(value) for value in OTHER_VALUES],
    )


def _observe(metrics, *requests):
    widget_ids = {metric.passive_source.widget_id for metric in metrics}
    observer = SourceWidgetObserver(
        widget_ids, approved_fingerprints=approved_fingerprints_by_widget(metrics),
    )
    for request in requests:
        observer._on_request_finished(request)
    observer.stop()
    return observer


class ApprovedRetentionTests(unittest.TestCase):
    def test_later_requests_cannot_evict_the_approved_fingerprint(self):
        approved_body = nap_payload()
        metrics = _nap_metrics(approved_body)
        approved = widget_request(approved_body, [cell(value) for value in NAP_VALUES])
        later = [_mismatch(index) for index in range(6)]
        self.assertGreater(len(later), 5)
        self.assertNotEqual(
            definition_fingerprint(approved_body),
            definition_fingerprint(json.loads(later[0].post_data)),
        )
        observer = _observe(metrics, approved, *later)
        widget_id = metrics[0].passive_source.widget_id
        self.assertNotIn(approved, list(observer._candidates[widget_id]))
        self.assertIs(observer.candidates(widget_id)[-1], approved)
        for metric, value in zip(metrics, NAP_VALUES):
            outcome = extract_passive_metric(metric, observer)
            self.assertEqual(outcome.status, PM_EXTRACTED)
            self.assertEqual(outcome.value, value)
            self.assertIsNone(source_check(metric.passive_source, json.loads(approved.post_data)))

    def test_latest_exact_approved_request_is_kept(self):
        approved_body = nap_payload()
        metrics = _nap_metrics(approved_body)
        first = widget_request(approved_body, [cell(value) for value in NAP_VALUES])
        second = widget_request(nap_payload(query_guid="approved-2"), [cell(44044), cell(55055), cell(66066)])
        observer = _observe(metrics, first, *[_mismatch(index) for index in range(6)], second)
        outcome = extract_passive_metric(metrics[0], observer)
        self.assertEqual((outcome.status, outcome.value), (PM_EXTRACTED, 44044))
        self.assertIs(observer.candidates(metrics[0].passive_source.widget_id)[-1], second)

    def test_no_approved_fingerprint_stays_mismatch(self):
        approved_body = nap_payload()
        metrics = _nap_metrics(approved_body)
        observer = _observe(metrics, *[_mismatch(index) for index in range(6)])
        for metric in metrics:
            outcome = extract_passive_metric(metric, observer)
            self.assertEqual(outcome.status, PM_FAILED)
            self.assertEqual(outcome.failure, "Request definition mismatch")
            self.assertIsNone(outcome.value)

    def test_parseable_wrong_fingerprint_is_not_production(self):
        approved_body = nap_payload()
        metric = promoted("new_enrolled", approved_body)
        wrong = widget_request(
            nap_payload(filters=8),
            [cell(value) for value in NAP_VALUES],
        )
        observer = _observe((metric,), wrong)
        outcome = extract_passive_metric(metric, observer)
        self.assertEqual(outcome.status, PM_FAILED)
        self.assertEqual(outcome.failure, "Request definition mismatch")
        self.assertIsNone(outcome.value)

    def test_exact_titles_not_positions(self):
        body = nap_payload(order=("PASSIVE", "NEW", "ACTIVE"))
        metrics = _nap_metrics(body)
        request = widget_request(body, [cell(30), cell(10), cell(20)])
        observer = _observe(metrics, request)
        values = [extract_passive_metric(metric, observer).value for metric in metrics]
        self.assertEqual(values, [10, 20, 30])
        binding = (PROJECT / "metrics" / "passive_metrics.py").read_text(encoding="utf-8")
        self.assertIn("titles.index(title)", binding)
        self.assertNotIn("resolve_targets", binding)


class OtherProductionMetricsTests(unittest.TestCase):
    def test_applications_started_and_submitted_still_require_their_fingerprint(self):
        for metric_id, value in (("applications_started", 44044), ("submitted_applications", 55055)):
            body = candidate_payload(metric_id)
            metric = promoted(metric_id, body)
            approved = widget_request(body, [cell(value)])
            other = widget_request(candidate_payload(metric_id, filters=6), [cell(1)])
            self.assertNotEqual(definition_fingerprint(body), definition_fingerprint(json.loads(other.post_data)))
            observer = _observe((metric,), approved, *[
                widget_request(candidate_payload(metric_id, filters=6 + index), [cell(1)])
                for index in range(6)
            ])
            outcome = extract_passive_metric(metric, observer)
            self.assertEqual((outcome.status, outcome.value), (PM_EXTRACTED, value))

    def test_percent_effectuated_has_no_fingerprint_pin(self):
        self.assertIsNone(PERCENT_EFFECTUATED.passive_source.expected_fingerprint)
        self.assertEqual(approved_fingerprints_by_widget([PERCENT_EFFECTUATED]), {})
        request = PassiveRequest(payload(), text=response_text(700_001))
        observer = SourceWidgetObserver(
            {PERCENT_EFFECTUATED.passive_source.widget_id},
            approved_fingerprints=approved_fingerprints_by_widget([PERCENT_EFFECTUATED]),
        )
        observer._on_request_finished(request)
        observer.stop()
        outcome = extract_passive_metric(PERCENT_EFFECTUATED, observer)
        self.assertEqual((outcome.status, outcome.value), (PM_EXTRACTED, 0.97))

    def test_currently_enrolled_control_stays_exact(self):
        ok = PassiveMetricOutcome("percent_effectuated", PM_EXTRACTED, 0.97, 700_001)
        self.assertTrue(apply_control(ok, 700_001).succeeded)
        self.assertIn("control mismatch", apply_control(ok, 700_002).failure)


class CatalogInvariantTests(unittest.TestCase):
    def test_catalog_and_network_are_unchanged(self):
        self.assertEqual(tuple(METRICS), PRODUCTION_IDS)
        self.assertEqual(list(PROMOTION_CANDIDATES), ["total_applicants"])
        self.assertNotIn("total_applicants", METRICS)
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertNotIn(metric_id, METRICS)
            self.assertNotIn(metric_id, PROMOTION_CANDIDATES)
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(main_text.count("retrieve_value("), 2)
        self.assertEqual((PROJECT / "sisense" / "client.py").read_text(encoding="utf-8").count(".request.post("), 1)
        forbidden = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({alias.name.split(".")[0] for alias in node.names} & forbidden)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden)
