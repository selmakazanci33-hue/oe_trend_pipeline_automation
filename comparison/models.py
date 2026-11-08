"""Comparison foundation types. No SQL, no JAQL, no Sisense calls.

Metric families stay separate. A snapshot is never an activity count.
People-level cancellation is never a policy-level cancellation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

COMPARABLE = "COMPARABLE"
DIAGNOSTIC = "DIAGNOSTIC"
NOT_COMPARABLE = "NOT_COMPARABLE"
COMPARISON_STATUSES = (COMPARABLE, DIAGNOSTIC, NOT_COMPARABLE)

FAMILY_POPULATION = "POPULATION_SNAPSHOT"
FAMILY_ACTIVITY = "ACTIVITY_EVENT"
FAMILY_LIFECYCLE = "LIFECYCLE_CLASSIFICATION"
FAMILY_APPLICATION = "APPLICATION"
METRIC_FAMILIES = (FAMILY_POPULATION, FAMILY_ACTIVITY, FAMILY_LIFECYCLE, FAMILY_APPLICATION)

GRAIN_PERSON = "PERSON"
GRAIN_POLICY = "POLICY_ENROLLMENT_RECORD"
GRAIN_APPLICATION = "APPLICATION"
GRAINS = (GRAIN_PERSON, GRAIN_POLICY, GRAIN_APPLICATION)

DATE_CURRENT_INDICATOR = "CURRENT_INDICATOR"
DATE_DAILY_EVENT = "DAILY_EVENT"
DATE_MONTH_END_SNAPSHOT = "MONTH_END_SNAPSHOT"
DATE_LIFECYCLE_WINDOW = "LIFECYCLE_WINDOW"
DATE_UNRESOLVED = "UNRESOLVED"

AGG_ACTIVITY_SUM = "ACTIVITY_SUM"
AGG_SNAPSHOT = "SNAPSHOT"
AGG_LIFECYCLE = "LIFECYCLE_CLASSIFICATION"
AGGREGATION_METHODS = (AGG_ACTIVITY_SUM, AGG_SNAPSHOT, AGG_LIFECYCLE)

IDENTIFIER_UNPROVEN = "IDENTIFIER_EQUIVALENCE_UNPROVEN"
IDENTIFIER_EXACT = "IDENTIFIER_EQUIVALENCE_EXACT"
IDENTIFIER_PROXY = "IDENTIFIER_DIAGNOSTIC_PROXY"

ALIGNED = "ALIGNED"
UNALIGNED = "UNALIGNED"

PY2026_SOURCE = "historical Georgia Access enrollment database"
PY2027_SOURCE = "Sisense PY 2027 OE Executive Dashboard"

SECTION_POPULATION = "enrollment_population"
SECTION_LIFECYCLE = "enrollment_lifecycle"
SECTION_ACTIVITY = "cancellation_termination_activity"
SECTION_APPLICATIONS = "applications"
SECTION_UNRESOLVED = "unresolved_metrics"

PENDING_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SemanticChecks:
    """Every flag must be true before a row may be COMPARABLE."""

    identifier_equivalence_proven: bool = False
    grain_match: bool = False
    family_match: bool = False
    date_semantics_match: bool = False
    py2026_definition_verified: bool = False
    py2027_definition_verified: bool = False
    filter_mapping_proven: bool = False
    both_values_present: bool = False
    pending_has_verified_definition: bool = True
    people_policy_not_mixed: bool = False
    snapshot_not_summed: bool = True
    py2026_sql_not_invented: bool = True

    def comparable_ready(self) -> bool:
        return all((
            self.identifier_equivalence_proven,
            self.grain_match,
            self.family_match,
            self.date_semantics_match,
            self.py2026_definition_verified,
            self.py2027_definition_verified,
            self.filter_mapping_proven,
            self.both_values_present,
            self.pending_has_verified_definition,
            self.people_policy_not_mixed,
            self.snapshot_not_summed,
            self.py2026_sql_not_invented,
        ))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str
    metric_name: str
    metric_family: str
    grain: str
    date_semantics: str
    section: str
    py2026_source: str
    py2027_source: str
    default_status: str
    reason: str
    identifier_status: str = IDENTIFIER_UNPROVEN
    py2027_widget: str | None = None
    py2027_measure: str | None = None
    aggregation_method: str | None = None
    pending_verified_definition: bool = True


@dataclass(frozen=True)
class ComparisonRow:
    metric_name: str
    metric_family: str
    py2026_value: int | None
    py2027_value: int | None
    absolute_difference: int | None
    percent_difference: int | str | None
    comparison_status: str
    grain: str
    date_semantics: str
    py2026_source: str
    py2027_source: str
    reason: str
    as_of_date_py2026: str | None
    as_of_date_py2027: str | None
    metric_id: str = ""
    section: str = ""
    aggregation_method: str | None = None
    identifier_status: str = IDENTIFIER_UNPROVEN
    percent_status: str = "NOT_COMPUTED"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DailyObservation:
    metric_name: str
    plan_year: int
    calendar_date: date
    oe_day: int | None
    value: int | None
    grain: str
    date_semantics: str
    source: str
    comparison_status: str
    metric_family: str = FAMILY_ACTIVITY
    alignment: str = UNALIGNED

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["calendar_date"] = self.calendar_date.isoformat()
        return payload


@dataclass(frozen=True)
class MonthlyObservation:
    metric_name: str
    plan_year: int
    year_month: str
    value: int | None
    grain: str
    date_semantics: str
    source: str
    comparison_status: str
    aggregation_method: str
    metric_family: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OeAnchor:
    plan_year: int
    anchor_date: date | None
    alignment: str
    reason: str


@dataclass(frozen=True)
class IdentifierEquivalence:
    py2027_field: str
    py2026_candidate: str
    status: str
    reason: str


def default_identifier_requirement() -> IdentifierEquivalence:
    return IdentifierEquivalence(
        py2027_field="Sisense enrollee exchange individual identifier",
        py2026_candidate="historical enrollee_id is a diagnostic proxy only",
        status=IDENTIFIER_UNPROVEN,
        reason=(
            "PY2027 Currently Enrolled counts the Sisense enrollee exchange individual "
            "identifier. PY2026 snapshots have used enrollee_id as a diagnostic proxy. "
            "Those names are not treated as one identifier until equivalence is proven."
        ),
    )


FOUNDATION_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        metric_id="currently_enrolled_people",
        metric_name="Currently Enrolled People",
        metric_family=FAMILY_POPULATION,
        grain=GRAIN_PERSON,
        date_semantics=DATE_CURRENT_INDICATOR,
        section=SECTION_POPULATION,
        py2026_source=PY2026_SOURCE,
        py2027_source="verified Sisense Currently Enrolled People widget",
        default_status=DIAGNOSTIC,
        reason=(
            "PY2027 uses the existing verified extraction. PY2026 has diagnostic "
            "latest-state and snapshot series, but period, filters, and person-identifier "
            "equivalence are not proven. Not promoted to COMPARABLE."
        ),
        py2027_widget="Currently Enrolled People",
        py2027_measure="Currently Enrolled People",
        aggregation_method=AGG_SNAPSHOT,
    ),
    MetricSpec(
        metric_id="enrolled_cancelled",
        metric_name="Enrolled & Cancelled People",
        metric_family=FAMILY_POPULATION,
        grain=GRAIN_PERSON,
        date_semantics=DATE_CURRENT_INDICATOR,
        section=SECTION_POPULATION,
        py2026_source=PY2026_SOURCE,
        py2027_source="Sisense Enrolled/Cancelled vs Cancelled/Not Enrolled People pivot",
        default_status=NOT_COMPARABLE,
        reason=(
            "Person metric: cancelled enrollment evidence plus Enrolled/Pending/Terminated "
            "population evidence. PY2026 columns needed to reproduce that rule are not verified. "
            "Not interchangeable with Cancelled Policies."
        ),
        py2027_widget="Enrolled/Cancelled vs Cancelled/Not Enrolled People",
        py2027_measure="Enrolled & Cancelled",
        aggregation_method=AGG_SNAPSHOT,
    ),
    MetricSpec(
        metric_id="cancelled_not_enrolled",
        metric_name="Cancelled - Not Enrolled People",
        metric_family=FAMILY_POPULATION,
        grain=GRAIN_PERSON,
        date_semantics=DATE_CURRENT_INDICATOR,
        section=SECTION_POPULATION,
        py2026_source=PY2026_SOURCE,
        py2027_source="Sisense Enrolled/Cancelled vs Cancelled/Not Enrolled People pivot",
        default_status=NOT_COMPARABLE,
        reason=(
            "Person metric: cancelled evidence without the widget's Enrolled/Pending/Terminated "
            "population condition. PY2026 equivalent is not implemented. Not a policy activity count."
        ),
        py2027_widget="Enrolled/Cancelled vs Cancelled/Not Enrolled People",
        py2027_measure="Cancelled - Not Enrolled",
        aggregation_method=AGG_SNAPSHOT,
    ),
    MetricSpec(
        metric_id="new_enrolled",
        metric_name="New Enrolled People",
        metric_family=FAMILY_LIFECYCLE,
        grain=GRAIN_PERSON,
        date_semantics=DATE_LIFECYCLE_WINDOW,
        section=SECTION_LIFECYCLE,
        py2026_source=PY2026_SOURCE,
        py2027_source="verified Sisense New/Active/Passive - Enrolled People widget, NEW measure",
        default_status=NOT_COMPARABLE,
        reason=(
            "Lifecycle classification, not an enrollment_status value. PY2027 stays on the "
            "existing Sisense path. PY2026 SQL with status NEW/ACTIVE/PASSIVE is not allowed. "
            "Historical columns for the OE-boundary case logic are not verified."
        ),
        py2027_widget="New/Active/Passive - Enrolled People",
        py2027_measure="NEW",
        aggregation_method=AGG_LIFECYCLE,
    ),
    MetricSpec(
        metric_id="active_enrolled",
        metric_name="Active Re-Enrolled People",
        metric_family=FAMILY_LIFECYCLE,
        grain=GRAIN_PERSON,
        date_semantics=DATE_LIFECYCLE_WINDOW,
        section=SECTION_LIFECYCLE,
        py2026_source=PY2026_SOURCE,
        py2027_source="verified Sisense New/Active/Passive - Enrolled People widget, ACTIVE measure",
        default_status=NOT_COMPARABLE,
        reason=(
            "Lifecycle classification requiring current-cycle create-after-OE-boundary plus "
            "qualifying prior-period coverage. PY2026 fields for that rule are not verified."
        ),
        py2027_widget="New/Active/Passive - Enrolled People",
        py2027_measure="ACTIVE",
        aggregation_method=AGG_LIFECYCLE,
    ),
    MetricSpec(
        metric_id="passive_enrolled",
        metric_name="Passive / Automatic Re-Enrolled People",
        metric_family=FAMILY_LIFECYCLE,
        grain=GRAIN_PERSON,
        date_semantics=DATE_LIFECYCLE_WINDOW,
        section=SECTION_LIFECYCLE,
        py2026_source=PY2026_SOURCE,
        py2027_source="verified Sisense New/Active/Passive - Enrolled People widget, PASSIVE measure",
        default_status=NOT_COMPARABLE,
        reason=(
            "Automatic/passive re-enrollee whose qualifying current-cycle record predates the "
            "configured active-renewal boundary. PY2026 fields for that rule are not verified."
        ),
        py2027_widget="New/Active/Passive - Enrolled People",
        py2027_measure="PASSIVE",
        aggregation_method=AGG_LIFECYCLE,
    ),
    MetricSpec(
        metric_id="cancelled_policies_daily",
        metric_name="Cancelled Policies",
        metric_family=FAMILY_ACTIVITY,
        grain=GRAIN_POLICY,
        date_semantics=DATE_DAILY_EVENT,
        section=SECTION_ACTIVITY,
        py2026_source=PY2026_SOURCE,
        py2027_source="Sisense Cancelled Policies daily activity (policy/enrollment grain)",
        default_status=NOT_COMPARABLE,
        reason=(
            "Daily policy activity, not a current cancelled population. PY2026 event SQL is not "
            "verified (table, COUNT of enrollment id, Cancelled status, last_update_timestamp, day grain). "
            "Do not call this currently cancelled."
        ),
        py2027_widget="Cancelled Policies",
        py2027_measure="Cancelled Policies",
        aggregation_method=AGG_ACTIVITY_SUM,
    ),
    MetricSpec(
        metric_id="terminated_policies_daily",
        metric_name="Terminated Policies",
        metric_family=FAMILY_ACTIVITY,
        grain=GRAIN_POLICY,
        date_semantics=DATE_DAILY_EVENT,
        section=SECTION_ACTIVITY,
        py2026_source=PY2026_SOURCE,
        py2027_source="Sisense Terminated Policies daily activity (policy/enrollment grain)",
        default_status=NOT_COMPARABLE,
        reason=(
            "Daily policy activity, not a current terminated population. PY2026 event SQL is not "
            "verified. Catalog discovery has not treated a terminated date column as observed; "
            "this phase does not copy the cancelled-policies date identity. Do not call this currently terminated."
        ),
        py2027_widget="Terminated Policies",
        py2027_measure="Terminated Policies",
        aggregation_method=AGG_ACTIVITY_SUM,
    ),
    MetricSpec(
        metric_id="applications_started",
        metric_name="Applications Started",
        metric_family=FAMILY_APPLICATION,
        grain=GRAIN_APPLICATION,
        date_semantics=DATE_UNRESOLVED,
        section=SECTION_APPLICATIONS,
        py2026_source=PY2026_SOURCE,
        py2027_source="verified Sisense Applications Started widget",
        default_status=NOT_COMPARABLE,
        reason="Application metrics stay separate from enrollment. No proven PY2026 application population or grain.",
        py2027_widget="Applications Started",
        aggregation_method=AGG_SNAPSHOT,
    ),
    MetricSpec(
        metric_id="submitted_applications",
        metric_name="Submitted Applications",
        metric_family=FAMILY_APPLICATION,
        grain=GRAIN_APPLICATION,
        date_semantics=DATE_UNRESOLVED,
        section=SECTION_APPLICATIONS,
        py2026_source=PY2026_SOURCE,
        py2027_source="verified Sisense Submitted Applications widget",
        default_status=NOT_COMPARABLE,
        reason="Application metrics stay separate from enrollment. No proven PY2026 application population or grain.",
        py2027_widget="Submitted Applications",
        aggregation_method=AGG_SNAPSHOT,
    ),
    MetricSpec(
        metric_id="total_applicants",
        metric_name="Total Applicants / Applicants On Submitted Applications",
        metric_family=FAMILY_APPLICATION,
        grain=GRAIN_APPLICATION,
        date_semantics=DATE_UNRESOLVED,
        section=SECTION_APPLICATIONS,
        py2026_source=PY2026_SOURCE,
        py2027_source="Sisense Total Applicants candidate (not a verified production total)",
        default_status=NOT_COMPARABLE,
        reason="No distinct verified Application Total. Started + Submitted are not summed. No PY2026 equivalent.",
        py2027_widget="Applicants On Submitted Applications",
        py2027_measure="Total Applicants",
        aggregation_method=AGG_SNAPSHOT,
    ),
    MetricSpec(
        metric_id="pending_people",
        metric_name="Pending People",
        metric_family=FAMILY_POPULATION,
        grain=GRAIN_PERSON,
        date_semantics=DATE_UNRESOLVED,
        section=SECTION_UNRESOLVED,
        py2026_source=PY2026_SOURCE,
        py2027_source="not a verified standalone Sisense metric",
        default_status=NOT_COMPARABLE,
        reason=(
            f"{PENDING_UNAVAILABLE}: Pending appears as a filter member of Currently Enrolled People. "
            "No standalone Pending People business metric has been proven."
        ),
        pending_verified_definition=False,
        aggregation_method=AGG_SNAPSHOT,
    ),
)


def spec_by_id(metric_id: str) -> MetricSpec:
    for spec in FOUNDATION_METRICS:
        if spec.metric_id == metric_id:
            return spec
    raise KeyError(f"Unknown foundation metric {metric_id!r}")


UNRESOLVED_DATABASE_QUESTIONS: tuple[str, ...] = (
    "Are the Sisense enrollee exchange individual identifier and historical enrollee_id the same person?",
    "Which PY2026 columns reproduce New/Active/Passive OE-boundary case logic?",
    "Which PY2026 columns reproduce Enrolled & Cancelled vs Cancelled - Not Enrolled person-level rules?",
    "Which PY2026 date column is the event date for Cancelled Policies daily activity?",
    "Which PY2026 date column is the event date for Terminated Policies daily activity?",
    "What is the authoritative OE start/anchor date for each plan year?",
    "Is there a verified standalone Pending People definition on either source?",
    "What PY2026 population and grain match Applications Started, Submitted Applications, and Total Applicants?",
)
