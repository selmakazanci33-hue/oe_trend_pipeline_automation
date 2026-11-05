"""Phase 3H-J: expose safe structural identity for distinct fingerprints.

Fakes only. No network. Filter members and formula text stay out of the model.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from metrics.cancellation_identification import (
    CONF_EXACT,
    _exact_measure_labels,
    classify_cancellation_widget,
    safe_inventory_row,
)
from metrics.catalog import CANCELLATION_CANDIDATES, METRICS, PROMOTION_CANDIDATES, STATE_PENDING
from metrics.discovery import resolve_targets
from sisense.discovery import (
    HIDDEN_STATE_METADATA,
    FieldIdentity,
    evaluate_discovery,
    explain_fingerprint_differences,
    title_matches,
    widget_structure,
)
from reporting.discovery_report import write_discovery_report
from sisense.passive import definition_fingerprint
from tests.discovery_fixtures import cell, widget_payload, widget_request
from tests.reporting_fixtures import T0
from tests.test_discovery import EXPECTED
from tests.test_phase3h import observe_all
from tests.test_phase3h_c import PRODUCTION_IDS

PROJECT = Path(__file__).resolve().parent.parent
MEMBER = "MEMBER-STATUS-SENTINEL"
LITERAL = "LITERAL-STATUS-SENTINEL"
FORMULA = "FORMULA-TEXT-SENTINEL([Id])"
CONSTANT = 99991


def _measure(title, column_title, context_column="enrollment_status_description"):
    return {
        "jaql": {
            "type": "measure",
            "title": title,
            "columnTitle": column_title,
            "table": "enrollment",
            "column": "Id",
            "formula": FORMULA,
            "context": {
                "[status]": {
                    "table": "enrollment",
                    "column": context_column,
                    "title": context_column,
                    "filter": {"explicit": True, "members": [MEMBER]},
                },
                "[literal]": LITERAL,
                "[constant]": CONSTANT,
            },
        }
    }


def _scope(member=MEMBER):
    return {
        "jaql": {
            "title": "enrollment_status_description",
            "columnTitle": "enrollment_status_description",
            "table": "enrollment",
            "column": "enrollment_status_description",
            "datatype": "text",
            "filter": {"explicit": True, "multiSelection": True, "members": [member]},
        },
        "panel": "scope",
    }


def _date(column="last_update_timestamp", level="days"):
    return {
        "jaql": {
            "title": "Days",
            "columnTitle": column,
            "table": "enrollment",
            "column": column,
            "datatype": "datetime",
            "level": level,
        }
    }


def _body(metadata, widget_id="w-nap", title="New/Active/Passive", guid="g"):
    body = widget_payload(widget_id, title, measures=(), filters=0, query_guid=guid)
    body["metadata"] = metadata
    return body


def _nap(context_column="enrollment_status_description", member=MEMBER, guid="g"):
    return _body(
        [
            _measure("NEW", "NEW", context_column),
            _measure("ACTIVE", "ACTIVE", context_column),
            _measure("PASSIVE", "PASSIVE", context_column),
            _scope(member),
        ],
        guid=guid,
    )


def _policies(column="last_update_timestamp", column_title="Cancelled Policies", guid="daily"):
    return _body(
        [
            _date(column),
            _measure("Policies", column_title),
            _scope(),
        ],
        widget_id="w-policies",
        title="Policy status",
        guid=guid,
    )


def _observed(*bodies):
    observer = observe_all(*(widget_request(body, [cell(1)]) for body in bodies))
    return evaluate_discovery(observer, EXPECTED), observer


def _blob(widget):
    return json.dumps(safe_inventory_row(widget))


class SafeStructureTests(unittest.TestCase):
    def test_column_title_is_safely_retained(self):
        result, _ = _observed(_policies())
        measure = result.widgets[0].structure.measures[0]
        self.assertEqual(measure.title, "Policies")
        self.assertEqual(measure.column_title, "Cancelled Policies")
        self.assertNotEqual(measure.title, measure.column_title)
        self.assertIn("Cancelled Policies", _blob(result.widgets[0]))
        self.assertNotIn("cancelled policies", title_matches(result.widgets[0].structure, ["Cancelled Policies"]))

    def test_result_table_and_column_are_safely_retained(self):
        result, _ = _observed(_policies())
        measure = result.widgets[0].structure.measures[0]
        date = result.widgets[0].structure.dimensions[0]
        self.assertEqual((measure.table, measure.column), ("enrollment", "Id"))
        self.assertEqual((date.table, date.column), ("enrollment", "last_update_timestamp"))

    def test_scope_filter_table_and_column_are_safely_retained(self):
        result, _ = _observed(_nap())
        scope = [role for role in result.widgets[0].structure.metadata_roles if role.panel == "scope"]
        self.assertEqual(len(scope), 1)
        self.assertEqual((scope[0].table, scope[0].column), ("enrollment", "enrollment_status_description"))
        self.assertIn("enrollment_status_description", _blob(result.widgets[0]))

    def test_filter_member_values_are_not_retained(self):
        first = _nap(member="MEMBER-ONE", guid="a")
        second = _nap(member="MEMBER-TWO", guid="b")
        self.assertNotEqual(definition_fingerprint(first), definition_fingerprint(second))
        result, _ = _observed(first, second)
        document = json.dumps([safe_inventory_row(widget) for widget in result.widgets])
        self.assertNotIn("MEMBER-ONE", document)
        self.assertNotIn("MEMBER-TWO", document)
        self.assertNotIn(MEMBER, document)
        differences = explain_fingerprint_differences(result.widgets)
        self.assertEqual(len(differences), 1)
        self.assertFalse(differences[0].column_title_changed)
        self.assertFalse(differences[0].table_column_changed)
        self.assertFalse(differences[0].context_fields_changed)
        self.assertFalse(differences[0].scope_structure_changed)
        self.assertFalse(differences[0].jaql_title_changed)
        self.assertEqual(differences[0].note, HIDDEN_STATE_METADATA)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_discovery_report(resolve_targets(result), result, Path(tmp), T0)
            html = path.read_text(encoding="utf-8")
            document = (path.parent / "wave1_metric_discovery.json").read_text(encoding="utf-8")
        self.assertIn(HIDDEN_STATE_METADATA, html)
        self.assertIn(HIDDEN_STATE_METADATA, document)
        self.assertIn("Definition state comparison", html)
        for secret in ("MEMBER-ONE", "MEMBER-TWO", MEMBER, FORMULA, LITERAL, str(CONSTANT), "formula", "members"):
            self.assertNotIn(secret.lower(), html.lower())
            self.assertNotIn(secret.lower(), document.lower())

    def test_raw_filter_objects_are_not_retained(self):
        result, _ = _observed(_nap())
        row = safe_inventory_row(result.widgets[0])
        for role in row["metadata_roles"]:
            self.assertNotIn("filter", role)
            self.assertNotIn("members", role)
        self.assertNotIn("explicit", json.dumps(row))
        self.assertNotIn("multiSelection", json.dumps(row))

    def test_context_column_identities_are_retained(self):
        result, _ = _observed(_policies())
        measure = result.widgets[0].structure.measures[0]
        self.assertEqual(
            measure.context_fields,
            (FieldIdentity("enrollment", "enrollment_status_description"),),
        )
        self.assertIn("enrollment.enrollment_status_description", _blob(result.widgets[0]))

    def test_raw_formula_text_is_not_retained(self):
        result, _ = _observed(_policies())
        document = _blob(result.widgets[0])
        self.assertNotIn(FORMULA, document)
        self.assertNotIn("FORMULA-TEXT-SENTINEL", document)
        self.assertNotIn("formula", document.lower())
        for role in result.widgets[0].structure.metadata_roles:
            self.assertNotIn(FORMULA, role.title or "")
            self.assertNotIn(FORMULA, role.column_title or "")

    def test_literal_context_values_are_not_retained(self):
        result, _ = _observed(_policies())
        document = _blob(result.widgets[0])
        self.assertNotIn(LITERAL, document)
        self.assertNotIn(str(CONSTANT), document)

    def test_same_titles_show_structural_differences(self):
        left = _nap(context_column="enrollment_status_description", guid="left")
        right = _nap(context_column="coverage_year", guid="right")
        result, _ = _observed(left, right)
        self.assertEqual(
            {tuple(c.title for c in widget.structure.measures) for widget in result.widgets},
            {("NEW", "ACTIVE", "PASSIVE")},
        )
        (difference,) = explain_fingerprint_differences(result.widgets)
        self.assertTrue(difference.context_fields_changed)
        self.assertFalse(difference.jaql_title_changed)
        self.assertFalse(difference.column_title_changed)
        self.assertIsNone(difference.note)

    def test_multiple_fingerprints_remain(self):
        result, observer = _observed(
            _nap(context_column="enrollment_status_description", guid="one"),
            _nap(context_column="coverage_year", guid="two"),
        )
        (entry,) = observer.entries()
        self.assertEqual(len(entry.definitions), 2)
        self.assertEqual(result.widget_count, 1)
        self.assertEqual(result.definition_count, 2)


class CancellationBoundaryTests(unittest.TestCase):
    def test_status_column_alone_is_not_a_cancellation_match(self):
        result, _ = _observed(_nap())
        self.assertIsNone(classify_cancellation_widget(result.widgets[0]))
        metrics = {metric.metric_id: metric for metric in resolve_targets(result)}
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertEqual(metrics[metric_id].state, STATE_PENDING)
            self.assertIn("No exact label match", metrics[metric_id].reason)

    def test_terminated_is_not_inferred_from_cancelled(self):
        result, _ = _observed(_policies())
        widget = result.widgets[0]
        self.assertEqual(_exact_measure_labels(widget), ("cancelled_policies_daily",))
        candidate = classify_cancellation_widget(widget)
        self.assertEqual(candidate.confidence, CONF_EXACT)
        self.assertIn("cancelled_policies_daily", candidate.reason)
        self.assertNotIn("terminated_policies_daily", candidate.reason)
        self.assertEqual(title_matches(widget.structure, ["Cancelled Policies"]), {})

    def test_daily_candidate_requires_exact_date_identity(self):
        wrong, _ = _observed(_policies(column="benefit_effective_date", guid="wrong"))
        right, _ = _observed(_policies(guid="right"))
        wrong_widget = wrong.widgets[0]
        right_widget = right.widgets[0]
        self.assertEqual(_exact_measure_labels(wrong_widget), ())
        wrong_candidate = classify_cancellation_widget(wrong_widget)
        self.assertNotEqual(wrong_candidate.confidence, CONF_EXACT)
        self.assertEqual(_exact_measure_labels(right_widget), ("cancelled_policies_daily",))
        self.assertEqual(classify_cancellation_widget(right_widget).confidence, CONF_EXACT)
        metrics = {metric.metric_id: metric for metric in resolve_targets(right)}
        self.assertEqual(metrics["cancelled_policies_daily"].state, STATE_PENDING)


class InvariantTests(unittest.TestCase):
    def test_no_cancellation_metric_is_promoted(self):
        for metric_id in CANCELLATION_CANDIDATES:
            self.assertNotIn(metric_id, METRICS)
            self.assertNotIn(metric_id, PROMOTION_CANDIDATES)

    def test_seven_production_metrics_unchanged(self):
        self.assertEqual(tuple(METRICS), PRODUCTION_IDS)

    def test_total_applicants_remains_unpromoted(self):
        self.assertEqual(list(PROMOTION_CANDIDATES), ["total_applicants"])
        self.assertNotIn("total_applicants", METRICS)

    def test_application_jaql_remains_one(self):
        main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(main_text.count("retrieve_value("), 2)
        self.assertEqual((PROJECT / "sisense" / "client.py").read_text(encoding="utf-8").count(".request.post("), 1)
        self.assertNotIn(".request.post(", (PROJECT / "sisense" / "discovery.py").read_text(encoding="utf-8"))

    def test_suite_makes_no_network_calls(self):
        forbidden = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "urllib3"}
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({alias.name.split(".")[0] for alias in node.names} & forbidden)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden)
