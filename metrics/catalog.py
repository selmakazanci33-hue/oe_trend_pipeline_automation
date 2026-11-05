"""Metric catalog: descriptive metadata only.

Active (production-VERIFIED) metrics have exactly one verified source:

- ``query_builder``: the verified application payload (Currently Enrolled);
- ``passive_source``: a widget/measure in the dashboard's own JAQL whose
  request definition is verified either by the verified payload of that same
  widget or by a definition fingerprint captured in a controlled run.

Payload contents are never stored here. Roadmap items and promotion
candidates stay inactive (no snapshots, not in the executive report) until
their source definition is verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from config.settings import SOURCE_LABEL, VERIFIED_DASHBOARD_TITLE
from sisense.models import JaqlQuery
from sisense.payloads.currently_enrolled import METRIC_NAME as CURRENTLY_ENROLLED_ID
from sisense.payloads.currently_enrolled import PLAN_YEAR, WIDGET_TITLE, build_currently_enrolled_query

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending_verification"
STATUS_VALIDATION_ONLY = "validation_only"
PENDING_MESSAGE = "Metric integration pending verification."

VALUE_FORMATS = {"integer", "decimal", "percent"}


@dataclass(frozen=True)
class PassiveSource:
    """A measure inside a widget request the dashboard sends by itself."""

    widget_id: str
    widget_title: str
    measure_title: str | None  # None: the widget's single measure (matched by widget title)
    expected_definition: Callable[[], JaqlQuery] | None = None
    expected_fingerprint: str | None = None
    # True: expected_fingerprint is taken from a previously recorded STABLE
    # observation (never invented, never taken from the current request).
    approved_from_observations: bool = False

    @property
    def verifiable(self) -> bool:
        return (self.expected_definition is not None or self.expected_fingerprint is not None
                or self.approved_from_observations)


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    display_name: str
    category: str
    unit: str
    value_format: str
    source: str
    dashboard: str
    plan_year: int
    description: str
    history_enabled: bool
    status: str
    module_id: str
    query_builder: Callable[[], JaqlQuery] | None = None
    passive_source: PassiveSource | None = None
    section_id: str | None = None

    def __post_init__(self) -> None:
        if self.status == STATUS_ACTIVE:
            if (self.query_builder is None) == (self.passive_source is None):
                raise ValueError(f"Active metric {self.metric_id!r} needs exactly one verified source")
            if self.passive_source is not None and not self.passive_source.verifiable:
                raise ValueError(f"Active metric {self.metric_id!r} has an unverified passive source")
        elif self.query_builder is not None:
            raise ValueError(f"Non-active metric {self.metric_id!r} must not reference a payload builder")
        if self.value_format not in VALUE_FORMATS:
            raise ValueError(f"Unsupported value_format {self.value_format!r}")


@dataclass(frozen=True)
class Module:
    module_id: str
    title: str
    status: str


@dataclass(frozen=True)
class RoadmapItem:
    name: str
    module_id: str
    status: str = STATUS_PENDING
    section_id: str | None = None


@dataclass(frozen=True)
class ReportSection:
    """Business grouping inside a module. Pending chips and future production values share a location."""

    section_id: str
    heading: str
    module_id: str = "enrollment"
    subtitle: str | None = None


SECTION_CURRENT_ENROLLMENT = "current_enrollment"
SECTION_ENROLLMENT_METRICS = "enrollment_metrics"
SECTION_ENROLLMENT_STATUS = "enrollment_status"
SECTION_CANCELLATION_OVERVIEW = "cancellation_overview"
SECTION_CANCELLATION_ACTIVITY = "cancellation_activity"
SECTION_PLAN_DISTRIBUTION = "plan_distribution"

REPORT_SECTIONS: tuple[ReportSection, ...] = (
    ReportSection(SECTION_CURRENT_ENROLLMENT, "Current Enrollment"),
    ReportSection(SECTION_ENROLLMENT_METRICS, "Enrollment Metrics"),
    ReportSection(SECTION_ENROLLMENT_STATUS, "Enrollment Status"),
    ReportSection(
        SECTION_CANCELLATION_OVERVIEW,
        "Cancellation Overview",
        subtitle="People-level cancellation populations — not interchangeable; not yet production-verified",
    ),
    ReportSection(
        SECTION_CANCELLATION_ACTIVITY,
        "Cancellation Activity",
        subtitle="Daily policy series — not the Currently Enrolled snapshot trend; not yet production-verified",
    ),
    ReportSection(SECTION_PLAN_DISTRIBUTION, "Plan Distribution"),
)


CURRENTLY_ENROLLED = MetricDefinition(
    metric_id=CURRENTLY_ENROLLED_ID,
    display_name=WIDGET_TITLE,
    category="Enrollment",
    unit="people",
    value_format="integer",
    source=SOURCE_LABEL,
    dashboard=VERIFIED_DASHBOARD_TITLE,
    plan_year=PLAN_YEAR,
    description="Count of enrolled people (enrollee.exchg_indiv_identifier) for the PY2027 Open "
                "Enrollment Health scope, as defined by the verified dashboard widget request.",
    history_enabled=True,
    status=STATUS_ACTIVE,
    module_id="enrollment",
    query_builder=build_currently_enrolled_query,
    section_id=SECTION_CURRENT_ENROLLMENT,
)

# Second measure of the verified Currently Enrolled widget. The widget's whole
# request definition (all scope filters and both measure definitions) is the
# verified payload, so the passive request is held to exactly that definition.
# Canonical value: the raw ratio returned by Sisense (e.g. 0.95 for 95%).
PERCENT_EFFECTUATED = MetricDefinition(
    metric_id="percent_effectuated",
    display_name="% Effectuated",
    category="Enrollment",
    unit="ratio",
    value_format="percent",
    source=SOURCE_LABEL,
    dashboard=VERIFIED_DASHBOARD_TITLE,
    plan_year=PLAN_YEAR,
    description="Effectuated share of enrolled people, as returned by the verified Currently Enrolled "
                "People widget's '% Effectuated' measure. Stored as the raw ratio; shown as a percentage.",
    history_enabled=True,
    status=STATUS_ACTIVE,
    module_id="enrollment",
    passive_source=PassiveSource(
        widget_id="6a98048ccef60720fe7f9b47",
        widget_title=WIDGET_TITLE,
        measure_title="% Effectuated",
        expected_definition=build_currently_enrolled_query,
    ),
    section_id=SECTION_ENROLLMENT_METRICS,
)

def _passive_production(metric_id: str, name: str, module_id: str, widget_id: str, widget_title: str,
                        measure_title: str | None, section_id: str | None) -> MetricDefinition:
    """Production passive metric. Fingerprint is bound at runtime from STABLE observation evidence."""
    bound = f"'{measure_title}' measure" if measure_title else "single measure"
    return MetricDefinition(
        metric_id=metric_id,
        display_name=name,
        category=module_id.title(),
        unit="people" if module_id == "enrollment" else "count",
        value_format="integer",
        source=SOURCE_LABEL,
        dashboard=VERIFIED_DASHBOARD_TITLE,
        plan_year=PLAN_YEAR,
        description=f"{bound} of the dashboard widget '{widget_title}'.",
        history_enabled=True,
        status=STATUS_ACTIVE,
        module_id=module_id,
        passive_source=PassiveSource(widget_id, widget_title, measure_title, approved_from_observations=True),
        section_id=section_id,
    )


APPLICATIONS_STARTED = _passive_production(
    "applications_started", "Applications Started", "applications",
    "6a98048ccef60720fe7f9b44", "Applications Started", None, None)
SUBMITTED_APPLICATIONS = _passive_production(
    "submitted_applications", "Submitted Applications", "applications",
    "6a98048ccef60720fe7f9b66", "Submitted Applications", None, None)
NEW_ENROLLED = _passive_production(
    "new_enrolled", "New", "enrollment",
    "6a98048ccef60720fe7f9b6e", "New/Active/Passive - Enrolled People", "NEW", SECTION_ENROLLMENT_STATUS)
ACTIVE_ENROLLED = _passive_production(
    "active_enrolled", "Active", "enrollment",
    "6a98048ccef60720fe7f9b6e", "New/Active/Passive - Enrolled People", "ACTIVE", SECTION_ENROLLMENT_STATUS)
PASSIVE_ENROLLED = _passive_production(
    "passive_enrolled", "Passive", "enrollment",
    "6a98048ccef60720fe7f9b6e", "New/Active/Passive - Enrolled People", "PASSIVE", SECTION_ENROLLMENT_STATUS)

METRICS: dict[str, MetricDefinition] = {m.metric_id: m for m in (
    CURRENTLY_ENROLLED, PERCENT_EFFECTUATED, APPLICATIONS_STARTED, SUBMITTED_APPLICATIONS,
    NEW_ENROLLED, ACTIVE_ENROLLED, PASSIVE_ENROLLED,
)}


def _candidate(metric_id: str, name: str, module_id: str, widget_id: str, widget_title: str,
               measure_title: str | None, section_id: str | None = None) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        display_name=name,
        category=module_id.title(),
        unit="people" if module_id == "enrollment" else "count",
        value_format="integer",
        source=SOURCE_LABEL,
        dashboard=VERIFIED_DASHBOARD_TITLE,
        plan_year=PLAN_YEAR,
        description=f"'{measure_title or 'single'}' measure of the dashboard widget '{widget_title}'.",
        history_enabled=True,
        status=STATUS_VALIDATION_ONLY,
        module_id=module_id,
        passive_source=PassiveSource(widget_id, widget_title, measure_title),
        section_id=section_id,
    )


# Remaining validation-only identities. Total Applicants stays here because the
# latest controlled run did not observe its widget. Do not promote from this
# dict without a STABLE/ELIGIBLE observation of that widget.
PROMOTION_CANDIDATES: dict[str, MetricDefinition] = {m.metric_id: m for m in (
    _candidate("total_applicants", "Total Applicants", "applications",
               "6a98048ccef60720fe7f9b4f", "Applicants On Submitted Applications", "Total Applicants"),
)}

MODULES: tuple[Module, ...] = (
    Module("overview", "Executive Overview", STATUS_ACTIVE),
    Module("enrollment", "Enrollment", STATUS_ACTIVE),
    Module("applications", "Applications", STATUS_ACTIVE),
    Module("issuers", "Issuers", STATUS_PENDING),
    Module("ede", "EDE", STATUS_PENDING),
    Module("financial_assistance", "Financial Assistance", STATUS_PENDING),
    Module("operations", "Operations", STATUS_PENDING),
    Module("dmi_notices", "DMI & Notices", STATUS_PENDING),
    Module("contact_center", "Contact Center", STATUS_PENDING),
    Module("agent_broker", "Agent / Broker", STATUS_PENDING),
)

# Roadmap only: names from the existing business dashboard. No payloads, no queries.
# Cancellation names are distinct business populations/series, not a generic "Cancelled".
ROADMAP: tuple[RoadmapItem, ...] = (
    RoadmapItem("Total Applicants", "applications"),
    RoadmapItem("Enrolled & Cancelled", "enrollment", section_id=SECTION_CANCELLATION_OVERVIEW),
    RoadmapItem("Cancelled — Not Enrolled", "enrollment", section_id=SECTION_CANCELLATION_OVERVIEW),
    RoadmapItem("Cancelled Policies — Daily", "enrollment", section_id=SECTION_CANCELLATION_ACTIVITY),
    RoadmapItem("Terminated Policies — Daily", "enrollment", section_id=SECTION_CANCELLATION_ACTIVITY),
    RoadmapItem("Metal Tier", "enrollment", section_id=SECTION_PLAN_DISTRIBUTION),
    RoadmapItem("FPL Level", "financial_assistance"),
    RoadmapItem("Financial Assistance", "financial_assistance"),
    RoadmapItem("Net Premium", "financial_assistance"),
    RoadmapItem("Issuer Distribution", "issuers"),
    RoadmapItem("EDE Application Count", "ede"),
    RoadmapItem("EDE Enrollees Count", "ede"),
    RoadmapItem("EDE % of Total", "ede"),
    RoadmapItem("EDE Previous Day & Today Applications", "ede"),
    RoadmapItem("EDE Previous Day & Today Enrollees", "ede"),
    RoadmapItem("EDE Partner Distribution", "ede"),
    RoadmapItem("DMI Aging", "dmi_notices"),
    RoadmapItem("Notices", "dmi_notices"),
    RoadmapItem("Contact Center", "contact_center"),
    RoadmapItem("Medicaid Account Transfers", "operations"),
    RoadmapItem("Agent/Broker Activity", "agent_broker"),
)


# Verification states. Only VERIFIED metrics (active, with a verified source)
# may produce snapshots or appear as values in the executive report. DISCOVERED
# is a runtime discovery result (structure only) and never an active metric.
STATE_VERIFIED = "VERIFIED"
STATE_DISCOVERED = "DISCOVERED"
STATE_PENDING = "PENDING"


@dataclass(frozen=True)
class DiscoveryTarget:
    """A business label to look for, exactly, in the dashboard's own JAQL requests."""

    metric_id: str
    label: str
    module_id: str


WAVE1_TARGETS: tuple[DiscoveryTarget, ...] = (
    DiscoveryTarget("applications_started", "Applications Started", "applications"),
    DiscoveryTarget("submitted_applications", "Submitted Applications", "applications"),
    DiscoveryTarget("total_applicants", "Total Applicants", "applications"),
    DiscoveryTarget(CURRENTLY_ENROLLED_ID, WIDGET_TITLE, "enrollment"),
    DiscoveryTarget("percent_effectuated", "% Effectuated", "enrollment"),
    DiscoveryTarget("new_enrolled", "New", "enrollment"),
    DiscoveryTarget("active_enrolled", "Active", "enrollment"),
    DiscoveryTarget("passive_enrolled", "Passive", "enrollment"),
    # Presentation placeholders only. Production HTML no longer uses these generic
    # labels; keep the ids so existing discovery/validation is not silently retargeted.
    DiscoveryTarget("cancelled", "Cancelled", "enrollment"),
    DiscoveryTarget("terminated", "Terminated", "enrollment"),
)


KIND_PEOPLE_AGGREGATE = "people_aggregate"
KIND_DAILY_POLICY_SERIES = "daily_policy_series"
GRAIN_DAYS = "days"


@dataclass(frozen=True)
class CancellationCandidate:
    """Discovery/validation identity only. No widget id, fingerprint, or payload until observed."""

    metric_id: str
    display_name: str
    discovery_label: str
    section_id: str
    kind: str
    source_table: str | None = None
    source_date_column: str | None = None


# Search labels are the source measure names to look for exactly in natural JAQL.
# Display names stay on the production roadmap. Widget ids are not invented here.
# Cancelled Policies' date identity is the column observed in natural dashboard traffic.
# Terminated Policies has no observed date column yet and is not copied from Cancelled.
CANCELLATION_CANDIDATES: dict[str, CancellationCandidate] = {c.metric_id: c for c in (
    CancellationCandidate("enrolled_cancelled", "Enrolled & Cancelled", "Enrolled & Cancelled",
                          SECTION_CANCELLATION_OVERVIEW, KIND_PEOPLE_AGGREGATE),
    CancellationCandidate("cancelled_not_enrolled", "Cancelled — Not Enrolled", "Cancelled - Not Enrolled",
                          SECTION_CANCELLATION_OVERVIEW, KIND_PEOPLE_AGGREGATE),
    CancellationCandidate("cancelled_policies_daily", "Cancelled Policies — Daily", "Cancelled Policies",
                          SECTION_CANCELLATION_ACTIVITY, KIND_DAILY_POLICY_SERIES,
                          source_table="enrollment", source_date_column="last_update_timestamp"),
    CancellationCandidate("terminated_policies_daily", "Terminated Policies — Daily", "Terminated Policies",
                          SECTION_CANCELLATION_ACTIVITY, KIND_DAILY_POLICY_SERIES),
)}

CANCELLATION_TARGETS: tuple[DiscoveryTarget, ...] = tuple(
    DiscoveryTarget(c.metric_id, c.discovery_label, "enrollment") for c in CANCELLATION_CANDIDATES.values()
)

DISCOVERY_TARGETS: tuple[DiscoveryTarget, ...] = WAVE1_TARGETS + CANCELLATION_TARGETS


VALUE_COUNT = "count"
VALUE_PERCENT = "percent"


@dataclass(frozen=True)
class RuntimeValidationTarget:
    """A Wave 1 metric whose runtime value may be read for UI validation (developer tooling)."""

    metric_id: str
    value_kind: str


# Cancelled and Terminated stay PENDING and are never extracted here.
RUNTIME_VALIDATION_TARGETS: tuple[RuntimeValidationTarget, ...] = (
    RuntimeValidationTarget("applications_started", VALUE_COUNT),
    RuntimeValidationTarget("submitted_applications", VALUE_COUNT),
    RuntimeValidationTarget("total_applicants", VALUE_COUNT),
    RuntimeValidationTarget("percent_effectuated", VALUE_PERCENT),
    RuntimeValidationTarget("new_enrolled", VALUE_COUNT),
    RuntimeValidationTarget("active_enrolled", VALUE_COUNT),
    RuntimeValidationTarget("passive_enrolled", VALUE_COUNT),
)

# Cancellation/termination: extracted for developer review only. Never production.
CANCELLATION_VALIDATION_TARGETS: tuple[RuntimeValidationTarget, ...] = tuple(
    RuntimeValidationTarget(c.metric_id, VALUE_COUNT) for c in CANCELLATION_CANDIDATES.values()
)


def catalog_state(metric_id: str) -> str:
    """Static state: VERIFIED for active catalogued metrics, otherwise PENDING until discovered at runtime."""
    metric = METRICS.get(metric_id)
    return STATE_VERIFIED if metric is not None and metric.status == STATUS_ACTIVE else STATE_PENDING


def get_metric(metric_id: str) -> MetricDefinition:
    try:
        return METRICS[metric_id]
    except KeyError:
        raise KeyError(f"Metric {metric_id!r} is not in the catalog") from None


def active_metrics() -> list[MetricDefinition]:
    return [m for m in METRICS.values() if m.status == STATUS_ACTIVE]


def passive_metrics() -> list[MetricDefinition]:
    """Production metrics read from the dashboard's own responses (no application request)."""
    return [m for m in active_metrics() if m.passive_source is not None]


def roadmap_for(module_id: str) -> list[RoadmapItem]:
    return [item for item in ROADMAP if item.module_id == module_id]


def report_sections(module_id: str) -> tuple[ReportSection, ...]:
    return tuple(section for section in REPORT_SECTIONS if section.module_id == module_id)
