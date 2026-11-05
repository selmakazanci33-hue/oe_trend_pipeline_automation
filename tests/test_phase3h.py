"""Phase 3H: cancellation/termination discovery architecture (fakes only, network blocked)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE
from history.store import SnapshotRejected, SnapshotStore
from metrics.cancellation import bind_exact_measure, cancellation_eligibility, extract_cancellation
from metrics.catalog import (
    CANCELLATION_CANDIDATES,
    CANCELLATION_VALIDATION_TARGETS,
    DISCOVERY_TARGETS,
    KIND_DAILY_POLICY_SERIES,
    KIND_PEOPLE_AGGREGATE,
    METRICS,
    PROMOTION_CANDIDATES,
    ROADMAP,
    RUNTIME_VALIDATION_TARGETS,
    WAVE1_TARGETS,
)
from metrics.discovery import resolve_targets
from metrics.fingerprint_evidence import (
    FP_CHANGED,
    FP_NO_BASELINE,
    FP_STABLE,
    collect_evidence,
)
from metrics.runtime_validation import (
    BINDING_MEASURE_TITLE,
    BindingError,
    RV_NOT_AVAILABLE,
    RV_PENDING,
    RV_SERIES_EXTRACTED,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    validate_runtime_values,
)
from metrics.source_series import (
    DAILY_SOURCE_METRIC_IDS,
    PEOPLE_AGGREGATE_METRIC_IDS,
    PROPOSED_SOURCE_SERIES_STORE,
    may_persist_in_metric_snapshots,
    production_daily_chart_allowed,
)
from reporting.html_report import chart_figure, source_daily_activity_figure
from reporting.series_semantics import (
    CANCELLED_POLICIES_DAILY,
    CURRENTLY_ENROLLED_SNAPSHOT_TREND,
    TERMINATED_POLICIES_DAILY,
    SeriesIdentity,
    identity_complete,
    series_equivalent,
)
from reporting.validation_report import write_validation_report
from sisense.discovery import DiscoveryObserver, evaluate_discovery, title_matches, widget_structure
from sisense.passive import definition_fingerprint
from tests.discovery_fixtures import FILTER_SENTINEL, cell, widget_payload, widget_request
from tests.reporting_fixtures import result
from tests.test_discovery import EXPECTED
from tests.test_passive_metrics_main import PassiveMetricsHarness
from tests.test_phase3f_d import section
from tests.test_runtime_validation import natural_wave1

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PEOPLE_ENROLLED = 12_001
PEOPLE_NOT_ENROLLED = 12_002
DAILY_CANCELLED = (901, 902, 903)
DAILY_TERMINATED = (811, 812, 813)
FORBIDDEN = (
    "566051", "566,051", "565200", "565,200", "2510", "2,510", "35266", "35,266",
    "756009", "756,009", "793512", "793,512", "791826", "791,826",
)
PHASE3H_SOURCES = (
    "metrics/catalog.py",
    "metrics/cancellation.py",
    "metrics/source_series.py",
    "metrics/runtime_validation.py",
    "metrics/fingerprint_evidence.py",
    "metrics/discovery.py",
    "reporting/validation_report.py",
    "reporting/html_report.py",
    "reporting/series_semantics.py",
)
PEOPLE_IDS = ("enrolled_cancelled", "cancelled_not_enrolled")
DAILY_IDS = ("cancelled_policies_daily", "terminated_policies_daily")


def observe_all(*requests):
    observer = DiscoveryObserver(
        VERIFIED_DASHBOARD_ID, VERIFIED_DATASOURCE["id"],
        [t.label for t in DISCOVERY_TARGETS], EXPECTED.widget_id,
    )
    for request in requests:
        observer._on_request_finished(request)
    return observer


def runtime(*requests):
    observer = observe_all(*requests)
    resolved = resolve_targets(evaluate_discovery(observer, EXPECTED))
    return {r.metric_id: r for r in validate_runtime_values(observer, resolved)}, observer


def people_payload(measures=None, **kw):
    return widget_payload(
        "w-canc-people",
        "Enrolled/Cancelled vs Cancelled/Not Enrolled People",
        measures=list(measures or ("Enrolled & Cancelled", "Cancelled - Not Enrolled")),
        **kw,
    )


def daily_payload(level="days", measures=None, **kw):
    return widget_payload(
        "w-canc-daily",
        "Cancellation Activity",
        measures=list(measures or ("Cancelled Policies", "Terminated Policies")),
        dimensions=[("Date", "datetime", level, "enrollment", "last_update_timestamp")],
        widget_type="chart/line",
        **kw,
    )


def people_request(enrolled=PEOPLE_ENROLLED, not_enrolled=PEOPLE_NOT_ENROLLED, measures=None, **kw):
    body = people_payload(measures=measures)
    titles = measures or ("Enrolled & Cancelled", "Cancelled - Not Enrolled")
    values = {"Enrolled & Cancelled": enrolled, "Cancelled - Not Enrolled": not_enrolled}
    return widget_request(body, [cell(values[title]) for title in titles], **kw)


def daily_request(level="days", labelled=True, **kw):
    rows = []
    for i, day in enumerate(("2026-02-01", "2026-02-02", "2026-02-03")):
        date = cell(day, f"02/0{i + 1}/2026") if labelled else {"data": i + 1}
        rows.append([date, cell(DAILY_CANCELLED[i]), cell(DAILY_TERMINATED[i])])
    return widget_request(daily_payload(level=level), rows, **kw)


def natural_with_cancellation(**kw):
    return [*natural_wave1(**kw), people_request(), daily_request()]


class CandidateIdentityTests(unittest.TestCase):
    def test_four_new_ids_are_independent_and_not_production(self):
        self.assertEqual(tuple(CANCELLATION_CANDIDATES), PEOPLE_IDS + DAILY_IDS)
        self.assertEqual(PEOPLE_AGGREGATE_METRIC_IDS, set(PEOPLE_IDS))
        self.assertEqual(DAILY_SOURCE_METRIC_IDS, set(DAILY_IDS))
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertNotIn(metric_id, METRICS)
            self.assertNotIn(metric_id, PROMOTION_CANDIDATES)
            self.assertNotIn(metric_id, {t.metric_id for t in WAVE1_TARGETS})
            self.assertNotIn(metric_id, {t.metric_id for t in RUNTIME_VALIDATION_TARGETS})
        self.assertEqual(CANCELLATION_CANDIDATES["enrolled_cancelled"].kind, KIND_PEOPLE_AGGREGATE)
        self.assertEqual(CANCELLATION_CANDIDATES["cancelled_not_enrolled"].kind, KIND_PEOPLE_AGGREGATE)
        self.assertEqual(CANCELLATION_CANDIDATES["cancelled_policies_daily"].kind, KIND_DAILY_POLICY_SERIES)
        self.assertEqual(CANCELLATION_CANDIDATES["terminated_policies_daily"].kind, KIND_DAILY_POLICY_SERIES)
        self.assertEqual(CANCELLATION_CANDIDATES["cancelled_not_enrolled"].discovery_label, "Cancelled - Not Enrolled")
        self.assertEqual({t.metric_id for t in CANCELLATION_VALIDATION_TARGETS}, set(CANCELLATION_CANDIDATES))
        self.assertTrue(set(DISCOVERY_TARGETS) > set(WAVE1_TARGETS))

    def test_wave1_cancelled_ids_remain_untouched(self):
        self.assertEqual([t.label for t in WAVE1_TARGETS if t.metric_id in ("cancelled", "terminated")],
                         ["Cancelled", "Terminated"])
        rows, _ = runtime(*natural_with_cancellation())
        self.assertEqual(rows["cancelled"].status, RV_PENDING)
        self.assertEqual(rows["terminated"].status, RV_PENDING)
        self.assertIsNone(rows["cancelled"].value)
        self.assertIsNone(rows["terminated"].value)

    def test_generic_cancelled_widget_does_not_satisfy_new_ids(self):
        extra = widget_request(widget_payload("w-c", "Cancelled", measures=["Cancelled"]), [cell(42)])
        rows, _ = runtime(*natural_wave1(), extra)
        self.assertEqual(rows["cancelled"].status, RV_PENDING)
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertEqual(rows[metric_id].status, RV_VALIDATION_FAILED)
            self.assertIn("Not DISCOVERED", rows[metric_id].reason)


class ExactMeasureBindingTests(unittest.TestCase):
    def test_people_level_metrics_remain_separate(self):
        rows, _ = runtime(*natural_with_cancellation())
        enrolled = rows["enrolled_cancelled"]
        other = rows["cancelled_not_enrolled"]
        self.assertEqual(enrolled.status, RV_VALUE_EXTRACTED)
        self.assertEqual(other.status, RV_VALUE_EXTRACTED)
        self.assertEqual(enrolled.value, PEOPLE_ENROLLED)
        self.assertEqual(other.value, PEOPLE_NOT_ENROLLED)
        self.assertNotEqual(enrolled.value, other.value)
        self.assertEqual(enrolled.widget_id, other.widget_id)
        self.assertEqual(enrolled.bound_measure_title, "Enrolled & Cancelled")
        self.assertEqual(other.bound_measure_title, "Cancelled - Not Enrolled")
        self.assertEqual(enrolled.binding, BINDING_MEASURE_TITLE)
        self.assertEqual(other.binding, BINDING_MEASURE_TITLE)
        self.assertIsNone(enrolled.date_dimension_title)
        self.assertIsNone(other.time_grain)

    def test_daily_policy_metrics_remain_separate(self):
        rows, _ = runtime(*natural_with_cancellation())
        cancelled = rows["cancelled_policies_daily"]
        terminated = rows["terminated_policies_daily"]
        self.assertEqual(cancelled.status, RV_SERIES_EXTRACTED)
        self.assertEqual(terminated.status, RV_SERIES_EXTRACTED)
        self.assertIsNone(cancelled.value)
        self.assertIsNone(terminated.value)
        self.assertEqual(cancelled.bound_measure_title, "Cancelled Policies")
        self.assertEqual(terminated.bound_measure_title, "Terminated Policies")
        self.assertEqual(cancelled.date_dimension_title, "Date")
        self.assertEqual(cancelled.time_grain, "days")
        self.assertEqual(cancelled.series_point_count, 3)
        self.assertIn("02/01/2026", cancelled.date_semantics)
        self.assertNotEqual(cancelled.bound_measure_title, terminated.bound_measure_title)
        self.assertNotEqual(cancelled.metric_id, "enrolled_cancelled")

    def test_reordered_measures_follow_title_not_position(self):
        order = ("Cancelled - Not Enrolled", "Enrolled & Cancelled")
        request = people_request(measures=order)
        rows, _ = runtime(*natural_wave1(), request)
        self.assertEqual(rows["enrolled_cancelled"].value, PEOPLE_ENROLLED)
        self.assertEqual(rows["cancelled_not_enrolled"].value, PEOPLE_NOT_ENROLLED)
        binding = bind_exact_measure(people_payload(measures=order), "Enrolled & Cancelled")
        self.assertEqual(binding.column, 1)
        self.assertEqual(binding.basis, BINDING_MEASURE_TITLE)

    def test_no_positional_or_widget_title_fallback(self):
        fallback = widget_request(
            widget_payload("w-fb", "Enrolled & Cancelled", measures=["People Count"]), [cell(PEOPLE_ENROLLED)])
        rows, _ = runtime(*natural_wave1(), fallback)
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALIDATION_FAILED)
        self.assertIn("exact measure-title binding", rows["enrolled_cancelled"].reason)
        with self.assertRaises(BindingError):
            bind_exact_measure(widget_payload("w-fb", "Enrolled & Cancelled", measures=["People Count"]),
                               "Enrolled & Cancelled")


class RejectionTests(unittest.TestCase):
    def test_wrong_widget_rejected(self):
        rows, _ = runtime(*natural_wave1(), daily_request())
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALIDATION_FAILED)
        self.assertEqual(rows["cancelled_not_enrolled"].status, RV_VALIDATION_FAILED)
        people = CANCELLATION_CANDIDATES["enrolled_cancelled"]
        common = dict(value_kind="count", widget_id="w-canc-daily", widget_title="Cancellation Activity",
                      http_status=200, datasource="MAIN")
        text = json.dumps({"values": [[cell("2026-02-01"), cell(1), cell(2)]]})
        row = extract_cancellation(people, daily_payload(), text, common)
        self.assertEqual(row.status, RV_VALIDATION_FAILED)
        self.assertIn("not a daily series", row.reason)

    def test_wrong_datasource_rejected(self):
        other = people_payload(datasource={"title": "OTHER", "id": "localhost_aOTHER"})
        request = widget_request(other, [cell(PEOPLE_ENROLLED), cell(PEOPLE_NOT_ENROLLED)])
        rows, observer = runtime(*natural_wave1(), request)
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALIDATION_FAILED)
        self.assertIn("Not DISCOVERED", rows["enrolled_cancelled"].reason)
        self.assertEqual(observer.out_of_scope, 1)

    def test_filter_change_changes_fingerprint(self):
        base = people_payload()
        changed = people_payload()
        changed["metadata"][-1]["jaql"]["filter"]["members"] = ["OTHER-SCOPE"]
        self.assertNotEqual(definition_fingerprint(base), definition_fingerprint(changed))
        rows_a, _ = runtime(*natural_wave1(), widget_request(base, [cell(PEOPLE_ENROLLED), cell(PEOPLE_NOT_ENROLLED)]))
        rows_b, _ = runtime(*natural_wave1(), widget_request(changed, [cell(PEOPLE_ENROLLED), cell(PEOPLE_NOT_ENROLLED)]))
        self.assertEqual(rows_a["enrolled_cancelled"].status, RV_VALUE_EXTRACTED)
        self.assertNotEqual(rows_a["enrolled_cancelled"].definition_fingerprint,
                            rows_b["enrolled_cancelled"].definition_fingerprint)

    def test_ambiguous_measure_rejected(self):
        body = people_payload(measures=("Enrolled & Cancelled", "Enrolled & Cancelled"))
        request = widget_request(body, [cell(1), cell(2)])
        rows, _ = runtime(*natural_wave1(), request)
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALIDATION_FAILED)
        self.assertIn("Ambiguous", rows["enrolled_cancelled"].reason)

    def test_invalid_count_rejected(self):
        negative = widget_request(people_payload(), [cell(-4), cell(PEOPLE_NOT_ENROLLED)])
        rows, _ = runtime(*natural_wave1(), negative)
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALIDATION_FAILED)
        self.assertRegex(rows["enrolled_cancelled"].reason or "", r"negative|nonnegative")
        fractional = widget_request(people_payload(), [cell(1.5), cell(PEOPLE_NOT_ENROLLED)])
        rows, _ = runtime(*natural_wave1(), fractional)
        self.assertEqual(rows["enrolled_cancelled"].status, RV_VALIDATION_FAILED)

    def test_missing_widget_fails_closed(self):
        rows, _ = runtime(*natural_wave1())
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertEqual(rows[metric_id].status, RV_VALIDATION_FAILED)
            self.assertIn("Not DISCOVERED", rows[metric_id].reason)
            self.assertFalse(cancellation_eligibility(
                rows[metric_id], CANCELLATION_CANDIDATES[metric_id], True) is None)

    def test_currently_enrolled_control_required(self):
        rows, _ = runtime(*natural_with_cancellation())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave1_fingerprint_observations.json"
            blocked = collect_evidence(tuple(rows.values()), "2026-11-05T12:00:00Z", path, False)
        self.assertFalse(blocked.observation_recorded)
        self.assertIn("Currently Enrolled control did not pass", blocked.observation_note)
        by_id = {e.metric_id: e for e in blocked.cancellation}
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertFalse(by_id[metric_id].eligible)
            self.assertEqual(by_id[metric_id].eligibility_reason, "Currently Enrolled control did not pass")
        self.assertFalse(path.exists())


class DailySeriesRulesTests(unittest.TestCase):
    def test_daily_series_requires_verified_date_dimension(self):
        body = widget_payload("w-canc-daily", "Cancellation Activity",
                              measures=["Cancelled Policies", "Terminated Policies"])
        request = widget_request(body, [cell(901), cell(811)])
        rows, _ = runtime(*natural_wave1(), request)
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("date dimension", rows["cancelled_policies_daily"].reason)

    def test_daily_series_requires_verified_grain(self):
        rows, _ = runtime(*natural_wave1(), daily_request(level="months"))
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("time grain", rows["cancelled_policies_daily"].reason)
        self.assertEqual(rows["cancelled_policies_daily"].time_grain, "months")

    def test_daily_series_requires_verified_date_semantics(self):
        rows, _ = runtime(*natural_wave1(), daily_request(labelled=False))
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_VALIDATION_FAILED)
        self.assertIn("Date semantics", rows["cancelled_policies_daily"].reason)

    def test_empty_daily_response_is_not_converted_to_zero(self):
        request = widget_request(daily_payload(), [])
        rows, _ = runtime(*natural_wave1(), request)
        self.assertEqual(rows["cancelled_policies_daily"].status, RV_NOT_AVAILABLE)
        self.assertIsNone(rows["cancelled_policies_daily"].value)
        self.assertNotEqual(rows["cancelled_policies_daily"].value, 0)

    def test_daily_source_series_cannot_use_snapshot_history(self):
        self.assertFalse(may_persist_in_metric_snapshots("cancelled_policies_daily"))
        self.assertFalse(may_persist_in_metric_snapshots("terminated_policies_daily"))
        self.assertTrue(may_persist_in_metric_snapshots("enrolled_cancelled"))
        self.assertTrue(may_persist_in_metric_snapshots("currently_enrolled_people"))
        self.assertEqual(PROPOSED_SOURCE_SERIES_STORE, "source_series_points")
        self.assertFalse(identity_complete(CANCELLED_POLICIES_DAILY))
        self.assertFalse(production_daily_chart_allowed(CANCELLED_POLICIES_DAILY))
        self.assertFalse(production_daily_chart_allowed(TERMINATED_POLICIES_DAILY))
        snapshot_as_daily = SeriesIdentity(
            label="Cancelled Policies — Daily",
            metric_id="cancelled_policies_daily",
            filters="copied",
            time_grain=CURRENTLY_ENROLLED_SNAPSHOT_TREND.time_grain,
            date_semantics=CURRENTLY_ENROLLED_SNAPSHOT_TREND.date_semantics,
        )
        self.assertFalse(production_daily_chart_allowed(snapshot_as_daily))
        self.assertFalse(series_equivalent(CURRENTLY_ENROLLED_SNAPSHOT_TREND, CANCELLED_POLICIES_DAILY))
        self.assertIsNone(source_daily_activity_figure(CANCELLED_POLICIES_DAILY, TERMINATED_POLICIES_DAILY))
        figure = chart_figure.__doc__
        self.assertIn("snapshot trend", figure.lower())

    def test_aggregate_histories_remain_independent(self):
        self.assertNotEqual(PEOPLE_IDS[0], PEOPLE_IDS[1])
        rows, _ = runtime(*natural_with_cancellation())
        self.assertNotEqual(rows["enrolled_cancelled"].value, rows["cancelled_not_enrolled"].value)
        with tempfile.TemporaryDirectory() as tmp, SnapshotStore(Path(tmp) / "h.sqlite3") as store:
            store.start_run("r1")
            for metric_id in CANCELLATION_CANDIDATES:
                with self.subTest(metric_id), self.assertRaises(SnapshotRejected):
                    store.record_snapshot("r1", result(metric=metric_id))


class PromotionIsolationTests(unittest.TestCase):
    def test_total_applicants_and_metal_tier_untouched(self):
        self.assertEqual(set(PROMOTION_CANDIDATES), {"total_applicants"})
        names = [item.name for item in ROADMAP]
        self.assertIn("Total Applicants", names)
        self.assertIn("Metal Tier", names)
        rows, _ = runtime(*natural_with_cancellation())
        self.assertEqual(rows["total_applicants"].status, RV_VALUE_EXTRACTED)
        self.assertEqual(rows["total_applicants"].production_status,
                         "VALIDATION ONLY — awaiting verified filter fingerprint")

    def test_existing_seven_production_metrics_unchanged(self):
        self.assertEqual(list(METRICS), [
            "currently_enrolled_people", "percent_effectuated", "applications_started", "submitted_applications",
            "new_enrolled", "active_enrolled", "passive_enrolled",
        ])
        rows, _ = runtime(*natural_with_cancellation())
        for metric_id in ("applications_started", "submitted_applications", "new_enrolled",
                          "active_enrolled", "passive_enrolled", "percent_effectuated"):
            self.assertEqual(rows[metric_id].status, RV_VALUE_EXTRACTED, metric_id)
            self.assertEqual(rows[metric_id].production_status, "VERIFIED")


class EvidenceAndStabilityTests(unittest.TestCase):
    def test_eligible_observation_records_safe_fingerprint_fields(self):
        rows, _ = runtime(*natural_with_cancellation())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave1_fingerprint_observations.json"
            first = collect_evidence(tuple(rows.values()), "2026-11-05T12:00:00Z", path, True)
            second = collect_evidence(tuple(rows.values()), "2026-11-05T13:00:00Z", path, True)
            self.assertTrue(first.observation_recorded, first.observation_note)
            by_id = {e.metric_id: e for e in first.cancellation}
            for metric_id in CANCELLATION_CANDIDATES:
                item = by_id[metric_id]
                self.assertTrue(item.eligible, item.eligibility_reason)
                self.assertTrue(item.fingerprint.startswith("sha256:"))
                self.assertEqual(item.http_status, 200)
                self.assertEqual(item.datasource, "MAIN")
                self.assertEqual(first.comparison.results[metric_id], FP_NO_BASELINE)
                self.assertEqual(second.comparison.results[metric_id], FP_STABLE)
            self.assertTrue(first.cancellation_shared)
            self.assertTrue(all(s.eligible_together for s in first.cancellation_shared))
            log = json.loads(path.read_text(encoding="utf-8"))
            recorded = log["observations"][0]["candidates"]["enrolled_cancelled"]
            self.assertNotIn("value", recorded)
            self.assertNotIn("runtime_value", recorded)
            self.assertEqual(recorded["bound_measure_title"], "Enrolled & Cancelled")
            daily = log["observations"][0]["candidates"]["cancelled_policies_daily"]
            self.assertEqual(daily["time_grain"], "days")
            self.assertEqual(daily["date_dimension_title"], "Date")
            blob = path.read_text(encoding="utf-8")
            self.assertNotIn(str(PEOPLE_ENROLLED), blob)
            self.assertNotIn(str(DAILY_CANCELLED[0]), blob)

    def test_filter_change_is_not_stable(self):
        rows_a, _ = runtime(*natural_with_cancellation())
        changed = people_payload()
        changed["metadata"][-1]["jaql"]["filter"]["members"] = ["OTHER-SCOPE"]
        rows_b, _ = runtime(*natural_wave1(), widget_request(changed, [cell(PEOPLE_ENROLLED), cell(PEOPLE_NOT_ENROLLED)]),
                            daily_request())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave1_fingerprint_observations.json"
            collect_evidence(tuple(rows_a.values()), "2026-11-05T12:00:00Z", path, True)
            later = collect_evidence(tuple(rows_b.values()), "2026-11-05T13:00:00Z", path, True)
        self.assertEqual(later.comparison.results["enrolled_cancelled"], FP_CHANGED)
        self.assertEqual(later.comparison.results["cancelled_policies_daily"], FP_STABLE)


class DeveloperReportTests(unittest.TestCase):
    def test_validation_report_lists_cancellation_evidence(self):
        from metrics.runtime_validation import ControlComparison
        rows, observer = runtime(*natural_with_cancellation(guid="GUID-3H"))
        from metrics.runtime_validation import passive_control_value
        control = ControlComparison(passive_control_value(observer, EXPECTED)[0], 700_001, None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            evidence = collect_evidence(tuple(rows.values()), "2026-11-05T12:00:00Z", path / "obs.json", True)
            html_path = write_validation_report(tuple(rows.values()), control, path,
                                                datetime(2026, 11, 5, tzinfo=timezone.utc), evidence)
            html = html_path.read_text(encoding="utf-8")
            doc = json.loads((html_path.parent / "wave1_runtime_validation.json").read_text(encoding="utf-8"))
        self.assertIn("Cancellation / termination evidence", html)
        self.assertIn("enrolled_cancelled", html)
        self.assertIn("cancelled_policies_daily", html)
        self.assertIn("Date", html)
        self.assertIn("days", html)
        candidates = {c["metric_id"]: c for c in doc["promotion_evidence"]["cancellation_candidates"]}
        self.assertEqual(set(candidates), set(CANCELLATION_CANDIDATES))
        self.assertEqual(candidates["enrolled_cancelled"]["runtime_value"], PEOPLE_ENROLLED)
        self.assertIsNone(candidates["cancelled_policies_daily"]["runtime_value"])
        self.assertEqual(candidates["cancelled_policies_daily"]["time_grain"], "days")
        self.assertEqual(set(c["metric_id"] for c in doc["promotion_evidence"]["candidates"]), {"total_applicants"})
        for forbidden in ("GUID-3H", FILTER_SENTINEL, "Count([", "formula", "xsrf", "cookie", "authorization"):
            self.assertNotIn(forbidden.lower(), html.lower())


class ProductionHtmlAndSafetyTests(PassiveMetricsHarness):
    def test_production_html_stays_pending_and_seven_metrics_live(self):
        code, _, _, posts = self.run_pipeline(
            700_001, self.ce_widget(ce=700_001, pct=0.97), *natural_with_cancellation(),
            runtime_validation=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 1, "only the existing verified application request")
        html = self.report_html()
        overview = section(html, "cancellation_overview")
        activity = section(html, "cancellation_activity")
        self.assertIn("PENDING", overview)
        self.assertIn("PENDING", activity)
        self.assertNotIn("badge-source", overview)
        self.assertNotIn("badge-source", activity)
        self.assertNotIn("12,001", overview)
        self.assertNotIn("12,002", overview)
        self.assertNotIn(str(DAILY_CANCELLED[0]), activity)
        self.assertNotIn('class="chart"', activity)
        chips = self.pending_chips()
        for name in ("Enrolled & Cancelled", "Cancelled — Not Enrolled",
                     "Cancelled Policies — Daily", "Terminated Policies — Daily",
                     "Total Applicants", "Metal Tier"):
            self.assertIn(name, chips)
        self.assertIn("Currently Enrolled", html)
        self.assertIn('data-definition="currently_enrolled_people"', html)
        for metric_id in ("percent_effectuated", "applications_started", "submitted_applications",
                          "new_enrolled", "active_enrolled", "passive_enrolled"):
            self.assertIn(f'data-metric="{metric_id}"', html)
        self.assertEqual([(s.metric_id) for s in self.snapshots() if s.metric_id in CANCELLATION_CANDIDATES], [])

    def test_passive_discovery_creates_zero_additional_requests(self):
        request = people_request()
        observer = observe_all(request)
        self.assertEqual(request.response_calls, 0)
        evaluate_discovery(observer, EXPECTED)
        self.assertGreaterEqual(request.response_calls, 1)
        self.assertFalse(hasattr(observer, "post"))
        self.assertFalse(hasattr(observer, "fetch"))

    def test_no_screenshot_values_or_network_capability(self):
        forbidden_modules = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        forbidden_attrs = {"request", "post", "fetch", "evaluate", "goto", "reload", "route", "new_page",
                           "new_context", "api_request", "wait_for_response", "expect_response"}
        for relative in PHASE3H_SOURCES:
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for literal in FORBIDDEN:
                self.assertNotIn(literal, text, relative)
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden_modules, relative)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], forbidden_modules, relative)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden_attrs, f"{relative}:{getattr(node, 'lineno', None)}")
        for relative in (*PHASE3H_SOURCES, "main.py"):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for literal in FORBIDDEN:
                self.assertNotIn(literal, text, relative)


class TitleMatchTests(unittest.TestCase):
    def test_discovery_labels_are_exact_not_substring(self):
        labels = [t.label for t in DISCOVERY_TARGETS]
        structure = widget_structure(widget_payload("w-c", "Cancelled", measures=["Cancelled"]))
        matches = title_matches(structure, labels)
        self.assertIn("cancelled", matches)
        self.assertNotIn("enrolled & cancelled", matches)
        self.assertNotIn("cancelled - not enrolled", matches)
        self.assertNotIn("cancelled policies", matches)
        people = widget_structure(people_payload())
        people_matches = title_matches(people, labels)
        self.assertIn("enrolled & cancelled", people_matches)
        self.assertIn("cancelled - not enrolled", people_matches)
        self.assertNotIn("cancelled", people_matches)


if __name__ == "__main__":
    unittest.main()
