"""Phase 3G-A: production promotion of five STABLE Wave 1 metrics (fakes only, invented values)."""

import json
import re
from datetime import timedelta

from history.store import SnapshotStore
from metrics.catalog import METRICS, PROMOTION_CANDIDATES, ROADMAP
from metrics.fingerprint_evidence import OBSERVATIONS_FILE, SCHEMA_VERSION, approved_sources, bind_approved_source
from metrics.passive_metrics import PM_EXTRACTED, extract_passive_metric
from sisense.passive import definition_fingerprint, split_reference
from tests.passive_fixtures import PassiveRequest
from tests.test_passive_metrics import observe
from tests.test_phase3f_b import (
    CANDIDATE,
    STARTED_MEASURE,
    SUBMITTED_MEASURE,
    VALUES,
    candidate_requests,
    nap_payload,
    started_payload,
    submitted_payload,
)
from tests.test_phase3f_d import applications_view, section, visible
from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_pipeline_main import RUN_TIME
from tests.test_report_consistency import VALIDATION_ONLY
from tests.test_session_context import FakeResponse
from validation.validators import result_measure_titles

PROMOTED = (
    "applications_started", "submitted_applications", "new_enrolled", "active_enrolled", "passive_enrolled",
)
INVENTED = {"applications_started": 411_001, "submitted_applications": 411_002, "total_applicants": 411_003,
            "new_enrolled": 4_111, "active_enrolled": 41_112, "passive_enrolled": 411_113}


def titles_for(payload):
    return list(result_measure_titles(payload))


def record_for(payloads):
    candidates = {}
    for metric_id, (body, title) in payloads.items():
        candidates[metric_id] = {
            "widget_id": split_reference(body["widget"])[0],
            "measure_titles": titles_for(body),
            "bound_measure_title": title,
            "fingerprint": definition_fingerprint(body),
            "extraction_status": "VALUE_EXTRACTED",
        }
    return candidates


class Wave1PromotionTests(PassiveMetricsHarness):
    def payloads(self, guid="GUID-A"):
        nap = nap_payload(query_guid=guid)
        return {
            "applications_started": (started_payload(query_guid=guid), STARTED_MEASURE),
            "submitted_applications": (submitted_payload(query_guid=guid), SUBMITTED_MEASURE),
            "new_enrolled": (nap, "NEW"),
            "active_enrolled": (nap, "ACTIVE"),
            "passive_enrolled": (nap, "PASSIVE"),
        }

    def seed_stable(self, guid="GUID-A", payloads=None):
        candidates = record_for(payloads or self.payloads(guid))
        document = {
            "schema_version": SCHEMA_VERSION,
            "observations": [
                {"observed_at_utc": "2026-11-10T12:00:00Z", "candidates": candidates},
                {"observed_at_utc": "2026-11-10T13:00:00Z", "candidates": candidates},
            ],
        }
        path = self.db.parent / OBSERVATIONS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def live_run(self, values=None, **kw):
        self.seed_stable()
        v = {**INVENTED, **(values or {})}
        return self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434),
                                 *candidate_requests(values=v), **kw)

    def test_exactly_five_approved_candidates_are_promoted(self):
        self.assertEqual(list(METRICS), [
            "currently_enrolled_people", "percent_effectuated", "applications_started", "submitted_applications",
            "new_enrolled", "active_enrolled", "passive_enrolled",
        ])
        self.assertEqual(set(PROMOTION_CANDIDATES), {"total_applicants"})
        for metric_id in PROMOTED:
            source = METRICS[metric_id].passive_source
            self.assertTrue(source.approved_from_observations)
            self.assertIsNone(source.expected_fingerprint)

    def test_total_applicants_cancelled_terminated_metal_tier_remain_pending(self):
        self.live_run()
        html = self.report_html()
        chips = self.pending_chips()
        self.assertIn("Total Applicants", chips)
        self.assertIn("Enrolled & Cancelled", chips)
        self.assertIn("Cancelled — Not Enrolled", chips)
        self.assertIn("Cancelled Policies — Daily", chips)
        self.assertIn("Terminated Policies — Daily", chips)
        self.assertNotIn("Cancelled", chips)
        self.assertNotIn("Terminated", chips)
        self.assertIn("Metal Tier", chips)
        self.assertNotIn("data-metric=\"total_applicants\"", html)
        pending_names = {item.name for item in ROADMAP}
        self.assertTrue({"Total Applicants", "Enrolled & Cancelled", "Cancelled — Not Enrolled",
                         "Cancelled Policies — Daily", "Terminated Policies — Daily", "Metal Tier"}
                        <= pending_names)

    def test_successful_current_values_render_source_live(self):
        self.live_run()
        html = self.report_html()
        expected = (
            ("applications_started", "411,001"),
            ("submitted_applications", "411,002"),
            ("new_enrolled", "4,111"),
            ("active_enrolled", "41,112"),
            ("passive_enrolled", "411,113"),
        )
        for metric_id, shown in expected:
            card = self.card(metric_id)
            self.assertIn(shown, card)
            self.assertIn('class="badge badge-live">LIVE</span>', card)
            self.assertIn('class="badge badge-source">SOURCE</span>', card)
            self.assertNotIn("PENDING", card)
        apps = applications_view(html)
        self.assertLess(apps.index("411,001"), apps.index("411,002"))
        self.assertLess(apps.index("411,002"), apps.index("Total Applicants"))
        status = section(html, "enrollment_status")
        self.assertIn('data-metric="new_enrolled"', status)
        self.assertIn('data-metric="active_enrolled"', status)
        self.assertIn('data-metric="passive_enrolled"', status)

    def test_insufficient_history_does_not_suppress_current_source_value(self):
        self.live_run()
        card = self.card("applications_started")
        self.assertIn("411,001", card)
        self.assertIn("LIVE", card)
        self.assertIn("SOURCE", card)
        self.assertIn("Insufficient history", card)

    def test_new_active_passive_bound_by_exact_measure_titles_not_position(self):
        body = nap_payload(order=("PASSIVE", "NEW", "ACTIVE"), query_guid="GUID-A")
        self.seed_stable(payloads={
            "applications_started": (started_payload(query_guid="GUID-A"), STARTED_MEASURE),
            "submitted_applications": (submitted_payload(query_guid="GUID-A"), SUBMITTED_MEASURE),
            "new_enrolled": (body, "NEW"),
            "active_enrolled": (body, "ACTIVE"),
            "passive_enrolled": (body, "PASSIVE"),
        })
        request = PassiveRequest(body, text=json.dumps({"values": [{"data": 30}, {"data": 10}, {"data": 20}]}))
        started = PassiveRequest(started_payload(), text=json.dumps({"values": [{"data": 411_001}]}))
        submitted = PassiveRequest(submitted_payload(), text=json.dumps({"values": [{"data": 411_002}]}))
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), started, submitted, request)
        new_value = self.card("new_enrolled").split("metric-value")[1].split("</div>")[0]
        active_value = self.card("active_enrolled").split("metric-value")[1].split("</div>")[0]
        passive_value = self.card("passive_enrolled").split("metric-value")[1].split("</div>")[0]
        self.assertIn("10", new_value)
        self.assertNotIn("30", new_value)
        self.assertIn("20", active_value)
        self.assertIn("30", passive_value)

    def test_no_positional_fallback(self):
        self.seed_stable()
        body = nap_payload()
        request = PassiveRequest(body, text=json.dumps({"values": [{"data": 1}, {"data": 2}]}))
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), request)
        for metric_id in ("new_enrolled", "active_enrolled", "passive_enrolled"):
            card = self.card(metric_id)
            self.assertIn("NOT AVAILABLE", card)
            self.assertNotIn(">1</div>", card)
            self.assertNotIn(">2</div>", card)

    def test_validation_only_values_cannot_leak_into_production_html(self):
        self.live_run()
        executive = visible(self.report_html())
        for value in VALIDATION_ONLY.values():
            self.assertNotIn(f"{value:,}", executive)
        self.assertNotIn("566,051", executive)
        self.assertNotIn("565,200", executive)
        self.assertNotIn("2,510", executive)
        self.assertNotIn("35,266", executive)
        self.assertNotIn("756,009", executive)
        self.assertNotIn(f"{VALUES['total_applicants']:,}", executive)

    def test_each_promoted_metric_has_isolated_history(self):
        self.live_run(at=RUN_TIME - timedelta(hours=3))
        self.live_run(values={"applications_started": 411_501, "submitted_applications": 411_802,
                              "new_enrolled": 4_201, "active_enrolled": 41_202, "passive_enrolled": 411_203})
        with SnapshotStore(self.db) as store:
            self.assertEqual(store.snapshot_count("currently_enrolled_people", 2027), 2)
            self.assertEqual(store.snapshot_count("percent_effectuated", 2027), 2)
            self.assertEqual([s.metric_value for s in store.snapshots("applications_started", 2027)],
                             [411_001, 411_501])
            self.assertEqual([s.metric_value for s in store.snapshots("submitted_applications", 2027)],
                             [411_002, 411_802])
            self.assertEqual([s.metric_value for s in store.snapshots("new_enrolled", 2027)], [4_111, 4_201])
            self.assertEqual(store.snapshot_count("total_applicants", 2027), 0)
        self.assertIn("411,501", self.card("applications_started"))
        self.assertIn("+500", self.card("applications_started"))
        self.assertNotIn("+500", self.card("submitted_applications"))

    def test_currently_enrolled_and_percent_effectuated_unchanged(self):
        self.live_run()
        kpi = re.search(r'<section class="card kpi">(.*?)</section>', self.report_html(), re.S).group(1)
        self.assertIn("700,100", kpi)
        self.assertIn("LIVE", kpi)
        self.assertIn("SOURCE", kpi)
        card = self.card("percent_effectuated")
        self.assertIn("90.44%", card)
        self.assertIn("LIVE", card)
        self.assertEqual(self.rows("currently_enrolled_people"), [700_100])
        self.assertEqual(self.rows("percent_effectuated"), [0.904434])

    def test_missing_stable_evidence_fails_closed_not_pending(self):
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), *candidate_requests(values=INVENTED))
        for metric_id in PROMOTED:
            card = self.card(metric_id)
            self.assertIn("NOT AVAILABLE", card)
            self.assertNotIn("PENDING", card)
            self.assertNotIn("411,001", card)
        self.assertEqual(self.rows("applications_started"), [])
        self.assertIn("90.44%", self.card("percent_effectuated"))
        self.assertIn("LIVE", self.card("percent_effectuated"))

    def test_fingerprint_mismatch_fails_closed(self):
        self.seed_stable()
        other = started_payload()
        other["metadata"][-1]["jaql"]["filter"] = {"members": ["OTHER-SCOPE"]}
        request = PassiveRequest(other, text=json.dumps({"values": [{"data": 411_001}]}))
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), request,
                          *candidate_requests(values=INVENTED, drop=("applications_started",)))
        self.assertIn("NOT AVAILABLE", self.card("applications_started"))
        self.assertNotIn("411,001", self.card("applications_started"))
        self.assertIn("411,002", self.card("submitted_applications"))

    def test_later_candidate_only_observation_keeps_stable_promoted_evidence(self):
        path = self.seed_stable()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["observations"].append({
            "observed_at_utc": "2026-11-10T14:00:00Z",
            "candidates": {"total_applicants": {
                "widget_id": "6a98048ccef60720fe7f9b4f",
                "measure_titles": ["Total Applicants"],
                "bound_measure_title": "Total Applicants",
                "fingerprint": "sha256:not-a-real-hash",
                "extraction_status": "VALUE_EXTRACTED",
            }},
        })
        path.write_text(json.dumps(document), encoding="utf-8")
        self.run_pipeline(700_100, self.ce_widget(ce=700_100, pct=0.904434), *candidate_requests(values=INVENTED))
        self.assertIn("411,001", self.card("applications_started"))
        self.assertIn("LIVE", self.card("applications_started"))
        self.assertIn("4,111", self.card("new_enrolled"))

    def test_bind_approved_source_uses_logged_measure_title(self):
        path = self.seed_stable()
        approved = approved_sources(path, PROMOTED)
        started = bind_approved_source(METRICS["applications_started"], approved["applications_started"])
        self.assertEqual(started.passive_source.measure_title, STARTED_MEASURE)
        self.assertEqual(started.passive_source.expected_fingerprint,
                         definition_fingerprint(started_payload(query_guid="GUID-A")))
        observer = observe(PassiveRequest(started_payload(query_guid="GUID-A"),
                                          text=json.dumps({"values": [{"data": 9}]})))
        out = extract_passive_metric(started, observer)
        self.assertEqual((out.status, out.value), (PM_EXTRACTED, 9))
        self.assertEqual(CANDIDATE["new_enrolled"].widget_id, METRICS["new_enrolled"].passive_source.widget_id)
        self.assertEqual(METRICS["new_enrolled"].passive_source.measure_title, "NEW")
        self.assertEqual(METRICS["active_enrolled"].passive_source.measure_title, "ACTIVE")
        self.assertEqual(METRICS["passive_enrolled"].passive_source.measure_title, "PASSIVE")
