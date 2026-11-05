"""Phase 3F-B: overall header health, refresh semantics, promotion evidence (fakes only, invented values)."""

import copy
import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from analytics.derived import compute_analytics
from history.store import SnapshotStore
from metrics import catalog
from metrics.catalog import CURRENTLY_ENROLLED, METRICS, PERCENT_EFFECTUATED, PROMOTION_CANDIDATES
from metrics.fingerprint_evidence import (
    FP_CHANGED,
    FP_MISSING,
    FP_NO_BASELINE,
    FP_STABLE,
    OBSERVATIONS_FILE,
    compare_observation,
)
from reporting.html_report import render_html
from reporting.model import (
    DATA_LIVE,
    DATA_NOT_AVAILABLE,
    DATA_PARTIAL,
    DATA_STALE,
    MetricPanel,
    RunOutcome,
    build_report_model,
    overall_status,
)
from sisense.models import MetricResult
from sisense.passive import definition_fingerprint
from tests.discovery_fixtures import cell, widget_payload, widget_request
from tests.passive_fixtures import PassiveRequest
from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_pipeline_main import RUN_TIME
from tests.test_session_context import FakeResponse

CANDIDATE = {m: c.passive_source for m, c in PROMOTION_CANDIDATES.items()}
VALUES = {"applications_started": 300_011, "submitted_applications": 300_022, "total_applicants": 300_033,
          "new_enrolled": 3_044, "active_enrolled": 30_055, "passive_enrolled": 300_066}
STARTED_MEASURE = "Count of started applications"
SUBMITTED_MEASURE = "Count of submitted applications"
DEVELOPER_TERMS = ("VERIFIED", "Verified", "DISCOVERED", "VALUE_EXTRACTED", "NOT_AVAILABLE", "VALIDATION",
                   "fingerprint", "sha256", "ELIGIBLE", "Observation", "NO HISTORY")


def visible(html):
    return re.sub(r"<script>.*?</script>|<style>.*?</style>", "", html, flags=re.S)


def started_payload(**kw):
    s = CANDIDATE["applications_started"]
    return widget_payload(s.widget_id, s.widget_title, measures=[STARTED_MEASURE], **kw)


def submitted_payload(**kw):
    s = CANDIDATE["submitted_applications"]
    return widget_payload(s.widget_id, s.widget_title, measures=[SUBMITTED_MEASURE], **kw)


def applicants_payload(**kw):
    s = CANDIDATE["total_applicants"]
    return widget_payload(s.widget_id, s.widget_title, measures=["Total Applicants"], **kw)


def nap_payload(order=("NEW", "ACTIVE", "PASSIVE"), **kw):
    s = CANDIDATE["new_enrolled"]
    return widget_payload(s.widget_id, s.widget_title, measures=list(order), widget_type="chart/pie", **kw)


def candidate_requests(values=None, guid="GUID-A", drop=(), mutate=None, sentinel="RESP-X"):
    v = {**VALUES, **(values or {})}
    bodies = {
        "applications_started": (started_payload(query_guid=guid), [cell(v["applications_started"])]),
        "submitted_applications": (submitted_payload(query_guid=guid), [cell(v["submitted_applications"])]),
        "total_applicants": (applicants_payload(query_guid=guid), [cell(v["total_applicants"])]),
        "nap": (nap_payload(query_guid=guid), [cell(v["new_enrolled"]), cell(v["active_enrolled"]),
                                               cell(v["passive_enrolled"])]),
    }
    requests = []
    for key, (body, row) in bodies.items():
        if key in drop:
            continue
        if mutate and key in mutate:
            mutate[key](body)
        requests.append(PassiveRequest(body, text=json.dumps({"values": row, "sentinel": sentinel})))
    return requests


def panel(metric, status, at=None):
    from history.store import Snapshot
    snaps = []
    if status == DATA_STALE:
        snaps = [Snapshot(1, "r", metric.metric_id, 0.5, 2027, at, "slot", "s", "d", "VALIDATED", at)]
    current = None
    if status == DATA_LIVE:
        from analytics.derived import DataPoint
        current = DataPoint(0.5, at, stored=False)
    return MetricPanel(metric, compute_analytics(metric.metric_id, snaps, current), status, None, None)


class OverallStatusTests(unittest.TestCase):
    def test_combinations(self):
        cases = {
            (DATA_LIVE, DATA_LIVE): DATA_LIVE,
            (DATA_LIVE, DATA_STALE): DATA_PARTIAL,
            (DATA_STALE, DATA_LIVE): DATA_PARTIAL,
            (DATA_LIVE, DATA_NOT_AVAILABLE): DATA_PARTIAL,
            (DATA_STALE, DATA_STALE): DATA_STALE,
            (DATA_STALE, DATA_NOT_AVAILABLE): DATA_STALE,
            (DATA_NOT_AVAILABLE, DATA_STALE): DATA_STALE,
            (DATA_NOT_AVAILABLE, DATA_NOT_AVAILABLE): DATA_NOT_AVAILABLE,
            (DATA_LIVE,): DATA_LIVE,
        }
        for statuses, expected in cases.items():
            with self.subTest(statuses=statuses):
                self.assertEqual(overall_status(list(statuses)), expected)

    def model(self, ce_live, pct_status, ce_history=False):
        at = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        snaps = []
        if ce_history:
            from history.store import Snapshot
            snaps = [Snapshot(1, "r0", CURRENTLY_ENROLLED.metric_id, 700_000, 2027, at - timedelta(hours=5),
                              "s", "s", "d", "VALIDATED", at)]
        if ce_live:
            outcome = RunOutcome("r1", MetricResult(CURRENTLY_ENROLLED.metric_id, 2027, 700_100,
                                                    at.isoformat(), "s", "d"))
        else:
            outcome = RunOutcome("r1", None, failure_type="X")
        return build_report_model(CURRENTLY_ENROLLED, snaps, outcome, at,
                                  panels=(panel(PERCENT_EFFECTUATED, pct_status, at - timedelta(hours=2)),))

    def header(self, model):
        html = render_html(model)
        return html.split('<dl class="meta">')[1].split("</dl>")[0]

    def test_header_reflects_all_metrics_and_cards_stay_independent(self):
        cases = [(True, DATA_LIVE, False, "LIVE", "PASS"),
                 (True, DATA_NOT_AVAILABLE, False, "PARTIAL", "PASS"),
                 (True, DATA_STALE, False, "PARTIAL", "PASS"),
                 (False, DATA_LIVE, False, "PARTIAL", "ERROR"),
                 (False, DATA_STALE, True, "STALE", "ERROR"),
                 (False, DATA_NOT_AVAILABLE, False, "NOT AVAILABLE", "ERROR")]
        for ce_live, pct, history, header_status, pipeline in cases:
            with self.subTest(ce_live=ce_live, pct=pct):
                model = self.model(ce_live, pct, history)
                self.assertEqual(model.overall_data_status, header_status)
                header = self.header(model)
                self.assertIn(f"<dt>Data Status</dt><dd><span class=\"badge", header)
                self.assertIn(f">{header_status}</span></dd></div>", header)
                self.assertIn(f"<dt>Pipeline Status</dt><dd><span class=\"badge badge-{'pass' if pipeline == 'PASS' else 'error'}\">"
                              f"{pipeline}</span>", header)
                card = render_html(model).split('data-metric="percent_effectuated"')[1][:300]
                self.assertIn(f">{pct}</span>", card)

    def test_last_successful_refresh_covers_all_production_metrics(self):
        model = self.model(False, DATA_STALE, ce_history=True)
        at = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        self.assertEqual(model.last_successful_refresh, at - timedelta(hours=2))
        self.assertEqual(model.last_successful_retrieval, at - timedelta(hours=5))
        html = render_html(model)
        self.assertIn("<dt>Last Successful Refresh</dt><dd>Nov 10, 2026 10:00 UTC</dd>", html)
        self.assertIn("<th>Last Successful Retrieval</th><td>Nov 10, 2026 07:00 UTC</td>", html)
        empty = self.model(False, DATA_NOT_AVAILABLE)
        self.assertIsNone(empty.last_successful_refresh)


class FingerprintNormalizationTests(unittest.TestCase):
    def test_volatile_fields_ignored(self):
        base = applicants_payload(query_guid="G1")
        other = copy.deepcopy(base)
        other["queryGuid"] = "G2"
        for item in other["metadata"]:
            item["instanceid"] = "INST-1"
            item["jaql"]["instanceid"] = "INST-2"
        self.assertEqual(definition_fingerprint(base), definition_fingerprint(other))

    def test_meaningful_filter_change_changes_fingerprint(self):
        base = applicants_payload()
        changed = copy.deepcopy(base)
        changed["metadata"][-1]["jaql"]["filter"] = {"members": ["OTHER"]}
        self.assertNotEqual(definition_fingerprint(base), definition_fingerprint(changed))
        fewer = applicants_payload(filters=2)
        self.assertNotEqual(definition_fingerprint(base), definition_fingerprint(fewer))

    def test_comparison_states(self):
        from metrics.fingerprint_evidence import CandidateEvidence
        def ev(fp):
            return (CandidateEvidence("total_applicants", "Total Applicants", "w", "t", "MAIN", ("x",), "x",
                                      "b", fp, "VALUE_EXTRACTED", None, 200, "t", True, None),)
        self.assertEqual(compare_observation(None, ev("sha256:a")).results["total_applicants"], FP_NO_BASELINE)
        baseline = {"observed_at_utc": "A", "candidates": {"total_applicants": {"fingerprint": "sha256:a"}}}
        self.assertEqual(compare_observation(baseline, ev("sha256:a")).results["total_applicants"], FP_STABLE)
        self.assertEqual(compare_observation(baseline, ev("sha256:b")).results["total_applicants"], FP_CHANGED)
        self.assertEqual(compare_observation(baseline, ev(None)).results["total_applicants"], FP_MISSING)


class EvidenceMainTests(PassiveMetricsHarness):
    def evidence_run(self, requests=None, ce=700_100, responses=None, **kw):
        observed = [self.ce_widget(ce=ce, pct=0.904434), *(candidate_requests() if requests is None else requests)]
        return self.run_pipeline(ce, *observed, responses=responses, runtime_validation=True, **kw)

    def evidence(self):
        path = self.reports / "validation" / "wave1_runtime_validation.json"
        return json.loads(path.read_text(encoding="utf-8"))["promotion_evidence"]

    def candidates(self):
        return {c["metric_id"]: c for c in self.evidence()["candidates"]}

    def log_path(self):
        return self.db.parent / OBSERVATIONS_FILE

    def test_safe_evidence_fields_and_eligibility(self):
        code, out, _, posts = self.evidence_run()
        self.assertEqual((code, len(posts)), (0, 1), "exactly one application request; evidence adds none")
        c = self.candidates()
        self.assertEqual(set(c), set(PROMOTION_CANDIDATES))
        for metric_id, item in c.items():
            with self.subTest(metric_id):
                self.assertEqual(item["widget_id"], CANDIDATE[metric_id].widget_id)
                self.assertEqual(item["datasource"], "MAIN")
                self.assertEqual(item["runtime_value"], VALUES[metric_id])
                self.assertEqual(item["http_status"], 200)
                self.assertTrue(item["definition_fingerprint"].startswith("sha256:"))
                self.assertTrue(item["eligible_for_future_promotion"], item["eligibility_reason"])
                self.assertEqual(item["fingerprint_stability"], FP_NO_BASELINE)
                self.assertEqual(item["observed_at_utc"], "2026-11-10T14:30:00Z")
        self.assertIn("Promotion evidence", out)

    def test_exact_measure_titles_recorded_never_invented(self):
        self.evidence_run()
        c = self.candidates()
        self.assertEqual(c["applications_started"]["measure_titles"], [STARTED_MEASURE])
        self.assertEqual(c["applications_started"]["bound_measure_title"], STARTED_MEASURE)
        self.assertEqual(c["submitted_applications"]["bound_measure_title"], SUBMITTED_MEASURE)
        self.assertEqual(c["total_applicants"]["bound_measure_title"], "Total Applicants")
        self.assertEqual(c["applications_started"]["measure_count"], 1)
        self.assertNotEqual(c["applications_started"]["bound_measure_title"], "Applications Started")

    def test_missing_measure_title_fails_closed(self):
        requests = candidate_requests(mutate={"applications_started": lambda b: b["metadata"][0]["jaql"].pop("title")})
        self.evidence_run(requests)
        item = self.candidates()["applications_started"]
        self.assertFalse(item["eligible_for_future_promotion"])
        self.assertEqual(item["measure_titles"], [])
        self.assertIsNone(item["runtime_value"])

    def test_new_active_passive_shared_widget(self):
        self.evidence_run()
        c = self.candidates()
        prints = {c[m]["definition_fingerprint"] for m in ("new_enrolled", "active_enrolled", "passive_enrolled")}
        self.assertEqual(len(prints), 1)
        self.assertEqual([c[m]["bound_measure_title"] for m in ("new_enrolled", "active_enrolled", "passive_enrolled")],
                         ["NEW", "ACTIVE", "PASSIVE"])
        shared = self.evidence()["shared_widgets"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["widget_id"], CANDIDATE["new_enrolled"].widget_id)
        self.assertTrue(shared[0]["structurally_eligible_together"])

    def test_reordered_measures_bind_by_identity(self):
        requests = candidate_requests(drop=("nap",))
        body = nap_payload(order=("PASSIVE", "NEW", "ACTIVE"))
        requests.append(widget_request(body, [cell(30), cell(10), cell(20)]))
        self.evidence_run(requests)
        c = self.candidates()
        self.assertEqual([c[m]["runtime_value"] for m in ("new_enrolled", "active_enrolled", "passive_enrolled")],
                         [10, 20, 30])

    def test_no_positional_fallback(self):
        requests = candidate_requests(drop=("nap",))
        requests.append(widget_request(nap_payload(), [cell(1), cell(2)]))
        self.evidence_run(requests)
        c = self.candidates()
        for m in ("new_enrolled", "active_enrolled", "passive_enrolled"):
            self.assertIsNone(c[m]["runtime_value"])
            self.assertFalse(c[m]["eligible_for_future_promotion"])
        self.assertFalse(self.evidence()["shared_widgets"][0]["structurally_eligible_together"])

    def test_observation_a_persisted_safely(self):
        self.evidence_run(candidate_requests(guid="GUID-SENTINEL", sentinel="RESP-SENTINEL"))
        text = self.log_path().read_text(encoding="utf-8")
        log = json.loads(text)
        self.assertEqual(len(log["observations"]), 1)
        record = log["observations"][0]["candidates"]["total_applicants"]
        self.assertEqual(set(record), {"widget_id", "measure_titles", "bound_measure_title", "fingerprint",
                                       "extraction_status"})
        for forbidden in ("GUID-SENTINEL", "RESP-SENTINEL", "FILTER-MEMBER-SENTINEL", "Count([",
                          self.request_sentinel, self.response_sentinel):
            self.assertNotIn(forbidden, text)

    def test_observation_b_comparison(self):
        self.evidence_run()
        mutate = {"total_applicants": lambda b: b["metadata"][-1]["jaql"].update(filter={"members": ["OTHER"]})}
        self.evidence_run(candidate_requests(guid="GUID-B", drop=("nap",), mutate=mutate),
                          at=RUN_TIME + timedelta(days=1))
        c = self.candidates()
        self.assertEqual(c["applications_started"]["fingerprint_stability"], FP_STABLE)
        self.assertEqual(c["total_applicants"]["fingerprint_stability"], FP_CHANGED)
        self.assertEqual(c["new_enrolled"]["fingerprint_stability"], FP_MISSING)
        self.assertEqual(self.evidence()["fingerprint_comparison"]["observation_a_observed_at_utc"],
                         "2026-11-10T14:30:00Z")
        self.assertEqual(len(json.loads(self.log_path().read_text())["observations"]), 2)

    def test_stable_never_promotes(self):
        before = (list(METRICS), {k: v.status for k, v in PROMOTION_CANDIDATES.items()})
        self.evidence_run()
        self.evidence_run(at=RUN_TIME + timedelta(days=1))
        self.assertEqual(self.candidates()["total_applicants"]["fingerprint_stability"], FP_STABLE)
        self.assertEqual((list(catalog.METRICS), {k: v.status for k, v in catalog.PROMOTION_CANDIDATES.items()}),
                         before)
        with SnapshotStore(self.db) as store:
            for metric_id in PROMOTION_CANDIDATES:
                self.assertEqual(store.snapshot_count(metric_id, 2027), 0)
            self.assertEqual(store.snapshot_count("currently_enrolled_people", 2027), 2)
            self.assertEqual(store.snapshot_count("percent_effectuated", 2027), 2)

    def test_not_recorded_when_control_fails(self):
        self.evidence_run(responses=[FakeResponse(401, "{}")])
        self.assertFalse(self.log_path().exists())
        self.assertFalse(self.evidence()["observation_recorded"])

    def test_corrupt_log_left_untouched(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.log_path().write_text("not json", encoding="utf-8")
        self.evidence_run()
        self.assertEqual(self.log_path().read_text(encoding="utf-8"), "not json")
        self.assertFalse(self.evidence()["observation_recorded"])

    def test_executive_report_isolated(self):
        _, out, logs, _ = self.evidence_run(candidate_requests(sentinel="RESP-SENTINEL"))
        html = visible(self.report_html())
        for value in VALUES.values():
            self.assertNotIn(f"{value:,}", html)
        for term in DEVELOPER_TERMS:
            self.assertNotIn(term, html)
        self.assertIn('data-view="applications">Applications<span class="nav-tag">Pending</span>', html)
        self.assertIn(">LIVE</span></dd>", html.split('<dl class="meta">')[1])
        self.assert_no_secrets_anywhere(out, logs)
        files = [p for p in self.root.rglob("*") if p.is_file()]
        for content in [p.read_bytes().decode("utf-8", errors="ignore") for p in files] + [out, logs]:
            for forbidden in ("RESP-SENTINEL", self.request_sentinel, self.response_sentinel,
                              "FILTER-MEMBER-SENTINEL", "Count(["):
                self.assertNotIn(forbidden, content)


class HeaderMainTests(PassiveMetricsHarness):
    def header(self):
        return self.report_html().split('<dl class="meta">')[1].split("</dl>")[0]

    def test_live_partial_stale_not_available_end_to_end(self):
        self.run_pipeline(700_000, self.ce_widget(ce=700_000, pct=0.9), at=RUN_TIME - timedelta(hours=3))
        self.assertIn(">LIVE</span></dd>", self.header())
        self.run_pipeline(700_100, at=RUN_TIME - timedelta(hours=2))
        self.assertIn(">PARTIAL</span></dd>", self.header())
        self.assertIn("banner-partial", self.report_html())
        self.run_pipeline(None, responses=[FakeResponse(401, "{}")])
        self.assertIn(">STALE</span></dd>", self.header())
        self.assertIn(">ERROR</span>", self.header())

    def test_not_available_without_history(self):
        self.run_pipeline(None, responses=[FakeResponse(401, "{}")])
        self.assertIn(">NOT AVAILABLE</span></dd>", self.header())
