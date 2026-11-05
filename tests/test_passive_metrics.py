"""Phase 3E: production passive metrics (verified source definitions, fail closed). No network."""

import ast
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from history.store import SnapshotRejected, SnapshotStore
from metrics.catalog import (
    METRICS,
    PERCENT_EFFECTUATED,
    PROMOTION_CANDIDATES,
    ROADMAP,
    STATUS_ACTIVE,
    STATUS_VALIDATION_ONLY,
    PassiveSource,
    passive_metrics,
)
from metrics.passive_metrics import (
    PM_EXTRACTED,
    PM_FAILED,
    PM_NOT_AVAILABLE,
    PassiveMetricOutcome,
    SourceWidgetObserver,
    apply_control,
    extract_passive_metric,
    source_check,
)
from reporting.formatting import fmt_metric_change, fmt_metric_value, fmt_pp, fmt_ratio_pct
from sisense.models import MetricResult
from sisense.passive import definition_fingerprint
from tests.discovery_fixtures import cell, rows_text, widget_payload, widget_request
from tests.passive_fixtures import PassiveRequest, measure, payload, response_text, scope

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAP = PROMOTION_CANDIDATES["new_enrolled"].passive_source


def candidate_payload(metric_id, measures=None, **kw):
    """A dashboard request for a candidate's real widget identity (structure and values invented)."""
    source = PROMOTION_CANDIDATES[metric_id].passive_source
    if measures is None:
        measures = [source.measure_title or "Count of applications"]
    return widget_payload(source.widget_id, source.widget_title, measures=measures, **kw)


def nap_payload(order=("NEW", "ACTIVE", "PASSIVE"), **kw):
    return widget_payload(NAP.widget_id, NAP.widget_title, measures=list(order), widget_type="chart/pie", **kw)


def promoted(metric_id, approved_payload):
    """Test-only promotion: a candidate whose fingerprint was approved from ``approved_payload``."""
    candidate = PROMOTION_CANDIDATES[metric_id]
    source = replace(candidate.passive_source, expected_fingerprint=definition_fingerprint(approved_payload))
    return replace(candidate, status=STATUS_ACTIVE, passive_source=source)


def observe(*requests, widget_ids=None):
    ids = widget_ids or {json.loads(r.post_data)["widget"].split(";")[0] for r in requests if r.post_data}
    observer = SourceWidgetObserver(ids)
    for request in requests:
        observer._on_request_finished(request)
    observer.stop()
    return observer


def extract(metric, *requests):
    return extract_passive_metric(metric, observe(*requests, widget_ids={metric.passive_source.widget_id}))


class CatalogPromotionTests(unittest.TestCase):
    def test_percent_effectuated_is_production_verified(self):
        m = METRICS["percent_effectuated"]
        self.assertEqual((m.status, m.value_format, m.module_id), (STATUS_ACTIVE, "percent", "enrollment"))
        self.assertTrue(m.history_enabled)
        self.assertIsNone(m.query_builder)
        self.assertEqual(m.passive_source.measure_title, "% Effectuated")
        self.assertEqual(m.passive_source.widget_id, json.loads(PassiveRequest(payload()).post_data)["widget"]
                         .split(";")[0])
        self.assertEqual([x.metric_id for x in passive_metrics()], ["percent_effectuated"])

    def test_six_without_filter_evidence_are_refused(self):
        self.assertEqual(set(PROMOTION_CANDIDATES), {"applications_started", "submitted_applications",
                                                     "total_applicants", "new_enrolled", "active_enrolled",
                                                     "passive_enrolled"})
        with tempfile.TemporaryDirectory() as tmp, SnapshotStore(Path(tmp) / "h.sqlite3") as store:
            store.start_run("r1")
            for metric_id, m in PROMOTION_CANDIDATES.items():
                with self.subTest(metric_id):
                    self.assertNotIn(metric_id, METRICS)
                    self.assertEqual(m.status, STATUS_VALIDATION_ONLY)
                    self.assertFalse(m.passive_source.verifiable)
                    with self.assertRaises(SnapshotRejected):
                        store.record_snapshot("r1", MetricResult(metric_id, 2027, 1, "2026-11-10T12:00:00+00:00",
                                                                 "Sisense MAIN", "d"))

    def test_active_metric_requires_a_verified_source(self):
        base = PROMOTION_CANDIDATES["total_applicants"]
        with self.assertRaises(ValueError):
            replace(base, status=STATUS_ACTIVE)
        with self.assertRaises(ValueError):
            replace(PERCENT_EFFECTUATED, query_builder=lambda: None)
        with self.assertRaises(ValueError):
            replace(base, status=STATUS_ACTIVE,
                    passive_source=PassiveSource("w", "t", "m"))

    def test_roadmap_keeps_cancelled_terminated_metal_tier_pending(self):
        names = {i.name for i in ROADMAP}
        self.assertTrue({"Cancelled", "Terminated", "Metal Tier"} <= names)
        self.assertNotIn("% Effectuated", names)
        for item in ROADMAP:
            self.assertNotIn(item.name, {m.display_name for m in METRICS.values()})


class PercentEffectuatedTests(unittest.TestCase):
    def test_exact_extraction_with_control(self):
        out = extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), text=response_text(700_001)))
        self.assertEqual((out.status, out.value, out.control_value), (PM_EXTRACTED, 0.97, 700_001))

    def test_raw_ratio_preserved_in_history(self):
        raw = 0.1234567890123
        with tempfile.TemporaryDirectory() as tmp, SnapshotStore(Path(tmp) / "h.sqlite3") as store:
            store.start_run("r1")
            store.record_snapshot("r1", MetricResult("percent_effectuated", 2027, raw, "2026-11-10T12:00:00+00:00",
                                                     "Sisense MAIN", "d"))
            self.assertEqual(store.snapshots("percent_effectuated", 2027)[0].metric_value, raw)

    def test_percentage_presentation(self):
        self.assertEqual(fmt_ratio_pct(0.1234567), "12.35%")
        self.assertEqual(fmt_metric_value(0.5, "percent"), "50.00%")
        self.assertEqual(fmt_metric_value(1234, "integer"), "1,234")
        self.assertEqual(fmt_pp(0.0032), "+0.32 pp")
        self.assertEqual(fmt_metric_change(-0.001, "percent"), "-0.10 pp")

    def test_never_substitutes_currently_enrolled(self):
        swapped = response_text(700_001, {"headers": ["% Effectuated", "Currently Enrolled People"]})
        out = extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), text=swapped))
        self.assertEqual(out.status, PM_FAILED)
        self.assertIsNone(out.value)
        one_cell = json.dumps({"values": [cell(700_001)]})
        self.assertEqual(extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), text=one_cell)).status, PM_FAILED)

    def test_null_and_empty_are_not_available(self):
        null = json.dumps({"values": [cell(700_001), {"data": None, "text": "N\\A"}]})
        self.assertEqual(extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), text=null)).status, PM_NOT_AVAILABLE)
        empty = json.dumps({"values": []})
        out = extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), text=empty))
        self.assertEqual((out.status, out.value), (PM_NOT_AVAILABLE, None))

    def test_requires_percentage_measure(self):
        unformatted = payload(lambda b: measure(b, "% Effectuated").pop("format", None))
        out = extract(PERCENT_EFFECTUATED, PassiveRequest(unformatted, text=response_text(1)))
        self.assertEqual(out.status, PM_FAILED)

    def test_control_canary(self):
        ok = PassiveMetricOutcome("percent_effectuated", PM_EXTRACTED, 0.97, 700_001)
        self.assertTrue(apply_control(ok, 700_001).succeeded)
        self.assertIn("control mismatch", apply_control(ok, 700_002).failure)
        self.assertEqual(apply_control(ok, None).status, PM_FAILED)


class CandidateExtractionTests(unittest.TestCase):
    """The promotion path for the six, exercised with test-only approved fingerprints."""

    def test_single_measure_widgets(self):
        for metric_id, value in (("applications_started", 400_002), ("submitted_applications", 300_003),
                                 ("total_applicants", 500_004)):
            with self.subTest(metric_id):
                body = candidate_payload(metric_id)
                out = extract(promoted(metric_id, body), widget_request(body, [cell(value)]))
                self.assertEqual((out.status, out.value), (PM_EXTRACTED, value))
                self.assertIsInstance(out.value, int)
                self.assertIsNone(out.control_value)

    def test_single_measure_binding_needs_exact_widget_title(self):
        source = PROMOTION_CANDIDATES["applications_started"].passive_source
        body = widget_payload(source.widget_id, "Applications Started (copy)", measures=["Count of applications"])
        out = extract(promoted("applications_started", body), widget_request(body, [cell(1)]))
        self.assertEqual(out.status, PM_FAILED)

    def test_new_active_passive_exact(self):
        body = nap_payload()
        for metric_id, value in (("new_enrolled", 100_005), ("active_enrolled", 200_006),
                                 ("passive_enrolled", 300_007)):
            with self.subTest(metric_id):
                request = widget_request(body, [cell(100_005), cell(200_006), cell(300_007)])
                out = extract(promoted(metric_id, body), request)
                self.assertEqual((out.status, out.value), (PM_EXTRACTED, value))

    def test_multi_measure_mapping_follows_measure_identity(self):
        body = nap_payload(order=("PASSIVE", "NEW", "ACTIVE"))
        request = widget_request(body, [cell(30), cell(10), cell(20)])
        values = [extract(promoted(m, body), request).value
                  for m in ("new_enrolled", "active_enrolled", "passive_enrolled")]
        self.assertEqual(values, [10, 20, 30])

    def test_no_fallback_to_another_cell(self):
        body = nap_payload()
        metric = promoted("passive_enrolled", body)
        for text in (rows_text([cell(1), cell(2)]), rows_text([cell(1), cell(2), {"data": None, "text": ""}])):
            with self.subTest(text=text):
                out = extract(metric, PassiveRequest(body, text=text))
                self.assertIn(out.status, (PM_FAILED, PM_NOT_AVAILABLE))
                self.assertIsNone(out.value)

    def test_ambiguous_measure_rejected(self):
        body = nap_payload(order=("NEW", "NEW", "PASSIVE"))
        out = extract(promoted("new_enrolled", body), widget_request(body, [cell(1), cell(2), cell(3)]))
        self.assertEqual(out.status, PM_FAILED)

    def test_count_must_be_integer(self):
        body = candidate_payload("total_applicants")
        for value in (1.5, True, "12", float("nan")):
            with self.subTest(value=value):
                out = extract(promoted("total_applicants", body), widget_request(body, [cell(value)]))
                self.assertEqual(out.status, PM_FAILED)

    def test_unapproved_candidate_never_extracts(self):
        body = candidate_payload("total_applicants")
        out = extract(PROMOTION_CANDIDATES["total_applicants"], widget_request(body, [cell(5)]))
        self.assertEqual((out.status, out.value), (PM_FAILED, None))


class SourceVerificationTests(unittest.TestCase):
    def assert_rejected(self, metric, body, text=None):
        out = extract(metric, PassiveRequest(body, text=text or response_text(1)))
        self.assertEqual(out.status, PM_FAILED)
        self.assertIsNone(out.value)

    def test_wrong_widget_rejected(self):
        approved = candidate_payload("total_applicants")
        other = widget_payload("6a98048ccef60720fe7f9b4f", "Applicants On Submitted Applications",
                               measures=["Total Applicants"])
        other["widget"] = "ffffffffffffffffffffffff;Applicants On Submitted Applications"
        self.assertEqual(source_check(promoted("total_applicants", approved).passive_source, other),
                         "Source widget identity mismatch")
        self.assert_rejected(PERCENT_EFFECTUATED, payload(lambda b: b.update(widget="x;Currently Enrolled People")))

    def test_wrong_dashboard_rejected(self):
        self.assert_rejected(PERCENT_EFFECTUATED, payload(lambda b: b.update(dashboard="other;Other")))
        approved = candidate_payload("total_applicants")
        changed = candidate_payload("total_applicants", dashboard="other;Other")
        self.assert_rejected(promoted("total_applicants", approved), changed, rows_text([cell(1)]))

    def test_wrong_datasource_rejected(self):
        self.assert_rejected(PERCENT_EFFECTUATED, payload(lambda b: b["datasource"].update(title="OTHER")))
        approved = candidate_payload("total_applicants")
        changed = candidate_payload("total_applicants", datasource={"id": "x", "title": "OTHER"})
        self.assert_rejected(promoted("total_applicants", approved), changed, rows_text([cell(1)]))

    def test_wrong_measure_rejected(self):
        self.assert_rejected(PERCENT_EFFECTUATED,
                             payload(lambda b: measure(b, "% Effectuated")["jaql"].update(title="% Other")))
        approved = candidate_payload("total_applicants")
        changed = candidate_payload("total_applicants", measures=["Total Applicants (all)"])
        self.assert_rejected(promoted("total_applicants", approved), changed, rows_text([cell(1)]))

    def test_wrong_filters_rejected(self):
        self.assert_rejected(PERCENT_EFFECTUATED,
                             payload(lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"])))
        approved = candidate_payload("total_applicants")
        for changed in (candidate_payload("total_applicants", filters=2),
                        candidate_payload("total_applicants", filters=4)):
            self.assert_rejected(promoted("total_applicants", approved), changed, rows_text([cell(1)]))
        changed = candidate_payload("total_applicants")
        changed["metadata"][-1]["jaql"]["filter"] = {"members": ["SOMETHING-ELSE"]}
        self.assert_rejected(promoted("total_applicants", approved), changed, rows_text([cell(1)]))

    def test_changed_query_guid_is_equivalent(self):
        out = extract(PERCENT_EFFECTUATED, PassiveRequest(payload(query_guid="OTHER-GUID"), text=response_text(3)))
        self.assertEqual(out.status, PM_EXTRACTED)
        approved = candidate_payload("total_applicants", query_guid="GUID-A")
        later = candidate_payload("total_applicants", query_guid="GUID-B")
        self.assertEqual(definition_fingerprint(approved), definition_fingerprint(later))
        self.assertEqual(extract(promoted("total_applicants", approved), widget_request(later, [cell(9)])).value, 9)

    def test_non_200_rejected_and_never_falls_back_to_older_response(self):
        self.assertEqual(extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), status=503)).status, PM_FAILED)
        older = PassiveRequest(payload(), text=response_text(1))
        newer = PassiveRequest(payload(), status=500)
        out = extract(PERCENT_EFFECTUATED, older, newer)
        self.assertEqual((out.status, out.failure), (PM_FAILED, "Dashboard response HTTP 500"))

    def test_latest_verified_response_wins(self):
        older = PassiveRequest(payload(), text=json.dumps({"values": [cell(1), cell(0.5)]}))
        newer = PassiveRequest(payload(), text=json.dumps({"values": [cell(1), cell(0.6)]}))
        self.assertEqual(extract(PERCENT_EFFECTUATED, older, newer).value, 0.6)

    def test_response_unavailable_rejected(self):
        for request in (PassiveRequest(payload(), body_error=True), PassiveRequest(payload(), no_response=True)):
            self.assertEqual(extract(PERCENT_EFFECTUATED, request).status, PM_FAILED)

    def test_malformed_response_rejected(self):
        for text in ("<html>login</html>", '{"error": true}', '{"values": "x"}',
                     json.dumps({"values": [cell(1), cell("0.9x")]}),
                     json.dumps({"values": [cell(1), cell(float("inf"))]})):
            with self.subTest(text=text):
                self.assertEqual(extract(PERCENT_EFFECTUATED, PassiveRequest(payload(), text=text)).status, PM_FAILED)

    def test_not_observed_fails_closed(self):
        out = extract(PERCENT_EFFECTUATED)
        self.assertEqual((out.status, out.failure), (PM_FAILED, "Source widget request not observed"))


class ObserverTests(unittest.TestCase):
    def test_bounded_and_scoped(self):
        observer = SourceWidgetObserver({"6a98048ccef60720fe7f9b47"}, max_candidates=2)
        requests = [PassiveRequest(payload()) for _ in range(4)]
        other = widget_request(widget_payload("w-other", "Other", measures=["X"]), [cell(1)])
        get = PassiveRequest(payload(), method="GET")
        for r in (*requests, other, get):
            observer._on_request_finished(r)
        self.assertEqual(observer.candidates("6a98048ccef60720fe7f9b47"), requests[-2:])
        self.assertEqual(observer.target_requests_seen, 5)
        self.assertEqual(observer.missing(), set())

    def test_stop_ignores_later_requests(self):
        class Context:
            def __init__(self):
                self.handlers = []

            def on(self, event, handler):
                self.handlers.append(handler)

        context = Context()
        observer = SourceWidgetObserver({"6a98048ccef60720fe7f9b47"})
        observer.attach(context)
        context.handlers[0](PassiveRequest(payload()))
        observer.stop()
        context.handlers[0](PassiveRequest(payload()))
        self.assertEqual(len(observer.candidates("6a98048ccef60720fe7f9b47")), 1)


class SafetyTests(unittest.TestCase):
    SOURCES = ("metrics/passive_metrics.py", "metrics/catalog.py", "reporting/pipeline.py", "reporting/model.py",
               "reporting/html_report.py", "reporting/formatting.py")

    def test_zero_http_capability(self):
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        forbidden_attrs = {"request", "post", "fetch", "evaluate", "goto", "reload", "route", "new_page",
                           "new_context", "api_request", "wait_for_response", "expect_response"}
        tree = ast.parse((PROJECT_ROOT / "metrics/passive_metrics.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden_modules)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden_modules)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden_attrs, node.lineno)

    def test_no_hardcoded_ui_validation_values(self):
        literals = ("566051", "566,051", "565200", "565,200", "832776", "832,776", "0.954845", "95.48",
                    "2510", "2,510", "35266", "35,266", "756009", "756,009", "793512", "793,512",
                    "791826", "791,826")
        for relative in (*self.SOURCES, "main.py", "metrics/runtime_validation.py"):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for literal in literals:
                self.assertNotIn(literal, text, relative)

    def test_production_never_reads_developer_artifacts(self):
        for relative in self.SOURCES:
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for name in ("wave1_", "discovery_report", "validation_report", "screenshot"):
                self.assertNotIn(name, text, relative)


if __name__ == "__main__":
    unittest.main()
