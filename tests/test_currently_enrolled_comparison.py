"""PY2026 vs PY2027 comparison gate. Local payload only — no database and no Sisense."""

import json
import tempfile
import unittest
from pathlib import Path

from comparison.currently_enrolled import (
    EXACT,
    FILTER_BLOCKED,
    NOT_AVAILABLE,
    PERIOD_UNRESOLVED,
    PERSON_MISMATCH,
    PERSON_UNRESOLVED,
    PY2026_PERIOD_UNRESOLVED,
    PY2026_SNAPSHOT_CANDIDATES,
    READY,
    UNCERTAIN,
    VERIFIED_PAYLOAD_FILE,
    WIDGET_ID,
    ComparisonOutputError,
    PeriodAssessment,
    apply_counts,
    build_report,
    classify_period,
    comparison_status,
    guard_no_pii,
    interpret_payload,
    map_person_identifier,
    percent_change,
    select_py2026_snapshot,
    translate_filters,
    write_report,
)

PROJECT = Path(__file__).resolve().parents[1]
JANUARY_SNAPSHOT = "dbo.Enrollments_PY2526_01312026"


def payload() -> dict:
    return json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))


def definition() -> dict:
    return interpret_payload(payload())


class PeriodInterpretationTests(unittest.TestCase):
    def test_verified_request_is_a_coverage_window_not_a_month(self):
        body = payload()
        parsed = interpret_payload(body)
        period = classify_period(parsed)
        coverage = next(item for item in parsed["scope_filters"] if item["column"] == "coverage_year")
        benefit = next(item for item in parsed["scope_filters"] if item["column"] == "benefit_effective_date")
        self.assertEqual(parsed["widget_id"], WIDGET_ID)
        self.assertEqual(parsed["dashboard_title"], "PY 2027 - OE Executive Dashboard")
        self.assertEqual(parsed["widget_type"], "indicator")
        self.assertIs(parsed["datasource_live"], False)
        self.assertEqual(parsed["measure"]["aggregation"], "Count")
        self.assertEqual(parsed["measure"]["fields"][0]["table"], "enrollee")
        self.assertEqual(parsed["measure"]["fields"][0]["column"], "exchg_indiv_identifier")
        self.assertEqual(coverage["table"], "ssap_applications")
        self.assertEqual(coverage["members"], ["2027"])
        self.assertEqual(benefit["level"], "days")
        self.assertEqual(benefit["from"], "2026-10-01")
        self.assertEqual(benefit["to"], "2027-12-31")
        self.assertEqual(period.status, PERIOD_UNRESOLVED)
        self.assertIsNone(period.equivalent_snapshot)
        self.assertTrue(any("2026-10-01" in item and "2027-12-31" in item for item in period.missing_evidence))
        self.assertTrue(any("as-of" in item for item in period.missing_evidence))
        self.assertTrue(any("turnedOff" in item for item in period.missing_evidence))
        self.assertNotIn("enrollment_confirmation_date", {item["column"] for item in parsed["scope_filters"]})
        self.assertNotIn("application_type", {item["column"] for item in parsed["scope_filters"]})
        self.assertNotIn("hios_issuer_id", {item["column"] for item in parsed["scope_filters"]})
        self.assertEqual(body, payload())

    def test_changed_window_stays_unresolved(self):
        body = payload()
        for item in body["metadata"]:
            jaql = item.get("jaql") or {}
            if jaql.get("column") == "benefit_effective_date":
                jaql["filter"]["to"] = "2027-06-30"
        period = classify_period(interpret_payload(body))
        self.assertEqual(period.status, PERIOD_UNRESOLVED)
        self.assertTrue(any("2027-06-30" in item for item in period.missing_evidence))
        self.assertIsNone(period.equivalent_snapshot)

    def test_secondary_measure_filters_are_separate_from_the_people_scope(self):
        parsed = definition()
        scope_status = next(
            item for item in parsed["scope_filters"] if item["column"] == "enrollment_status_description")
        self.assertEqual(scope_status["members"], ["Enrolled", "Pending", "Terminated"])
        self.assertTrue(parsed["secondary_measure_filters"])
        self.assertTrue(all(item["measure_title"] != "Currently Enrolled People"
                            for item in parsed["secondary_measure_filters"]))


class SnapshotAndMappingTests(unittest.TestCase):
    def test_filename_does_not_select_the_january_snapshot(self):
        period = classify_period(definition())
        selected = select_py2026_snapshot(period)
        self.assertIsNone(selected["selected"])
        self.assertIn(JANUARY_SNAPSHOT, PY2026_SNAPSHOT_CANDIDATES)
        self.assertIn(JANUARY_SNAPSHOT, {item["table"] for item in selected["candidates_not_selected"]})

    def test_unresolved_period_ignores_a_named_snapshot(self):
        period = PeriodAssessment(PERIOD_UNRESOLVED, "", (), (), JANUARY_SNAPSHOT)
        self.assertIsNone(select_py2026_snapshot(period)["selected"])

    def test_selector_returns_only_an_already_evidenced_snapshot(self):
        october = "dbo.Enrollments_PY2526_10312026"
        period = PeriodAssessment("RESOLVED", "", (), (), october)
        self.assertEqual(select_py2026_snapshot(period)["selected"], october)

    def test_enrollee_id_is_not_the_sisense_person_field(self):
        person = map_person_identifier(definition(), {"enrollee_id", "coverage_year"})
        self.assertEqual(person["sisense_column"], "exchg_indiv_identifier")
        self.assertEqual(person["py2026_db_column"], "enrollee_id")
        self.assertEqual(person["identity_status"], PERSON_MISMATCH)
        self.assertEqual(person["translation_status"], UNCERTAIN)

    def test_matching_person_column_is_exact(self):
        person = map_person_identifier(definition(), {"exchg_indiv_identifier"})
        self.assertEqual(person["translation_status"], EXACT)
        self.assertEqual(person["py2026_db_column"], "exchg_indiv_identifier")

    def test_filter_translation_keeps_ambiguous_and_missing_columns(self):
        columns = {
            "coverage_year", "enrollee_id", "enrollment_status_description",
            "enrollee_status_description", "hios_issuer_id",
        }
        rows = {row["sisense_concept"]: row for row in translate_filters(definition(), columns)}
        self.assertEqual(rows["Plan year"]["translation_status"], UNCERTAIN)
        self.assertEqual(rows["Benefit effective date"]["translation_status"], NOT_AVAILABLE)
        self.assertEqual(rows["Enrollment status"]["translation_status"], UNCERTAIN)
        self.assertEqual(rows["Enrollee status"]["translation_status"], UNCERTAIN)
        self.assertEqual(rows["Person type"]["translation_status"], NOT_AVAILABLE)
        self.assertEqual(rows["Insurance type"]["translation_status"], NOT_AVAILABLE)
        self.assertEqual(rows["Application type"]["translation_status"], EXACT)
        self.assertEqual(rows["Issuer"]["translation_status"], EXACT)
        self.assertFalse(rows["Application type"]["population_critical"])

    def test_explicit_member_filter_matches_when_the_column_exists(self):
        rows = {row["sisense_concept"]: row for row in translate_filters(definition(), {
            "person_type_description", "insurance_type_description",
        })}
        self.assertEqual(rows["Person type"]["translation_status"], EXACT)
        self.assertEqual(rows["Insurance type"]["translation_status"], EXACT)
        self.assertEqual(rows["Person type"]["py2026_db_column"], "person_type_description")


class GateAndArithmeticTests(unittest.TestCase):
    def _resolved(self, snapshot=None):
        return PeriodAssessment("RESOLVED", "", (), (), snapshot)

    def test_real_payload_stops_before_numbers(self):
        report = build_report(
            payload(),
            db_columns={"enrollee_id"},
            py2026_people=100,
            py2027_people=140,
            raw_row_count=900_000,
        )
        self.assertEqual(report["comparison_status"], PERIOD_UNRESOLVED)
        self.assertFalse(report["comparison_executed"])
        self.assertIsNone(report["py2026_currently_enrolled_people"])
        self.assertIsNone(report["py2027_currently_enrolled_people"])
        self.assertIsNone(report["absolute_difference"])
        self.assertIsNone(report["percent_change"])
        self.assertEqual(report["person_identifier"]["identity_status"], PERSON_MISMATCH)
        self.assertFalse(report["issuer_comparison_ready"])
        self.assertIsNone(report["issuer_dimensions"]["sisense_dimension"])
        self.assertEqual(report["refused_inputs"], ["raw_row_count", "py2026_people", "py2027_people"])
        text = json.dumps(report)
        self.assertNotIn("900000", text)
        self.assertNotIn("17769-998", text)
        self.assertNotIn(payload()["queryGuid"], text)

    def test_schema_is_not_read_when_the_period_stops_the_command_path(self):
        report = build_report(payload())
        self.assertEqual(report["person_identifier"]["identity_status"], PERSON_UNRESOLVED)
        self.assertIsNone(report["person_identifier"]["py2026_db_column"])
        self.assertTrue(all(
            row["translation_status"] == UNCERTAIN
            for row in report["filter_mapping"]
            if row["population_critical"]
        ))

    def test_later_gates_fail_closed_when_a_period_is_supplied(self):
        mismatch = {"identity_status": PERSON_MISMATCH, "translation_status": UNCERTAIN}
        exact = {"identity_status": EXACT, "translation_status": EXACT}
        blocked = [{"population_critical": True, "translation_status": UNCERTAIN}]
        clear = [{"population_critical": True, "translation_status": EXACT}]
        self.assertEqual(comparison_status(self._resolved(), mismatch, clear), PERSON_MISMATCH)
        self.assertEqual(comparison_status(self._resolved(), exact, blocked), FILTER_BLOCKED)
        self.assertEqual(
            comparison_status(self._resolved(None), exact, clear), PY2026_PERIOD_UNRESOLVED)
        self.assertEqual(
            comparison_status(self._resolved(JANUARY_SNAPSHOT), exact, clear), READY)

    def test_difference_and_percent(self):
        self.assertEqual(percent_change(100, 120), {
            "absolute_difference": 20, "percent_change": 20, "percent_status": "COMPUTED"})
        self.assertEqual(percent_change(200, 150)["absolute_difference"], -50)
        self.assertEqual(percent_change(200, 150)["percent_change"], -25)
        self.assertEqual(percent_change(3, 4)["percent_change"], "33.3333")

    def test_zero_denominator_and_missing_inputs(self):
        zero = percent_change(0, 5)
        self.assertEqual(zero["absolute_difference"], 5)
        self.assertIsNone(zero["percent_change"])
        self.assertEqual(zero["percent_status"], "ZERO_DENOMINATOR")
        self.assertEqual(percent_change(None, 5)["percent_status"], "NOT_COMPUTED")
        self.assertIsNone(percent_change(None, 5)["absolute_difference"])

    def test_ready_counts_still_refuse_raw_rows(self):
        counts = apply_counts(READY, py2026_people=100, py2027_people=80, raw_row_count=5_000)
        self.assertEqual(counts["py2026_currently_enrolled_people"], 100)
        self.assertEqual(counts["absolute_difference"], -20)
        self.assertEqual(counts["percent_change"], -20)
        self.assertIn("raw_row_count", counts["refused_inputs"])
        self.assertNotEqual(counts["py2026_currently_enrolled_people"], 5_000)


class SerializationTests(unittest.TestCase):
    def test_report_round_trip_has_no_individual_identifiers(self):
        report = build_report(payload())
        guard_no_pii(report)
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["comparison_status"], PERIOD_UNRESOLVED)
            self.assertIn(PERIOD_UNRESOLVED, html_path.read_text(encoding="utf-8"))
            self._assert_keys(loaded)
        with self.assertRaises(ComparisonOutputError):
            guard_no_pii({"rows": [{"enrollee_id": "hidden-person"}]})

    def test_command_module_does_not_open_sisense_or_the_database(self):
        source = (PROJECT / "comparison" / "currently_enrolled.py").read_text(encoding="utf-8")
        main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
        for token in ("pyodbc", "playwright", "SisenseJaqlClient", "execute_query", "database_connection"):
            self.assertNotIn(token, source)
        self.assertNotIn("import comparison", main_source)
        self.assertNotIn("comparison.currently_enrolled", main_source)

    def _assert_keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, {"enrollee_id", "enrollment_id", "exchg_indiv_identifier"})
                self._assert_keys(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_keys(item)


if __name__ == "__main__":
    unittest.main()
