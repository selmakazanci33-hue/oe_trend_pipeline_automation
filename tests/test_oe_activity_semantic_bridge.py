"""Phase 6C OE activity semantic bridge on synthetic evidence (no database, no Sisense, no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from comparison.oe_activity_semantic_bridge import (
    APPLICANT,
    APPLICATION,
    APPLICATION_CREATED,
    APPLICATION_SUBMITTED,
    BULK_LOAD_FLAG,
    CAPTURED,
    CURRENT_POPULATION,
    CURRENT_STATE,
    CUTOFFS,
    DATE_MISMATCH,
    DIAGNOSTIC,
    ENROLLMENT_CONFIRMED,
    ENROLLMENT_CREATED,
    GRAIN_MISMATCH,
    INSUFFICIENT,
    LIFECYCLE_CLASSIFICATION,
    LOCKED_PHASE_6B_PEOPLE,
    NEW_UNRESOLVED,
    NO_BRIDGE,
    NONE,
    OBSERVED_NOT_OFFICIAL,
    OE_ANCHOR_UNPROVEN,
    PERSON,
    POLICY,
    PY2026_CONFIRM,
    PY2026_CREATE,
    STRONG,
    TITLE_ONLY,
    UNKNOWN,
    UNKNOWN_EVENT,
    USER_REPORTED_UNVERIFIED,
    MetricEvidence,
    align,
    assess_metric,
    build_report,
    load_definition,
    render_html,
    write_report,
)


def metric(metric_id="m", grain=POLICY, event=ENROLLMENT_CREATED, evidence=CAPTURED,
           date_field="enrollment.created_ts", identifier="enrollment.id"):
    return MetricEvidence(metric_id, metric_id.title(), f"w-{metric_id}", None, grain, CAPTURED, identifier,
                          date_field, "days", "event axis", event, evidence, "YES")


def statuses(item):
    return {cell["py2026_field"]: cell["alignment_status"] for cell in item["per_field"]}


def walk(value, key):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, key)


def real_report(phase_6b=None):
    return build_report(load_definition(), phase_6b)


class GrainTests(unittest.TestCase):
    def test_application_grain_cannot_map_to_enrollment_grain(self):
        for grain in (APPLICATION, APPLICANT):
            for event in (ENROLLMENT_CREATED, ENROLLMENT_CONFIRMED, APPLICATION_CREATED):
                item = assess_metric(metric(grain=grain, event=event), identifier_equivalence_proven=True)
                self.assertEqual(set(statuses(item).values()), {GRAIN_MISMATCH})
                self.assertEqual(item["py2026_candidate"], NONE)

    def test_real_application_metrics_rejected(self):
        report = real_report()
        by_id = {item["metric_id"]: item for item in report["py2027_candidate_metrics"]}
        self.assertEqual(by_id["applications_started"]["event_meaning"], APPLICATION_CREATED)
        self.assertEqual(by_id["submitted_applications"]["event_meaning"], APPLICATION_SUBMITTED)
        for metric_id in ("applications_started", "submitted_applications", "total_applicants"):
            self.assertEqual(by_id[metric_id]["alignment_status"], GRAIN_MISMATCH)
            self.assertEqual(by_id[metric_id]["py2026_candidate"], NONE)

    def test_unknown_grain_is_insufficient(self):
        self.assertEqual(align(metric(grain=UNKNOWN), PY2026_CREATE)["alignment_status"], INSUFFICIENT)


class SemanticTests(unittest.TestCase):
    def test_current_state_cannot_map_to_activity(self):
        item = assess_metric(metric(grain=PERSON, event=CURRENT_POPULATION), identifier_equivalence_proven=True)
        self.assertEqual(set(statuses(item).values()), {CURRENT_STATE})
        by_id = {m["metric_id"]: m for m in real_report()["py2027_candidate_metrics"]}
        self.assertEqual(by_id["currently_enrolled"]["alignment_status"], CURRENT_STATE)
        self.assertEqual(by_id["percent_effectuated"]["alignment_status"], CURRENT_STATE)

    def test_date_name_similarity_is_not_equivalence(self):
        named = metric(event=ENROLLMENT_CREATED, evidence=TITLE_ONLY, date_field="enrollment.enrollment_create_date")
        self.assertEqual(set(statuses(assess_metric(named, True)).values()), {INSUFFICIENT})
        unknown = metric(event=UNKNOWN_EVENT, evidence=CAPTURED, date_field="enrollment.confirmation_date")
        self.assertEqual(set(statuses(assess_metric(unknown, True)).values()), {INSUFFICIENT})

    def test_event_meaning_must_match_field(self):
        captured = assess_metric(metric(event=ENROLLMENT_CREATED))
        self.assertEqual(statuses(captured), {PY2026_CREATE: DIAGNOSTIC, PY2026_CONFIRM: DATE_MISMATCH})
        self.assertEqual(captured["py2026_candidate"], PY2026_CREATE)
        proven = assess_metric(metric(event=ENROLLMENT_CONFIRMED), identifier_equivalence_proven=True)
        self.assertEqual(statuses(proven), {PY2026_CREATE: DATE_MISMATCH, PY2026_CONFIRM: STRONG})
        self.assertFalse(proven["production_comparable"])

    def test_last_update_activity_is_date_mismatch(self):
        by_id = {m["metric_id"]: m for m in real_report()["py2027_candidate_metrics"]}
        for metric_id in ("cancelled_policies_daily", "terminated_policies_daily"):
            self.assertEqual(by_id[metric_id]["grain"], POLICY)
            self.assertEqual(by_id[metric_id]["alignment_status"], DATE_MISMATCH)


class NewMetricTests(unittest.TestCase):
    def test_new_remains_unresolved(self):
        report = real_report()
        new = report["new_metric_assessment"]
        self.assertEqual(new["status"], NEW_UNRESOLVED)
        self.assertEqual(new["alignment_status"], INSUFFICIENT)
        self.assertEqual(new["user_reported_status"], USER_REPORTED_UNVERIFIED)
        self.assertIsNone(new["captured"]["date_field"])
        self.assertIsNone(new["captured"]["identifier"])
        self.assertEqual(new["py2026_formula"], "Not implemented in Phase 6C.")
        self.assertTrue(report["phase_6c_decision"]["targeted_sisense_validation_justified"])

    def test_lifecycle_needs_exact_semantics(self):
        lifecycle = metric(grain=PERSON, event=LIFECYCLE_CLASSIFICATION, evidence=CAPTURED,
                           date_field="ssap_applications.creation_timestamp")
        self.assertEqual(set(statuses(assess_metric(lifecycle, True)).values()), {INSUFFICIENT})
        application_dated = metric(grain=PERSON, event=APPLICATION_CREATED, evidence=CAPTURED)
        self.assertEqual(set(statuses(assess_metric(application_dated, True)).values()), {DATE_MISMATCH})


class RiskAndAnchorTests(unittest.TestCase):
    def test_october_bulk_load_risk_visible(self):
        risk = real_report()["october_bulk_load_risk"]
        self.assertEqual(risk["flag"], BULK_LOAD_FLAG)
        self.assertFalse(risk["explanation_proven"])
        self.assertAlmostEqual(risk["evidence"][PY2026_CREATE]["share_present_by_first_cutoff"], 0.8847, places=4)
        self.assertIn(BULK_LOAD_FLAG, render_html(real_report()))

    def test_official_anchor_unproven(self):
        anchor = real_report()["oe_anchor_status"]["official_oe_anchor"]
        self.assertEqual(anchor["status"], OE_ANCHOR_UNPROVEN)
        self.assertIsNone(anchor["PY2026"])
        self.assertIsNone(anchor["PY2027"])

    def test_observed_window_separate_from_official_anchor(self):
        status = real_report()["oe_anchor_status"]
        observed = status["observed_dashboard_activity_window"]
        self.assertEqual(observed["status"], OBSERVED_NOT_OFFICIAL)
        self.assertNotEqual(observed["status"], status["official_oe_anchor"]["status"])
        self.assertNotIn(observed["observed_from"], json.dumps(status["official_oe_anchor"]))
        self.assertIn("not an OE start", observed["evidence"])
        self.assertEqual(status["operational_comparison_window"]["status"], "CALENDAR_SHIFT_ONLY")
        for item in status["dashboard_scope_date_filters"]:
            self.assertIn("not an OE anchor", item["role"])


class EvidenceAndDecisionTests(unittest.TestCase):
    def test_locked_values_and_phase_6b_report_match(self):
        fields = {name: {"cumulative": [{"through": c, "distinct_people": v} for c, v in zip(CUTOFFS, values)],
                         "monthly": [{"month": "2025-10", "row_count": 900000}]}
                  for name, values in LOCKED_PHASE_6B_PEOPLE.items()}
        report = real_report({"oe_aligned_temporal_diagnostics": {"fields": fields}})
        evidence = report["py2026_activity_evidence"]["fields"]
        self.assertTrue(evidence[PY2026_CREATE]["phase_6b_report_match"])
        self.assertEqual(evidence[PY2026_CREATE]["october_2025"]["row_count"], 900000)
        self.assertEqual(evidence[PY2026_CREATE]["cumulative_people"][1]["increment_people"], 1050525 - 1037238)
        fields[PY2026_CONFIRM]["cumulative"][0]["distinct_people"] = 1
        report = real_report({"oe_aligned_temporal_diagnostics": {"fields": fields}})
        self.assertFalse(report["py2026_activity_evidence"]["fields"][PY2026_CONFIRM]["phase_6b_report_match"])
        self.assertIsNone(real_report()["py2026_activity_evidence"]["fields"][PY2026_CREATE]["phase_6b_report_match"])

    def test_no_bridge_from_captured_evidence(self):
        report = real_report()
        self.assertEqual(report["candidate_cross_year_bridges"], [])
        self.assertEqual(report["phase_6c_decision"]["status"], NO_BRIDGE)
        self.assertIsNone(report["phase_6c_decision"]["strongest_bridge"])

    def test_synthetic_bridge_stays_diagnostic(self):
        report = build_report(None, None, metrics=[metric(event=ENROLLMENT_CONFIRMED)])
        decision = report["phase_6c_decision"]
        self.assertEqual(decision["strongest_bridge"]["py2026_field"], PY2026_CONFIRM)
        self.assertEqual(decision["strongest_bridge"]["alignment_status"], DIAGNOSTIC)
        self.assertFalse(decision["targeted_sisense_validation_justified"])

    def test_production_comparable_false(self):
        for report in (real_report(), build_report(None, None, metrics=[metric()])):
            flags = list(walk(report, "production_comparable"))
            self.assertGreater(len(flags), 10)
            self.assertTrue(all(flag is False for flag in flags))

    def test_report_sections_and_write(self):
        report = real_report()
        page = render_html(report)
        for section in ("PY2026 Activity Evidence", "PY2027 Candidate Metrics", "Grain Comparison",
                        "Date Semantic Comparison", "New Metric Assessment", "October Bulk-Load Risk",
                        "OE Anchor Status", "Candidate Cross-Year Bridges", "Rejected Bridges",
                        "Phase 6C Decision"):
            self.assertIn(f"<h2>{section}</h2>", page)
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(report, Path(tmp))
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["phase"], "6C")
            self.assertTrue(html_path.exists())


if __name__ == "__main__":
    unittest.main()
