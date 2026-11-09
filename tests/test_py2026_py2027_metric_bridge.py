"""Phase 6A metric semantic bridge: classification and report generation on synthetic evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from comparison.currently_enrolled import ComparisonOutputError, guard_no_pii
from comparison.py2026_py2027_metric_bridge import (
    BLOCKED_EVENT_HISTORY,
    BLOCKED_IDENTIFIER,
    BLOCKED_LIFECYCLE,
    CLASSIFICATIONS,
    DIAGNOSTIC,
    HTML_NAME,
    INSUFFICIENT,
    JSON_NAME,
    PROVEN_FIELDS,
    READY,
    WRONG_SOURCE_FAMILY,
    BridgeSpec,
    build_report,
    classify,
    load_definition,
    render_html,
    write_report,
)

PERSON = {"table": "enrollee", "column": "exchg_indiv_identifier",
          "dim": "[enrollee.exchg_indiv_identifier]", "datatype": "text"}


def status_ctx(members):
    return {"table": "enrollment", "column": "enrollment_status_description",
            "dim": "[enrollment.enrollment_status_description]", "filter": {"members": members}}


def scope(table, column, **filt):
    return {"panel": "scope", "jaql": {"table": table, "column": column, "title": column, "filter": filt}}


def payload(formula="(Count([P]),[N])/(Count([P]),[D])"):
    return {
        "widget": "w1;Currently Enrolled People", "dashboard": "d1;Dash", "datasource": {"title": "MAIN"},
        "metadata": [
            {"jaql": {"type": "measure", "title": "Currently Enrolled People", "formula": "Count([P])",
                      "context": {"[P]": PERSON}}},
            {"jaql": {"type": "measure", "title": "% Effectuated", "formula": formula,
                      "context": {"[P]": PERSON, "[N]": status_ctx(["S1", "S3"]),
                                  "[D]": status_ctx(["S1", "S2", "S3"])}}},
            scope("enrollment", "enrollment_status_description", members=["S1", "S2", "S3"],
                  filter={"exclude": {"members": ["S3"]}}),
            scope("enrollee", "enrollee_status_description", members=["S1", "S2", "S3"],
                  filter={"exclude": {"members": ["S3"]}}),
            scope("enrollment", "benefit_effective_date", **{"from": "2026-10-01", "to": "2027-12-31"}),
            scope("enrollee", "person_type_description", members=["P1"]),
            scope("ssap_applications", "coverage_year", members=[2027]),
            scope("enrollment", "insurance_type_description", members=["H1"]),
        ],
    }


def definition(data=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "payload.json"
        path.write_text(json.dumps(data or payload()), encoding="utf-8")
        return load_definition(path)


def rows(report):
    return {row["metric_id"]: row for row in report["metric_semantic_matrix"]}


BASE = BridgeSpec(
    "x", "X", "enrollment population", "w", None, "person", "sisense.person", "indicator", (),
    ("coverage_year", "enrollee_id"), "person", "COUNT(DISTINCT enrollee_id)",
    identifier_candidate="enrollee_id", identifier_proven=True,
)


class ClassificationTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(classify(BASE), READY)
        self.assertEqual(classify(replace(BASE, gaps=("period",))), DIAGNOSTIC)
        self.assertEqual(classify(replace(BASE, unmapped_filters=("enrollee.person_type_description",))), DIAGNOSTIC)
        self.assertEqual(classify(replace(BASE, identifier_proven=False)), BLOCKED_IDENTIFIER)
        self.assertEqual(classify(replace(BASE, lifecycle_rules_captured=False, identifier_proven=False)),
                         BLOCKED_LIFECYCLE)
        self.assertEqual(classify(replace(BASE, requires_event_history=True, lifecycle_rules_captured=False)),
                         BLOCKED_EVENT_HISTORY)
        self.assertEqual(classify(replace(BASE, source_family_supported=False, requires_event_history=True)),
                         WRONG_SOURCE_FAMILY)

    def test_unproven_fields_or_semantics_are_insufficient(self):
        self.assertEqual(classify(replace(BASE, db_fields=("person_type_description",))), INSUFFICIENT)
        self.assertEqual(classify(replace(BASE, sisense_grain=None)), INSUFFICIENT)
        self.assertEqual(classify(replace(BASE, identifier_candidate="exchg_indiv_identifier")), INSUFFICIENT)
        self.assertEqual(classify(BASE, proven_fields=("coverage_year",)), INSUFFICIENT)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = build_report(definition())
        self.rows = rows(self.report)

    def test_expected_classifications(self):
        expected = {
            "currently_enrolled": BLOCKED_IDENTIFIER, "percent_effectuated": BLOCKED_IDENTIFIER,
            "new_enrolled": BLOCKED_LIFECYCLE, "active_enrolled": BLOCKED_LIFECYCLE,
            "passive_enrolled": BLOCKED_LIFECYCLE,
            "cancelled_policies_daily": BLOCKED_EVENT_HISTORY, "terminated_policies_daily": BLOCKED_EVENT_HISTORY,
            "applications_started": WRONG_SOURCE_FAMILY, "submitted_applications": WRONG_SOURCE_FAMILY,
            "total_applicants": WRONG_SOURCE_FAMILY,
        }
        self.assertEqual({key: row["classification"] for key, row in self.rows.items()}, expected)
        self.assertTrue(all(row["classification"] in CLASSIFICATIONS for row in self.rows.values()))

    def test_nothing_is_production_comparable(self):
        self.assertFalse(self.report["production_comparable"])
        self.assertTrue(all(row["production_comparable"] is False for row in self.rows.values()))
        self.assertTrue(all(c["production_comparable"] is False for c in self.report["currently_enrolled_candidates"]))
        split = self.report["cancellation_termination"]
        self.assertFalse(split["activity"]["production_comparable"])
        self.assertTrue(all(p["production_comparable"] is False for p in split["population_state"]))
        self.assertIn("production_comparable = false", render_html(self.report))

    def test_currently_enrolled_candidates_come_from_payload(self):
        candidates = {c["candidate_id"]: c for c in self.report["currently_enrolled_candidates"]}
        self.assertEqual(sorted(candidates), ["CE1", "CE2", "CE3", "CE4"])
        base = "coverage_year = 2026 AND benefit_effective_date BETWEEN '2025-10-01' AND '2026-12-31'"
        self.assertTrue(all(base in c["formula"] for c in candidates.values()))
        self.assertNotIn("status", candidates["CE1"]["formula"])
        self.assertIn("enrollee_status_description IN ('S1', 'S2')", candidates["CE2"]["formula"])
        self.assertIn("enrollee_status_description IN ('S1', 'S2', 'S3')", candidates["CE3"]["formula"])
        self.assertIn("enrollment_status_description IN ('S1', 'S2')", candidates["CE4"]["formula"])
        self.assertEqual(candidates["CE1"]["not_applicable_filters"],
                         ["enrollee.person_type_description in ['P1']",
                          "enrollment.insurance_type_description in ['H1']"])
        self.assertTrue(all(c["formula"].startswith("COUNT(DISTINCT enrollee_id)") for c in candidates.values()))
        self.assertFalse(any(key in c for c in candidates.values() for key in ("winner", "selected", "rank")))

    def test_identifier_and_filter_gaps_stated(self):
        ce = self.rows["currently_enrolled"]
        self.assertEqual(ce["py2027_identifier"], "enrollee.exchg_indiv_identifier")
        gaps = " ".join(ce["semantic_gaps"])
        self.assertIn("enrollee_id is a proxy", gaps)
        self.assertIn("person_type_description has no proven PY2026 field", gaps)
        self.assertIn("insurance_type_description has no proven PY2026 field", gaps)
        self.assertIn("Lifecycle point", gaps)
        self.assertTrue(set(ce["available_py2026_db_fields"]) <= set(PROVEN_FIELDS))

    def test_percent_effectuated_follows_formula_order(self):
        formula = self.rows["percent_effectuated"]["candidate_formula"]
        self.assertTrue(formula.startswith(
            "COUNT(DISTINCT enrollee_id WHERE enrollment_status_description IN ('S1', 'S3')) / "
            "COUNT(DISTINCT enrollee_id WHERE enrollment_status_description IN ('S1', 'S2', 'S3'))"))
        swapped = rows(build_report(definition(payload("(Count([P]),[D])/(Count([P]),[N])"))))
        self.assertTrue(swapped["percent_effectuated"]["candidate_formula"].startswith(
            "COUNT(DISTINCT enrollee_id WHERE enrollment_status_description IN ('S1', 'S2', 'S3')) /"))
        self.assertNotEqual(self.rows["percent_effectuated"]["family"], "application")

    def test_cancellation_population_separate_from_activity(self):
        split = self.report["cancellation_termination"]
        self.assertEqual(split["activity"]["classification"], BLOCKED_EVENT_HISTORY)
        formulas = [p["formula"] for p in split["population_state"]]
        self.assertEqual(formulas, [
            "COUNT(DISTINCT enrollment_id) WHERE coverage_year = 2026 AND enrollment_status_description = 'Cancelled'",
            "COUNT(DISTINCT enrollment_id) WHERE coverage_year = 2026 AND enrollment_status_description LIKE 'Term%'",
        ])
        self.assertTrue(all(p["classification"] == DIAGNOSTIC for p in split["population_state"]))
        self.assertIsNone(self.rows["cancelled_policies_daily"]["candidate_formula"])

    def test_application_metrics_wrong_family_without_formulas(self):
        for key in ("applications_started", "submitted_applications", "total_applicants"):
            self.assertIsNone(self.rows[key]["candidate_formula"])
            self.assertEqual(self.rows[key]["available_py2026_db_fields"], [])
        assessed = {a["metric"]: a["classification"] for a in self.report["application_source_family"]}
        self.assertEqual(assessed["% Effectuated"], BLOCKED_IDENTIFIER)

    def test_ready_and_blocked_sections(self):
        ready = self.report["ready_for_next_validation"]
        self.assertEqual(ready["ready_for_aggregate_validation"], [])
        self.assertEqual(ready["phase_6b_diagnostic_validation"], ["Currently Enrolled", "% Effectuated"])
        self.assertEqual(len(self.report["blocked_metrics"]), 10)
        self.assertIn("CE1, CE2, CE3, CE4", " ".join(self.report["recommended_phase_6b"]["steps"]))

    def test_missing_payload_is_insufficient_not_guessed(self):
        report = build_report(None)
        missing = rows(report)
        self.assertEqual(missing["currently_enrolled"]["classification"], INSUFFICIENT)
        self.assertEqual(missing["percent_effectuated"]["classification"], INSUFFICIENT)
        self.assertEqual(report["currently_enrolled_candidates"], [])
        self.assertEqual(report["ready_for_next_validation"]["phase_6b_diagnostic_validation"], [])
        self.assertFalse(report["verified_payload_loaded"])
        self.assertEqual(missing["new_enrolled"]["classification"], BLOCKED_LIFECYCLE)
        self.assertIn("Verified payload not loaded", render_html(report))

    def test_report_files_and_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = write_report(self.report, Path(tmp))
            self.assertEqual((json_path.name, html_path.name), (JSON_NAME, HTML_NAME))
            page = html_path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["phase"], "6A")
        for section in ("Locked Sources", "Metric Semantic Matrix", "Currently Enrolled Candidate Definitions",
                        "Cancellation/Termination Population vs Activity",
                        "Application Metric Source-Family Assessment", "Metrics Ready for Next Validation",
                        "Blocked Metrics", "Recommended Phase 6B"):
            self.assertIn(section, page)
        self.assertNotIn("2027", json.dumps(self.report["locked_sources"]["py2026"]))

    def test_no_identifier_fields_in_report(self):
        guard_no_pii(self.report)
        with self.assertRaises(ComparisonOutputError):
            guard_no_pii({"metric_semantic_matrix": [{"enrollee_id": 1}]})


if __name__ == "__main__":
    unittest.main()
