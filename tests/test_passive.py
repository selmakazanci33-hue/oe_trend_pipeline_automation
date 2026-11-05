import ast
import unittest
from pathlib import Path

from sisense.passive import (
    FAIL,
    MAX_CANDIDATES,
    PASS,
    ExpectedWidgetQuery,
    PassiveComparison,
    PassiveObservation,
    PassiveWidgetObserver,
    check_candidate,
    comparison_lines,
    observe_passive_value,
)
from tests.passive_fixtures import QUERY, PassiveRequest, measure, payload, response_text, scope

EXPECTED = ExpectedWidgetQuery.from_verified_query(QUERY)
PASSIVE_SOURCE = Path(__file__).resolve().parent.parent / "sisense" / "passive.py"


def observer_with(*requests):
    observer = PassiveWidgetObserver(EXPECTED)
    for request in requests:
        observer._on_request_finished(request)
    return observer


class ExpectedDefinitionTests(unittest.TestCase):
    def test_derived_from_verified_payload(self):
        shape = EXPECTED.shape
        self.assertEqual(shape.dashboard, ("6a98048ccef60720fe7f9b30", "PY 2027 - OE Executive Dashboard"))
        self.assertEqual(shape.widget, ("6a98048ccef60720fe7f9b47", "Currently Enrolled People"))
        self.assertEqual(shape.datasource, ("localhost_aMAIN", "MAIN"))
        self.assertEqual(shape.measure_titles, ("Currently Enrolled People", "% Effectuated"))
        self.assertEqual(len(shape.scope_filters), 6)

    def test_no_identifiers_or_values_hardcoded_in_passive_module(self):
        source = PASSIVE_SOURCE.read_text(encoding="utf-8")
        for literal in ("6a98048ccef60720fe7f9b30", "6a98048ccef60720fe7f9b47", "Currently Enrolled",
                        "coverage_year", "2027", "791826", "791,826"):
            self.assertNotIn(literal, source)


class CandidateCheckTests(unittest.TestCase):
    def assert_accepted(self, mutate=None, **kw):
        check = check_candidate(payload(mutate, **kw), EXPECTED)
        self.assertTrue(check.accepted, check)
        return check

    def assert_rejected(self, mutate, **flags):
        check = check_candidate(payload(mutate), EXPECTED)
        self.assertFalse(check.accepted)
        for name, value in flags.items():
            self.assertEqual(getattr(check, name), value, name)
        return check

    def test_exact_widget_accepted(self):
        self.assert_accepted()

    def test_query_guid_difference_ignored(self):
        self.assert_accepted(query_guid="11111-2222-3333")
        self.assert_accepted(lambda b: b.pop("queryGuid"))

    def test_presentation_and_ordering_differences_ignored(self):
        def mutate(b):
            for item in b["metadata"]:
                item["instanceid"] = "XXXXX"
                item.pop("format", None)
                item["jaql"]["collapsed"] = False
            b["metadata"] = [m for m in b["metadata"] if m.get("panel") != "scope"] + list(
                reversed([m for m in b["metadata"] if m.get("panel") == "scope"]))
            scope(b, "person_type_description")["jaql"]["filter"]["members"].reverse()
            ctx = measure(b, "Currently Enrolled People")["jaql"]
            ctx["formula"] = ctx["formula"].replace("[17769-998]", "[AAAAA-111]")
            ctx["context"] = {"[AAAAA-111]": ctx["context"]["[17769-998]"]}
        self.assert_accepted(mutate)

    def test_wrong_widget_rejected(self):
        self.assert_rejected(lambda b: b.update(widget="6a98048ccef60720fe7f9b99;Currently Enrolled People"),
                             widget=False, dashboard=True)
        self.assert_rejected(lambda b: b.update(widget="6a98048ccef60720fe7f9b47;Renamed"), widget=False)
        self.assert_rejected(lambda b: b.pop("widget"), widget=False)

    def test_wrong_dashboard_rejected(self):
        self.assert_rejected(lambda b: b.update(dashboard="000000000000000000000000;Other"), dashboard=False)

    def test_wrong_datasource_rejected(self):
        self.assert_rejected(lambda b: b["datasource"].update(id="localhost_aOTHER"), datasource=False)

    def test_wrong_primary_measure_rejected(self):
        self.assert_rejected(lambda b: measure(b, "Currently Enrolled People")["jaql"].update(title="Enrolled"),
                             primary_measure=False)
        self.assert_rejected(
            lambda b: measure(b, "Currently Enrolled People")["jaql"].update(formula="Sum([17769-998])"),
            primary_measure=False)
        self.assert_rejected(
            lambda b: measure(b, "Currently Enrolled People")["jaql"]["context"]["[17769-998]"].update(
                column="other_identifier"),
            primary_measure=False)

    def test_wrong_coverage_year_rejected(self):
        self.assert_rejected(lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"]),
                             filters=False, primary_measure=True, widget=True, scope_filters_missing=1,
                             scope_filters_unexpected=1)

    def test_changed_business_filters_rejected(self):
        mutations = {
            "insurance type": lambda b: scope(b, "insurance_type_description")["jaql"]["filter"].update(
                members=["Dental"]),
            "benefit date range": lambda b: scope(b, "benefit_effective_date")["jaql"]["filter"].update(
                to="2027-06-30"),
            "date level": lambda b: scope(b, "benefit_effective_date")["jaql"].update(level="months"),
            "status members": lambda b: scope(b, "enrollment_status_description")["jaql"]["filter"].update(
                members=["Enrolled"]),
            "turned-off exclusion enabled": lambda b: scope(b, "enrollee_status_description")["jaql"]["filter"][
                "filter"].update(turnedOff=False),
            "filter disabled": lambda b: scope(b, "person_type_description").update(disabled=True),
            "filter removed": lambda b: b["metadata"].remove(scope(b, "insurance_type_description")),
            "filter added": lambda b: b["metadata"].append({
                "jaql": {"table": "enrollment", "column": "county", "dim": "[enrollment.county]",
                         "filter": {"members": ["X"]}}, "panel": "scope"}),
            "secondary measure filter": lambda b: measure(b, "% Effectuated")["jaql"]["context"]["[AD577-D38]"][
                "filter"].update(members=["Enrolled"]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                self.assert_rejected(mutate, filters=False)


class ObserverTests(unittest.TestCase):
    def test_scope_limited_to_jaql_post(self):
        observer = observer_with(
            PassiveRequest(payload(), method="GET"),
            PassiveRequest(payload(), url="https://gahixreports.getinsured.com/api/v1/other"),
            PassiveRequest(None),
        )
        self.assertEqual(observer.candidates(), [])
        self.assertEqual(observer.target_requests_seen, 1)

    def test_other_widgets_not_retained(self):
        other = PassiveRequest(payload(lambda b: b.update(widget="6a98048ccef60720fe7f9b11;Applications")))
        observer = observer_with(*[other] * 20, PassiveRequest(payload()))
        self.assertEqual(len(observer.candidates()), 1)
        self.assertEqual(observer.target_requests_seen, 21)

    def test_bounded_keeps_most_recent(self):
        requests = [PassiveRequest(payload()) for _ in range(MAX_CANDIDATES + 7)]
        observer = observer_with(*requests)
        self.assertEqual(observer.candidates(), requests[-MAX_CANDIDATES:])
        self.assertEqual(observer.widget_requests_seen, MAX_CANDIDATES + 7)

    def test_handler_makes_no_driver_calls(self):
        request = PassiveRequest(payload())
        observer_with(request)
        self.assertEqual(request.response_calls, 0)

    def test_stop_ignores_later_requests(self):
        class Context:
            def on(self, event, handler):
                self.handler = handler
        context = Context()
        observer = PassiveWidgetObserver(EXPECTED)
        observer.attach(context)
        context.handler(PassiveRequest(payload()))
        observer.stop()
        context.handler(PassiveRequest(payload()))
        self.assertEqual(len(observer.candidates()), 1)


class ObservationTests(unittest.TestCase):
    def test_accepted_and_parsed(self):
        obs = observe_passive_value(observer_with(PassiveRequest(payload(), text=response_text(803_210))))
        self.assertTrue(obs.parsed)
        self.assertEqual((obs.value, obs.http_status, obs.failure), (803_210, 200, None))

    def test_no_candidate(self):
        obs = observe_passive_value(observer_with())
        self.assertFalse(obs.found)
        self.assertIsNone(obs.value)

    def test_non_200_rejected(self):
        for status in (401, 403, 500, 304):
            with self.subTest(status=status):
                obs = observe_passive_value(observer_with(PassiveRequest(payload(), status=status)))
                self.assertIsNone(obs.value)
                self.assertEqual(obs.http_status, status)
        obs = observe_passive_value(observer_with(PassiveRequest(payload(), no_response=True)))
        self.assertIsNone(obs.value)

    def test_filter_mismatch_never_parsed(self):
        request = PassiveRequest(payload(lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(
            members=["2026"])), text=response_text(5))
        obs = observe_passive_value(observer_with(request))
        self.assertIsNone(obs.value)
        self.assertFalse(obs.check.filters)
        self.assertEqual(request._response.body_reads, 0)

    def test_latest_matching_successful_response_wins(self):
        older = PassiveRequest(payload(), text=response_text(100))
        newer = PassiveRequest(payload(query_guid="B"), text=response_text(200))
        failed_latest = PassiveRequest(payload(), status=500, text=response_text(300))
        wrong_filter_latest = PassiveRequest(
            payload(lambda b: scope(b, "insurance_type_description")["jaql"]["filter"].update(members=["Dental"])),
            text=response_text(400))
        obs = observe_passive_value(observer_with(older, newer, failed_latest, wrong_filter_latest))
        self.assertEqual(obs.value, 200)
        self.assertEqual(older._response.body_reads, 0)

    def test_unavailable_body_fails_closed(self):
        older = PassiveRequest(payload(), text=response_text(100))
        latest = PassiveRequest(payload(), body_error=True)
        obs = observe_passive_value(observer_with(older, latest))
        self.assertIsNone(obs.value)
        self.assertIn("Response body unavailable", obs.failure)
        self.assertEqual(older._response.body_reads, 0, "must not fall back to another response")

    def test_existing_parser_protections_apply(self):
        bad = {
            "html login page": "<html>login</html>",
            "error": '{"error": true, "values": []}',
            "non-integer": response_text(12.5),
            "negative": response_text(-1),
            "wrong column count": '{"values": [{"data": 5}]}',
            "swapped headers": response_text(5, {"headers": ["% Effectuated", "Currently Enrolled People"]}),
        }
        for name, text in bad.items():
            with self.subTest(name):
                obs = observe_passive_value(observer_with(PassiveRequest(payload(), text=text)))
                self.assertIsNone(obs.value)
                self.assertFalse(obs.parsed)
                self.assertIn("Parse failed", obs.failure)


class ComparisonTests(unittest.TestCase):
    def observation(self, value):
        check = check_candidate(payload(), EXPECTED)
        return PassiveObservation(1, check, 200, value is not None, value, None if value is not None else "x")

    def test_equal_values_pass(self):
        comparison = PassiveComparison(self.observation(803_210), 803_210)
        self.assertEqual((comparison.status, comparison.difference), (PASS, 0))
        lines = "\n".join(comparison_lines(comparison))
        for text in ("Candidate: FOUND", "Dashboard: MATCH", "Widget: MATCH", "Primary Measure: MATCH",
                     "Filters: MATCH", "HTTP Status: 200", "Parse: PASS", "Passive Value: 803,210",
                     "Application Value: 803,210", "Difference: 0", "Passive Validation: PASS"):
            self.assertIn(text, lines)

    def test_mismatch_fails_and_keeps_application_value(self):
        comparison = PassiveComparison(self.observation(803_000), 803_210)
        self.assertEqual(comparison.status, FAIL)
        self.assertEqual(comparison.difference, -210)
        self.assertEqual(comparison.application_value, 803_210)
        self.assertIn("Difference: -210", "\n".join(comparison_lines(comparison)))

    def test_missing_passive_value_fails(self):
        comparison = PassiveComparison(self.observation(None), 803_210)
        self.assertEqual(comparison.status, FAIL)
        self.assertIsNone(comparison.difference)


class NoNetworkTests(unittest.TestCase):
    def test_passive_module_has_no_network_mechanism(self):
        tree = ast.parse(PASSIVE_SOURCE.read_text(encoding="utf-8"))
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        forbidden_attrs = {"request", "post", "fetch", "evaluate", "goto", "reload", "route", "new_page",
                           "new_context", "api_request", "wait_for_response", "expect_response"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden_modules)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden_modules)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden_attrs, f"line {node.lineno}")


if __name__ == "__main__":
    unittest.main()
