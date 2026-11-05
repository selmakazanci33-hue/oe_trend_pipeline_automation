"""Promotion evidence for validation-only candidates (developer tooling; never promotes).

Built from runtime validation rows: safe structural metadata, the extracted
value (for human comparison with the Sisense UI) and the request-definition
fingerprint. Observations persisted for fingerprint stability checks keep only
identities, exact measure titles and fingerprints: no values, no raw request
or response content, no headers. Nothing here changes the catalog; promotion
stays a human code change.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from metrics.catalog import PROMOTION_CANDIDATES
from metrics.runtime_validation import BINDING_MEASURE_TITLE, RV_VALUE_EXTRACTED, RuntimeValue

logger = logging.getLogger(__name__)

OBSERVATIONS_FILE = "wave1_fingerprint_observations.json"
MAX_OBSERVATIONS = 20
SCHEMA_VERSION = 1

FP_STABLE = "STABLE"
FP_CHANGED = "CHANGED"
FP_MISSING = "MISSING"
FP_NO_BASELINE = "NO BASELINE"


@dataclass(frozen=True)
class CandidateEvidence:
    metric_id: str
    label: str
    widget_id: str | None
    widget_title: str | None
    datasource: str | None
    measure_titles: tuple[str, ...]
    bound_measure_title: str | None
    binding: str | None
    fingerprint: str | None
    extraction_status: str
    value: int | float | None
    http_status: int | None
    observed_at_utc: str
    eligible: bool
    eligibility_reason: str | None

    @property
    def measure_count(self) -> int:
        return len(self.measure_titles)


def _eligibility(row: RuntimeValue) -> str | None:
    """None when the observed definition is structurally eligible for future (human) promotion."""
    expected = PROMOTION_CANDIDATES[row.metric_id].passive_source
    if row.status != RV_VALUE_EXTRACTED:
        return f"Extraction status {row.status}"
    if row.http_status != 200:
        return f"HTTP {row.http_status}"
    if row.widget_id != expected.widget_id:
        return "Observed widget differs from the catalog candidate widget"
    if not row.definition_fingerprint:
        return "No definition fingerprint"
    if not row.measure_titles:
        return "Exact measure titles not observed"
    if not row.bound_measure_title or row.measure_titles.count(row.bound_measure_title) != 1:
        return "Value not bound to exactly one exact measure title"
    return None


def candidate_evidence(rows: tuple[RuntimeValue, ...], observed_at_utc: str) -> tuple[CandidateEvidence, ...]:
    by_id = {r.metric_id: r for r in rows}
    evidence = []
    for metric_id, candidate in PROMOTION_CANDIDATES.items():
        row = by_id.get(metric_id)
        if row is None:
            evidence.append(CandidateEvidence(metric_id, candidate.display_name, None, None, None, (), None, None,
                                              None, "NOT OBSERVED", None, None, observed_at_utc, False,
                                              "Not in runtime validation scope"))
            continue
        reason = _eligibility(row)
        evidence.append(CandidateEvidence(
            metric_id, row.label, row.widget_id, row.widget_title, row.datasource, row.measure_titles,
            row.bound_measure_title, row.binding, row.definition_fingerprint, row.status, row.value,
            row.http_status, observed_at_utc, reason is None, reason,
        ))
    return tuple(evidence)


@dataclass(frozen=True)
class SharedWidgetCheck:
    widget_id: str
    metric_ids: tuple[str, ...]
    same_fingerprint: bool
    distinct_measures: bool
    exact_binding: bool
    all_eligible: bool

    @property
    def eligible_together(self) -> bool:
        return self.same_fingerprint and self.distinct_measures and self.exact_binding and self.all_eligible


def shared_widget_checks(evidence: tuple[CandidateEvidence, ...]) -> tuple[SharedWidgetCheck, ...]:
    """Candidates the catalog places on one widget must share one definition and bind distinct exact measures."""
    groups: dict[str, list[str]] = {}
    for metric_id, candidate in PROMOTION_CANDIDATES.items():
        groups.setdefault(candidate.passive_source.widget_id, []).append(metric_id)
    by_id = {e.metric_id: e for e in evidence}
    checks = []
    for widget_id, metric_ids in groups.items():
        if len(metric_ids) < 2:
            continue
        items = [by_id[m] for m in metric_ids if m in by_id]
        prints = {e.fingerprint for e in items}
        titles = [e.bound_measure_title for e in items]
        checks.append(SharedWidgetCheck(
            widget_id=widget_id,
            metric_ids=tuple(metric_ids),
            same_fingerprint=len(items) == len(metric_ids) and len(prints) == 1 and None not in prints,
            distinct_measures=None not in titles and len(set(titles)) == len(titles),
            exact_binding=all(e.binding == BINDING_MEASURE_TITLE and e.widget_id == widget_id for e in items),
            all_eligible=all(e.eligible for e in items),
        ))
    return tuple(checks)


def observation_record(evidence: tuple[CandidateEvidence, ...], observed_at_utc: str) -> dict[str, Any]:
    """Safe, value-free record of one observation (identities, exact measure titles, fingerprints)."""
    return {
        "observed_at_utc": observed_at_utc,
        "candidates": {
            e.metric_id: {
                "widget_id": e.widget_id,
                "measure_titles": list(e.measure_titles),
                "bound_measure_title": e.bound_measure_title,
                "fingerprint": e.fingerprint,
                "extraction_status": e.extraction_status,
            }
            for e in evidence
        },
    }


@dataclass(frozen=True)
class FingerprintComparison:
    baseline_observed_at_utc: str | None
    results: dict[str, str]


def compare_observation(
    baseline: dict[str, Any] | None, evidence: tuple[CandidateEvidence, ...]
) -> FingerprintComparison:
    """Observation A (baseline, previous run) vs Observation B (this run). Never approves anything."""
    previous = (baseline or {}).get("candidates", {})
    results = {}
    for e in evidence:
        before = (previous.get(e.metric_id) or {}).get("fingerprint")
        if e.fingerprint is None:
            results[e.metric_id] = FP_MISSING
        elif before is None:
            results[e.metric_id] = FP_NO_BASELINE
        else:
            results[e.metric_id] = FP_STABLE if before == e.fingerprint else FP_CHANGED
    return FingerprintComparison((baseline or {}).get("observed_at_utc"), results)


class ObservationLogError(Exception):
    pass


def load_observations(path: Path) -> list[dict[str, Any]]:
    """Previous observations; raises ObservationLogError if the file exists but is not a valid log."""
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ObservationLogError(f"Unreadable observation log ({type(exc).__name__})") from None
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(document.get("observations"), list):
        raise ObservationLogError("Observation log has an unexpected format")
    return [o for o in document["observations"] if isinstance(o, dict)]


def append_observation(path: Path, observations: list[dict[str, Any]], record: dict[str, Any]) -> None:
    kept = (observations + [record])[-MAX_OBSERVATIONS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": SCHEMA_VERSION, "observations": kept}, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class EvidenceOutcome:
    evidence: tuple[CandidateEvidence, ...]
    shared: tuple[SharedWidgetCheck, ...]
    comparison: FingerprintComparison
    observation_recorded: bool
    observation_note: str | None


def collect_evidence(
    rows: tuple[RuntimeValue, ...], observed_at_utc: str, log_path: Path | None, control_passed: bool
) -> EvidenceOutcome:
    """Compare with the previous observation and record this one (only when the control passed)."""
    evidence = candidate_evidence(rows, observed_at_utc)
    shared = shared_widget_checks(evidence)
    if log_path is None:
        return EvidenceOutcome(evidence, shared, compare_observation(None, evidence), False,
                               "No history directory configured")
    try:
        observations = load_observations(log_path)
    except ObservationLogError as exc:
        return EvidenceOutcome(evidence, shared, compare_observation(None, evidence), False,
                               f"{exc}; left untouched")
    comparison = compare_observation(observations[-1] if observations else None, evidence)
    if not control_passed:
        return EvidenceOutcome(evidence, shared, comparison, False,
                               "Not recorded: Currently Enrolled control did not pass")
    append_observation(log_path, observations, observation_record(evidence, observed_at_utc))
    return EvidenceOutcome(evidence, shared, comparison, True, None)
