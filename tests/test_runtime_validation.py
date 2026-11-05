import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from metrics.catalog import METRICS, RUNTIME_VALIDATION_TARGETS, VALUE_COUNT, VALUE_PERCENT, active_metrics
from metrics.discovery import resolve_targets
from metrics.runtime_validation import (
    BINDING_MEASURE_TITLE,
    BINDING_SINGLE_MEASURE_WIDGET,
    RV_NOT_AVAILABLE,
    RV_PENDING,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    ControlComparison,
    extract_runtime_value,
    passive_control_value,
    validate_runtime_values,
)
from reporting.validation_report import format_runtime_value, write_validation_report
from sisense.discovery import evaluate_discovery
from tests.discovery_fixtures import FILTER_SENTINEL, cell, rows_text, widget_payload, widget_request
from tests.passive_fixtures import PassiveRequest, payload, response_text, scope
from tests.test_discovery import EXPECTED, observe

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAP_TITLE = "New/Active/Passive - Enrolled People"


def nap_payload(order=("NEW", "ACTIVE", "PASSIVE"), **kw):
    return widget_payload("w-nap", NAP_TITLE, measures=list(order), widget_type="chart/pie", **kw)


def natural_wave1(values=None, guid="GUID-1"):
    """Widgets shaped like the controlled Phase 3C observation; all values invented."""
    v = {"ce": 700_001, "started": 400_002, "submitted": 300_003, "applicants": 500_004,
         "new": 100_005, "active": 200_006, "passive": 300_007, **(values or {})}
    return [
        PassiveRequest(payload(query_guid=guid), text=response_text(v["ce"])),
        widget_request(widget_payload("w-start", "Applications Started", measures=["Applications Started"],
                                      query_guid=guid), [cell(v["started"])]),
        widget_request(widget_payload("w-sub", "Submitted Applications", measures=["Count of applications"]),
                       [cell(v["submitted"])]),
        widget_request(widget_payload("w-appl", "Applicants On Submitted Applications",
                                      measures=["Total Applicants"]), [cell(v["applicants"])]),
        widget_request(nap_payload(), [cell(v["new"]), cell(v["active"]), cell(v["passive"])]),
    ]


def runtime_rows(*requests):
    observer = observe(*requests)
    result = evaluate_discovery(observer, EXPECTED)
    return {r.metric_id: r for r in validate_runtime_values(observer, resolve_targets(result))}, observer


class SevenMetricExtractionTests(unittest.TestCase):
    def test_all_seven_extracted_and_bound(self):
        rows, _ = runtime_rows(*natural_wave1())
        expected = {"applications_started": (400_002, BINDING_MEASURE_TITLE),
                    "submitted_applications": (300_003, BINDING_SINGLE_MEASURE_WIDGET),
                    "total_applicants": (500_004, BINDING_MEASURE_TITLE),
                    "new": (100_005, BINDING_MEASURE_TITLE),
                    "active": (200_006, BINDING_MEASURE_TITLE),
                    "passive": (300_007, BINDING_MEASURE_TITLE),
                    "pct_effectuated": (0.97, BINDING_MEASURE_TITLE)}
        for metric_id, (value, binding) in expected.items():
            with self.subTest(metric_id):
                row = rows[metric_id]
                self.assertEqual(row.status, RV_VALUE_EXTRACTED, row.reason)
                self.assertEqual(row.value, value)
                self.assertEqual(row.binding, binding)
                self.assertEqual(row.production_status, "DISCOVERED — awaiting UI verification")
        self.assertIsInstance(rows["new"].value, int)
        self.assertEqual(rows["total_applicants"].widget_title, "Applicants On Submitted Applications")
        self.assertEqual(rows["new"].shape, "MULTI_MEASURE")

    def test_effectuated_never_substitutes_currently_enrolled(self):
        rows, _ = runtime_rows(*natural_wave1())
        self.assertNotEqual(rows["pct_effectuated"].value, 700_001)
        self.assertTrue(rows["pct_effectuated"].percent_format)
        self.assertEqual(format_runtime_value(rows["pct_effectuated"]), "97.00% (raw 0.97)")
        swapped = response_text(700_001, {"headers": ["% Effectuated", "Currently Enrolled People"]})
        status, value, _, _ = extract_runtime_value(payload(), swapped, "% Effectuated", VALUE_PERCENT)
        self.assertEqual((status, value), (RV_VALIDATION_FAILED, None))
        only_ce = json.dumps({"values": [cell(700_001)]})
        self.assertEqual(extract_runtime_value(payload(), only_ce, "% Effectuated", VALUE_PERCENT)[0],
                         RV_VALIDATION_FAILED)

    def test_effectuated_requires_strict_control_pass(self):
        changed = payload(lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"]))
        rows, observer = runtime_rows(PassiveRequest(changed, text=response_text(5)), *natural_wave1()[1:])
        self.assertEqual(rows["pct_effectuated"].status, RV_VALIDATION_FAILED)
        self.assertIn("strict control check", rows["pct_effectuated"].reason)
        self.assertEqual(passive_control_value(observer, EXPECTED)[0], None)


class MultiMeasureMappingTests(unittest.TestCase):
    def extract(self, body, cells, label):
        return extract_runtime_value(body, rows_text(cells), label, VALUE_COUNT)

    def test_exact_mapping_by_definition(self):
        body = nap_payload()
        cells = [cell(1), cell(2), cell(3)]
        self.assertEqual([self.extract(body, cells, label)[1] for label in ("New", "Active", "Passive")], [1, 2, 3])

    def test_reordered_definitions_follow_request_order(self):
        body = nap_payload(order=("PASSIVE", "NEW", "ACTIVE"))
        cells = [cell(30), cell(10), cell(20)]
        self.assertEqual([self.extract(body, cells, label)[1] for label in ("New", "Active", "Passive")],
                         [10, 20, 30])

    def test_headers_must_agree_with_definition(self):
        body = nap_payload()
        text = rows_text([cell(1), cell(2), cell(3)], {"headers": ["ACTIVE", "NEW", "PASSIVE"]})
        self.assertEqual(extract_runtime_value(body, text, "New", VALUE_COUNT)[0], RV_VALIDATION_FAILED)

    def test_missing_measure(self):
        body = nap_payload(order=("NEW", "ACTIVE"))
        status, value, binding, reason = self.extract(body, [cell(1), cell(2)], "Passive")
        self.assertEqual((status, value, binding), (RV_VALIDATION_FAILED, None, None))
        self.assertIn("No measure", reason)

    def test_extra_measure_in_definition(self):
        body = nap_payload(order=("NEW", "ACTIVE", "PASSIVE", "RENEWAL"))
        cells = [cell(1), cell(2), cell(3), cell(4)]
        self.assertEqual([self.extract(body, cells, label)[1] for label in ("New", "Active", "Passive")], [1, 2, 3])

    def test_missing_or_extra_cell_fails_every_measure(self):
        body = nap_payload()
        for cells in ([cell(1), cell(2)], [cell(1), cell(2), cell(3), cell(4)]):
            for label in ("New", "Active", "Passive"):
                with self.subTest(cells=len(cells), label=label):
                    self.assertEqual(self.extract(body, cells, label)[0], RV_VALIDATION_FAILED)

    def test_ambiguous_mapping(self):
        body = nap_payload(order=("NEW", "New", "ACTIVE"))
        status, _, _, reason = self.extract(body, [cell(1), cell(2), cell(3)], "New")
        self.assertEqual(status, RV_VALIDATION_FAILED)
        self.assertIn("Ambiguous", reason)

    def test_dimension_widget_not_bound(self):
        body = widget_payload("w", "New", measures=["Enrollees"], dimensions=["Type"])
        status, _, _, reason = self.extract(body, [[cell("New"), cell(1)]], "New")
        self.assertEqual(status, RV_VALIDATION_FAILED)
        self.assertIn("not a measures-only query", reason)


class ValueTypeTests(unittest.TestCase):
    body = widget_payload("w-start", "Applications Started", measures=["Applications Started"])

    def status(self, text, kind=VALUE_COUNT):
        return extract_runtime_value(self.body, text, "Applications Started", kind)

    def test_not_available(self):
        for text in ('{"values": []}', rows_text([{"data": None, "text": "N\\\\A"}])):
            with self.subTest(text=text):
                status, value, _, _ = self.status(text)
                self.assertEqual((status, value), (RV_NOT_AVAILABLE, None))

    def test_rejected_values(self):
        bad = {
            "malformed string": rows_text([{"data": "12a", "text": "12a"}]),
            "numeric string": rows_text([{"data": "123", "text": "123"}]),
            "bool": rows_text([{"data": True, "text": "true"}]),
            "NaN": '{"values": [{"data": NaN, "text": "NaN"}]}',
            "infinity": '{"values": [{"data": Infinity, "text": "inf"}]}',
            "negative": rows_text([cell(-3)]),
            "fractional count": rows_text([cell(12.5)]),
            "missing data": '{"values": [{"text": "1"}]}',
            "error": '{"error": true, "values": [{"data": 1}]}',
            "html": "<html>login</html>",
            "no values key": '{"rows": []}',
        }
        for name, text in bad.items():
            with self.subTest(name):
                status, value, _, _ = self.status(text)
                self.assertEqual((status, value), (RV_VALIDATION_FAILED, None))

    def test_percent_accepts_fraction_but_not_invalid(self):
        self.assertEqual(self.status(rows_text([cell(0.4321)]), VALUE_PERCENT)[:2], (RV_VALUE_EXTRACTED, 0.4321))
        self.assertEqual(self.status('{"values": [{"data": NaN}]}', VALUE_PERCENT)[0], RV_VALIDATION_FAILED)

    def test_integral_float_count_accepted_as_int(self):
        status, value, _, _ = self.status(rows_text([cell(400.0)]))
        self.assertEqual((status, value), (RV_VALUE_EXTRACTED, 400))
        self.assertIsInstance(value, int)


class ResponseAvailabilityTests(unittest.TestCase):
    def test_non_200(self):
        requests = natural_wave1()
        requests[1] = PassiveRequest(widget_payload("w-start", "Applications Started",
                                                    measures=["Applications Started"]), status=503)
        rows, _ = runtime_rows(*requests)
        self.assertEqual(rows["applications_started"].status, RV_VALIDATION_FAILED)
        self.assertIsNone(rows["applications_started"].value)

    def test_unavailable_body(self):
        requests = natural_wave1()
        requests[4] = PassiveRequest(nap_payload(), body_error=True)
        rows, _ = runtime_rows(*requests)
        for metric_id in ("new", "active", "passive"):
            self.assertEqual(rows[metric_id].status, RV_VALIDATION_FAILED)
            self.assertIsNone(rows[metric_id].value)

    def test_not_observed(self):
        rows, _ = runtime_rows(*natural_wave1()[:1])
        self.assertEqual(rows["applications_started"].status, RV_VALIDATION_FAILED)
        self.assertIn("Not DISCOVERED", rows["applications_started"].reason)


class PendingAndControlTests(unittest.TestCase):
    def test_cancelled_and_terminated_remain_pending(self):
        rows, _ = runtime_rows(*natural_wave1())
        for metric_id in ("cancelled", "terminated"):
            self.assertEqual(rows[metric_id].status, RV_PENDING)
            self.assertIsNone(rows[metric_id].value)
            self.assertEqual(rows[metric_id].production_status, "PENDING")

    def test_cancelled_never_extracted_even_if_a_widget_matches(self):
        extra = widget_request(widget_payload("w-c", "Cancelled", measures=["Cancelled"]), [cell(42)])
        rows, _ = runtime_rows(*natural_wave1(), extra)
        self.assertEqual(rows["cancelled"].status, RV_PENDING)
        self.assertIsNone(rows["cancelled"].value)

    def test_scope_is_exactly_seven_plus_pending(self):
        rows, _ = runtime_rows(*natural_wave1())
        self.assertEqual(len(RUNTIME_VALIDATION_TARGETS), 7)
        self.assertEqual(set(rows), {t.metric_id for t in RUNTIME_VALIDATION_TARGETS} | {"cancelled", "terminated"})
        self.assertNotIn("currently_enrolled_people", rows)

    def test_control_comparison(self):
        _, observer = runtime_rows(*natural_wave1())
        passive, reason = passive_control_value(observer, EXPECTED)
        self.assertEqual((passive, reason), (700_001, None))
        self.assertEqual(ControlComparison(passive, 700_001, None).status, "PASS")
        mismatch = ControlComparison(passive, 700_000, None)
        self.assertEqual((mismatch.status, mismatch.difference), ("FAIL", 1))
        self.assertEqual(ControlComparison(None, 700_001, "x").status, "FAIL")

    def test_catalog_not_promoted(self):
        runtime_rows(*natural_wave1())
        self.assertEqual(list(METRICS), ["currently_enrolled_people"])
        self.assertEqual([m.metric_id for m in active_metrics()], ["currently_enrolled_people"])


class ArtifactAndSafetyTests(unittest.TestCase):
    def test_validation_report_has_values_but_no_raw_content(self):
        rows, observer = runtime_rows(*natural_wave1(guid="GUID-SENTINEL"))
        control = ControlComparison(passive_control_value(observer, EXPECTED)[0], 700_001, None)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_validation_report(tuple(rows.values()), control, Path(tmp),
                                           datetime(2026, 11, 10, tzinfo=timezone.utc))
            self.assertEqual(path, Path(tmp) / "validation" / "wave1_runtime_validation.html")
            html = path.read_text(encoding="utf-8")
            doc = (path.parent / "wave1_runtime_validation.json").read_text(encoding="utf-8")
        for text in ("400,002", "300,003", "500,004", "100,005", "200,006", "300,007", "97.00%",
                     "VALUE_EXTRACTED", "PENDING", "DISCOVERED — awaiting UI verification",
                     "Passive Currently Enrolled", "700,001", "PASS"):
            self.assertIn(text, html)
        self.assertEqual(json.loads(doc)["currently_enrolled_control"]["status"], "PASS")
        for forbidden in ("GUID-SENTINEL", FILTER_SENTINEL, "Count([", "formula", "xsrf", "cookie",
                          "x-tenant", "authorization", "server.session"):
            self.assertNotIn(forbidden.lower(), html.lower())
            self.assertNotIn(forbidden.lower(), doc.lower())

    def test_no_network_capability(self):
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        forbidden_attrs = {"request", "post", "fetch", "evaluate", "goto", "reload", "route", "new_page",
                           "new_context", "api_request", "wait_for_response", "expect_response"}
        for relative in ("metrics/runtime_validation.py", "reporting/validation_report.py"):
            tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden_modules, relative)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], forbidden_modules, relative)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden_attrs, f"{relative}:{node.lineno}")

    def test_no_hardcoded_runtime_or_screenshot_values(self):
        for relative in ("metrics/runtime_validation.py", "reporting/validation_report.py", "metrics/catalog.py"):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for literal in ("791826", "791,826", "793512", "793,512"):
                self.assertNotIn(literal, text)


if __name__ == "__main__":
    unittest.main()
