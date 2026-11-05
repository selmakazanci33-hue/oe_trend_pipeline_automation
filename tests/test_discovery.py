import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE
from history.store import SnapshotRejected, SnapshotStore
from metrics.catalog import (
    METRICS,
    STATE_DISCOVERED,
    STATE_PENDING,
    STATE_VERIFIED,
    WAVE1_TARGETS,
    active_metrics,
    catalog_state,
)
from metrics.discovery import resolve_targets
from reporting.discovery_report import write_discovery_report
from sisense.discovery import (
    CONTROL_PASS,
    SHAPE_CATEGORICAL,
    SHAPE_MULTI_MEASURE,
    SHAPE_NOT_SUCCESSFUL,
    SHAPE_SINGLE,
    SHAPE_TIME_SERIES,
    SHAPE_UNAVAILABLE,
    SHAPE_UNPARSEABLE,
    DiscoveryObserver,
    evaluate_discovery,
    normalize_label,
    title_matches,
    widget_structure,
)
from sisense.passive import ExpectedWidgetQuery
from tests.discovery_fixtures import FILTER_SENTINEL, cell, widget_payload, widget_request
from tests.passive_fixtures import QUERY, PassiveRequest, payload, response_text, scope
from tests.reporting_fixtures import result

EXPECTED = ExpectedWidgetQuery.from_verified_query(QUERY)
LABELS = [t.label for t in WAVE1_TARGETS]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISCOVERY_SOURCES = ("sisense/discovery.py", "metrics/discovery.py", "reporting/discovery_report.py")


def make_observer(**kw):
    return DiscoveryObserver(VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE["id"], LABELS, EXPECTED.widget_id, **kw)


def observe(*requests, **kw):
    observer = make_observer(**kw)
    for request in requests:
        observer._on_request_finished(request)
    return observer


def discover(*requests):
    result_ = evaluate_discovery(observe(*requests), EXPECTED)
    return result_, {m.metric_id: m for m in resolve_targets(result_)}


def single(widget_id, title, value=1):
    return widget_request(widget_payload(widget_id, title, measures=[title]), [cell(value)])


class ScopeTests(unittest.TestCase):
    def test_only_main_jaql_posts_for_target_dashboard(self):
        body = widget_payload("w1", "Applications Started", measures=["Applications Started"])
        observer = observe(
            PassiveRequest(body, method="GET"),
            PassiveRequest(body, url="https://gahixreports.getinsured.com/api/v1/dashboards"),
            PassiveRequest(widget_payload("w2", "Total Applicants", dashboard="ffff;Other Dashboard")),
            PassiveRequest(widget_payload("w3", "Total Applicants",
                                          datasource={"title": "OTHER", "id": "localhost_aOTHER"})),
            PassiveRequest({"datasource": VERIFIED_DATASOURCE, "metadata": []}),
            PassiveRequest(None),
        )
        self.assertEqual(observer.entries(), [])
        self.assertEqual(observer.target_requests_seen, 4)
        self.assertEqual(observer.out_of_scope, 4)

    def test_handler_makes_no_driver_calls(self):
        request = single("w1", "Applications Started")
        observe(request)
        self.assertEqual(request.response_calls, 0)

    def test_stop_ignores_later_requests(self):
        class Context:
            def on(self, event, handler):
                self.handler = handler
        context = Context()
        observer = make_observer()
        observer.attach(context)
        context.handler(single("w1", "Applications Started"))
        observer.stop()
        context.handler(single("w2", "Total Applicants"))
        self.assertEqual([e.structure.widget_id for e in observer.entries()], ["w1"])


class BoundingTests(unittest.TestCase):
    def test_duplicate_widget_keeps_latest(self):
        first, second, third = (single("w1", "Applications Started", v) for v in (1, 2, 3))
        observer = observe(first, second, third, third)
        (entry,) = observer.entries()
        self.assertEqual(entry.observations, 3)
        self.assertIs(entry.latest_request, third)
        self.assertFalse(entry.structure_changed)

    def test_structure_change_flagged(self):
        a = single("w1", "Applications Started")
        b = widget_request(widget_payload("w1", "Applications Started", measures=["Applications Started"],
                                          filters=4), [cell(1)])
        (entry,) = observe(a, b).entries()
        self.assertTrue(entry.structure_changed)
        self.assertEqual(entry.structure.filter_count, 4)

    def test_irrelevant_widgets_summarized_not_retained(self):
        (entry,) = observe(single("w9", "Average Age")).entries()
        self.assertIsNone(entry.latest_request)
        self.assertEqual(entry.structure.widget_title, "Average Age")

    def test_widget_and_retention_limits(self):
        requests = [single(f"w{i}", "Applications Started") for i in range(10)]
        observer = observe(*requests, max_widgets=6, max_retained=3)
        self.assertEqual(len(observer.entries()), 6)
        self.assertEqual(observer.widgets_dropped, 4)
        self.assertEqual(observer.retained_count, 3)
        result_ = evaluate_discovery(observer, EXPECTED)
        self.assertEqual(result_.retention_skipped, 3)


class MatchingTests(unittest.TestCase):
    def test_normalization_is_case_and_whitespace_only(self):
        self.assertEqual(normalize_label("  Applications   STARTED "), "applications started")
        structure = widget_structure(widget_payload("w1", "applications  started"))
        self.assertIn("applications started", title_matches(structure, LABELS))

    def test_substrings_and_near_misses_rejected(self):
        near_misses = ["Applications Started (YTD)", "New Applications", "Active Enrollees", "Inactive",
                       "Passive Renewals", "Cancelled %", "Terminations", "Submitted Application",
                       "Total Applicant", "Effectuated"]
        for title in near_misses:
            with self.subTest(title):
                structure = widget_structure(widget_payload("w1", title, measures=[title]))
                self.assertEqual(title_matches(structure, LABELS), {})

    def test_match_sources(self):
        structure = widget_structure(widget_payload("w1", "Enrollment Mix", measures=["New", "Active"],
                                                    dimensions=["Terminated"]))
        self.assertEqual(title_matches(structure, LABELS),
                         {"new": ("measure title",), "active": ("measure title",), "terminated": ("dimension title",)})

    def test_single_indicator_discovered(self):
        result_, metrics = discover(single("w1", "Applications Started", 4321))
        m = metrics["applications_started"]
        self.assertEqual(m.state, STATE_DISCOVERED)
        self.assertEqual(m.widget.structure.widget_id, "w1")
        self.assertEqual(m.widget.response.shape, SHAPE_SINGLE)
        self.assertEqual(m.match_sources, ("widget title", "measure title"))

    def test_pending_without_exact_match(self):
        _, metrics = discover(single("w1", "Applications Started (YTD)"))
        self.assertEqual(metrics["applications_started"].state, STATE_PENDING)
        self.assertIn("No exact label match", metrics["applications_started"].reason)
        self.assertEqual(metrics["cancelled"].state, STATE_PENDING)

    def test_ambiguous_label_stays_pending(self):
        _, metrics = discover(single("w1", "Total Applicants"), single("w2", "Total Applicants"))
        self.assertEqual(metrics["total_applicants"].state, STATE_PENDING)
        self.assertIn("Ambiguous", metrics["total_applicants"].reason)


class ShapeTests(unittest.TestCase):
    def test_multi_measure_widget(self):
        body = widget_payload("w5", "Enrollment Type", measures=["New", "Active", "Passive"])
        result_, metrics = discover(widget_request(body, [cell(1), cell(2), cell(3)]))
        for metric_id in ("new_enrolled", "active_enrolled", "passive_enrolled"):
            self.assertEqual(metrics[metric_id].state, STATE_DISCOVERED)
            self.assertEqual(metrics[metric_id].widget.structure.widget_id, "w5")
        self.assertEqual(result_.widgets[0].response.shape, SHAPE_MULTI_MEASURE)
        self.assertEqual(result_.widgets[0].response.value_cells, 3)

    def test_category_widget(self):
        body = widget_payload("w6", "Enrollment by Type", measures=["Enrollees"], dimensions=["Type"],
                              widget_type="chart/pie")
        rows = [[cell("New"), cell(10)], [cell("ACTIVE"), cell(20)], [cell("Passive Renewal"), cell(5)],
                [cell("Other"), cell(1)]]
        result_, metrics = discover(widget_request(body, rows))
        response = result_.widgets[0].response
        self.assertEqual(response.shape, SHAPE_CATEGORICAL)
        self.assertEqual((response.rows, response.value_cells), (4, 8))
        self.assertEqual(set(response.matched_category_labels), {"New", "Active"})
        self.assertEqual(metrics["new_enrolled"].state, STATE_DISCOVERED)
        self.assertEqual(metrics["new_enrolled"].match_sources, ("category label",))
        self.assertEqual(metrics["passive_enrolled"].state, STATE_PENDING)

    def test_time_series_widget(self):
        body = widget_payload("w7", "Submitted Applications", measures=["Submitted Applications"],
                              dimensions=[("Date", "datetime", "days")], widget_type="chart/line")
        rows = [[cell("2026-11-01T00:00:00", "11/01/2026"), cell(5)],
                [cell("2026-11-02T00:00:00", "11/02/2026"), cell(7)],
                [cell("2026-11-03T00:00:00", "11/03/2026"), cell(9)]]
        result_, metrics = discover(widget_request(body, rows))
        response = result_.widgets[0].response
        self.assertEqual(response.shape, SHAPE_TIME_SERIES)
        self.assertEqual((response.time_points, response.first_period, response.last_period),
                         (3, "11/01/2026", "11/03/2026"))
        self.assertEqual(metrics["submitted_applications"].state, STATE_DISCOVERED)

    def test_non_200_stays_pending(self):
        request = PassiveRequest(widget_payload("w1", "Applications Started", measures=["Applications Started"]),
                                 status=500)
        result_, metrics = discover(request)
        self.assertEqual(result_.widgets[0].response.shape, SHAPE_NOT_SUCCESSFUL)
        self.assertEqual(metrics["applications_started"].state, STATE_PENDING)
        self.assertEqual(request._response.body_reads, 0)

    def test_unavailable_body_stays_pending(self):
        request = PassiveRequest(widget_payload("w1", "Applications Started", measures=["Applications Started"]),
                                 body_error=True)
        result_, metrics = discover(request)
        self.assertEqual(result_.widgets[0].response.shape, SHAPE_UNAVAILABLE)
        self.assertEqual(metrics["applications_started"].state, STATE_PENDING)

    def test_unparseable_response(self):
        body = widget_payload("w1", "Applications Started", measures=["Applications Started"])
        for text in ("<html>login</html>", '{"error": true}', '{"values": [{"data": 1}, {"data": 2}]}'):
            with self.subTest(text=text):
                result_, metrics = discover(PassiveRequest(body, text=text))
                self.assertEqual(result_.widgets[0].response.shape, SHAPE_UNPARSEABLE)
                self.assertEqual(metrics["applications_started"].state, STATE_PENDING)


class VerifiedControlTests(unittest.TestCase):
    def test_control_remains_verified(self):
        result_, metrics = discover(PassiveRequest(payload(), text=response_text(803_210)))
        m = metrics["currently_enrolled_people"]
        self.assertEqual(m.state, STATE_VERIFIED)
        self.assertEqual(m.control, CONTROL_PASS)
        self.assertEqual(metrics["percent_effectuated"].state, STATE_VERIFIED)
        self.assertEqual(metrics["percent_effectuated"].control, CONTROL_PASS)

    def test_control_keeps_strict_filter_check(self):
        changed = payload(lambda b: scope(b, "coverage_year")["jaql"]["filter"].update(members=["2026"]))
        _, metrics = discover(PassiveRequest(changed, text=response_text(5)))
        m = metrics["currently_enrolled_people"]
        self.assertEqual(m.state, STATE_VERIFIED)
        self.assertTrue(m.control.startswith("FAIL"))

    def test_control_not_observed(self):
        _, metrics = discover(single("w1", "Applications Started"))
        self.assertEqual(metrics["currently_enrolled_people"].control, "NOT OBSERVED")


class ProductionIsolationTests(unittest.TestCase):
    def test_catalog_unchanged(self):
        self.assertEqual(list(METRICS), ["currently_enrolled_people", "percent_effectuated"])
        self.assertEqual([m.metric_id for m in active_metrics()], ["currently_enrolled_people", "percent_effectuated"])
        for target in WAVE1_TARGETS:
            expected = STATE_VERIFIED if target.metric_id in METRICS else STATE_PENDING
            self.assertEqual(catalog_state(target.metric_id), expected)

    def test_discovered_metric_cannot_create_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, SnapshotStore(Path(tmp) / "h.sqlite3") as store:
            store.start_run("r1")
            for target in WAVE1_TARGETS:
                if target.metric_id in METRICS:
                    continue
                with self.subTest(target.metric_id), self.assertRaises(SnapshotRejected):
                    store.record_snapshot("r1", result(metric=target.metric_id))
            self.assertEqual(store.snapshot_count("currently_enrolled_people", 2027), 0)

    def test_discovery_has_no_network_capability(self):
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        forbidden_attrs = {"request", "post", "fetch", "evaluate", "goto", "reload", "route", "new_page",
                           "new_context", "api_request", "wait_for_response", "expect_response"}
        for relative in DISCOVERY_SOURCES:
            tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                with self.subTest(file=relative, line=getattr(node, "lineno", None)):
                    if isinstance(node, ast.Import):
                        self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden_modules)
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn((node.module or "").split(".")[0], forbidden_modules)
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, forbidden_attrs)

    def test_no_values_or_screenshot_numbers_in_discovery_code(self):
        for relative in DISCOVERY_SOURCES:
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for literal in ("791826", "791,826", "793512", "793,512"):
                self.assertNotIn(literal, text)


class ArtifactTests(unittest.TestCase):
    def test_artifact_has_structure_but_no_values_or_bodies(self):
        guid = "QUERY-GUID-SENTINEL"
        requests = [
            widget_request(widget_payload("w1", "Applications Started", measures=["Applications Started"],
                                          query_guid=guid), [cell(9876543)]),
            widget_request(widget_payload("w6", "Enrollment by Type", measures=["Enrollees"], dimensions=["Type"]),
                           [[cell("New"), cell(1234567)], [cell("Hidden Category"), cell(7654321)]]),
            PassiveRequest(payload(query_guid=guid), text=response_text(8765432, {"extra": "RESP-SENTINEL"})),
            widget_request(widget_payload("w8", "Average Age", measures=["Average Age"]), [cell(4455667)]),
        ]
        result_, _ = discover(*requests)
        metrics = resolve_targets(result_)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_discovery_report(metrics, result_, Path(tmp), datetime(2026, 11, 10, tzinfo=timezone.utc))
            self.assertEqual(path, Path(tmp) / "discovery" / "wave1_metric_discovery.html")
            html = path.read_text(encoding="utf-8")
            doc_text = (path.parent / "wave1_metric_discovery.json").read_text(encoding="utf-8")
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                             ["wave1_metric_discovery.html", "wave1_metric_discovery.json"])
        for text in ("Executive Core Discovery Summary", "Applications Started", "w1", "SINGLE_INDICATOR",
                     "CATEGORICAL", "DISCOVERED", "VERIFIED", "PENDING", "Average Age", "Not the executive"):
            self.assertIn(text.lower(), html.lower())
        for forbidden in ("9876543", "9,876,543", "1234567", "7654321", "8765432", "8,765,432", "4455667",
                          "Hidden Category", guid, "RESP-SENTINEL", FILTER_SENTINEL, "Count([", "formula",
                          "xsrf", "cookie", "x-tenant", "authorization"):
            self.assertNotIn(forbidden.lower(), html.lower())
            self.assertNotIn(forbidden.lower(), doc_text.lower())
        doc = json.loads(doc_text)
        states = {m["metric_id"]: m["state"] for m in doc["metrics"]}
        self.assertEqual(states["currently_enrolled_people"], STATE_VERIFIED)
        self.assertEqual(states["applications_started"], STATE_DISCOVERED)
        self.assertEqual(states["total_applicants"], STATE_PENDING)


if __name__ == "__main__":
    unittest.main()
