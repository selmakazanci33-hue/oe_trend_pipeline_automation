"""PY2026 vs PY2027 Cancelled/Terminated activity semantic bridge.

Read-only diagnostics. Does not send JAQL. Does not query coverage year 2027.
Does not union snapshots. Does not create official activity output.
Does not copy Currently Enrolled filters onto this metric.

Every check is decided from captured evidence against the thresholds below.
Missing evidence stays UNRESOLVED; contradicting evidence is NOT_SUPPORTED.

    python -m database.comparison.activity_semantic_bridge
    python -m database.comparison.activity_semantic_bridge --existing
"""

from __future__ import annotations

import html
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from comparison.currently_enrolled import (
    OUTPUT_DIR,
    VERIFIED_PAYLOAD_FILE,
    guard_no_pii,
    interpret_payload,
)
from comparison.diagnostic_numbers import COLUMNS_SQL, TABLES_SQL
from comparison.lifecycle import (
    INBOUND_MARKERS,
    PREVIOUS_PLAN_YEAR,
    QUERY_TIMEOUT_SECONDS,
    SNAPSHOT_PREFIX,
)
from comparison.models import COMPARABLE, DIAGNOSTIC, NOT_COMPARABLE
from database.comparison.event_date_validation import (
    SELECTED_SNAPSHOTS,
    snapshot_date_context,
)
from database.comparison.field_mapping import (
    is_blocked_event_date_name,
    is_datetime_sql_type,
    select_event_date_candidate,
)
from database.discovery.enrollment_source import qualified_name, quote_identifier
from database.models import DatabaseError, QueryExecutionError
from database.query_runner import assert_read_only
from sisense.discovery import filter_definition_fingerprint, filter_kind, normalize_label
from sisense.status_metric_inventory import load_discovery_artifact

JSON_NAME = "py2026_activity_semantic_bridge.json"
HTML_NAME = "py2026_activity_semantic_bridge.html"
EXISTING_JSON_NAME = "py2026_activity_stability_existing.json"
COMMAND = r".\.venv\Scripts\python.exe -m database.comparison.activity_semantic_bridge"
DIAGNOSTIC_LABEL = "ACTIVITY_SEMANTIC_BRIDGE_DIAGNOSTIC"

WIDGET_TITLE = "Cancelled & Terminated Policies - By Day"
PY2027_EVENT_FIELD = "enrollment.last_update_timestamp"
PY2027_COUNT_FIELD = "enrollment.id"
CANCELLED_STATUS = "Cancelled"
TERM_PREFIX = "Term%"
HISTORICAL_CANDIDATE = "enrollment_last_update_date"

SUPPORTED = "SUPPORTED"
UNRESOLVED = "UNRESOLVED"
NOT_SUPPORTED = "NOT_SUPPORTED"
PASS = "PASS"
FAIL = "FAIL"

WIDGET_OWN = "WIDGET_OWN"
DASHBOARD_APPLIED = "DASHBOARD_APPLIED"
DASHBOARD_IGNORED = "DASHBOARD_IGNORED"
DISABLED = "DISABLED"

PLUS_ONE_LABEL = "POSSIBLE_BOUNDARY_OR_TIMEZONE_PATTERN"

STABLE = "STABLE"
DRIFTING = "DRIFTING"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
CONTRADICTED = "CONTRADICTED"
DIFFERENT_FIELD = "DIFFERENT_FIELD"
NOT_AVAILABLE = "NOT_AVAILABLE"

# Sisense COUNT() is a unique count; DUPCOUNT()/countduplicates counts every row.
UNIQUE_COUNT_FUNCTIONS = frozenset({"count"})
DUPLICATE_COUNT_FUNCTIONS = frozenset({"dupcount", "countduplicates"})

# Decision thresholds. Raw values are always reported next to the decision.
MIN_DISTINCT_EVENT_DATES = 20
MAX_EVENT_NULL_PERCENT = 0.1
PLAN_YEAR_MATCH_SHARE = 0.99
STABILITY_LOOKBACK_DAYS = 90
STABILITY_TOTAL_TOLERANCE = 0.02
STABILITY_DAY_TOLERANCE = 0.05
STABILITY_DAY_SHARE = 0.95
LINEAGE_MIN_UNCHANGED_KEYS = 100
LINEAGE_SAME_DATE_SHARE = 0.99
LINEAGE_SHIFT_SHARE = 0.05
LINEAGE_RELATED_SHARE = 0.5
MAX_LATE_SNAPSHOTS = 8
MAX_LINEAGE_TABLES = 3

HEALTH_MEMBER = "Health"
DASHBOARD_FILTER_MAPPING = {
    "ssap_applications.coverage_year": "coverage_year",
    "enrollment.insurance_type_description": "insurance_type_description",
}

LINEAGE_COLUMNS_SQL = """
SELECT c.TABLE_SCHEMA AS table_schema, c.TABLE_NAME AS table_name, c.DATA_TYPE AS data_type
FROM INFORMATION_SCHEMA.COLUMNS AS c
WHERE c.COLUMN_NAME = 'last_update_timestamp'
  AND c.TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'sys')
  AND c.TABLE_NAME NOT LIKE 'Enrollments[_]PY2526%'
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME
"""

RELEVANT_DATE_FIELDS = (
    "enrollment_last_update_date",
    "last_update_timestamp",
    "enrollment_update_date",
    "enrollment_create_date",
    "enrollment_confirmation_date",
    "benefit_effective_date",
    "benefit_end_date",
)
CURRENTLY_ENROLLED_FILTERS_NOT_COPIED = (
    "person_type_description",
    "insurance_type_description",
    "benefit_effective_date",
    "enrollee_status_description",
    "enrollment_status_description",
)


def duplicate_factor(count, distinct):
    if count is None or distinct in (None, 0):
        return None
    return round(count / distinct, 6)


def count_operator_note(factor) -> str:
    base = (
        "Historical rows per enrollment_id are reported, never silently collapsed. "
        "The operator is decided from the captured Sisense aggregation function: "
        "COUNT() is a unique count, DUPCOUNT() counts every row."
    )
    if factor is None:
        return base
    if factor == 1:
        return base + " COUNT and COUNT DISTINCT coincide numerically in this grouping."
    return base + " Multiple rows per enrollment_id exist, so the operator choice changes the value."


def decide_count_operator(functions, grain_rows: list[dict], family: str) -> dict:
    """Match the Sisense aggregation to a historical operator at policy grain."""
    rows = [item for item in grain_rows if item.get("status_family") == family
            and item.get("family_enrollment_keys") is not None]
    multi_date = sum(item.get("keys_with_multiple_event_dates") or 0 for item in rows)
    multi_rows = sum(item.get("keys_with_multiple_family_rows") or 0 for item in rows)
    names = set(functions or ())
    document = {
        "status_family": family,
        "sisense_aggregation_functions": sorted(names),
        "historical_operator": None,
        "keys_with_multiple_event_dates": multi_date if rows else None,
        "keys_with_multiple_family_rows": multi_rows if rows else None,
    }
    if not names:
        document.update(decision=UNRESOLVED, reason=(
            "The activity widget aggregation function was not captured. "
            "Re-run discovery so the measure's function name is recorded."))
    elif not rows:
        document.update(decision=UNRESOLVED, reason="Policy-grain evidence did not run.")
    elif names <= UNIQUE_COUNT_FUNCTIONS:
        if multi_date:
            document.update(decision=UNRESOLVED, reason=(
                "Sisense COUNT() is a unique count, but some enrollment_id values carry more "
                "than one event date in a snapshot, so per-day distinct counts are ambiguous."))
        else:
            document.update(
                decision=SUPPORTED,
                historical_operator="COUNT(DISTINCT enrollment_id)",
                reason=(
                    "Sisense COUNT() counts unique enrollment.id values. Each historical "
                    "enrollment_id has one event date per snapshot, so COUNT(DISTINCT enrollment_id) "
                    "per day is the same policy count. Extra rows per id are member rows, not policies."
                ),
            )
    elif names & DUPLICATE_COUNT_FUNCTIONS:
        if multi_rows == 0:
            document.update(decision=SUPPORTED, historical_operator="COUNT(enrollment_id)", reason=(
                "Sisense counts duplicates and historical rows are already one per enrollment_id."))
        else:
            document.update(decision=UNRESOLVED, reason=(
                "Sisense DUPCOUNT counts enrollment rows. Historical snapshots carry several rows per "
                "enrollment_id, and the Sisense enrollment table grain is not evidenced."))
    else:
        document.update(decision=UNRESOLVED, reason="Unrecognised aggregation function.")
    return document


def decide_coverage_year(rows: list[dict], scope_rows: list[dict] | None = None) -> dict:
    """Name identity alone is not semantic equivalence to ssap_applications.coverage_year.

    SUPPORTED needs coverage_year = 2026 present and benefit_effective_date inside
    calendar 2026 for the Cancelled/Terminated rows, which shows it is the plan year.
    """
    years = []
    for item in rows:
        value = item.get("observed_coverage_year")
        if value is None:
            continue
        try:
            years.append(int(value))
        except (TypeError, ValueError):
            years.append(value)
    has_2026 = PREVIOUS_PLAN_YEAR in years
    matches = sum(item.get("benefit_year_matches") or 0 for item in scope_rows or [])
    dated = sum(item.get("benefit_non_null_rows") or 0 for item in scope_rows or [])
    share = None if not dated else round(matches / dated, 6)
    if not rows:
        decision = NOT_SUPPORTED
        reason = "coverage_year was not inspected."
    elif not has_2026:
        decision = NOT_SUPPORTED
        reason = (
            "Selected snapshots did not show coverage_year = 2026. "
            "A PY2026 plan-year filter cannot be applied from this evidence."
        )
    elif share is not None and share >= PLAN_YEAR_MATCH_SHARE:
        decision = SUPPORTED
        reason = (
            f"{share:.2%} of coverage_year = 2026 Cancelled/Terminated rows have "
            "benefit_effective_date in calendar 2026, so coverage_year is the plan year. "
            "Sisense ssap_applications.coverage_year = 2027 is the same plan-year scope, shifted one year."
        )
    elif share is not None:
        decision = UNRESOLVED
        reason = (
            f"Only {share:.2%} of coverage_year = 2026 rows have a 2026 benefit_effective_date. "
            "coverage_year is not shown to be the plan year."
        )
    else:
        decision = UNRESOLVED
        reason = (
            "Historical coverage_year = 2026 is a plausible plan-year filter analog to "
            "Sisense ssap_applications.coverage_year = 2027, but benefit-date evidence is missing "
            "and name identity is not semantic equivalence."
        )
    return {
        "decision": decision,
        "reason": reason,
        "observed_years": years,
        "py2026_year_present": has_2026,
        "benefit_year_match_share": share,
        "sisense_field": "ssap_applications.coverage_year",
        "sisense_member": "2027",
        "historical_field": "coverage_year",
        "historical_member": PREVIOUS_PLAN_YEAR,
    }


def label_plus_one_pattern(rows: list[dict]) -> str | None:
    """Report a consistent +1 day ceiling. Not proof of timezone conversion."""
    dated = [
        item for item in rows
        if item.get("slice") == "ALL" and item.get("event_on_snapshot_plus_one") is not None
    ]
    if not dated:
        return None
    after = [(item.get("event_after_snapshot_plus_one") or 0) for item in dated]
    plus_one = [(item.get("event_on_snapshot_plus_one") or 0) for item in dated]
    if all(value == 0 for value in after) and any(value > 0 for value in plus_one):
        return PLUS_ONE_LABEL
    return None


def classify_activity_filters(discovery: dict | None, columns: dict) -> dict:
    """Filters for Cancelled & Terminated Policies - By Day only.

    Currently Enrolled filters are not copied. Missing activity-widget JAQL
    leaves dashboard inheritance unresolved.
    """
    historical = {
        key: columns[key]["name"] for key in (
            "enrollment_id", "enrollment_status_description", "coverage_year",
            "enrollment_last_update_date", "last_update_timestamp",
            "person_type_description", "insurance_type_description",
            "benefit_effective_date",
        ) if key in columns
    }
    inventory = [
        {
            "sisense_field": PY2027_COUNT_FIELD,
            "role": "measure_identifier",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("enrollment_id"),
            "evidence": "Sisense COUNT(enrollment.id). Operator decided from the captured aggregation function.",
        },
        {
            "sisense_field": "enrollment.enrollment_status_description",
            "role": "cancelled_measure_filter",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("enrollment_status_description"),
            "evidence": "Exact member Cancelled on the Cancelled Policies measure.",
        },
        {
            "sisense_field": "enrollment.enrollment_status_description",
            "role": "terminated_measure_filter",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("enrollment_status_description"),
            "evidence": 'startsWith("Term") on the Terminated Policies measure.',
        },
        {
            "sisense_field": PY2027_EVENT_FIELD,
            "role": "date_dimension",
            "classification": WIDGET_OWN,
            "py2026_field": historical.get("last_update_timestamp") or historical.get(
                "enrollment_last_update_date"),
            "evidence": "Calendar level = days. Historical last_update_timestamp is not assumed equal.",
        },
    ]
    widget = _activity_widget(discovery)
    roles = [] if widget is None else list(widget.get("metadata_roles") or [])
    scope = [role for role in roles if role.get("panel") == "scope"]
    capture_complete = bool(roles) and all("is_dashboard_filter" in role for role in scope)
    verified_dashboard = verified_dashboard_filters()
    classified = [_classify_discovered_filter(role, historical, verified_dashboard) for role in scope]
    inventory.extend(classified)
    seen_fields = {item["sisense_field"] for item in classified}
    ignored = []
    if capture_complete:
        ignored = [
            {
                "sisense_field": item["sisense_field"],
                "role": "dashboard_filter",
                "classification": DASHBOARD_IGNORED,
                "py2026_field": None,
                "mapping_status": SUPPORTED,
                "historical_predicate": None,
                "evidence": (
                    "Dashboard filter on the verified Currently Enrolled request that is absent from "
                    "the activity widget request. Sisense sends every applied dashboard filter."
                ),
            }
            for item in verified_dashboard if item["sisense_field"] not in seen_fields
        ]
        inventory.extend(ignored)
    date_dimension = _date_dimension(roles)
    measures = {
        family: _measure_functions(roles, f"{family} Policies") for family in ("Cancelled", "Terminated")
    }
    if widget is None:
        decision, reason = UNRESOLVED, "Activity widget is not in the discovery artifact."
    elif not capture_complete:
        decision, reason = UNRESOLVED, (
            "Discovery artifact predates filter-flag capture. Re-run discovery to record "
            "isDashboardFilter, disabled and the filter fingerprint for each scope filter.")
    elif any(item["mapping_status"] == NOT_SUPPORTED for item in classified):
        decision, reason = NOT_SUPPORTED, "An applied filter has no historical column."
    elif any(item["mapping_status"] != SUPPORTED for item in classified):
        decision, reason = UNRESOLVED, "An applied filter is not mapped to a proven historical predicate."
    elif not any(item["sisense_field"] == "ssap_applications.coverage_year"
                 and item.get("historical_predicate") for item in classified):
        decision, reason = UNRESOLVED, (
            "The activity widget does not apply the plan-year filter, but the historical "
            "queries restrict coverage_year = 2026. Plan-year scope differs.")
    else:
        decision, reason = SUPPORTED, "Every applied filter maps to a historical predicate; ignored and disabled filters are excluded."
    predicates = [item["historical_predicate"] for item in classified if item.get("historical_predicate")]
    return {
        "widget_title": WIDGET_TITLE,
        "widget_id": None if widget is None else widget.get("widget_id"),
        "discovery_matched": widget is not None,
        "capture_complete": capture_complete,
        "decision": decision,
        "reason": reason,
        "inventory": inventory,
        "historical_predicates": predicates,
        "apply_insurance_health": any(
            item["sisense_field"] == "enrollment.insurance_type_description"
            and item.get("historical_predicate") for item in classified
        ),
        "date_dimension": date_dimension,
        "measure_aggregation_functions": measures,
        "dashboard_filters_unresolved_without_activity_jaql": (
            [] if capture_complete else _dashboard_filters_from_currently_enrolled(historical)
        ),
        "currently_enrolled_filters_not_copied": list(CURRENTLY_ENROLLED_FILTERS_NOT_COPIED),
        "note": (
            "Currently Enrolled People filters are not applied to this activity metric. "
            "Only filters present on the activity widget request are mapped."
        ),
    }


def verified_dashboard_filters() -> list[dict]:
    """Dashboard filters on the verified Currently Enrolled request, fingerprinted."""
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    rows = []
    for item in payload.get("metadata") or []:
        if not isinstance(item, dict) or item.get("panel") != "scope":
            continue
        jaql = item.get("jaql") or {}
        if jaql.get("isDashboardFilter") is not True:
            continue
        rows.append({
            "sisense_field": f"{jaql.get('table')}.{jaql.get('column')}",
            "filter_kind": filter_kind(jaql.get("filter")),
            "filter_fingerprint": filter_definition_fingerprint(jaql.get("filter")),
        })
    return rows


def _date_dimension(roles: list[dict]) -> dict:
    dates = [role for role in roles if role.get("panel") == "result" and role.get("date_level")]
    verified = (
        len(dates) == 1
        and dates[0].get("table") == "enrollment"
        and dates[0].get("column") == "last_update_timestamp"
        and dates[0].get("date_level") == "days"
    )
    return {
        "observed": [
            {"table": role.get("table"), "column": role.get("column"), "level": role.get("date_level")}
            for role in dates
        ],
        "verified": verified,
    }


def _measure_functions(roles: list[dict], title: str) -> list[str] | None:
    key = normalize_label(title)
    hits = [
        role for role in roles
        if role.get("panel") == "result" and role.get("kind") == "measure"
        and normalize_label(role.get("title")) == key
    ]
    if len(hits) != 1 or "aggregation_functions" not in hits[0]:
        return None
    return list(hits[0].get("aggregation_functions") or [])


def daily_stability(count_rows: list[dict], snapshot_dates: dict[str, str | None]) -> dict:
    """Does a later snapshot reproduce an earlier snapshot's daily distinct counts?

    For every (earlier, later) dated snapshot pair, compares COUNT(DISTINCT enrollment_id)
    per event date for complete days before the earlier snapshot date. A day absent from
    the later snapshot's GROUP BY output has zero rows there.
    """
    series: dict[tuple[str, str], dict[str, int]] = {}
    for item in count_rows:
        if item.get("grain") != "day" or not item.get("event_calendar_date"):
            continue
        value = item.get("count_distinct_enrollment_keys")
        if value is None:
            continue
        key = (item["table"], item["status_family"])
        series.setdefault(key, {})[str(item["event_calendar_date"])[:10]] = value
    dated = sorted((iso, table) for table, iso in snapshot_dates.items() if iso)
    families = {}
    for family in ("Cancelled", "Terminated"):
        pairs = []
        for index, (early_iso, early_table) in enumerate(dated):
            early = series.get((early_table, family))
            if not early:
                continue
            end = date.fromisoformat(early_iso)
            start = end - timedelta(days=STABILITY_LOOKBACK_DAYS)
            days = sorted(day for day in early if start <= date.fromisoformat(day) < end)
            if not days:
                continue
            for late_iso, late_table in dated[index + 1:]:
                late = series.get((late_table, family))
                if late is None:
                    continue
                early_total = sum(early[day] for day in days)
                late_total = sum(late.get(day, 0) for day in days)
                deviations = [abs(late.get(day, 0) - early[day]) / early[day] for day in days if early[day]]
                within = sum(1 for value in deviations if value <= STABILITY_DAY_TOLERANCE)
                retention = None if not early_total else round(late_total / early_total, 6)
                day_share = None if not deviations else round(within / len(deviations), 6)
                stable = (
                    retention is not None and day_share is not None
                    and abs(retention - 1) <= STABILITY_TOTAL_TOLERANCE
                    and day_share >= STABILITY_DAY_SHARE
                )
                pairs.append({
                    "earlier": early_table,
                    "later": late_table,
                    "lag_days": (date.fromisoformat(late_iso) - end).days,
                    "window": [days[0], days[-1]],
                    "days_compared": len(days),
                    "earlier_total": early_total,
                    "later_total": late_total,
                    "retention": retention,
                    "days_within_tolerance_share": day_share,
                    "max_day_deviation": None if not deviations else round(max(deviations), 6),
                    "status": STABLE if stable else DRIFTING,
                })
        status = INSUFFICIENT if not pairs else STABLE if all(
            item["status"] == STABLE for item in pairs) else DRIFTING
        families[family] = {"status": status, "pairs": pairs}
    return {
        "tolerances": {
            "lookback_days": STABILITY_LOOKBACK_DAYS,
            "total_retention": STABILITY_TOTAL_TOLERANCE,
            "per_day_deviation": STABILITY_DAY_TOLERANCE,
            "share_of_days_within": STABILITY_DAY_SHARE,
        },
        "operator": "COUNT(DISTINCT enrollment_id) per event date",
        "families": families,
    }


def evaluate_strategies(count_rows: list[dict], plus_one_rows: list[dict],
                        coverage: dict, stability: dict | None = None,
                        family: str | None = None) -> list[dict]:
    """Evaluate extraction strategies. A is activated only on STABLE late-snapshot evidence."""
    factor_over_one = any(
        (item.get("duplicate_factor") or 0) > 1 for item in count_rows
        if item.get("grain") == "family"
    )
    plus_one = label_plus_one_pattern(plus_one_rows)
    status = None
    if stability and family:
        status = ((stability.get("families") or {}).get(family) or {}).get("status")
    if status == STABLE:
        a_decision = SUPPORTED
        a_reason = (
            "Later snapshots reproduce earlier snapshots' daily distinct counts within tolerance, "
            "so one snapshot dated on or after the comparison as-of date reconstructs the daily series."
        )
    elif status == DRIFTING:
        a_decision = UNRESOLVED
        a_reason = (
            "Later snapshots do not preserve earlier daily counts within tolerance. "
            "A single late snapshot is not equivalent; an as-of-aligned snapshot is required."
        )
    else:
        a_decision = UNRESOLVED
        a_reason = (
            "Diagnostic candidate only. No dated later-snapshot pair was compared, "
            "so no snapshot is proven sufficiently late."
        )
    return [
        {
            "strategy": "A",
            "name": "single sufficiently-late snapshot + event date",
            "decision": a_decision,
            "evidence_supports": a_decision == SUPPORTED,
            "stability_status": status,
            "duplication_risk": (
                "Using one snapshot avoids summing repeats. Snapshot duplication still exists."
            ),
            "missing_event_risk": (
                "Events after the chosen snapshot, and the +1-day boundary pattern, can omit or shift days."
                if plus_one else
                "Events after the chosen snapshot are omitted. Which snapshot is sufficiently late is unproven."
            ),
            "can_reproduce_sisense_daily_count": a_decision == SUPPORTED,
            "reason": a_reason,
            "plus_one_pattern": plus_one,
            "duplicate_rows_observed": factor_over_one,
        },
        {
            "strategy": "B",
            "name": "multiple snapshots with proven event-key deduplication",
            "decision": NOT_SUPPORTED,
            "evidence_supports": False,
            "duplication_risk": (
                "The same enrollment_id + status + event date repeats across snapshots. "
                "Deduplication is not proven."
            ),
            "missing_event_risk": "Dedup rules can drop real events or keep snapshot occupancy.",
            "can_reproduce_sisense_daily_count": False,
            "reason": "Snapshots must not be unioned or summed. No proven event-key dedup strategy exists.",
        },
        {
            "strategy": "C",
            "name": "snapshot delta",
            "decision": NOT_SUPPORTED,
            "evidence_supports": False,
            "duplication_risk": "Snapshot presence can be mistaken for event activity.",
            "missing_event_risk": "Rows that persist, disappear, or change status are not proven events.",
            "can_reproduce_sisense_daily_count": False,
            "reason": "Phase 5B.1 showed cross-snapshot repeats. Delta is not a proven event extractor.",
        },
        {
            "activated_strategy": "A" if a_decision == SUPPORTED else None,
            "official_activity_output": False,
            "coverage_year_decision": coverage.get("decision"),
        },
    ]


def decide_event_date(candidate: dict, grain_rows: list[dict], date_profiles: list[dict],
                      plus_one_rows: list[dict], lineage: dict) -> dict:
    """Same business axis as enrollment.last_update_timestamp, day-truncated.

    The Sisense widget buckets by the enrollment row's last-update timestamp. The
    historical field qualifies when it behaves as that field: one value per
    enrollment_id even when the id has several member rows, near-complete coverage,
    many distinct dates (not a load stamp), and never later than extraction.
    """
    name = candidate.get("py2026_candidate")
    profiles = [item for item in date_profiles
                if item.get("field") == name and item.get("date_field_rows") is not None]
    grains = [item for item in grain_rows if item.get("family_enrollment_keys") is not None]
    dated_plus = [item for item in plus_one_rows if item.get("event_after_snapshot_plus_one") is not None]
    facts = {
        "candidate_field": name,
        "sql_datatype": candidate.get("sql_datatype"),
        "max_null_percent": None if not profiles else max(item.get("null_rate") or 0 for item in profiles),
        "min_distinct_dates": None if not profiles else min(
            item.get("date_field_distinct_dates") or 0 for item in profiles),
        "keys_with_multiple_event_dates": None if not grains else sum(
            item.get("keys_with_multiple_event_dates") or 0 for item in grains),
        "keys_with_multiple_member_rows": None if not grains else sum(
            item.get("keys_with_multiple_family_rows") or 0 for item in grains),
        "events_after_snapshot_plus_one": None if not dated_plus else sum(
            item.get("event_after_snapshot_plus_one") or 0 for item in dated_plus),
        "lineage_status": lineage.get("status"),
    }
    if not name or not candidate.get("datetime_compatible"):
        decision, reason = NOT_SUPPORTED, "No date/time-compatible event-date candidate was selected."
    elif lineage.get("status") == DIFFERENT_FIELD:
        decision, reason = NOT_SUPPORTED, lineage.get("reason")
    elif not profiles or not grains or not dated_plus:
        decision, reason = UNRESOLVED, "Date-profile, policy-grain or snapshot-boundary evidence did not run."
    elif facts["keys_with_multiple_event_dates"]:
        decision, reason = UNRESOLVED, "Some enrollment_id values carry several dates in one snapshot."
    elif facts["max_null_percent"] > MAX_EVENT_NULL_PERCENT:
        decision, reason = UNRESOLVED, "Event-date null rate exceeds the threshold."
    elif facts["min_distinct_dates"] < MIN_DISTINCT_EVENT_DATES:
        decision, reason = UNRESOLVED, "Too few distinct dates; could be a load stamp."
    elif facts["events_after_snapshot_plus_one"]:
        decision, reason = UNRESOLVED, "Dates later than snapshot extraction exist."
    else:
        decision = SUPPORTED
        reason = (
            "Behaves as the enrollment row's last-update date: one date per enrollment_id across "
            "member rows, complete coverage, spread across many days, never after extraction. "
            "That is the same business axis as Sisense enrollment.last_update_timestamp at day level. "
            "Day-boundary basis is decided separately."
        )
    return {**facts, "decision": decision, "reason": reason}


def decide_daily_aggregation(filters: dict, candidate: dict, lineage: dict) -> dict:
    date_dimension = (filters or {}).get("date_dimension") or {}
    if not date_dimension.get("verified"):
        decision, reason = UNRESOLVED, (
            "Sisense date dimension enrollment.last_update_timestamp at level 'days' was not captured.")
    elif not candidate.get("datetime_compatible"):
        decision, reason = NOT_SUPPORTED, "Historical event field is not date/time-compatible."
    elif lineage.get("status") == SUPPORTED:
        decision, reason = SUPPORTED, (
            "Day level of the Sisense timestamp equals the historical DATE for unchanged records.")
    elif lineage.get("status") == CONTRADICTED:
        decision, reason = NOT_SUPPORTED, lineage.get("reason")
    else:
        decision, reason = UNRESOLVED, (
            "Day-boundary basis is unproven. A DATE column cannot show which clock truncated it; "
            "same-record agreement with a last_update_timestamp source is required.")
    return {"decision": decision, "reason": reason, "date_dimension": date_dimension}


def decide_lineage(rows: list[dict]) -> dict:
    probed = [item for item in rows if not item.get("skip_reason")]
    usable = [item for item in probed if item.get("matched_keys")]
    if not probed:
        return {"status": NOT_AVAILABLE, "reason": "No non-snapshot table carries last_update_timestamp with enrollment_id and coverage_year."}
    if not usable:
        return {"status": UNRESOLVED, "reason": "Lineage probe matched no PY2026 Cancelled/Terminated enrollment_id values."}
    best = max(usable, key=lambda item: item["matched_keys"])
    unchanged = sum(best.get(key) or 0 for key in ("same_date", "live_plus_one", "live_minus_one", "live_earlier"))
    shifted = (best.get("live_plus_one") or 0) + (best.get("live_minus_one") or 0)
    same_share = None if not unchanged else round((best.get("same_date") or 0) / unchanged, 6)
    shift_share = None if not unchanged else round(shifted / unchanged, 6)
    document = {"table": best.get("live_table"), "unchanged_keys": unchanged,
                "same_date_share": same_share, "shifted_share": shift_share}
    if unchanged < LINEAGE_MIN_UNCHANGED_KEYS:
        return {**document, "status": UNRESOLVED, "reason": "Too few unchanged records to compare."}
    if same_share >= LINEAGE_SAME_DATE_SHARE:
        return {**document, "status": SUPPORTED, "reason": (
            "For records unchanged since the snapshot, the source timestamp's calendar date equals "
            "the snapshot DATE. Same field, same day boundary.")}
    if same_share + shift_share < LINEAGE_RELATED_SHARE:
        return {**document, "status": DIFFERENT_FIELD, "reason": (
            "For unchanged records the source timestamp date and the snapshot DATE mostly disagree "
            "by more than one day. They are not the same field.")}
    if shift_share > LINEAGE_SHIFT_SHARE:
        return {**document, "status": CONTRADICTED, "reason": (
            "Unchanged records show a one-day shift between the source timestamp and the snapshot DATE. "
            "Same field, different day boundary.")}
    return {**document, "status": UNRESOLVED, "reason": "Agreement is below the threshold without a clear shift."}


def cancelled_policies_checks(**kwargs) -> list[dict]:
    return _activity_checks("Cancelled", **kwargs)


def terminated_policies_checks(**kwargs) -> list[dict]:
    return _activity_checks("Terminated", **kwargs)


def metric_from_checks(metric_id: str, metric_name: str, checks: list[dict]) -> dict:
    statuses = [item["status"] for item in checks]
    if statuses and all(status == PASS for status in statuses):
        comparison = COMPARABLE
        reason = "All required checks PASS."
    elif any(status == FAIL for status in statuses):
        comparison = NOT_COMPARABLE
        reason = "A required check FAILED: " + ", ".join(
            item["check"] for item in checks if item["status"] == FAIL)
    else:
        comparison = DIAGNOSTIC
        reason = "COMPARABLE requires every required check to PASS. UNRESOLVED: " + ", ".join(
            item["check"] for item in checks if item["status"] != PASS)
    return {
        "metric_id": metric_id,
        "metric_name": metric_name,
        "comparison_status": comparison,
        "full_metric_comparable": comparison == COMPARABLE,
        "reason": reason,
        "checks": checks,
    }


def _require_event_column(name: str, data_type: str | None) -> None:
    if is_blocked_event_date_name(name):
        raise ValueError("event-date SQL refused identifier column")
    if not is_datetime_sql_type(data_type):
        raise ValueError("event-date SQL refused non-datetime column")


def _year_sql(columns: dict) -> tuple[str, tuple]:
    if "coverage_year" not in columns:
        return "", ()
    return f" AND {quote_identifier(columns['coverage_year']['name'])} = ?", (PREVIOUS_PLAN_YEAR,)


def _status_sql(columns: dict, mode: str) -> tuple[str, tuple]:
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    if mode == "cancelled":
        return f" AND {status} = ?", (CANCELLED_STATUS,)
    if mode == "term":
        return f" AND {status} LIKE ?", (TERM_PREFIX,)
    return "", ()


def _insurance_sql(columns: dict, apply_health: bool) -> tuple[str, tuple]:
    if not apply_health or "insurance_type_description" not in columns:
        return "", ()
    return f" AND {quote_identifier(columns['insurance_type_description']['name'])} = ?", (HEALTH_MEMBER,)


def build_count_family_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool, apply_health: bool = False,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    health_sql, health_params = _insurance_sql(columns, apply_health)
    sql = (
        "SELECT COUNT_BIG(*) AS count_star,\n"
        f"       COUNT({enroll}) AS count_enrollment_ids,\n"
        f"       COUNT(DISTINCT {enroll}) AS count_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}{health_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params + health_params


def build_count_daily_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool, apply_health: bool = False,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    event = quote_identifier(event_name)
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    health_sql, health_params = _insurance_sql(columns, apply_health)
    sql = (
        f"SELECT CAST({event} AS date) AS event_calendar_date,\n"
        "       COUNT_BIG(*) AS count_star,\n"
        f"       COUNT({enroll}) AS count_enrollment_ids,\n"
        f"       COUNT(DISTINCT {enroll}) AS count_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        f"WHERE {event} IS NOT NULL"
        f"{year_sql}{status_sql}{health_sql}\n"
        f"GROUP BY CAST({event} AS date)\n"
        "ORDER BY event_calendar_date"
    )
    assert_read_only(sql)
    return sql, year_params + status_params + health_params


def build_policy_grain_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    """Why do several rows share one enrollment_id? Aggregates only; no identifiers returned."""
    _require_event_column(event_name, event_type)
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    event = quote_identifier(event_name)
    member = columns.get("enrollee_id")
    member_expr = (
        f"COUNT(DISTINCT {quote_identifier(member['name'])})" if member else "CAST(NULL AS bigint)"
    )
    year_sql, year_params = _year_sql(columns)
    predicate = f"{status} = ?" if cancelled else f"{status} LIKE ?"
    member_param = CANCELLED_STATUS if cancelled else TERM_PREFIX
    sql = (
        "SELECT COUNT_BIG(*) AS family_enrollment_keys,\n"
        "       SUM(family_rows) AS family_rows,\n"
        "       COUNT_BIG(CASE WHEN family_rows > 1 THEN 1 END) AS keys_with_multiple_family_rows,\n"
        "       COUNT_BIG(CASE WHEN all_rows > family_rows THEN 1 END) AS keys_with_rows_outside_family,\n"
        "       COUNT_BIG(CASE WHEN status_values > 1 THEN 1 END) AS keys_with_multiple_status_values,\n"
        "       COUNT_BIG(CASE WHEN event_dates > 1 THEN 1 END) AS keys_with_multiple_event_dates,\n"
        "       COUNT_BIG(CASE WHEN member_keys IS NOT NULL AND member_keys < all_rows THEN 1 END) "
        "AS keys_with_repeated_member_rows\n"
        "FROM (\n"
        f"    SELECT {enroll} AS enrollment_key,\n"
        "           COUNT_BIG(*) AS all_rows,\n"
        f"           SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS family_rows,\n"
        f"           COUNT(DISTINCT {status}) AS status_values,\n"
        f"           COUNT(DISTINCT CAST({event} AS date)) AS event_dates,\n"
        f"           {member_expr} AS member_keys\n"
        f"    FROM {qualified}\n"
        f"    WHERE {enroll} IS NOT NULL{year_sql}\n"
        f"    GROUP BY {enroll}\n"
        f"    HAVING SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) > 0\n"
        ") AS per_enrollment"
    )
    assert_read_only(sql)
    return sql, (member_param,) + year_params + (member_param,)


def build_scope_profile_sql(qualified: str, columns: dict, *, cancelled: bool) -> tuple[str, tuple]:
    """Plan-year check (benefit_effective_date year) and insurance-type mix per family."""
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    insurance = columns.get("insurance_type_description")
    benefit = columns.get("benefit_effective_date")
    select = []
    params: tuple = ()
    if insurance:
        select.append(f"{quote_identifier(insurance['name'])} AS insurance_type_value")
    else:
        select.append("CAST(NULL AS varchar(64)) AS insurance_type_value")
    select.append("COUNT_BIG(*) AS scope_rows")
    select.append(f"COUNT_BIG(DISTINCT {enroll}) AS scope_policies")
    if benefit and is_datetime_sql_type(benefit.get("data_type")):
        quoted = quote_identifier(benefit["name"])
        select.append(
            f"COUNT_BIG(CASE WHEN {quoted} IS NOT NULL AND YEAR({quoted}) = ? THEN 1 END) "
            "AS benefit_year_matches")
        select.append(f"COUNT_BIG(CASE WHEN {quoted} IS NOT NULL THEN 1 END) AS benefit_non_null_rows")
        params = (PREVIOUS_PLAN_YEAR,)
    else:
        select.append("CAST(NULL AS bigint) AS benefit_year_matches")
        select.append("CAST(NULL AS bigint) AS benefit_non_null_rows")
    sql = (
        "SELECT " + ",\n       ".join(select) + "\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    if insurance:
        sql += f"\nGROUP BY {quote_identifier(insurance['name'])}"
    assert_read_only(sql)
    return sql, params + year_params + status_params


def build_lineage_agreement_sql(
    live_qualified: str, live_columns: dict, snapshot_qualified: str, snapshot_columns: dict,
    event_name: str, event_type: str | None,
) -> tuple[str, tuple]:
    """Same-record comparison of source timestamp date vs snapshot DATE. PY2026 rows only."""
    _require_event_column(event_name, event_type)
    s_enroll = quote_identifier(snapshot_columns["enrollment_id"]["name"])
    s_event = quote_identifier(event_name)
    s_year = quote_identifier(snapshot_columns["coverage_year"]["name"])
    s_status = quote_identifier(snapshot_columns["enrollment_status_description"]["name"])
    l_enroll = quote_identifier(live_columns["enrollment_id"]["name"])
    l_stamp = quote_identifier(live_columns["last_update_timestamp"]["name"])
    l_year = quote_identifier(live_columns["coverage_year"]["name"])
    live_date = "CAST(live.live_value AS date)"
    sql = (
        "WITH snap AS (\n"
        f"    SELECT DISTINCT {s_enroll} AS enrollment_key, CAST({s_event} AS date) AS snapshot_value\n"
        f"    FROM {snapshot_qualified}\n"
        f"    WHERE {s_enroll} IS NOT NULL AND {s_event} IS NOT NULL AND {s_year} = ?\n"
        f"      AND ({s_status} = ? OR {s_status} LIKE ?)\n"
        "), live AS (\n"
        f"    SELECT {l_enroll} AS enrollment_key, MAX({l_stamp}) AS live_value\n"
        f"    FROM {live_qualified}\n"
        f"    WHERE {l_enroll} IS NOT NULL AND {l_stamp} IS NOT NULL AND {l_year} = ?\n"
        f"    GROUP BY {l_enroll}\n"
        ")\n"
        "SELECT COUNT_BIG(*) AS matched_keys,\n"
        f"       COUNT_BIG(CASE WHEN {live_date} = snap.snapshot_value THEN 1 END) AS same_date,\n"
        f"       COUNT_BIG(CASE WHEN {live_date} = DATEADD(day, 1, snap.snapshot_value) THEN 1 END) AS live_plus_one,\n"
        f"       COUNT_BIG(CASE WHEN {live_date} = DATEADD(day, -1, snap.snapshot_value) THEN 1 END) AS live_minus_one,\n"
        f"       COUNT_BIG(CASE WHEN {live_date} > DATEADD(day, 1, snap.snapshot_value) THEN 1 END) AS live_later,\n"
        f"       COUNT_BIG(CASE WHEN {live_date} < DATEADD(day, -1, snap.snapshot_value) THEN 1 END) AS live_earlier\n"
        "FROM snap INNER JOIN live ON live.enrollment_key = snap.enrollment_key"
    )
    assert_read_only(sql)
    return sql, (PREVIOUS_PLAN_YEAR, CANCELLED_STATUS, TERM_PREFIX, PREVIOUS_PLAN_YEAR)


def build_status_value_sql(qualified: str, columns: dict) -> tuple[str, tuple]:
    enroll = quote_identifier(columns["enrollment_id"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    year_sql, params = _year_sql(columns)
    sql = (
        f"SELECT {status} AS observed_status_value,\n"
        "       COUNT_BIG(*) AS status_row_count,\n"
        f"       COUNT(DISTINCT {enroll}) AS status_distinct_enrollment_keys\n"
        f"FROM {qualified}\n"
        f"WHERE ({status} = ? OR {status} LIKE ?)"
        f"{year_sql}\n"
        f"GROUP BY {status}\n"
        "ORDER BY observed_status_value"
    )
    assert_read_only(sql)
    return sql, (CANCELLED_STATUS, TERM_PREFIX) + params


def build_coverage_year_sql(qualified: str, columns: dict) -> tuple[str, tuple]:
    year = quote_identifier(columns["coverage_year"]["name"])
    status = quote_identifier(columns["enrollment_status_description"]["name"])
    sql = (
        f"SELECT {year} AS observed_coverage_year,\n"
        "       COUNT_BIG(*) AS coverage_year_row_count,\n"
        f"       COUNT_BIG(CASE WHEN {status} = ? THEN 1 END) AS cancelled_row_count,\n"
        f"       COUNT_BIG(CASE WHEN {status} LIKE ? THEN 1 END) AS terminated_row_count\n"
        f"FROM {qualified}\n"
        f"GROUP BY {year}\n"
        "ORDER BY observed_coverage_year"
    )
    assert_read_only(sql)
    return sql, (CANCELLED_STATUS, TERM_PREFIX)


def build_plus_one_sql(
    qualified: str, columns: dict, event_name: str, event_type: str | None, *,
    mode: str, snapshot_date: str,
) -> tuple[str, tuple]:
    _require_event_column(event_name, event_type)
    event = quote_identifier(event_name)
    date_expr = f"CAST({event} AS date)"
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, mode)
    sql = (
        f"SELECT COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} < ? THEN 1 END) "
        "AS event_before_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} = ? THEN 1 END) "
        "AS event_on_snapshot,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} = "
        "DATEADD(day, 1, CAST(? AS date)) THEN 1 END) AS event_on_snapshot_plus_one,\n"
        f"       COUNT_BIG(CASE WHEN {event} IS NOT NULL AND {date_expr} > "
        "DATEADD(day, 1, CAST(? AS date)) THEN 1 END) AS event_after_snapshot_plus_one\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    params = (snapshot_date, snapshot_date, snapshot_date, snapshot_date) + year_params + status_params
    return sql, params


def build_date_field_profile_sql(
    qualified: str, columns: dict, field_name: str, field_type: str | None, *,
    cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(field_name, field_type)
    quoted = quote_identifier(field_name)
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    sql = (
        f"SELECT MIN({quoted}) AS date_field_min,\n"
        f"       MAX({quoted}) AS date_field_max,\n"
        f"       COUNT_BIG(CASE WHEN {quoted} IS NULL THEN 1 END) AS date_field_nulls,\n"
        f"       COUNT_BIG(*) AS date_field_rows,\n"
        f"       COUNT_BIG(DISTINCT CAST({quoted} AS date)) AS date_field_distinct_dates\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def build_date_relationship_sql(
    qualified: str, columns: dict, candidate_name: str, candidate_type: str | None,
    other_name: str, other_type: str | None, *, cancelled: bool,
) -> tuple[str, tuple]:
    _require_event_column(candidate_name, candidate_type)
    _require_event_column(other_name, other_type)
    left = quote_identifier(candidate_name)
    right = quote_identifier(other_name)
    year_sql, year_params = _year_sql(columns)
    status_sql, status_params = _status_sql(columns, "cancelled" if cancelled else "term")
    sql = (
        f"SELECT COUNT_BIG(CASE WHEN CAST({left} AS date) = CAST({right} AS date) THEN 1 END) "
        "AS same_calendar_date_count,\n"
        f"       COUNT_BIG(CASE WHEN CAST({left} AS date) > CAST({right} AS date) THEN 1 END) "
        "AS candidate_after_other,\n"
        f"       COUNT_BIG(CASE WHEN CAST({left} AS date) < CAST({right} AS date) THEN 1 END) "
        "AS candidate_before_other,\n"
        f"       COUNT_BIG(CASE WHEN {left} IS NOT NULL AND {right} IS NULL THEN 1 END) "
        "AS other_null_when_candidate_present\n"
        f"FROM {qualified}\n"
        "WHERE 1 = 1"
        f"{year_sql}{status_sql}"
    )
    assert_read_only(sql)
    return sql, year_params + status_params


def relevant_date_fields(columns: dict) -> list[dict]:
    found = []
    for key in RELEVANT_DATE_FIELDS:
        meta = columns.get(key)
        if meta is None:
            continue
        if is_blocked_event_date_name(meta["name"]):
            continue
        if not is_datetime_sql_type(meta.get("data_type")):
            continue
        found.append({"name": meta["name"], "data_type": meta.get("data_type")})
    return found


def late_snapshots(tables: list[dict], selected: list[dict]) -> list[dict]:
    """Dated snapshots after the latest selected one, newest first, capped."""
    selected_dates = [
        (item.get("snapshot_date") or {}).get("snapshot_date") for item in selected
    ]
    floor = max((value for value in selected_dates if value), default=None)
    names = {item["table"].lower() for item in selected}
    later = [
        item for item in tables
        if item["table"].lower() not in names
        and (item.get("snapshot_date") or {}).get("snapshot_date")
        and (floor is None or item["snapshot_date"]["snapshot_date"] > floor)
    ]
    later.sort(key=lambda item: item["snapshot_date"]["snapshot_date"], reverse=True)
    return later[:MAX_LATE_SNAPSHOTS]


def collect_evidence(client, discovery: dict | None = None) -> dict:
    failures = []
    tables = _list_snapshots(client, failures)
    for item in tables:
        item["columns"] = _columns(client, failures, item)
        item["event_date_candidate"] = select_event_date_candidate(item["columns"])
        item["snapshot_date"] = snapshot_date_context(item["table"])
        item["date_fields"] = relevant_date_fields(item["columns"])
    selected_names = {name.lower() for name in SELECTED_SNAPSHOTS}
    selected = [item for item in tables if item["table"].lower() in selected_names]
    later = late_snapshots(tables, selected)
    columns = {} if not selected else selected[0].get("columns") or {}
    filters = classify_activity_filters(discovery, columns)
    apply_health = bool(filters.get("apply_insurance_health"))
    count_rows = []
    status_rows = []
    coverage_rows = []
    plus_one_rows = []
    date_profiles = []
    relationships = []
    grain_rows = []
    scope_rows = []
    for item in selected:
        count_rows.extend(_count_semantics(client, failures, item, apply_health=apply_health))
        status_rows.extend(_status_values(client, failures, item))
        coverage_rows.extend(_coverage_years(client, failures, item))
        plus_one_rows.extend(_plus_one(client, failures, item))
        date_profiles.extend(_date_profiles(client, failures, item))
        relationships.extend(_date_relationships(client, failures, item))
    for item in selected + later:
        grain_rows.extend(_policy_grain(client, failures, item))
        scope_rows.extend(_scope_profile(client, failures, item))
    for item in later:
        count_rows.extend(_count_semantics(client, failures, item, apply_health=apply_health))
        plus_one_rows.extend(_plus_one(client, failures, item))
    dated = [item for item in selected + later if (item.get("snapshot_date") or {}).get("snapshot_date")]
    latest = max(dated, key=lambda item: item["snapshot_date"]["snapshot_date"], default=None)
    return {
        "tables": tables,
        "selected_snapshots": [item["table"] for item in selected],
        "late_snapshots": [item["table"] for item in later],
        "snapshot_dates": {
            item["table"]: (item.get("snapshot_date") or {}).get("snapshot_date") for item in selected + later
        },
        "count_semantics": count_rows,
        "status_values": status_rows,
        "coverage_years": coverage_rows,
        "plus_one": plus_one_rows,
        "date_profiles": date_profiles,
        "date_relationships": relationships,
        "policy_grain": grain_rows,
        "scope_profile": scope_rows,
        "lineage": _lineage(client, failures, latest),
        "filter_inventory": filters,
        "insurance_health_applied": apply_health,
        "failures": failures,
    }


def build_report(evidence: dict) -> dict:
    tables = evidence.get("tables") or []
    columns = {}
    for item in tables:
        if item.get("table") in (evidence.get("selected_snapshots") or []) and item.get("columns"):
            columns = item["columns"]
            break
    candidate = select_event_date_candidate(columns)
    grain_rows = evidence.get("policy_grain") or []
    scope_rows = evidence.get("scope_profile") or []
    coverage = decide_coverage_year(evidence.get("coverage_years") or [], scope_rows)
    plus_one_label = label_plus_one_pattern(evidence.get("plus_one") or [])
    stability = daily_stability(
        evidence.get("count_semantics") or [], evidence.get("snapshot_dates") or {})
    strategies = evaluate_strategies(
        evidence.get("count_semantics") or [],
        evidence.get("plus_one") or [],
        coverage,
    )
    strategies_by_family = {
        family: evaluate_strategies(
            evidence.get("count_semantics") or [], evidence.get("plus_one") or [],
            coverage, stability, family)
        for family in ("Cancelled", "Terminated")
    }
    filters = evidence.get("filter_inventory") or classify_activity_filters(None, columns)
    lineage = decide_lineage(evidence.get("lineage") or [])
    event = decide_event_date(
        candidate, grain_rows, evidence.get("date_profiles") or [],
        evidence.get("plus_one") or [], lineage)
    daily = decide_daily_aggregation(filters, candidate, lineage)
    functions = filters.get("measure_aggregation_functions") or {}
    operators = {
        family: decide_count_operator(functions.get(family), grain_rows, family)
        for family in ("Cancelled", "Terminated")
    }
    identifier_present = "enrollment_id" in columns
    cancelled_status = _status_observed(evidence.get("status_values") or [], exact_cancelled=True)
    term_values = _term_values(evidence.get("status_values") or [])
    count_ready = _count_ready(evidence.get("count_semantics") or [], "Cancelled")
    count_ready_term = _count_ready(evidence.get("count_semantics") or [], "Terminated")
    event_decision = event["decision"]
    filter_decision = filters.get("decision") or UNRESOLVED
    common = {
        "event": event["reason"],
        "coverage": coverage["reason"],
        "filters": filters.get("reason"),
        "daily": daily["reason"],
    }

    def family_checks(builder, family, ready, observed):
        strategy = strategies_by_family[family][0]
        return builder(
            identifier_present=identifier_present,
            count_ready=ready,
            count_factor=_family_factor(evidence.get("count_semantics") or [], family),
            status_observed=observed,
            event_decision=event_decision,
            coverage_decision=coverage["decision"],
            filter_decision=filter_decision,
            strategy_decision=strategy["decision"],
            term_values=term_values,
            count_decision=operators[family]["decision"],
            daily_decision=daily["decision"],
            details={**common, "count": operators[family]["reason"], "strategy": strategy["reason"]},
        )

    cancelled_checks = family_checks(cancelled_policies_checks, "Cancelled", count_ready, cancelled_status)
    terminated_checks = family_checks(
        terminated_policies_checks, "Terminated", count_ready_term, bool(term_values))
    cancelled = metric_from_checks("cancelled_policies_daily", "Cancelled Policies", cancelled_checks)
    terminated = metric_from_checks("terminated_policies_daily", "Terminated Policies", terminated_checks)
    report = {
        "report_kind": "PY2026_ACTIVITY_SEMANTIC_BRIDGE",
        "label": DIAGNOSTIC_LABEL,
        "not_official_activity": True,
        "queries_py2027_enrollment": False,
        "visualization_created": False,
        "py2027_widget": WIDGET_TITLE,
        "py2027_event_field": PY2027_EVENT_FIELD,
        "py2027_count": "COUNT(enrollment.id)",
        "count_operator": operators,
        "policy_grain": grain_rows,
        "scope_profile": scope_rows,
        "insurance_health_applied": bool(evidence.get("insurance_health_applied")),
        "late_snapshots": evidence.get("late_snapshots") or [],
        "daily_stability": stability,
        "lineage_probe": {**lineage, "rows": evidence.get("lineage") or []},
        "daily_aggregation_decision": daily,
        "event_date_candidate": candidate,
        "count_semantics": evidence.get("count_semantics") or [],
        "status_values": evidence.get("status_values") or [],
        "term_status_values_observed": term_values,
        "coverage_year": coverage,
        "coverage_year_rows": evidence.get("coverage_years") or [],
        "filter_inventory": filters,
        "date_profiles": evidence.get("date_profiles") or [],
        "date_relationships": evidence.get("date_relationships") or [],
        "event_date_decision": event,
        "plus_one_pattern": {
            "label": plus_one_label,
            "note": "Not proof of timezone conversion.",
            "rows": evidence.get("plus_one") or [],
        },
        "extraction_strategies": strategies,
        "extraction_strategies_by_family": strategies_by_family,
        "cancelled_policies": cancelled,
        "terminated_policies": terminated,
        "out_of_scope_this_phase": {
            "currently_enrolled_people": "unchanged",
            "new_active_passive": "unchanged",
            "applications": "unchanged",
            "pending_people": "unchanged",
        },
        "comparable": [
            item["metric_id"] for item in (cancelled, terminated)
            if item["comparison_status"] == COMPARABLE
        ],
        "failures": evidence.get("failures") or [],
        "command": COMMAND,
    }
    for metric in (cancelled, terminated):
        if metric["comparison_status"] == COMPARABLE and not all(
            check["status"] == PASS for check in metric["checks"]
        ):
            raise AssertionError("COMPARABLE requires every required check to PASS")
    guard_no_pii(report)
    return report


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    guard_no_pii(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    html_path = output_dir / HTML_NAME
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def render_html(report: dict) -> str:
    cancelled = report.get("cancelled_policies") or {}
    terminated = report.get("terminated_policies") or {}
    coverage = report.get("coverage_year") or {}
    plus = report.get("plus_one_pattern") or {}
    filters = report.get("filter_inventory") or {}
    counts = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('grain'))}</td><td>{_e(item.get('event_calendar_date'))}</td>"
        f"<td>{_e(item.get('count_star'))}</td><td>{_e(item.get('count_enrollment_ids'))}</td>"
        f"<td>{_e(item.get('count_distinct_enrollment_keys'))}</td>"
        f"<td>{_e(item.get('duplicate_factor'))}</td></tr>"
        for item in report.get("count_semantics") or []
    )
    statuses = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('observed_status_value'))}</td>"
        f"<td>{_e(item.get('status_row_count'))}</td>"
        f"<td>{_e(item.get('status_distinct_enrollment_keys'))}</td></tr>"
        for item in report.get("status_values") or []
    )
    years = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('observed_coverage_year'))}</td>"
        f"<td>{_e(item.get('coverage_year_row_count'))}</td>"
        f"<td>{_e(item.get('cancelled_row_count'))}</td>"
        f"<td>{_e(item.get('terminated_row_count'))}</td></tr>"
        for item in report.get("coverage_year_rows") or []
    )
    filter_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('sisense_field'))}</td><td>{_e(item.get('role'))}</td>"
        f"<td>{_e(item.get('classification'))}</td><td>{_e(item.get('py2026_field'))}</td>"
        f"<td>{_e(item.get('evidence'))}</td></tr>"
        for item in filters.get("inventory") or []
    )
    dates = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('field'))}</td><td>{_e(item.get('sql_datatype'))}</td>"
        f"<td>{_e(item.get('date_field_min'))}</td><td>{_e(item.get('date_field_max'))}</td>"
        f"<td>{_e(item.get('null_rate'))}</td><td>{_e(item.get('date_field_distinct_dates'))}</td></tr>"
        for item in report.get("date_profiles") or []
    )
    rels = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('status_family'))}</td>"
        f"<td>{_e(item.get('other_field'))}</td>"
        f"<td>{_e(item.get('same_calendar_date_count'))}</td>"
        f"<td>{_e(item.get('candidate_after_other'))}</td>"
        f"<td>{_e(item.get('candidate_before_other'))}</td></tr>"
        for item in report.get("date_relationships") or []
    )
    plus_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('table'))}</td><td>{_e(item.get('slice'))}</td>"
        f"<td>{_e(item.get('snapshot_date'))}</td>"
        f"<td>{_e(item.get('event_before_snapshot'))}</td>"
        f"<td>{_e(item.get('event_on_snapshot'))}</td>"
        f"<td>{_e(item.get('event_on_snapshot_plus_one'))}</td>"
        f"<td>{_e(item.get('event_after_snapshot_plus_one'))}</td></tr>"
        for item in plus.get("rows") or []
    )
    strategies = "".join(
        "<tr>"
        f"<td>{_e(item.get('strategy') or item.get('activated_strategy'))}</td>"
        f"<td>{_e(item.get('name'))}</td><td>{_e(item.get('decision'))}</td>"
        f"<td>{_e(item.get('duplication_risk'))}</td>"
        f"<td>{_e(item.get('missing_event_risk'))}</td>"
        f"<td>{_e(item.get('can_reproduce_sisense_daily_count'))}</td>"
        f"<td>{_e(item.get('reason'))}</td></tr>"
        for item in report.get("extraction_strategies") or []
        if item.get("strategy")
    )
    cancelled_checks = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in cancelled.get("checks") or []
    )
    terminated_checks = "".join(
        f"<tr><td>{_e(item['check'])}</td><td>{_e(item['status'])}</td><td>{_e(item['detail'])}</td></tr>"
        for item in terminated.get("checks") or []
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>PY2026 activity semantic bridge</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:1200px}"
        "table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}"
        "th,td{border:1px solid #d8e0ea;padding:.4rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#f4f7fb}</style></head><body>"
        "<h1>PY2026 activity semantic bridge</h1>"
        f"<p>Widget: {_e(WIDGET_TITLE)}. Label: {_e(DIAGNOSTIC_LABEL)}. "
        "Not official activity. Snapshots are not summed. NULL is not zero.</p>"
        "<h2>Metric status</h2>"
        f"<p>Cancelled Policies: {_e(cancelled.get('comparison_status'))}. "
        f"Terminated Policies: {_e(terminated.get('comparison_status'))}. "
        "COMPARABLE only if every required check PASS.</p>"
        "<h2>COUNT semantics</h2>"
        "<p>Sisense COUNT(enrollment.id) is not DISTINCT.</p>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Grain</th><th>Date</th>"
        "<th>COUNT(*)</th><th>COUNT(enrollment_id)</th><th>COUNT DISTINCT</th>"
        "<th>duplicate_factor</th></tr></thead>"
        f"<tbody>{counts}</tbody></table>"
        "<h2>Status values</h2>"
        "<p>Every Term* value is listed. Terminated is not assumed to be the only member.</p>"
        "<table><thead><tr><th>Table</th><th>Status</th><th>Rows</th>"
        "<th>Distinct enrollment keys</th></tr></thead>"
        f"<tbody>{statuses}</tbody></table>"
        "<h2>Coverage year</h2>"
        f"<p>decision={_e(coverage.get('decision'))}. {_e(coverage.get('reason'))}</p>"
        "<table><thead><tr><th>Table</th><th>coverage_year</th><th>Rows</th>"
        "<th>Cancelled</th><th>Terminated</th></tr></thead>"
        f"<tbody>{years}</tbody></table>"
        "<h2>Activity widget filters</h2>"
        f"<p>{_e(filters.get('note'))}</p>"
        "<table><thead><tr><th>Sisense field</th><th>Role</th><th>Classification</th>"
        "<th>PY2026 field</th><th>Evidence</th></tr></thead>"
        f"<tbody>{filter_rows}</tbody></table>"
        "<h2>Event-date field comparison</h2>"
        "<p>Name similarity is not the decision rule.</p>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Field</th><th>Datatype</th>"
        "<th>Min</th><th>Max</th><th>Null rate</th><th>Distinct dates</th></tr></thead>"
        f"<tbody>{dates}</tbody></table>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Other field</th>"
        "<th>Same calendar date</th><th>Candidate after</th><th>Candidate before</th>"
        "</tr></thead>"
        f"<tbody>{rels}</tbody></table>"
        "<h2>+1 day pattern</h2>"
        f"<p>label={_e(plus.get('label'))}. {_e(plus.get('note'))}</p>"
        "<table><thead><tr><th>Table</th><th>Slice</th><th>Snapshot date</th>"
        "<th>Before</th><th>On date</th><th>On date + 1</th><th>After date + 1</th>"
        "</tr></thead>"
        f"<tbody>{plus_rows}</tbody></table>"
        "<h2>Extraction strategies</h2>"
        "<p>None is activated. Snapshots are not unioned.</p>"
        "<table><thead><tr><th>Strategy</th><th>Name</th><th>Decision</th>"
        "<th>Duplication risk</th><th>Missing-event risk</th>"
        "<th>Can reproduce Sisense COUNT</th><th>Reason</th></tr></thead>"
        f"<tbody>{strategies}</tbody></table>"
        + _decisive_html(report)
        + "<h2>Cancelled Policies checks</h2>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{cancelled_checks}</tbody></table>"
        "<h2>Terminated Policies checks</h2>"
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{terminated_checks}</tbody></table>"
        "</body></html>"
    )


def _decisive_html(report: dict) -> str:
    operators = "".join(
        f"<tr><td>{_e(family)}</td><td>{_e(item.get('sisense_aggregation_functions'))}</td>"
        f"<td>{_e(item.get('historical_operator'))}</td><td>{_e(item.get('decision'))}</td>"
        f"<td>{_e(item.get('reason'))}</td></tr>"
        for family, item in (report.get("count_operator") or {}).items()
    )
    grain = "".join(
        "<tr>" + "".join(f"<td>{_e(item.get(key))}</td>" for key in (
            "table", "status_family", "family_enrollment_keys", "family_rows",
            "keys_with_multiple_family_rows", "keys_with_multiple_status_values",
            "keys_with_multiple_event_dates", "keys_with_repeated_member_rows")) + "</tr>"
        for item in report.get("policy_grain") or []
    )
    scope = "".join(
        "<tr>" + "".join(f"<td>{_e(item.get(key))}</td>" for key in (
            "table", "status_family", "insurance_type_value", "scope_rows", "scope_policies",
            "benefit_year_matches", "benefit_non_null_rows")) + "</tr>"
        for item in report.get("scope_profile") or []
    )
    stability = "".join(
        f"<tr><td>{_e(family)}</td>" + "".join(f"<td>{_e(pair.get(key))}</td>" for key in (
            "earlier", "later", "lag_days", "days_compared", "retention",
            "days_within_tolerance_share", "max_day_deviation", "status")) + "</tr>"
        for family, block in ((report.get("daily_stability") or {}).get("families") or {}).items()
        for pair in block.get("pairs") or []
    )
    lineage = report.get("lineage_probe") or {}
    daily = report.get("daily_aggregation_decision") or {}
    return (
        "<h2>Count operator</h2>"
        "<p>Sisense COUNT() is a unique count; DUPCOUNT() counts every row.</p>"
        "<table><thead><tr><th>Family</th><th>Sisense functions</th><th>Historical operator</th>"
        f"<th>Decision</th><th>Reason</th></tr></thead><tbody>{operators}</tbody></table>"
        "<h2>Policy grain</h2>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Enrollment keys</th><th>Rows</th>"
        "<th>Keys with several rows</th><th>Keys with several statuses</th>"
        "<th>Keys with several dates</th><th>Keys with repeated member rows</th></tr></thead>"
        f"<tbody>{grain}</tbody></table>"
        "<h2>Plan year and insurance scope</h2>"
        "<table><thead><tr><th>Table</th><th>Family</th><th>Insurance type</th><th>Rows</th>"
        "<th>Policies</th><th>Benefit year = 2026</th><th>Benefit dated rows</th></tr></thead>"
        f"<tbody>{scope}</tbody></table>"
        "<h2>Daily stability across snapshots</h2>"
        "<table><thead><tr><th>Family</th><th>Earlier</th><th>Later</th><th>Lag days</th>"
        "<th>Days</th><th>Retention</th><th>Days within tolerance</th><th>Max deviation</th>"
        f"<th>Status</th></tr></thead><tbody>{stability}</tbody></table>"
        "<h2>Lineage probe and day boundary</h2>"
        f"<p>Lineage: {_e(lineage.get('status'))}. {_e(lineage.get('reason'))}</p>"
        f"<p>Daily aggregation: {_e(daily.get('decision'))}. {_e(daily.get('reason'))}</p>"
    )


def print_summary(report: dict) -> None:
    cancelled = report.get("cancelled_policies") or {}
    terminated = report.get("terminated_policies") or {}
    coverage = report.get("coverage_year") or {}
    event = report.get("event_date_decision") or {}
    plus = report.get("plus_one_pattern") or {}
    filters = report.get("filter_inventory") or {}
    operators = report.get("count_operator") or {}
    stability = (report.get("daily_stability") or {}).get("families") or {}
    lineage = report.get("lineage_probe") or {}
    daily = report.get("daily_aggregation_decision") or {}
    print("PHASE 5E ACTIVITY SEMANTIC BRIDGE")
    print(f"Widget: {WIDGET_TITLE}")
    for family in ("Cancelled", "Terminated"):
        item = operators.get(family) or {}
        print(f"Count operator {family}: {item.get('decision')} "
              f"sisense={item.get('sisense_aggregation_functions')} "
              f"historical={item.get('historical_operator')}")
    print(f"Event-date decision: {event.get('decision')} ({event.get('candidate_field')})")
    print(f"Coverage-year decision: {coverage.get('decision')} "
          f"(benefit-year share {coverage.get('benefit_year_match_share')})")
    print(f"Widget filters: {filters.get('decision')} predicates={filters.get('historical_predicates')}")
    for item in filters.get("inventory") or []:
        if item.get("role") in {"discovered_scope_filter", "dashboard_filter"}:
            print(f"  {item.get('sisense_field')}: {item.get('classification')} "
                  f"{item.get('mapping_status')} {item.get('historical_predicate') or ''}")
    for family in ("Cancelled", "Terminated"):
        block = stability.get(family) or {}
        print(f"Daily stability {family}: {block.get('status')}")
        for pair in block.get("pairs") or []:
            print(f"  {pair['earlier']} -> {pair['later']} lag={pair['lag_days']}d "
                  f"retention={pair['retention']} days_within={pair['days_within_tolerance_share']} "
                  f"max_dev={pair['max_day_deviation']} {pair['status']}")
    print(f"Lineage probe: {lineage.get('status')} table={lineage.get('table')} "
          f"same_date_share={lineage.get('same_date_share')} shifted_share={lineage.get('shifted_share')}")
    print(f"Daily aggregation: {daily.get('decision')}")
    print(f"+1 day label: {plus.get('label')}")
    print(f"Cancelled Policies: {cancelled.get('comparison_status')} — {cancelled.get('reason')}")
    print(f"Terminated Policies: {terminated.get('comparison_status')} — {terminated.get('reason')}")
    print("No official activity output. No visualization.")
    print("Currently Enrolled / New-Active-Passive / Applications / Pending: unchanged")


def stability_from_existing(path: Path = OUTPUT_DIR / JSON_NAME) -> dict:
    """Re-read daily counts from a previous bridge report. No database, no Sisense."""
    previous = json.loads(path.read_text(encoding="utf-8"))
    rows = previous.get("count_semantics") or []
    tables = sorted({item["table"] for item in rows if item.get("table")})
    dates = {table: snapshot_date_context(table).get("snapshot_date") for table in tables}
    document = {
        "report_kind": "PY2026_ACTIVITY_STABILITY_FROM_EXISTING_REPORT",
        "source_report": path.name,
        "snapshot_dates": dates,
        "daily_stability": daily_stability(rows, dates),
        "plus_one_label": label_plus_one_pattern(previous.get("plus_one_pattern", {}).get("rows") or []),
    }
    guard_no_pii(document)
    return document


def main_existing() -> int:
    path = OUTPUT_DIR / JSON_NAME
    if not path.is_file():
        print(f"No existing report at {path}")
        return 2
    document = stability_from_existing(path)
    out = OUTPUT_DIR / EXISTING_JSON_NAME
    out.write_text(json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8")
    print("ACTIVITY STABILITY FROM EXISTING REPORT (no database, no Sisense)")
    print(f"Snapshot dates: {document['snapshot_dates']}")
    for family, block in document["daily_stability"]["families"].items():
        print(f"{family}: {block['status']}")
        for pair in block["pairs"]:
            print(f"  {pair['earlier']} -> {pair['later']} lag={pair['lag_days']}d "
                  f"retention={pair['retention']} days_within={pair['days_within_tolerance_share']} "
                  f"max_dev={pair['max_day_deviation']} {pair['status']}")
    print(f"Wrote: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if "--existing" in (sys.argv[1:] if argv is None else argv):
        return main_existing()
    evidence = {
        "tables": [], "selected_snapshots": [], "count_semantics": [],
        "status_values": [], "coverage_years": [], "plus_one": [],
        "date_profiles": [], "date_relationships": [],
        "filter_inventory": classify_activity_filters(None, {}),
        "failures": [],
    }
    try:
        from database.config import load_database_config
        from database.connection import database_connection
        from database.query_runner import DatabaseClient

        config = load_database_config()
        with database_connection(config) as connection:
            connection.set_query_timeout(QUERY_TIMEOUT_SECONDS)
            client = DatabaseClient(connection)
            evidence = collect_evidence(client, load_discovery_artifact())
    except DatabaseError as exc:
        evidence["failures"].append({"step": "database", "error": str(exc)})
    report = build_report(evidence)
    json_path, html_path = write_report(report)
    print_summary(report)
    print(f"Wrote: {json_path}")
    print(f"Wrote: {html_path}")
    return 0


def _state(decision: str | None) -> str:
    return PASS if decision == SUPPORTED else FAIL if decision == NOT_SUPPORTED else UNRESOLVED


def _activity_checks(family: str, *, identifier_present: bool, count_ready: bool,
                     count_factor=None, status_observed: bool, event_decision: str,
                     coverage_decision: str, filter_decision: str, strategy_decision: str,
                     term_values: list[str], count_decision: str = UNRESOLVED,
                     daily_decision: str = UNRESOLVED, details: dict | None = None) -> list[dict]:
    details = details or {}
    status_name = "status_semantics"
    if family == "Cancelled":
        status_detail = "Exact Cancelled. Canceled is not treated as Cancelled."
        status_state = PASS if status_observed else UNRESOLVED
    else:
        listed = ", ".join(term_values) if term_values else "(none observed)"
        status_detail = f"startsWith('Term') / LIKE 'Term%'. Observed Term* values: {listed}."
        status_state = PASS if status_observed else UNRESOLVED
    count_detail = details.get("count") or count_operator_note(None if not count_ready else count_factor)
    return [
        _check("enrollment_identifier", PASS if identifier_present else FAIL,
               "enrollment_id is present." if identifier_present else "enrollment_id is absent."),
        _check("count_semantics", _state(count_decision) if count_ready else UNRESOLVED, count_detail),
        _check(status_name, status_state, status_detail),
        _check("event_date_semantics", _state(event_decision), details.get("event") or (
            "enrollment_last_update_date is not auto-promoted to last_update_timestamp.")),
        _check("coverage_year_semantics", _state(coverage_decision), details.get("coverage") or (
            "ssap_applications.coverage_year is not assumed equal to snapshot coverage_year.")),
        _check("actual_widget_filter_equivalence", _state(filter_decision), details.get("filters") or (
            "Currently Enrolled filters are not copied. Dashboard inheritance is unresolved without activity JAQL.")),
        _check("historical_extraction_strategy", _state(strategy_decision), details.get("strategy") or (
            "No strategy is activated. Snapshots are not unioned or summed.")),
        _check("daily_aggregation_semantics", _state(daily_decision), details.get("daily") or (
            "Sisense day grain vs CAST(event AS date) and the +1-day pattern are unproven.")),
    ]


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def _activity_widget(discovery: dict | None) -> dict | None:
    if not discovery:
        return None
    wanted = normalize_label(WIDGET_TITLE)
    cancelled = normalize_label("Cancelled Policies")
    terminated = normalize_label("Terminated Policies")
    for widget in _iter_widgets(discovery):
        title = normalize_label(widget.get("widget_title") or widget.get("widget"))
        if title == wanted:
            return widget
        measures = _measure_labels(widget)
        if cancelled in measures and terminated in measures:
            return widget
    return None


def _iter_widgets(discovery: dict) -> list[dict]:
    widgets = list(discovery.get("widgets") or [])
    widgets.extend(discovery.get("widget_inventory") or [])
    widgets.extend(discovery.get("cancellation_source_identification") or [])
    return widgets


def _measure_labels(widget: dict) -> set[str]:
    labels = set()
    for item in widget.get("measures") or []:
        if isinstance(item, dict):
            labels.add(normalize_label(item.get("title")))
        else:
            labels.add(normalize_label(item))
    for item in widget.get("measure_titles") or []:
        labels.add(normalize_label(item))
    labels.discard(None)
    return labels


def _classify_discovered_filter(role: dict, historical: dict, verified_dashboard: list[dict]) -> dict:
    column = role.get("column")
    field = None if not column else f"{role.get('table')}.{column}"
    disabled = role.get("disabled")
    dashboard = role.get("is_dashboard_filter")
    if disabled is True:
        classification = DISABLED
    elif dashboard is True:
        classification = DASHBOARD_APPLIED
    elif dashboard is False:
        classification = WIDGET_OWN
    else:
        classification = UNRESOLVED
    target = DASHBOARD_FILTER_MAPPING.get(field or "")
    py2026 = historical.get(target) if target else (
        None if column is None else historical.get(str(column).lower()))
    verified = next((item for item in verified_dashboard if item["sisense_field"] == field), None)
    predicate = None
    if classification == UNRESOLVED:
        status, note = UNRESOLVED, "isDashboardFilter was not captured."
    elif classification == DISABLED:
        status, note = SUPPORTED, "Disabled; not applied."
    elif role.get("filter_kind") == "all":
        status, note = SUPPORTED, "Include-all selection; no restriction."
    elif target is None:
        status, note = UNRESOLVED, "No proven historical mapping for this filter."
    elif py2026 is None:
        status, note = NOT_SUPPORTED, "Historical snapshot has no column for this filter."
    elif verified is None or role.get("filter_fingerprint") != verified["filter_fingerprint"]:
        status, note = UNRESOLVED, "Filter definition differs from the verified dashboard filter."
    elif target == "coverage_year":
        status, note = SUPPORTED, "Same definition as verified 2027; plan-year shifted."
        predicate = f"{py2026} = {PREVIOUS_PLAN_YEAR}"
    else:
        status, note = SUPPORTED, "Same definition as the verified Health dashboard filter."
        predicate = f"{py2026} = '{HEALTH_MEMBER}'"
    return {
        "sisense_field": field,
        "role": "discovered_scope_filter",
        "classification": classification,
        "py2026_field": py2026,
        "filter_kind": role.get("filter_kind"),
        "locked": role.get("locked"),
        "mapping_status": status,
        "historical_predicate": predicate,
        "evidence": f"Captured activity-widget scope filter. {note} Member values are compared by fingerprint only.",
    }


def _dashboard_filters_from_currently_enrolled(historical: dict) -> list[dict]:
    """Dashboard filters seen on Currently Enrolled are not assumed to apply here."""
    payload = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
    definition = interpret_payload(payload)
    rows = []
    for item in definition.get("scope_filters") or []:
        if item.get("is_dashboard_filter") is not True:
            continue
        column = item.get("column")
        rows.append({
            "sisense_field": None if not column else f"{item.get('table')}.{column}",
            "classification": UNRESOLVED,
            "py2026_field": None if column is None else historical.get(str(column).lower()),
            "evidence": (
                "Present as isDashboardFilter on the verified Currently Enrolled request. "
                "Whether this activity widget applies it is unproven."
            ),
        })
    return rows


def _usable(item: dict) -> tuple[bool, str | None, dict]:
    columns = item.get("columns") or {}
    candidate = item.get("event_date_candidate") or select_event_date_candidate(columns)
    if "enrollment_id" not in columns:
        return False, "enrollment_id absent", candidate
    if "enrollment_status_description" not in columns:
        return False, "enrollment_status_description absent", candidate
    if "coverage_year" not in columns:
        return False, "coverage_year absent", candidate
    if not candidate.get("py2026_candidate"):
        return False, "no datetime event-date candidate", candidate
    return True, None, candidate


def _policy_grain(client, failures, item) -> list[dict]:
    usable, reason, candidate = _usable(item)
    rows = []
    for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
        row = {"table": item["table"], "status_family": label, "family_enrollment_keys": None,
               "member_column_present": "enrollee_id" in (item.get("columns") or {}),
               "skip_reason": reason}
        if usable:
            sql, params = build_policy_grain_sql(
                qualified_name(item["schema_name"], item["table"]), item["columns"],
                candidate["py2026_candidate"], candidate.get("sql_datatype"), cancelled=cancelled)
            result = _run(client, failures, f"policy grain {label} {item['table']}", sql, params)
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                row.update({key: _as_int(value) for key, value in mapped.items()})
                row["skip_reason"] = None
        rows.append(row)
    return rows


def _scope_profile(client, failures, item) -> list[dict]:
    usable, reason, _candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    rows = []
    for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
        sql, params = build_scope_profile_sql(
            qualified_name(item["schema_name"], item["table"]), item["columns"], cancelled=cancelled)
        result = _run(client, failures, f"scope profile {label} {item['table']}", sql, params)
        if result is None:
            continue
        for raw in result.rows:
            mapped = dict(zip(result.columns, raw))
            value = mapped.get("insurance_type_value")
            rows.append({
                "table": item["table"],
                "status_family": label,
                "insurance_type_value": None if value is None else str(value),
                "scope_rows": _as_int(mapped.get("scope_rows")),
                "scope_policies": _as_int(mapped.get("scope_policies")),
                "benefit_year_matches": _as_int(mapped.get("benefit_year_matches")),
                "benefit_non_null_rows": _as_int(mapped.get("benefit_non_null_rows")),
            })
    return rows


def _lineage(client, failures, snapshot) -> list[dict]:
    """Metadata probe for a non-snapshot last_update_timestamp source; PY2026 rows only."""
    if snapshot is None:
        return []
    usable, _reason, candidate = _usable(snapshot)
    if not usable:
        return []
    result = _run(client, failures, "lineage columns", LINEAGE_COLUMNS_SQL, None)
    if result is None:
        return []
    rows = []
    for schema_name, table_name, data_type in result.rows:
        if len(rows) >= MAX_LINEAGE_TABLES:
            break
        if not is_datetime_sql_type(str(data_type or "")):
            continue
        live = {"schema_name": str(schema_name), "table": str(table_name)}
        columns = _columns(client, failures, live)
        if not {"enrollment_id", "coverage_year", "last_update_timestamp"} <= set(columns):
            rows.append({"live_table": live["table"], "matched_keys": None,
                         "skip_reason": "enrollment_id or coverage_year absent"})
            continue
        sql, params = build_lineage_agreement_sql(
            qualified_name(live["schema_name"], live["table"]), columns,
            qualified_name(snapshot["schema_name"], snapshot["table"]), snapshot["columns"],
            candidate["py2026_candidate"], candidate.get("sql_datatype"))
        agreement = _run(client, failures, f"lineage {live['table']}", sql, params)
        row = {"live_table": live["table"], "snapshot_table": snapshot["table"], "matched_keys": None}
        if agreement is not None and agreement.row_count == 1:
            row.update({key: _as_int(value) for key, value in _row_dict(agreement).items()})
        rows.append(row)
    return rows


def _count_semantics(client, failures, item, *, apply_health: bool = False) -> list[dict]:
    usable, reason, candidate = _usable(item)
    rows = []
    for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
        family = {
            "table": item["table"],
            "status_family": label,
            "grain": "family",
            "event_calendar_date": None,
            "count_star": None,
            "count_enrollment_ids": None,
            "count_distinct_enrollment_keys": None,
            "duplicate_factor": None,
            "operator": "COUNT",
            "not_distinct": True,
            "skip_reason": reason,
        }
        if usable:
            sql, params = build_count_family_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate["py2026_candidate"],
                candidate.get("sql_datatype"), cancelled=cancelled, apply_health=apply_health,
            )
            result = _run(client, failures, f"count family {label} {item['table']}", sql, params)
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                count = _as_int(mapped.get("count_enrollment_ids"))
                distinct = _as_int(mapped.get("count_distinct_enrollment_keys"))
                factor = duplicate_factor(count, distinct)
                family.update({
                    "count_star": _as_int(mapped.get("count_star")),
                    "count_enrollment_ids": count,
                    "count_distinct_enrollment_keys": distinct,
                    "duplicate_factor": factor,
                    "note": count_operator_note(factor),
                    "skip_reason": None,
                })
            sql, params = build_count_daily_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate["py2026_candidate"],
                candidate.get("sql_datatype"), cancelled=cancelled, apply_health=apply_health,
            )
            daily = _run(client, failures, f"count daily {label} {item['table']}", sql, params)
            if daily is not None:
                for raw in daily.rows:
                    mapped = dict(zip(daily.columns, raw))
                    count = _as_int(mapped.get("count_enrollment_ids"))
                    distinct = _as_int(mapped.get("count_distinct_enrollment_keys"))
                    rows.append({
                        "table": item["table"],
                        "status_family": label,
                        "grain": "day",
                        "event_calendar_date": _json_value(mapped.get("event_calendar_date")),
                        "count_star": _as_int(mapped.get("count_star")),
                        "count_enrollment_ids": count,
                        "count_distinct_enrollment_keys": distinct,
                        "duplicate_factor": duplicate_factor(count, distinct),
                        "operator": "COUNT",
                        "not_distinct": True,
                    })
        rows.append(family)
    return rows


def _status_values(client, failures, item) -> list[dict]:
    usable, reason, _candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    sql, params = build_status_value_sql(
        qualified_name(item["schema_name"], item["table"]), item["columns"])
    result = _run(client, failures, f"status values {item['table']}", sql, params)
    if result is None:
        return [{"table": item["table"], "skip_reason": "status query failed"}]
    rows = []
    for raw in result.rows:
        mapped = dict(zip(result.columns, raw))
        value = mapped.get("observed_status_value")
        rows.append({
            "table": item["table"],
            "observed_status_value": None if value is None else str(value),
            "status_row_count": _as_int(mapped.get("status_row_count")),
            "status_distinct_enrollment_keys": _as_int(mapped.get("status_distinct_enrollment_keys")),
            "term_family": bool(value) and str(value).strip().casefold().startswith("term"),
            "cancelled_exact": bool(value) and str(value).strip().casefold() == "cancelled",
        })
    return rows


def _coverage_years(client, failures, item) -> list[dict]:
    columns = item.get("columns") or {}
    if "coverage_year" not in columns or "enrollment_status_description" not in columns:
        return [{"table": item["table"], "skip_reason": "coverage_year or status absent"}]
    sql, params = build_coverage_year_sql(
        qualified_name(item["schema_name"], item["table"]), columns)
    result = _run(client, failures, f"coverage year {item['table']}", sql, params)
    if result is None:
        return [{"table": item["table"], "skip_reason": "coverage_year query failed"}]
    rows = []
    for raw in result.rows:
        mapped = dict(zip(result.columns, raw))
        rows.append({
            "table": item["table"],
            "observed_coverage_year": _json_value(mapped.get("observed_coverage_year")),
            "coverage_year_row_count": _as_int(mapped.get("coverage_year_row_count")),
            "cancelled_row_count": _as_int(mapped.get("cancelled_row_count")),
            "terminated_row_count": _as_int(mapped.get("terminated_row_count")),
        })
    return rows


def _plus_one(client, failures, item) -> list[dict]:
    context = item.get("snapshot_date") or snapshot_date_context(item["table"])
    usable, reason, candidate = _usable(item)
    if not context.get("snapshot_date"):
        return [{
            "table": item["table"],
            "slice": "ALL",
            "snapshot_date": None,
            "skip_reason": "SKIPPED_NO_PROVEN_SNAPSHOT_DATE",
        }]
    if not usable:
        return [{"table": item["table"], "slice": "ALL", "skip_reason": reason}]
    rows = []
    for mode, label in (("all", "ALL"), ("cancelled", "Cancelled"), ("term", "Terminated")):
        sql, params = build_plus_one_sql(
            qualified_name(item["schema_name"], item["table"]),
            item["columns"], candidate["py2026_candidate"], candidate.get("sql_datatype"),
            mode=mode, snapshot_date=context["snapshot_date"],
        )
        result = _run(client, failures, f"plus one {label} {item['table']}", sql, params)
        row = {
            "table": item["table"],
            "slice": label,
            "snapshot_date": context["snapshot_date"],
            "event_before_snapshot": None,
            "event_on_snapshot": None,
            "event_on_snapshot_plus_one": None,
            "event_after_snapshot_plus_one": None,
        }
        if result is not None and result.row_count == 1:
            mapped = _row_dict(result)
            row.update({
                "event_before_snapshot": _as_int(mapped.get("event_before_snapshot")),
                "event_on_snapshot": _as_int(mapped.get("event_on_snapshot")),
                "event_on_snapshot_plus_one": _as_int(mapped.get("event_on_snapshot_plus_one")),
                "event_after_snapshot_plus_one": _as_int(mapped.get("event_after_snapshot_plus_one")),
            })
        rows.append(row)
    return rows


def _date_profiles(client, failures, item) -> list[dict]:
    usable, reason, _candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    rows = []
    for field in item.get("date_fields") or relevant_date_fields(item["columns"]):
        for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
            sql, params = build_date_field_profile_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], field["name"], field.get("data_type"), cancelled=cancelled,
            )
            result = _run(
                client, failures, f"date profile {field['name']} {label} {item['table']}",
                sql, params)
            row = {
                "table": item["table"],
                "status_family": label,
                "field": field["name"],
                "sql_datatype": field.get("data_type"),
                "date_field_min": None,
                "date_field_max": None,
                "date_field_nulls": None,
                "date_field_rows": None,
                "date_field_distinct_dates": None,
                "null_rate": None,
            }
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                total = _as_int(mapped.get("date_field_rows"))
                nulls = _as_int(mapped.get("date_field_nulls"))
                row.update({
                    "date_field_min": _json_value(mapped.get("date_field_min")),
                    "date_field_max": _json_value(mapped.get("date_field_max")),
                    "date_field_nulls": nulls,
                    "date_field_rows": total,
                    "date_field_distinct_dates": _as_int(mapped.get("date_field_distinct_dates")),
                    "null_rate": None if total in (None, 0) or nulls is None else round(100.0 * nulls / total, 4),
                })
            rows.append(row)
    return rows


def _date_relationships(client, failures, item) -> list[dict]:
    usable, reason, candidate = _usable(item)
    if not usable:
        return [{"table": item["table"], "skip_reason": reason}]
    candidate_name = candidate["py2026_candidate"]
    candidate_type = candidate.get("sql_datatype")
    rows = []
    for field in item.get("date_fields") or []:
        if field["name"].lower() == candidate_name.lower():
            continue
        for cancelled, label in ((True, "Cancelled"), (False, "Terminated")):
            sql, params = build_date_relationship_sql(
                qualified_name(item["schema_name"], item["table"]),
                item["columns"], candidate_name, candidate_type,
                field["name"], field.get("data_type"), cancelled=cancelled,
            )
            result = _run(
                client, failures,
                f"date rel {field['name']} {label} {item['table']}", sql, params)
            row = {
                "table": item["table"],
                "status_family": label,
                "candidate_field": candidate_name,
                "other_field": field["name"],
                "same_calendar_date_count": None,
                "candidate_after_other": None,
                "candidate_before_other": None,
                "other_null_when_candidate_present": None,
            }
            if result is not None and result.row_count == 1:
                mapped = _row_dict(result)
                row.update({
                    "same_calendar_date_count": _as_int(mapped.get("same_calendar_date_count")),
                    "candidate_after_other": _as_int(mapped.get("candidate_after_other")),
                    "candidate_before_other": _as_int(mapped.get("candidate_before_other")),
                    "other_null_when_candidate_present": _as_int(
                        mapped.get("other_null_when_candidate_present")),
                })
            rows.append(row)
    return rows


def _family_factor(rows: list[dict], family: str):
    for item in rows:
        if item.get("status_family") == family and item.get("grain") == "family":
            return item.get("duplicate_factor")
    return None


def _count_ready(rows: list[dict], family: str) -> bool:
    return any(
        item.get("status_family") == family
        and item.get("grain") == "family"
        and item.get("count_enrollment_ids") is not None
        and item.get("count_distinct_enrollment_keys") is not None
        for item in rows
    )


def _status_observed(rows: list[dict], *, exact_cancelled: bool) -> bool:
    if exact_cancelled:
        return any(item.get("cancelled_exact") for item in rows)
    return any(item.get("term_family") for item in rows)


def _term_values(rows: list[dict]) -> list[str]:
    values = []
    for item in rows:
        if not item.get("term_family"):
            continue
        value = item.get("observed_status_value")
        if value and value not in values:
            values.append(value)
    return values


def _list_snapshots(client, failures) -> list[dict]:
    result = _run(client, failures, "snapshot tables", TABLES_SQL, None)
    if result is None:
        return []
    rows = []
    for schema_name, table_name, table_type in result.rows:
        name = str(table_name)
        if not name.lower().startswith(SNAPSHOT_PREFIX.lower()):
            continue
        if any(marker in name.lower() for marker in INBOUND_MARKERS):
            continue
        rows.append({
            "schema_name": str(schema_name),
            "table": name,
            "table_type": str(table_type or ""),
        })
    return rows


def _columns(client, failures, item) -> dict:
    result = _run(
        client, failures, f"columns {item['table']}", COLUMNS_SQL,
        (item["schema_name"], item["table"]),
    )
    if result is None:
        return {}
    return {
        str(name).lower(): {"name": str(name), "data_type": str(dtype or "")}
        for name, dtype in result.rows if name
    }


def _row_dict(result) -> dict:
    return {str(column): value for column, value in zip(result.columns, result.rows[0])}


def _run(client, failures, step, sql, params):
    try:
        return client.execute_query(sql, params)
    except QueryExecutionError as exc:
        failures.append({"step": step, "error": str(exc)})
        return None


def _as_int(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value)


def _json_value(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


if __name__ == "__main__":
    raise SystemExit(main())
