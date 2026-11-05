"""Metric catalog: descriptive metadata only.

Active metrics reference their verified payload builder; payload contents are
never stored here. Roadmap items are names from the business dashboard and
stay inactive (no builder, no values) until their exact requests are verified.
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
PENDING_MESSAGE = "Metric integration pending verification."


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

    def __post_init__(self) -> None:
        if self.status == STATUS_ACTIVE and self.query_builder is None:
            raise ValueError(f"Active metric {self.metric_id!r} must reference a verified payload builder")
        if self.status != STATUS_ACTIVE and self.query_builder is not None:
            raise ValueError(f"Non-active metric {self.metric_id!r} must not reference a payload builder")
        if self.value_format not in {"integer", "decimal", "percent"}:
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
)

METRICS: dict[str, MetricDefinition] = {CURRENTLY_ENROLLED.metric_id: CURRENTLY_ENROLLED}

MODULES: tuple[Module, ...] = (
    Module("overview", "Executive Overview", STATUS_ACTIVE),
    Module("enrollment", "Enrollment", STATUS_ACTIVE),
    Module("applications", "Applications", STATUS_PENDING),
    Module("issuers", "Issuers", STATUS_PENDING),
    Module("ede", "EDE", STATUS_PENDING),
    Module("financial_assistance", "Financial Assistance", STATUS_PENDING),
    Module("operations", "Operations", STATUS_PENDING),
    Module("dmi_notices", "DMI & Notices", STATUS_PENDING),
    Module("contact_center", "Contact Center", STATUS_PENDING),
    Module("agent_broker", "Agent / Broker", STATUS_PENDING),
)

# Roadmap only: names from the existing business dashboard. No payloads, no queries.
ROADMAP: tuple[RoadmapItem, ...] = (
    RoadmapItem("Applications Started", "applications"),
    RoadmapItem("Submitted Applications", "applications"),
    RoadmapItem("Total Applicants", "applications"),
    RoadmapItem("% Effectuated", "enrollment"),
    RoadmapItem("New", "enrollment"),
    RoadmapItem("Active", "enrollment"),
    RoadmapItem("Passive", "enrollment"),
    RoadmapItem("Cancelled", "enrollment"),
    RoadmapItem("Terminated", "enrollment"),
    RoadmapItem("Metal Tier", "enrollment"),
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


# Verification states. Only VERIFIED metrics (active, with a verified payload
# builder) may produce snapshots or appear in the executive report. DISCOVERED
# is a runtime discovery result (structure only) and never a MetricDefinition.
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
    DiscoveryTarget("pct_effectuated", "% Effectuated", "enrollment"),
    DiscoveryTarget("new", "New", "enrollment"),
    DiscoveryTarget("active", "Active", "enrollment"),
    DiscoveryTarget("passive", "Passive", "enrollment"),
    DiscoveryTarget("cancelled", "Cancelled", "enrollment"),
    DiscoveryTarget("terminated", "Terminated", "enrollment"),
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


def roadmap_for(module_id: str) -> list[RoadmapItem]:
    return [item for item in ROADMAP if item.module_id == module_id]
