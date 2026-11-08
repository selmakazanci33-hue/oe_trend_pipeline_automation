"""Semantic gates for PY2026 vs PY2027 comparison rows.

COMPARABLE is never assigned unless every required check passes.
NULL/N/A is never coerced to zero. Percent is not computed for an invalid comparison.
"""

from __future__ import annotations

from decimal import Decimal

from comparison.currently_enrolled import percent_change
from comparison.models import (
    COMPARABLE,
    DIAGNOSTIC,
    FAMILY_ACTIVITY,
    FAMILY_POPULATION,
    GRAIN_PERSON,
    GRAIN_POLICY,
    IDENTIFIER_EXACT,
    NOT_COMPARABLE,
    ComparisonRow,
    MetricSpec,
    SemanticChecks,
    default_identifier_requirement,
)


class ComparisonValidationError(ValueError):
    """A comparison rule was violated."""


def require_comparable(checks: SemanticChecks) -> None:
    if not checks.comparable_ready():
        raise ComparisonValidationError(
            "COMPARABLE requires identifier equivalence, matching grain and family, "
            "matching date semantics, verified definitions on both sources, proven filter "
            "mapping, both values present, a verified Pending definition when Pending is "
            "the metric, people/policy not mixed, snapshots not summed, and no invented PY2026 SQL."
        )


def assign_status(spec: MetricSpec, checks: SemanticChecks) -> str:
    """COMPARABLE only when every semantic check passes. Otherwise keep the spec default."""
    if spec.pending_verified_definition is False:
        return NOT_COMPARABLE
    if checks.comparable_ready():
        return COMPARABLE
    if spec.default_status == DIAGNOSTIC:
        return DIAGNOSTIC
    return NOT_COMPARABLE


def differences(status: str, py2026: int | None, py2027: int | None) -> dict:
    """Percent only for COMPARABLE rows with both values. Missing values stay missing."""
    if py2026 is None or py2027 is None:
        return {
            "absolute_difference": None,
            "percent_difference": None,
            "percent_status": "NOT_COMPUTED",
        }
    if status != COMPARABLE:
        absolute = None if status == NOT_COMPARABLE else (py2027 - py2026)
        return {
            "absolute_difference": absolute,
            "percent_difference": None,
            "percent_status": "NOT_COMPUTED",
        }
    computed = percent_change(py2026, py2027)
    return {
        "absolute_difference": computed["absolute_difference"],
        "percent_difference": computed["percent_change"],
        "percent_status": computed["percent_status"],
    }


def refuse_null_as_zero(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ComparisonValidationError("Boolean is not a metric value")
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


def assert_grains_compatible(left: MetricSpec, right: MetricSpec) -> None:
    if left.grain == GRAIN_PERSON and right.grain == GRAIN_POLICY:
        raise ComparisonValidationError(
            "People metrics cannot be combined with policy metrics."
        )
    if left.grain == GRAIN_POLICY and right.grain == GRAIN_PERSON:
        raise ComparisonValidationError(
            "People metrics cannot be combined with policy metrics."
        )
    if left.metric_family == FAMILY_POPULATION and right.metric_family == FAMILY_ACTIVITY:
        raise ComparisonValidationError(
            "Population snapshots cannot be combined with activity events."
        )
    if left.metric_family == FAMILY_ACTIVITY and right.metric_family == FAMILY_POPULATION:
        raise ComparisonValidationError(
            "Population snapshots cannot be combined with activity events."
        )


def assert_pending_unavailable(spec: MetricSpec) -> None:
    if spec.metric_id == "pending_people" and spec.pending_verified_definition is False:
        return
    if spec.metric_id == "pending_people":
        raise ComparisonValidationError("Pending People has no verified definition")


def identifier_status(proven: bool) -> str:
    requirement = default_identifier_requirement()
    return IDENTIFIER_EXACT if proven else requirement.status


def build_row(
    spec: MetricSpec,
    *,
    py2026_value: int | None,
    py2027_value: int | None,
    checks: SemanticChecks,
    as_of_date_py2026: str | None = None,
    as_of_date_py2027: str | None = None,
    reason: str | None = None,
) -> ComparisonRow:
    py2026 = refuse_null_as_zero(py2026_value)
    py2027 = refuse_null_as_zero(py2027_value)
    populated = checks
    if py2026 is None or py2027 is None:
        populated = SemanticChecks(
            **{**checks.as_dict(), "both_values_present": False}
        )
    if spec.pending_verified_definition is False:
        populated = SemanticChecks(
            **{**populated.as_dict(), "pending_has_verified_definition": False}
        )
    status = assign_status(spec, populated)
    if status == COMPARABLE:
        require_comparable(populated)
    delta = differences(status, py2026, py2027)
    pending_reason = spec.reason
    if spec.metric_id == "pending_people":
        pending_reason = spec.reason
        assert_pending_unavailable(spec)
    return ComparisonRow(
        metric_id=spec.metric_id,
        metric_name=spec.metric_name,
        metric_family=spec.metric_family,
        py2026_value=py2026,
        py2027_value=py2027,
        absolute_difference=delta["absolute_difference"],
        percent_difference=delta["percent_difference"],
        comparison_status=status,
        grain=spec.grain,
        date_semantics=spec.date_semantics,
        py2026_source=spec.py2026_source,
        py2027_source=spec.py2027_source,
        reason=reason or pending_reason,
        as_of_date_py2026=as_of_date_py2026,
        as_of_date_py2027=as_of_date_py2027,
        section=spec.section,
        aggregation_method=spec.aggregation_method,
        identifier_status=spec.identifier_status,
        percent_status=delta["percent_status"],
    )
