"""Activity semantic bridge. Fake query results only — no database, no Sisense."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from comparison.models import DIAGNOSTIC, NOT_COMPARABLE
from database.comparison.activity_semantic_bridge import (
    CONTRADICTED,
    DASHBOARD_APPLIED,
    DASHBOARD_IGNORED,
    DIAGNOSTIC_LABEL,
    DIFFERENT_FIELD,
    DRIFTING,
    INSUFFICIENT,
    NOT_AVAILABLE,
    NOT_SUPPORTED,
    PLUS_ONE_LABEL,
    STABLE,
    SUPPORTED,
    UNRESOLVED,
    WIDGET_OWN,
    WIDGET_TITLE,
    build_count_daily_sql,
    build_count_family_sql,
    build_coverage_year_sql,
    build_lineage_agreement_sql,
    build_plus_one_sql,
    build_policy_grain_sql,
    build_report,
    build_scope_profile_sql,
    build_status_value_sql,
    cancelled_policies_checks,
    classify_activity_filters,
    collect_evidence,
    daily_stability,
    decide_count_operator,
    decide_coverage_year,
    decide_daily_aggregation,
    decide_event_date,
    decide_lineage,
    duplicate_factor,
    evaluate_strategies,
    label_plus_one_pattern,
    metric_from_checks,
    write_report,
)
from database.models import QueryResult
from database.query_runner import assert_read_only
from sisense.discovery import widget_structure

PROJECT = Path(__file__).resolve().parents[1]

LIVE_COLUMNS = {
    "coverage_year": "int",
    "enrollment_id": "bigint",
    "enrollment_status_description": "varchar",
    "enrollment_last_update_date": "date",
    "enrollment_create_date": "date",
    "enrollment_confirmation_date": "date",
    "benefit_effective_date": "date",
    "ENROLLEE_ID_PROXY": "varchar",
}

TABLES = [
    ("dbo", "Enrollments_PY2526_NOV30", "BASE TABLE"),
    ("dbo", "Enrollments_PY2526_DEC31", "BASE TABLE"),
    ("dbo", "Enrollments_PY2526_01312026", "BASE TABLE"),
    ("dbo", "Enrollments_PY2526_02272026", "BASE TABLE"),
]


def result(columns, rows):
    materialized = tuple(tuple(row) for row in rows)
    return QueryResult(
        columns=tuple(columns), rows=materialized,
        row_count=len(materialized), duration_seconds=0.0,
    )


def columns_meta():
    return {
        "enrollment_id": {"name": "enrollment_id", "data_type": "bigint"},
        "enrollment_status_description": {
            "name": "enrollment_status_description", "data_type": "varchar",
        },
        "enrollment_last_update_date": {
            "name": "enrollment_last_update_date", "data_type": "date",
        },
        "coverage_year": {"name": "coverage_year", "data_type": "int"},
    }


class FakeClient:
    def __init__(self, tables=None, columns=None):
        self.tables = tables or list(TABLES)
        self.columns = columns or {table: dict(LIVE_COLUMNS) for _, table, _ in self.tables}
        self.calls = []

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        assert_read_only(sql)
        if "2027" in sql or (params and 2027 in params):
            raise AssertionError((sql, params))
        if "COLUMN_NAME = 'last_update_timestamp'" in sql:
            return result(("table_schema", "table_name", "data_type"), [])
        if "AS family_enrollment_keys" in sql:
            return result(
                ("family_enrollment_keys", "family_rows", "keys_with_multiple_family_rows",
                 "keys_with_rows_outside_family", "keys_with_multiple_status_values",
                 "keys_with_multiple_event_dates", "keys_with_repeated_member_rows"),
                [(Decimal("5"), Decimal("6"), Decimal("1"), Decimal("0"), Decimal("0"),
                  Decimal("0"), None)],
            )
        if "AS scope_rows" in sql:
            return result(
                ("insurance_type_value", "scope_rows", "scope_policies",
                 "benefit_year_matches", "benefit_non_null_rows"),
                [(None, Decimal("6"), Decimal("5"), Decimal("6"), Decimal("6"))],
            )
        if "LIKE 'Enrollments[_]PY2526%'" in sql:
            return result(("table_schema", "table_name", "table_type"), self.tables)
        if "ORDINAL_POSITION" in sql:
            return result(("column_name", "data_type"), list(self.columns[params[1]].items()))
        if "AS event_calendar_date" in sql:
            return result(
                ("event_calendar_date", "count_star", "count_enrollment_ids",
                 "count_distinct_enrollment_keys"),
                [(date(2025, 11, 15), Decimal("3"), Decimal("3"), Decimal("3"))],
            )
        if "AS count_star" in sql:
            return result(
                ("count_star", "count_enrollment_ids", "count_distinct_enrollment_keys"),
                [(Decimal("6"), Decimal("6"), Decimal("5"))],
            )
        if "AS observed_status_value" in sql:
            return result(
                ("observed_status_value", "status_row_count", "status_distinct_enrollment_keys"),
                [("Cancelled", Decimal("4"), Decimal("4")),
                 ("Terminated", Decimal("2"), Decimal("2")),
                 ("Termination Pending", Decimal("1"), Decimal("1"))],
            )
        if "AS observed_coverage_year" in sql:
            return result(
                ("observed_coverage_year", "coverage_year_row_count",
                 "cancelled_row_count", "terminated_row_count"),
                [(Decimal("2026"), Decimal("20"), Decimal("6"), Decimal("3"))],
            )
        if "AS event_on_snapshot_plus_one" in sql:
            return result(
                ("event_before_snapshot", "event_on_snapshot",
                 "event_on_snapshot_plus_one", "event_after_snapshot_plus_one"),
                [(Decimal("2"), Decimal("4"), Decimal("1"), Decimal("0"))],
            )
        if "AS date_field_min" in sql:
            return result(
                ("date_field_min", "date_field_max", "date_field_nulls",
                 "date_field_rows", "date_field_distinct_dates"),
                [(date(2025, 10, 1), date(2026, 2, 1), Decimal("0"), Decimal("6"), Decimal("5"))],
            )
        if "AS same_calendar_date_count" in sql:
            return result(
                ("same_calendar_date_count", "candidate_after_other",
                 "candidate_before_other", "other_null_when_candidate_present"),
                [(Decimal("1"), Decimal("4"), Decimal("1"), Decimal("0"))],
            )
        raise AssertionError(sql)


class SemanticBridgeTests(unittest.TestCase):
    def test_count_sql_is_not_distinct_and_not_unioned(self):
        columns = columns_meta()
        qualified = "[dbo].[Enrollments_PY2526_01312026]"
        family_sql, family_params = build_count_family_sql(
            qualified, columns, "enrollment_last_update_date", "date", cancelled=True)
        daily_sql, daily_params = build_count_daily_sql(
            qualified, columns, "enrollment_last_update_date", "date", cancelled=False)
        for sql, params in ((family_sql, family_params), (daily_sql, daily_params)):
            assert_read_only(sql)
            self.assertIn("COUNT(", sql)
            self.assertIn("COUNT(DISTINCT", sql)
            self.assertIn("COUNT_BIG(*)", sql)
            self.assertNotIn("UNION", sql.upper())
            self.assertIn(2026, params)
            self.assertNotIn(2027, params)
        self.assertEqual(family_params[1] if len(family_params) > 1 else family_params[0], "Cancelled")
        self.assertEqual(daily_params[-1] if "Term%" in daily_params else daily_params[0], "Term%")

    def test_duplicate_factor_does_not_silently_deduplicate(self):
        self.assertEqual(duplicate_factor(6, 5), 1.2)
        self.assertIsNone(duplicate_factor(6, 0))
        self.assertEqual(duplicate_factor(4, 4), 1)

    def test_status_sql_lists_term_prefix_not_only_terminated(self):
        sql, params = build_status_value_sql(
            "[dbo].[Enrollments_PY2526_01312026]", columns_meta())
        assert_read_only(sql)
        self.assertIn("Term%", params)
        self.assertIn("Cancelled", params)
        self.assertNotIn("UNION", sql.upper())
        self.assertNotIn(2027, params)

    def test_coverage_year_is_not_equivalent_by_name(self):
        decision = decide_coverage_year([
            {"table": "Enrollments_PY2526_01312026", "observed_coverage_year": 2026},
        ])
        self.assertEqual(decision["decision"], UNRESOLVED)
        self.assertIn("not semantic equivalence", decision["reason"])
        missing = decide_coverage_year([
            {"table": "Enrollments_PY2526_01312026", "observed_coverage_year": 2025},
        ])
        self.assertEqual(missing["decision"], NOT_SUPPORTED)

    def test_currently_enrolled_filters_are_not_copied(self):
        inventory = classify_activity_filters(None, columns_meta())
        roles = {item["role"]: item for item in inventory["inventory"]}
        self.assertEqual(roles["measure_identifier"]["classification"], WIDGET_OWN)
        self.assertEqual(roles["cancelled_measure_filter"]["classification"], WIDGET_OWN)
        self.assertIn("person_type_description", inventory["currently_enrolled_filters_not_copied"])
        self.assertIn("benefit_effective_date", inventory["currently_enrolled_filters_not_copied"])
        self.assertTrue(all(
            item.get("classification") == UNRESOLVED
            for item in inventory["dashboard_filters_unresolved_without_activity_jaql"]
        ))
        self.assertFalse(any(
            "benefit_effective_date" in str(item.get("sisense_field"))
            and item.get("classification") == WIDGET_OWN
            for item in inventory["inventory"]
        ))

    def test_plus_one_pattern_is_not_timezone_proof(self):
        sql, params = build_plus_one_sql(
            "[dbo].[Enrollments_PY2526_01312026]", columns_meta(),
            "enrollment_last_update_date", "date",
            mode="cancelled", snapshot_date="2026-01-31",
        )
        assert_read_only(sql)
        self.assertIn("DATEADD", sql.upper())
        self.assertNotIn("UNION", sql.upper())
        self.assertNotIn(2027, params)
        label = label_plus_one_pattern([
            {"slice": "ALL", "event_on_snapshot_plus_one": 2, "event_after_snapshot_plus_one": 0},
            {"slice": "ALL", "event_on_snapshot_plus_one": 1, "event_after_snapshot_plus_one": 0},
        ])
        self.assertEqual(label, PLUS_ONE_LABEL)
        self.assertIsNone(label_plus_one_pattern([
            {"slice": "ALL", "event_on_snapshot_plus_one": 2, "event_after_snapshot_plus_one": 3},
        ]))

    def test_strategies_are_not_activated(self):
        strategies = evaluate_strategies(
            [{"grain": "family", "duplicate_factor": 1.2}],
            [{"slice": "ALL", "event_on_snapshot_plus_one": 1, "event_after_snapshot_plus_one": 0}],
            {"decision": UNRESOLVED},
        )
        by_id = {item.get("strategy"): item for item in strategies if item.get("strategy")}
        self.assertEqual(by_id["A"]["decision"], UNRESOLVED)
        self.assertEqual(by_id["B"]["decision"], NOT_SUPPORTED)
        self.assertEqual(by_id["C"]["decision"], NOT_SUPPORTED)
        self.assertFalse(by_id["A"]["can_reproduce_sisense_daily_count"])
        self.assertIsNone(next(item.get("activated_strategy") for item in strategies if "activated_strategy" in item))

    def test_event_date_is_not_auto_supported(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertEqual(report["event_date_decision"]["decision"], UNRESOLVED)
        self.assertNotEqual(report["event_date_decision"]["decision"], SUPPORTED)
        self.assertEqual(report["coverage_year"]["decision"], SUPPORTED)
        self.assertEqual(report["lineage_probe"]["status"], NOT_AVAILABLE)
        self.assertEqual(report["daily_aggregation_decision"]["decision"], UNRESOLVED)

    def test_event_date_supported_only_from_behavior_evidence(self):
        candidate = {"py2026_candidate": "enrollment_last_update_date", "sql_datatype": "date",
                     "datetime_compatible": True}
        grain = [{"status_family": "Cancelled", "family_enrollment_keys": 20059,
                  "keys_with_multiple_event_dates": 0, "keys_with_multiple_family_rows": 6000}]
        profiles = [{"field": "enrollment_last_update_date", "date_field_rows": 28188,
                     "null_rate": 0.0, "date_field_distinct_dates": 120}]
        plus = [{"slice": "ALL", "event_on_snapshot_plus_one": 3, "event_after_snapshot_plus_one": 0}]
        lineage = {"status": NOT_AVAILABLE}
        self.assertEqual(decide_event_date(candidate, grain, profiles, plus, lineage)["decision"], SUPPORTED)
        multi = [{**grain[0], "keys_with_multiple_event_dates": 2}]
        self.assertEqual(decide_event_date(candidate, multi, profiles, plus, lineage)["decision"], UNRESOLVED)
        future = [{**plus[0], "event_after_snapshot_plus_one": 4}]
        self.assertEqual(decide_event_date(candidate, grain, profiles, future, lineage)["decision"], UNRESOLVED)
        sparse = [{**profiles[0], "date_field_distinct_dates": 3}]
        self.assertEqual(decide_event_date(candidate, grain, sparse, plus, lineage)["decision"], UNRESOLVED)
        self.assertEqual(decide_event_date(candidate, [], profiles, plus, lineage)["decision"], UNRESOLVED)
        different = {"status": DIFFERENT_FIELD, "reason": "x"}
        self.assertEqual(decide_event_date(candidate, grain, profiles, plus, different)["decision"], NOT_SUPPORTED)
        shifted = {"status": CONTRADICTED, "reason": "x"}
        self.assertEqual(decide_event_date(candidate, grain, profiles, plus, shifted)["decision"], SUPPORTED)

    def test_count_operator_follows_sisense_function(self):
        grain = [{"status_family": "Cancelled", "family_enrollment_keys": 20059,
                  "keys_with_multiple_event_dates": 0, "keys_with_multiple_family_rows": 6000}]
        unique = decide_count_operator(["count"], grain, "Cancelled")
        self.assertEqual(unique["decision"], SUPPORTED)
        self.assertEqual(unique["historical_operator"], "COUNT(DISTINCT enrollment_id)")
        self.assertEqual(decide_count_operator(None, grain, "Cancelled")["decision"], UNRESOLVED)
        self.assertEqual(decide_count_operator(["dupcount"], grain, "Cancelled")["decision"], UNRESOLVED)
        single = [{**grain[0], "keys_with_multiple_family_rows": 0}]
        self.assertEqual(decide_count_operator(["dupcount"], single, "Cancelled")["decision"], SUPPORTED)
        ambiguous = [{**grain[0], "keys_with_multiple_event_dates": 1}]
        self.assertEqual(decide_count_operator(["count"], ambiguous, "Cancelled")["decision"], UNRESOLVED)
        self.assertEqual(decide_count_operator(["count"], grain, "Terminated")["decision"], UNRESOLVED)

    def test_coverage_year_supported_by_benefit_year(self):
        rows = [{"table": "Enrollments_PY2526_01312026", "observed_coverage_year": 2026}]
        supported = decide_coverage_year(rows, [{"benefit_year_matches": 995, "benefit_non_null_rows": 1000}])
        self.assertEqual(supported["decision"], SUPPORTED)
        weak = decide_coverage_year(rows, [{"benefit_year_matches": 800, "benefit_non_null_rows": 1000}])
        self.assertEqual(weak["decision"], UNRESOLVED)

    def test_daily_stability_detects_stable_and_drifting(self):
        def day_rows(table, values):
            return [
                {"table": table, "status_family": "Cancelled", "grain": "day",
                 "event_calendar_date": f"2026-01-{day:02d}", "count_distinct_enrollment_keys": value}
                for day, value in values.items()
            ]
        early = {day: 100 for day in range(1, 31)}
        dates = {"Enrollments_PY2526_01312026": "2026-01-31", "Enrollments_PY2526_02272026": "2026-02-27"}
        stable = daily_stability(
            day_rows("Enrollments_PY2526_01312026", early)
            + day_rows("Enrollments_PY2526_02272026", {day: 99 for day in early}), dates)
        self.assertEqual(stable["families"]["Cancelled"]["status"], STABLE)
        self.assertEqual(stable["families"]["Terminated"]["status"], INSUFFICIENT)
        drifting = daily_stability(
            day_rows("Enrollments_PY2526_01312026", early)
            + day_rows("Enrollments_PY2526_02272026", {day: 80 for day in early}), dates)
        self.assertEqual(drifting["families"]["Cancelled"]["status"], DRIFTING)
        strategy = evaluate_strategies([], [], {"decision": SUPPORTED}, stable, "Cancelled")
        self.assertEqual(strategy[0]["decision"], SUPPORTED)
        self.assertEqual(strategy[-1]["activated_strategy"], "A")
        blocked = evaluate_strategies([], [], {"decision": SUPPORTED}, drifting, "Cancelled")
        self.assertEqual(blocked[0]["decision"], UNRESOLVED)

    def test_lineage_and_daily_boundary(self):
        self.assertEqual(decide_lineage([])["status"], NOT_AVAILABLE)
        agree = decide_lineage([{"live_table": "enrollment", "matched_keys": 5000, "same_date": 4000,
                                 "live_plus_one": 2, "live_minus_one": 0, "live_later": 998,
                                 "live_earlier": 0}])
        self.assertEqual(agree["status"], SUPPORTED)
        shifted = decide_lineage([{"live_table": "enrollment", "matched_keys": 5000, "same_date": 3000,
                                   "live_plus_one": 1000, "live_minus_one": 0, "live_later": 1000,
                                   "live_earlier": 0}])
        self.assertEqual(shifted["status"], CONTRADICTED)
        unrelated = decide_lineage([{"live_table": "enrollment", "matched_keys": 5000, "same_date": 100,
                                     "live_plus_one": 0, "live_minus_one": 0, "live_later": 900,
                                     "live_earlier": 4000}])
        self.assertEqual(unrelated["status"], DIFFERENT_FIELD)
        filters = {"date_dimension": {"verified": True}}
        candidate = {"datetime_compatible": True}
        self.assertEqual(decide_daily_aggregation(filters, candidate, agree)["decision"], SUPPORTED)
        self.assertEqual(decide_daily_aggregation(filters, candidate, shifted)["decision"], NOT_SUPPORTED)
        self.assertEqual(decide_daily_aggregation(filters, candidate, {"status": NOT_AVAILABLE})["decision"],
                         UNRESOLVED)
        self.assertEqual(decide_daily_aggregation({}, candidate, agree)["decision"], UNRESOLVED)

    def test_new_sql_is_read_only_and_py2026_only(self):
        columns = {**columns_meta(),
                   "enrollee_id": {"name": "enrollee_id", "data_type": "bigint"},
                   "insurance_type_description": {"name": "insurance_type_description", "data_type": "varchar"},
                   "benefit_effective_date": {"name": "benefit_effective_date", "data_type": "date"}}
        qualified = "[dbo].[Enrollments_PY2526_01312026]"
        grain_sql, grain_params = build_policy_grain_sql(
            qualified, columns, "enrollment_last_update_date", "date", cancelled=True)
        self.assertEqual(grain_params, ("Cancelled", 2026, "Cancelled"))
        self.assertIn("COUNT(DISTINCT [enrollee_id])", grain_sql)
        scope_sql, scope_params = build_scope_profile_sql(qualified, columns, cancelled=False)
        self.assertIn("GROUP BY [insurance_type_description]", scope_sql)
        self.assertEqual(scope_params, (2026, 2026, "Term%"))
        daily_sql, daily_params = build_count_daily_sql(
            qualified, columns, "enrollment_last_update_date", "date", cancelled=True, apply_health=True)
        self.assertIn("[insurance_type_description] = ?", daily_sql)
        self.assertEqual(daily_params[-1], "Health")
        live = {"enrollment_id": {"name": "enrollment_id"},
                "last_update_timestamp": {"name": "last_update_timestamp"},
                "coverage_year": {"name": "coverage_year"}}
        lineage_sql, lineage_params = build_lineage_agreement_sql(
            "[dbo].[enrollment]", live, qualified, columns, "enrollment_last_update_date", "date")
        for sql, params in ((grain_sql, grain_params), (scope_sql, scope_params),
                            (lineage_sql, lineage_params)):
            assert_read_only(sql)
            self.assertNotIn("UNION", sql.upper())
            self.assertNotIn(2027, params)
            self.assertNotIn("2027", sql)

    def test_activity_widget_filters_from_captured_flags(self):
        columns = {**columns_meta(),
                   "insurance_type_description": {"name": "insurance_type_description", "data_type": "varchar"}}

        def discovery(scope):
            return {"widget_inventory": [{
                "widget_id": "w1",
                "widget_title": WIDGET_TITLE,
                "measure_titles": ["Cancelled Policies", "Terminated Policies"],
                "metadata_roles": [
                    {"panel": "result", "kind": "dimension", "table": "enrollment",
                     "column": "last_update_timestamp", "date_level": "days"},
                    {"panel": "result", "kind": "measure", "title": "Cancelled Policies",
                     "aggregation_functions": ["count"]},
                    {"panel": "result", "kind": "measure", "title": "Terminated Policies",
                     "aggregation_functions": ["count"]},
                ] + scope,
            }]}

        year_role = {"panel": "scope", "kind": "filter", "table": "ssap_applications",
                     "column": "coverage_year", "is_dashboard_filter": True, "disabled": False,
                     "filter_kind": "member_list", "matches_verified_dashboard_filter": True}
        ignored = classify_activity_filters(discovery([year_role]), columns)
        self.assertEqual(ignored["decision"], SUPPORTED)
        self.assertEqual(ignored["historical_predicates"], ["coverage_year = 2026"])
        self.assertFalse(ignored["apply_insurance_health"])
        self.assertTrue(ignored["date_dimension"]["verified"])
        self.assertEqual(ignored["measure_aggregation_functions"]["Cancelled"], ["count"])
        by_field = {item["sisense_field"]: item for item in ignored["inventory"] if "mapping_status" in item}
        self.assertEqual(by_field["ssap_applications.coverage_year"]["classification"], DASHBOARD_APPLIED)
        self.assertEqual(by_field["enrollment.insurance_type_description"]["classification"], DASHBOARD_IGNORED)

        health_role = {"panel": "scope", "kind": "filter", "table": "enrollment",
                       "column": "insurance_type_description", "is_dashboard_filter": True,
                       "disabled": False, "filter_kind": "member_list",
                       "matches_verified_dashboard_filter": True}
        applied = classify_activity_filters(discovery([year_role, health_role]), columns)
        self.assertEqual(applied["decision"], SUPPORTED)
        self.assertTrue(applied["apply_insurance_health"])

        other = {**health_role, "matches_verified_dashboard_filter": False}
        self.assertEqual(classify_activity_filters(discovery([year_role, other]), columns)["decision"], UNRESOLVED)
        uncaptured = {**health_role, "matches_verified_dashboard_filter": None}
        self.assertEqual(
            classify_activity_filters(discovery([year_role, uncaptured]), columns)["decision"], UNRESOLVED)
        own = {"panel": "scope", "kind": "filter", "table": "enrollment", "column": "hios_issuer_id",
               "is_dashboard_filter": False, "disabled": False, "filter_kind": "member_list",
               "matches_verified_dashboard_filter": None}
        self.assertEqual(classify_activity_filters(discovery([year_role, own]), columns)["decision"], UNRESOLVED)
        disabled = {**own, "disabled": True}
        self.assertEqual(classify_activity_filters(discovery([year_role, disabled]), columns)["decision"], SUPPORTED)
        no_year = classify_activity_filters(discovery([]), columns)
        self.assertEqual(no_year["decision"], UNRESOLVED)
        legacy = {key: value for key, value in year_role.items() if key != "is_dashboard_filter"}
        self.assertEqual(classify_activity_filters(discovery([legacy]), columns)["decision"], UNRESOLVED)

    def test_discovery_captures_filter_flags_without_member_values(self):
        structure = widget_structure({
            "dashboard": "d1;PY 2027 - OE Executive Dashboard",
            "widget": f"w1;{WIDGET_TITLE}",
            "datasource": {"id": "localhost_aMAIN", "title": "MAIN"},
            "metadata": [
                {"jaql": {"table": "enrollment", "column": "last_update_timestamp",
                          "datatype": "datetime", "level": "days", "title": "Days"}},
                {"jaql": {"type": "measure", "formula": "Count([A1]) ", "title": "Cancelled Policies",
                          "context": {"[A1]": {"table": "enrollment", "column": "id"}}}},
                {"jaql": {"type": "measure", "formula": "CUSTOMNAME([A2]) ", "title": "Other",
                          "context": {"[A2]": {"table": "enrollment", "column": "id"}}}},
                {"jaql": {"table": "ssap_applications", "column": "coverage_year",
                          "isDashboardFilter": True,
                          "filter": {"multiSelection": True, "members": ["2027"], "explicit": True}},
                 "panel": "scope", "disabled": False, "locked": True},
                {"jaql": {"table": "enrollment", "column": "insurance_type_description",
                          "isDashboardFilter": True,
                          "filter": {"explicit": True, "multiSelection": True, "members": ["Dental"]}},
                 "panel": "scope"},
                {"jaql": {"table": "enrollment", "column": "hios_issuer_id",
                          "filter": {"explicit": True, "multiSelection": True, "members": ["2027"]}},
                 "panel": "scope"},
            ],
        })
        year, insurance, issuer = [role for role in structure.metadata_roles if role.panel == "scope"]
        count, custom = [role for role in structure.metadata_roles if role.kind == "measure"]
        self.assertTrue(year.is_dashboard_filter)
        self.assertFalse(year.disabled)
        self.assertTrue(year.locked)
        self.assertEqual(year.filter_kind, "member_list")
        self.assertIs(year.matches_verified_dashboard_filter, True)
        self.assertIs(insurance.matches_verified_dashboard_filter, False)
        self.assertIsNone(issuer.matches_verified_dashboard_filter)
        self.assertEqual(count.aggregation_functions, ("count",))
        self.assertEqual(custom.aggregation_functions, ("other",))
        persisted = json.dumps([
            {key: value for key, value in vars(role).items() if key != "context_fields"}
            for role in structure.metadata_roles
        ])
        for leaked in ("2027", "Dental", "members", "explicit", "multiSelection", "customname", "sha256"):
            self.assertNotIn(leaked.lower(), persisted.lower())

    def test_term_values_are_all_listed(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertIn("Terminated", report["term_status_values_observed"])
        self.assertIn("Termination Pending", report["term_status_values_observed"])
        factor = next(
            item["duplicate_factor"] for item in report["count_semantics"]
            if item["grain"] == "family" and item["status_family"] == "Cancelled"
        )
        self.assertEqual(factor, 1.2)

    def test_comparable_requires_every_check(self):
        unresolved = cancelled_policies_checks(
            identifier_present=True, count_ready=True, count_factor=1,
            status_observed=True, event_decision=UNRESOLVED,
            coverage_decision=UNRESOLVED, filter_decision=UNRESOLVED,
            strategy_decision=UNRESOLVED, term_values=["Terminated"],
        )
        metric = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", unresolved)
        self.assertEqual(metric["comparison_status"], DIAGNOSTIC)
        self.assertFalse(metric["full_metric_comparable"])
        passing = cancelled_policies_checks(
            identifier_present=True, count_ready=True, count_factor=1,
            status_observed=True, event_decision=SUPPORTED,
            coverage_decision=SUPPORTED, filter_decision=SUPPORTED,
            strategy_decision=SUPPORTED, term_values=["Terminated"],
            count_decision=SUPPORTED, daily_decision=SUPPORTED,
        )
        comparable = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", passing)
        self.assertEqual(comparable["comparison_status"], "COMPARABLE")
        no_count = cancelled_policies_checks(
            identifier_present=True, count_ready=True, count_factor=1,
            status_observed=True, event_decision=SUPPORTED,
            coverage_decision=SUPPORTED, filter_decision=SUPPORTED,
            strategy_decision=SUPPORTED, term_values=["Terminated"], daily_decision=SUPPORTED,
        )
        self.assertEqual(
            metric_from_checks("cancelled_policies_daily", "Cancelled Policies", no_count)["comparison_status"],
            DIAGNOSTIC)
        boundary = cancelled_policies_checks(
            identifier_present=True, count_ready=True, count_factor=1,
            status_observed=True, event_decision=SUPPORTED,
            coverage_decision=SUPPORTED, filter_decision=SUPPORTED,
            strategy_decision=SUPPORTED, term_values=["Terminated"],
            count_decision=SUPPORTED, daily_decision=NOT_SUPPORTED,
        )
        self.assertEqual(
            metric_from_checks("cancelled_policies_daily", "Cancelled Policies", boundary)["comparison_status"],
            NOT_COMPARABLE)
        missing = cancelled_policies_checks(
            identifier_present=False, count_ready=False, status_observed=False,
            event_decision=NOT_SUPPORTED, coverage_decision=NOT_SUPPORTED,
            filter_decision=UNRESOLVED, strategy_decision=NOT_SUPPORTED,
            term_values=[],
        )
        blocked = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", missing)
        self.assertEqual(blocked["comparison_status"], NOT_COMPARABLE)

    def test_collect_evidence_does_not_union_or_query_2027(self):
        client = FakeClient()
        report = build_report(collect_evidence(client))
        self.assertEqual(report["label"], DIAGNOSTIC_LABEL)
        self.assertEqual(report["py2027_widget"], WIDGET_TITLE)
        self.assertTrue(report["not_official_activity"])
        self.assertEqual(report["comparable"], [])
        self.assertEqual(report["cancelled_policies"]["comparison_status"], DIAGNOSTIC)
        self.assertEqual(report["terminated_policies"]["comparison_status"], DIAGNOSTIC)
        self.assertEqual(report["out_of_scope_this_phase"]["currently_enrolled_people"], "unchanged")
        self.assertEqual(report["plus_one_pattern"]["label"], PLUS_ONE_LABEL)
        self.assertIsNone(next(
            item.get("activated_strategy")
            for item in report["extraction_strategies"] if "activated_strategy" in item
        ))
        for sql, params in client.calls:
            self.assertNotIn("UNION", sql.upper())
            self.assertNotIn(2027, params or ())
        relative = [sql for sql, _params in client.calls if "AS event_on_snapshot_plus_one" in sql]
        self.assertTrue(relative)
        self.assertTrue(all("01312026" in sql or "02272026" in sql for sql in relative))
        self._assert_keys(report)

    def test_coverage_year_sql_has_no_year_parameter(self):
        sql, params = build_coverage_year_sql(
            "[dbo].[Enrollments_PY2526_01312026]", columns_meta())
        assert_read_only(sql)
        self.assertNotIn(2026, params)
        self.assertNotIn(2027, params)
        self.assertEqual(params, ("Cancelled", "Term%"))

    def test_write_report_and_html_sections(self):
        report = build_report(collect_evidence(FakeClient()))
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_report(report, Path(directory))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            page = html_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["report_kind"], "PY2026_ACTIVITY_SEMANTIC_BRIDGE")
        self.assertEqual(json_path.name, "py2026_activity_semantic_bridge.json")
        self.assertEqual(html_path.name, "py2026_activity_semantic_bridge.html")
        for heading in (
            "COUNT semantics", "Status values", "Coverage year",
            "Activity widget filters", "Event-date field comparison",
            "+1 day pattern", "Extraction strategies",
            "Cancelled Policies checks", "Terminated Policies checks",
        ):
            self.assertIn(heading, page)
        self.assertNotIn("<canvas", page.lower())
        self.assertNotIn("chart", page.lower())

    def test_command_module_does_not_change_main_or_catalog(self):
        main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
        catalog = (PROJECT / "metrics" / "catalog.py").read_text(encoding="utf-8")
        self.assertNotIn("database.comparison.activity_semantic_bridge", main_source)
        self.assertNotIn("activity_semantic_bridge", catalog)

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
