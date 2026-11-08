"""Passive PY2027 status inventory. Mocked events and local fixtures only."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from comparison.currently_enrolled import VERIFIED_PAYLOAD_FILE, interpret_payload
from sisense.status_metric_inventory import (
    FILTER_ONLY,
    NEEDS_VALIDATION,
    NOT_OBSERVED,
    PASSIVE,
    PY2026_MISSING,
    PY2027_NOT_VERIFIED,
    TEXT_ONLY,
    VERIFIED,
    build_inventory,
    canonical_keyword,
    classify_status_target,
    keyword_in_text,
    meets_verified_requirements,
    print_table,
    read_validated_metrics,
    render_html,
    sanitize_observation,
    write_report,
)
from tests.discovery_fixtures import FILTER_SENTINEL


def definition() -> dict:
    return interpret_payload(json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8")))


def payload_digest() -> str:
    return hashlib.sha256(VERIFIED_PAYLOAD_FILE.read_bytes()).hexdigest()


def inventory(**kwargs) -> dict:
    return build_inventory(definition(), **kwargs)


def by_id(rows: list[dict], metric_id: str) -> dict:
    return next(item for item in rows if item["metric_id"] == metric_id)


class KeywordTests(unittest.TestCase):
    def test_canceled_normalizes_to_cancelled(self):
        self.assertEqual(canonical_keyword("Canceled"), "cancelled")
        self.assertEqual(canonical_keyword("Cancelled"), "cancelled")
        self.assertEqual(canonical_keyword("Cancellation"), "cancelled")
        self.assertEqual(canonical_keyword("Cancellations"), "cancelled")

    def test_termination_normalizes_to_terminated(self):
        self.assertEqual(canonical_keyword("Terminated"), "terminated")
        self.assertEqual(canonical_keyword("Termination"), "terminated")
        self.assertEqual(canonical_keyword("Terminations"), "terminated")

    def test_pending_is_exact(self):
        self.assertEqual(canonical_keyword("Pending"), "pending")
        self.assertIsNone(canonical_keyword("Enrolled & Cancelled"))

    def test_keyword_in_longer_title(self):
        self.assertTrue(keyword_in_text("Enrolled & Cancelled", "cancelled"))
        self.assertTrue(keyword_in_text("Terminated Policies — Daily", "terminated"))
        self.assertFalse(keyword_in_text("Currently Enrolled People", "pending"))


class FilterMemberTests(unittest.TestCase):
    def test_pending_and_terminated_are_filter_members_of_verified_payload(self):
        pending = classify_status_target("pending", definition())
        terminated = classify_status_target("terminated", definition())
        self.assertEqual(pending["classification"], FILTER_ONLY)
        self.assertEqual(terminated["classification"], FILTER_ONLY)
        self.assertEqual(pending["widget"], "Currently Enrolled People")
        self.assertEqual(pending["widget_id"], "6a98048ccef60720fe7f9b47")
        self.assertFalse(meets_verified_requirements(pending))
        self.assertFalse(meets_verified_requirements(terminated))

    def test_cancelled_is_not_a_verified_payload_filter_member(self):
        cancelled = classify_status_target("cancelled", definition())
        self.assertEqual(cancelled["classification"], NOT_OBSERVED)

    def test_filter_member_is_not_promoted_when_currently_enrolled_has_a_value(self):
        result = inventory(snapshots={
            "currently_enrolled_people": {"metric_value": 797301, "retrieved_at_utc": "2026-11-08T15:04:05Z"},
        })
        pending = by_id(result["target_status_metrics"], "pending")
        self.assertEqual(pending["classification"], FILTER_ONLY)
        self.assertIsNone(pending["value"])
        self.assertFalse(meets_verified_requirements(pending))
        currently = by_id(result["enrolled_metrics"], "currently_enrolled_people")
        self.assertEqual(currently["classification"], VERIFIED)
        self.assertEqual(currently["value"], 797301)

    def test_filter_member_event_does_not_become_a_metric(self):
        events = [{
            "sequence": 1,
            "endpoint": "/api/datasources/MAIN/jaql",
            "method": "POST",
            "widget_id": "6a98048ccef60720fe7f9b47",
            "widget_title": "Currently Enrolled People",
            "filter_member_labels": ["Cancelled", FILTER_SENTINEL],
            "headers": {"Authorization": "Bearer secret"},
        }]
        cancelled = classify_status_target("cancelled", definition(), events=events)
        self.assertEqual(cancelled["classification"], FILTER_ONLY)
        self.assertFalse(meets_verified_requirements(cancelled))


class TextAndPassiveTests(unittest.TestCase):
    def test_longer_title_is_text_only(self):
        discovery = {
            "widgets": [{
                "widget_id": "w-text",
                "widget_title": "Enrolled & Cancelled",
                "measures": ["Enrolled & Cancelled"],
            }],
        }
        cancelled = classify_status_target("cancelled", definition(), discovery)
        self.assertEqual(cancelled["classification"], TEXT_ONLY)
        self.assertEqual(cancelled["widget_id"], "w-text")

    def test_termination_title_is_exact_terminated_evidence(self):
        discovery = {
            "widgets": [{
                "widget_id": "w-term",
                "widget_title": "Termination",
                "measures": ["Termination"],
            }],
        }
        terminated = classify_status_target("terminated", definition(), discovery)
        self.assertEqual(terminated["classification"], PASSIVE)
        self.assertEqual(terminated["widget"], "Termination")
        self.assertFalse(meets_verified_requirements(terminated))

    def test_canceled_measure_is_cancelled_passive_evidence(self):
        events = [{
            "sequence": 2,
            "widget_id": "w-cancel",
            "widget_title": "Status mix",
            "measures": ["Canceled"],
            "datasource": "MAIN",
        }]
        cancelled = classify_status_target("cancelled", definition(), events=events)
        self.assertEqual(cancelled["classification"], PASSIVE)
        self.assertEqual(cancelled["widget_id"], "w-cancel")

    def test_wave1_discovered_label_is_passive_not_verified(self):
        discovery = {
            "metrics": [{
                "metric_id": "cancelled",
                "label": "Cancelled",
                "state": "DISCOVERED",
                "widget_ids": ["w-disc"],
            }],
        }
        cancelled = classify_status_target("cancelled", definition(), discovery)
        self.assertEqual(cancelled["classification"], PASSIVE)
        self.assertNotEqual(cancelled["classification"], VERIFIED)

    def test_complete_natural_request_meets_verified_requirements(self):
        events = [{
            "widget_id": "w-pending",
            "widget_title": "Pending",
            "aggregation": "Count",
            "identifier": "enrollee.exchg_indiv_identifier",
            "plan_year": 2027,
            "filters": [{"column": "enrollment_status_description", "members": ["Pending"]}],
            "numeric_result": 12,
            "natural_widget_request": True,
        }]
        pending = classify_status_target("pending", definition(), events=events)
        self.assertEqual(pending["classification"], VERIFIED)
        self.assertEqual(pending["value"], 12)
        self.assertTrue(meets_verified_requirements(pending))

    def test_incomplete_exact_widget_stays_passive(self):
        events = [{
            "widget_id": "w-pending",
            "widget_title": "Pending",
            "measures": ["Pending"],
        }]
        pending = classify_status_target("pending", definition(), events=events)
        self.assertEqual(pending["classification"], PASSIVE)
        self.assertFalse(meets_verified_requirements(pending))


class SanitizationTests(unittest.TestCase):
    def test_secret_keys_and_headers_are_redacted(self):
        dirty = {
            "widget_title": "Pending",
            "cookie": "session=abc",
            "headers": {"Authorization": "Bearer xyz", "X-XSRF-TOKEN": "csrf"},
            "authorization": "secret",
            "csrf": "token",
            "storage_state": {"cookies": []},
            "jaql_structure": {"title": "Pending", "table": "enrollee", "column": "exchg_indiv_identifier"},
        }
        clean = sanitize_observation(dirty)
        self.assertNotIn("cookie", clean)
        self.assertNotIn("headers", clean)
        self.assertNotIn("authorization", clean)
        self.assertNotIn("csrf", clean)
        self.assertNotIn("storage_state", clean)
        self.assertEqual(clean["widget_title"], "Pending")
        self.assertEqual(clean["jaql_structure"]["column"], "exchg_indiv_identifier")

    def test_inventory_serialization_drops_injected_secrets(self):
        events = [{
            "widget_id": "w1",
            "widget_title": "Cancelled",
            "measures": ["Cancelled"],
            "cookie": "secret",
            "headers": {"Authorization": "Bearer no"},
            "password": "no",
        }]
        result = inventory(events=events)
        dumped = json.dumps(result)
        self.assertNotIn("Bearer no", dumped)
        self.assertNotIn("password", dumped)
        self.assertNotIn('"cookie"', dumped)
        self.assertNotIn("secret", dumped)
        json.loads(dumped)


class InventoryTests(unittest.TestCase):
    def test_currently_enrolled_is_unchanged_verified_metric(self):
        result = inventory()
        currently = by_id(result["enrolled_metrics"], "currently_enrolled_people")
        self.assertEqual(currently["classification"], VERIFIED)
        self.assertEqual(currently["widget"], "Currently Enrolled People")
        self.assertEqual(currently["widget_id"], "6a98048ccef60720fe7f9b47")
        self.assertEqual(currently["aggregation"], "Count")
        self.assertEqual(currently["identifier"], "enrollee.exchg_indiv_identifier")
        self.assertEqual(currently["plan_year"], 2027)

    def test_new_active_passive_remain_catalog_verified_without_changing_stabilization(self):
        result = inventory()
        for metric_id, measure in (
            ("new_enrolled", "NEW"),
            ("active_enrolled", "ACTIVE"),
            ("passive_enrolled", "PASSIVE"),
        ):
            row = by_id(result["enrolled_metrics"], metric_id)
            self.assertEqual(row["classification"], VERIFIED)
            self.assertEqual(row["widget"], "New/Active/Passive - Enrolled People")
            self.assertEqual(row["widget_id"], "6a98048ccef60720fe7f9b6e")
            self.assertEqual(row["identifier"], measure)

    def test_application_metrics_do_not_invent_a_total(self):
        result = inventory()
        started = by_id(result["application_metrics"], "applications_started")
        submitted = by_id(result["application_metrics"], "submitted_applications")
        total = by_id(result["application_metrics"], "application_total")
        self.assertEqual(started["classification"], VERIFIED)
        self.assertEqual(submitted["classification"], VERIFIED)
        self.assertEqual(total["classification"], PASSIVE)
        self.assertEqual(total["label"], "Application Total")
        self.assertIsNone(total["value"])

    def test_readiness_matrix(self):
        result = inventory()
        rows = {item["metric"]: item for item in result["readiness_matrix"]}
        self.assertEqual(rows["Currently Enrolled"]["comparison_readiness"], NEEDS_VALIDATION)
        self.assertEqual(rows["Currently Enrolled"]["py2026_db"], "LATEST_STATE_POPULATION")
        self.assertEqual(rows["Pending"]["comparison_readiness"], PY2027_NOT_VERIFIED)
        self.assertEqual(rows["Cancelled"]["comparison_readiness"], PY2027_NOT_VERIFIED)
        self.assertEqual(rows["Terminated"]["comparison_readiness"], PY2027_NOT_VERIFIED)
        self.assertEqual(rows["New Enrolled"]["py2026_db"], PY2026_MISSING)
        self.assertEqual(rows["Applications Started"]["py2026_db"], PY2026_MISSING)
        self.assertEqual(result["turned_off_semantics"], "unresolved")
        self.assertEqual(
            result["concepts_preserved"],
            ["SNAPSHOT POPULATION", "MONTHLY ENROLLMENT ACTIVITY", "LATEST STATE POPULATION"],
        )

    def test_html_is_tables_not_charts(self):
        page = render_html(inventory())
        self.assertIn("Target status metrics", page)
        self.assertIn("Final dashboard readiness", page)
        self.assertNotIn("<canvas", page)
        self.assertNotIn("<svg", page)
        self.assertNotIn("Chart.js", page)

    def test_write_report_round_trip_and_payload_untouched(self):
        before = payload_digest()
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(inventory(), Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["report_kind"], "PY2027_STATUS_METRIC_INVENTORY")
            self.assertTrue(html_path.is_file())
            self.assertTrue(loaded["no_experimental_jaql"])
            self.assertTrue(loaded["verified_payload_unmodified"])
            self.assertFalse(loaded["visualization_created"])
        self.assertEqual(payload_digest(), before)

    def test_print_table_is_text(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_table(inventory())
        text = buffer.getvalue()
        self.assertIn("Pending", text)
        self.assertIn("FILTER_MEMBER_ONLY", text)
        self.assertIn("no JAQL sent", text)

    def test_sqlite_value_is_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE metric_snapshots (
                    run_id TEXT, metric_id TEXT, metric_value NUMERIC, plan_year INTEGER,
                    retrieved_at_utc TEXT, snapshot_slot_utc TEXT, retrieval_status TEXT
                )
                """
            )
            connection.execute(
                """INSERT INTO metric_snapshots VALUES
                   ('run-1', 'currently_enrolled_people', 424242, 2027, '2026-11-08T15:04:05Z',
                    '2026-11-08T15:00:00Z', 'VALIDATED')"""
            )
            connection.execute(
                """INSERT INTO metric_snapshots VALUES
                   ('run-1', 'new_enrolled', 11, 2027, '2026-11-08T15:04:05Z',
                    '2026-11-08T15:00:00Z', 'VALIDATED')"""
            )
            connection.commit()
            connection.close()
            snapshots = read_validated_metrics(path)
        self.assertEqual(snapshots["currently_enrolled_people"]["metric_value"], 424242)
        result = inventory(snapshots=snapshots)
        self.assertEqual(by_id(result["enrolled_metrics"], "currently_enrolled_people")["value"], 424242)
        self.assertEqual(by_id(result["enrolled_metrics"], "new_enrolled")["value"], 11)

    def test_does_not_mutate_verified_payload_object(self):
        payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
        original = json.dumps(payload, sort_keys=True)
        parsed = interpret_payload(payload)
        build_inventory(parsed)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)


if __name__ == "__main__":
    unittest.main()
