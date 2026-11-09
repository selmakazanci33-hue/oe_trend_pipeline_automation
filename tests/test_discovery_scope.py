"""Discovery scope gates and the activity widget's structural capture (fakes only, no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from config.settings import VERIFIED_DATASOURCE, VERIFIED_JAQL_URL
from metrics.cancellation_identification import (
    CONF_EXACT,
    ROLE_DAILY,
    identify_cancellation_sources,
    safe_inventory_row,
)
from metrics.discovery import resolve_targets
from metrics.runtime_validation import RV_SERIES_EXTRACTED
from reporting.discovery_report import write_discovery_report
from sisense.discovery import (
    OUT_OF_SCOPE_DASHBOARD,
    OUT_OF_SCOPE_DATASOURCE,
    OUT_OF_SCOPE_NO_REFERENCE,
    OUT_OF_SCOPE_UNPARSEABLE,
    evaluate_discovery,
)
from tests.discovery_fixtures import DASHBOARD_REF, cell, widget_request
from tests.passive_fixtures import PassiveRequest
from tests.test_discovery import EXPECTED
from tests.test_phase3h import observe_all, runtime

ACTIVITY_ID = "6a98048ccef60720fe7f9b39"
ACTIVITY_TITLE = "Cancelled & Terminated Policies - By Day"
CANCELLED_MEMBER = "CANCELLED-MEMBER-SENTINEL"
TERM_PREFIX = "TERM-PREFIX-SENTINEL"
GUID = "ACTIVITY-GUID-SENTINEL"
TRACE = "TRACE-QUERY-SENTINEL"
SENTINELS = (CANCELLED_MEMBER, TERM_PREFIX, GUID, TRACE, "[ID1]", "[ST1]")


def _measure(title, status_filter):
    return {"jaql": {
        "type": "measure",
        "title": title,
        "formula": "(Count([ID1]), [ST1])",
        "context": {
            "[ID1]": {"table": "enrollment", "column": "id", "dim": "[enrollment.id]", "datatype": "numeric"},
            "[ST1]": {"table": "enrollment", "column": "enrollment_status_description",
                      "dim": "[enrollment.enrollment_status_description]", "datatype": "text",
                      "filter": status_filter},
        },
    }}


def activity_payload(datasource=None, dashboard=DASHBOARD_REF, widget_id=ACTIVITY_ID,
                     cancelled_title="Cancelled Policies"):
    """Structure of the line-chart widget; member values are sentinels."""
    return {
        "datasource": dict(datasource or VERIFIED_DATASOURCE),
        "metadata": [
            {"jaql": {"table": "enrollment", "column": "last_update_timestamp",
                      "dim": "[enrollment.last_update_timestamp (Calendar)]", "datatype": "datetime",
                      "level": "days", "title": "Days in last_update_timestamp"}},
            _measure(cancelled_title, {"explicit": True, "multiSelection": True, "members": [CANCELLED_MEMBER]}),
            _measure("Terminated Policies", {"startsWith": TERM_PREFIX}),
            {"jaql": {"table": "ssap_applications", "column": "coverage_year", "datatype": "numeric",
                      "isDashboardFilter": True,
                      "filter": {"explicit": True, "multiSelection": True, "members": ["2027"]}},
             "panel": "scope"},
        ],
        "widgetType": "chart/line",
        "by": "widget",
        "dashboard": dashboard,
        "widget": f"{widget_id};{ACTIVITY_TITLE}",
        "queryGuid": GUID,
    }


def activity_request(body=None, url=f"{VERIFIED_JAQL_URL}?trc={TRACE}", **kw):
    rows = [
        [cell("2026-11-01T00:00:00", "11/1/2026"), cell(1201), cell(None, "N/A")],
        [cell("2026-11-02T00:00:00", "11/2/2026"), cell(1302), cell(None, "N/A")],
    ]
    return widget_request(body or activity_payload(), rows, url=url, **kw)


def _report(result):
    with tempfile.TemporaryDirectory() as tmp:
        path = write_discovery_report(resolve_targets(result), result, Path(tmp),
                                      datetime(2026, 11, 9, tzinfo=timezone.utc))
        return json.loads((path.parent / "wave1_metric_discovery.json").read_text(encoding="utf-8"))


class EndpointTests(unittest.TestCase):
    def test_query_string_on_verified_endpoint_is_observed(self):
        observer = observe_all(activity_request())
        self.assertEqual(observer.target_requests_seen, 1)
        self.assertEqual(observer.out_of_scope, 0)
        self.assertEqual([entry.structure.widget_id for entry in observer.entries()], [ACTIVITY_ID])

    def test_other_jaql_paths_are_counted_by_path_only(self):
        body = activity_payload()
        observer = observe_all(
            PassiveRequest(body, url=f"https://gahixreports.getinsured.com/api/datasources/OTHER/jaql?trc={TRACE}"),
            PassiveRequest(body, url="https://gahixreports.getinsured.com/api/datasources/OTHER/jaql", method="GET"),
            PassiveRequest(body, url="https://gahixreports.getinsured.com/api/v1/dashboards"),
            PassiveRequest(body, url="https://elsewhere.example/api/datasources/MAIN/jaql"),
        )
        self.assertEqual(observer.entries(), [])
        self.assertEqual(observer.target_requests_seen, 0)
        self.assertEqual(observer.endpoint_near_misses, {"/api/datasources/OTHER/jaql": 1})
        document = json.dumps(_report(evaluate_discovery(observer, EXPECTED)))
        self.assertNotIn(TRACE, document)


class ScopeReasonTests(unittest.TestCase):
    def test_datasource_title_scopes_a_widget_with_another_id_form(self):
        legacy = {**VERIFIED_DATASOURCE, "id": "aLOCALHOST_aMAIN"}
        observer = observe_all(activity_request(activity_payload(datasource=legacy)))
        self.assertEqual(observer.out_of_scope, 0)
        self.assertEqual(len(observer.entries()), 1)

    def test_out_of_scope_reasons_and_safe_samples(self):
        observer = observe_all(
            activity_request(activity_payload(datasource={"title": "OTHER", "id": "localhost_aOTHER"})),
            activity_request(activity_payload(dashboard="ffff;Other Dashboard")),
            activity_request({"datasource": VERIFIED_DATASOURCE, "metadata": []}),
            PassiveRequest(None),
        )
        self.assertEqual(observer.entries(), [])
        self.assertEqual(observer.out_of_scope, 4)
        self.assertEqual(observer.out_of_scope_reasons, {
            OUT_OF_SCOPE_DATASOURCE: 1, OUT_OF_SCOPE_DASHBOARD: 1,
            OUT_OF_SCOPE_NO_REFERENCE: 1, OUT_OF_SCOPE_UNPARSEABLE: 1,
        })
        datasource = next(s for s in observer.out_of_scope_samples if s["reason"] == OUT_OF_SCOPE_DATASOURCE)
        self.assertEqual(datasource["widget_id"], ACTIVITY_ID)
        self.assertEqual(datasource["widget_title"], ACTIVITY_TITLE)
        self.assertEqual(datasource["datasource_title"], "OTHER")
        self.assertEqual(set(datasource), {"reason", "dashboard_id", "widget_id", "widget_title",
                                           "widget_type", "datasource_id", "datasource_title"})
        observed = _report(evaluate_discovery(observer, EXPECTED))["observed"]
        self.assertEqual(observed["out_of_scope"], 4)
        self.assertEqual(observed["out_of_scope_reasons"][OUT_OF_SCOPE_DASHBOARD], 1)
        document = json.dumps(observed)
        for sentinel in SENTINELS + ("2027", "members", "filter", "formula"):
            self.assertNotIn(sentinel.lower(), document.lower())


class ActivityWidgetCaptureTests(unittest.TestCase):
    def test_inventory_row_is_structural_and_value_free(self):
        result = evaluate_discovery(observe_all(activity_request()), EXPECTED)
        row = safe_inventory_row(result.widgets[0])
        self.assertEqual(row["widget_id"], ACTIVITY_ID)
        self.assertEqual(row["widget_title"], ACTIVITY_TITLE)
        self.assertEqual(row["measure_titles"], ["Cancelled Policies", "Terminated Policies"])
        self.assertTrue(row["has_date_dimension"])
        self.assertEqual(row["date_grain"], "days")
        date = next(r for r in row["metadata_roles"] if r["kind"] == "dimension")
        self.assertEqual((date["table"], date["column"], date["date_level"]),
                         ("enrollment", "last_update_timestamp", "days"))
        measures = [r for r in row["metadata_roles"] if r["kind"] == "measure"]
        self.assertEqual([r["aggregation_functions"] for r in measures], [["count"], ["count"]])
        year = next(r for r in row["metadata_roles"] if r["panel"] == "scope")
        self.assertIs(year["is_dashboard_filter"], True)
        self.assertEqual(year["filter_kind"], "member_list")
        self.assertIs(year["matches_verified_dashboard_filter"], True)
        persisted = json.dumps({key: value for key, value in row.items() if key != "definition_fingerprint"})
        for sentinel in SENTINELS + ("2027", "members", "explicit", "startsWith"):
            self.assertNotIn(sentinel.lower(), persisted.lower())
        self.assertNotIn(TRACE, json.dumps(_report(result)))

    def test_exact_identity_for_both_daily_measures(self):
        result = evaluate_discovery(observe_all(activity_request()), EXPECTED)
        candidates = identify_cancellation_sources(result)
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0].semantic_role, candidates[0].confidence), (ROLE_DAILY, CONF_EXACT))
        self.assertIn("cancelled_policies_daily", candidates[0].reason)
        self.assertIn("terminated_policies_daily", candidates[0].reason)

    def test_exact_identity_is_not_relaxed(self):
        body = activity_payload(cancelled_title="Cancelled Policy")
        result = evaluate_discovery(observe_all(activity_request(body)), EXPECTED)
        candidate = identify_cancellation_sources(result)[0]
        self.assertNotIn("cancelled_policies_daily", candidate.reason)

    def test_cancelled_series_extracts_from_natural_response(self):
        rows, _ = runtime(activity_request())
        cancelled = rows["cancelled_policies_daily"]
        self.assertEqual(cancelled.status, RV_SERIES_EXTRACTED)
        self.assertEqual(cancelled.widget_id, ACTIVITY_ID)
        self.assertEqual((cancelled.date_table, cancelled.date_column, cancelled.time_grain),
                         ("enrollment", "last_update_timestamp", "days"))


if __name__ == "__main__":
    unittest.main()
