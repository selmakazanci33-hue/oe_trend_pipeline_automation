"""Phase 1: retrieve the live PY2027 "Currently Enrolled People" metric from Sisense."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from config.settings import (
    SOURCE_LABEL,
    VERIFIED_DATASOURCE,
    VERIFIED_JAQL_URL,
    ConfigurationError,
    MissingVerifiedInformation,
    Settings,
    load_settings,
)
from sisense.auth import AuthenticatedSession, SisenseAuthenticator
from sisense.client import SisenseJaqlClient
from sisense.diagnostics import BrowserJaqlObserver, log_cookie_diagnostics
from sisense.models import (
    JaqlQuery,
    MetricResult,
    RequestMetadataUnavailable,
    SisenseError,
    VerifiedJaqlPayloadRequired,
)
from sisense.passive import (
    ExpectedWidgetQuery,
    PassiveComparison,
    PassiveObservation,
    PassiveWidgetObserver,
    comparison_lines,
    observe_passive_value,
)
from sisense.payloads.currently_enrolled import build_currently_enrolled_query
from sisense.request_metadata import (
    ForwardedHeaders,
    csrf_header_matches_context_cookie,
    headers_from_observed_request,
)
from metrics.catalog import (
    CURRENTLY_ENROLLED,
    STATE_DISCOVERED,
    STATE_PENDING,
    STATE_VERIFIED,
    DISCOVERY_TARGETS,
    passive_metrics,
)
from metrics.cancellation_identification import identify_cancellation_sources
from metrics.discovery import resolve_targets
from metrics.fingerprint_evidence import OBSERVATIONS_FILE, collect_evidence, production_passive_metrics
from metrics.passive_metrics import (
    MAX_CANDIDATES_PER_WIDGET,
    PassiveMetricOutcome,
    SourceWidgetObserver,
    apply_control,
    extract_passive_metric,
)
from metrics.runtime_validation import (
    RV_NOT_AVAILABLE,
    RV_PENDING,
    RV_SERIES_EXTRACTED,
    RV_VALIDATION_FAILED,
    RV_VALUE_EXTRACTED,
    ControlComparison,
    RuntimeValue,
    passive_control_value,
    validate_runtime_values,
)
from reporting.discovery_report import write_discovery_report
from reporting.validation_report import format_runtime_value, write_validation_report
from reporting.formatting import fmt_comparison, fmt_int, fmt_metric_value, fmt_time
from sisense.discovery import DiscoveryObserver, evaluate_discovery
from reporting.pipeline import PassiveRunValue, ReportingPipeline, ReportingSummary
from sisense.retry import RetryPolicy
from validation.validators import extract_count_measure_value

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_MISSING_VERIFIED_INFO = 2

RULE = "=" * 60
DISCOVERY_QUIET_MS = 5000
DISCOVERY_MAX_EVENTS = 500
logger = logging.getLogger("oe_trend_pipeline")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def print_report(result: MetricResult, baseline: int, widget_title: str) -> None:
    rows = [
        ("Authentication", "PASS"),
        ("Dashboard", result.dashboard),
        ("Datasource", VERIFIED_DATASOURCE["title"]),
        ("Metric", widget_title),
        ("Coverage Year", str(result.plan_year)),
        ("Current Value", f"{result.value:,}"),
        ("Manual Baseline", f"{baseline:,}"),
        ("Retrieved", result.retrieved_at),
        ("JAQL Status", "PASS"),
    ]
    print()
    print(RULE)
    print("Georgia Access — PY2027 Sisense Retrieval")
    print(RULE)
    print()
    for label, value in rows:
        print(f"{label:<22}{value}")
    print()
    print(RULE)


def wait_for_dashboard_jaql(session: AuthenticatedSession, observer: BrowserJaqlObserver, timeout: int) -> None:
    """Wait passively until the dashboard's own JAQL POST has finished at least once."""
    if observer.finished_count:
        return
    try:
        session.context.wait_for_event(
            "requestfinished",
            predicate=lambda r: r.method == "POST" and r.url == VERIFIED_JAQL_URL,
            timeout=timeout,
        )
    except PlaywrightTimeoutError:
        pass


def obtain_request_metadata(
    session: AuthenticatedSession, observer: BrowserJaqlObserver, settings: Settings
) -> ForwardedHeaders:
    wait_for_dashboard_jaql(session, observer, settings.timeouts.slow)
    forwarded = headers_from_observed_request(observer)
    if not csrf_header_matches_context_cookie(forwarded, session.context):
        raise RequestMetadataUnavailable(
            "Observed CSRF header does not match the current BrowserContext cookie; "
            "refusing to send the application JAQL request"
        )
    logger.info("Request metadata taken from observed dashboard JAQL request: %s", forwarded.names())
    logger.info("CSRF header matches current BrowserContext cookie: True")
    return forwarded


def retrieve_value(
    session: AuthenticatedSession,
    settings: Settings,
    query: JaqlQuery,
    request_metadata: ForwardedHeaders | None = None,
) -> int:
    """Send the verified JAQL through the same BrowserContext that authenticated."""
    client = SisenseJaqlClient(
        session.context,
        timeouts=settings.timeouts,
        retry_policy=RetryPolicy.from_settings(settings.retry),
        request_metadata=request_metadata,
    )
    response = client.execute(query)
    return extract_count_measure_value(response.body, query.body, query.primary_measure_title)


def run_session_diagnostics(
    session: AuthenticatedSession, observer: BrowserJaqlObserver | None, settings: Settings
) -> None:
    logger.info("[debug] Authenticated BrowserContext ready (reused saved state: %s)",
                session.reused_saved_state)
    if observer is not None:
        wait_for_dashboard_jaql(session, observer, settings.timeouts.default)
        observer.log_summary()
    log_cookie_diagnostics(session.context)


def main() -> int:
    configure_logging()

    try:
        settings = load_settings()
        query = build_currently_enrolled_query()
    except VerifiedJaqlPayloadRequired as exc:
        logger.error("STOP: %s", exc)
        return EXIT_MISSING_VERIFIED_INFO
    except MissingVerifiedInformation as exc:
        logger.error("STOP: %s", exc)
        return EXIT_MISSING_VERIFIED_INFO
    except (ConfigurationError, SisenseError) as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_FAILURE

    run_id = uuid.uuid4().hex
    reporting = create_reporting(settings)
    if reporting is not None:
        try:
            reporting.begin_run(run_id)
        except Exception as exc:
            logger.error("Snapshot history unavailable (%s); Sisense was not contacted", type(exc).__name__)
            return EXIT_FAILURE

    production_passive = production_passive_metrics(
        None if settings.history_db_path is None else settings.history_db_path.parent / OBSERVATIONS_FILE
    )
    passive_observer = create_passive_observer(settings, query)
    passive_observation = None
    discovery_observer = create_discovery_observer(settings, query)
    discovery_outcome = None
    source_observer = create_source_observer(settings, production_passive)
    passive_outcomes: tuple[PassiveMetricOutcome, ...] = ()
    passive_retrieved_at = None
    try:
        with sync_playwright() as playwright:
            authenticator = SisenseAuthenticator(
                playwright, settings,
                extra_observers=tuple(o for o in (passive_observer, discovery_observer, source_observer)
                                      if o is not None),
            )
            session = authenticator.open_session()
            try:
                if settings.diagnostics_only:
                    run_session_diagnostics(session, authenticator.jaql_observer, settings)
                    logger.info("Diagnostics-only mode: no JAQL request was sent by this application")
                    return EXIT_OK
                if settings.debug_session:
                    run_session_diagnostics(session, authenticator.jaql_observer, settings)
                request_metadata = obtain_request_metadata(session, authenticator.jaql_observer, settings)
                if discovery_observer is not None:
                    discovery_outcome = run_discovery(session, discovery_observer, query, settings)
                if passive_observer is not None:
                    passive_observation = run_passive_observation(session, passive_observer, settings.timeouts.slow)
                if source_observer is not None:
                    passive_outcomes = read_passive_metrics(
                        session, source_observer, settings.timeouts.slow, production_passive
                    )
                    passive_retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                value = retrieve_value(session, settings, query, request_metadata)
            finally:
                session.close()
    except MissingVerifiedInformation as exc:
        logger.error("STOP: %s", exc)
        report_failure(reporting, run_id, exc, production_passive)
        finish_runtime_validation(discovery_outcome, settings, None)
        return EXIT_MISSING_VERIFIED_INFO
    except (SisenseError, PlaywrightError) as exc:
        logger.error("Retrieval failed: %s: %s", type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "")
        report_failure(reporting, run_id, exc, production_passive)
        finish_runtime_validation(discovery_outcome, settings, None)
        return EXIT_FAILURE

    result = MetricResult(
        metric=query.metric,
        plan_year=query.plan_year,
        value=value,
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=SOURCE_LABEL,
        dashboard=settings.dashboard_title,
    )

    logger.info("Current PY%d value: %d", result.plan_year, result.value)
    logger.info("Manual baseline: %d", settings.manual_baseline)
    logger.info(
        "Difference: %d - %d = %+d",
        result.value, settings.manual_baseline, result.value - settings.manual_baseline,
    )
    logger.info("Result: %s", json.dumps(result.to_dict()))

    print_report(result, settings.manual_baseline, query.widget_title)
    if passive_observation is not None:
        print_passive_comparison(PassiveComparison(passive_observation, result.value))
    if discovery_outcome is not None:
        print_discovery_summary(discovery_outcome)
    finish_runtime_validation(discovery_outcome, settings, result.value)

    passive_values = passive_run_values(passive_outcomes, result, passive_retrieved_at, production_passive)
    if reporting is None:
        print_passive_metrics(passive_values, None)
        return EXIT_OK
    try:
        summary = reporting.complete_success(run_id, result, passive_values)
    except Exception as exc:
        logger.error("Snapshot/report step failed: %s", type(exc).__name__)
        return EXIT_FAILURE
    print_reporting_summary(summary, result)
    print_passive_metrics(passive_values, summary)
    return EXIT_OK


def create_source_observer(settings: Settings, metrics) -> SourceWidgetObserver | None:
    """Production passive sources: always on for normal runs; never in diagnostics mode."""
    if settings.diagnostics_only or not metrics:
        return None
    return SourceWidgetObserver(m.passive_source.widget_id for m in metrics if m.passive_source)


def read_passive_metrics(
    session: AuthenticatedSession, observer: SourceWidgetObserver, timeout: int, metrics
) -> tuple[PassiveMetricOutcome, ...]:
    """Read verified passive metrics from already-returned responses; never raises, never sends a request."""
    try:
        deadline = time.monotonic() + timeout / 1000
        for _ in range(len(observer.widget_ids) * MAX_CANDIDATES_PER_WIDGET):
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if not observer.missing() or remaining_ms <= 0:
                break
            try:
                session.context.wait_for_event(
                    "requestfinished", predicate=observer.is_source_request, timeout=remaining_ms
                )
            except PlaywrightTimeoutError:
                break
    except Exception as exc:
        logger.error("Passive source wait failed: %s", type(exc).__name__)
    observer.stop()
    outcomes = tuple(extract_passive_metric(m, observer) for m in metrics)
    logger.info("Passive production metrics: %s",
                ", ".join(f"{o.metric_id} {o.status}" for o in outcomes))
    return outcomes


def passive_run_values(
    outcomes: tuple[PassiveMetricOutcome, ...], result: MetricResult, retrieved_at: str | None,
    metrics,
) -> tuple[PassiveRunValue, ...]:
    """Only values that passed every check and the Currently Enrolled canary become results."""
    values = []
    by_id = {o.metric_id: o for o in outcomes}
    for metric in metrics:
        outcome = by_id.get(metric.metric_id)
        if outcome is None:
            values.append(PassiveRunValue(metric, None, "Passive source not read in this run"))
            continue
        outcome = apply_control(outcome, result.value)
        if not outcome.succeeded or retrieved_at is None:
            values.append(PassiveRunValue(metric, None, outcome.failure or "Value not available"))
            continue
        values.append(PassiveRunValue(metric, MetricResult(
            metric=metric.metric_id,
            plan_year=metric.plan_year,
            value=outcome.value,
            retrieved_at=retrieved_at,
            source=metric.source,
            dashboard=metric.dashboard,
        )))
    return tuple(values)


def print_passive_metrics(values: tuple[PassiveRunValue, ...], summary: ReportingSummary | None) -> None:
    if not values:
        return
    panels = {p.metric.metric_id: p for p in summary.panels} if summary is not None else {}
    print("Verified dashboard metrics (passive, zero additional requests)")
    for item in values:
        metric = item.metric
        panel = panels.get(metric.metric_id)
        if item.result is not None:
            shown = fmt_metric_value(item.result.value, metric.value_format)
            status = "LIVE"
        else:
            shown = item.failure
            status = panel.data_status if panel is not None else "NOT AVAILABLE"
        snapshot = ""
        if panel is not None and panel.snapshot_write_status:
            snapshot = f"  snapshot {panel.snapshot_write_status}"
        print(f"  {metric.display_name:<28}{status:<15}{shown}{snapshot}")
    print()
    print(RULE)


def create_passive_observer(settings: Settings, query: JaqlQuery) -> PassiveWidgetObserver | None:
    if not settings.passive_proof or settings.diagnostics_only:
        return None
    try:
        return PassiveWidgetObserver(ExpectedWidgetQuery.from_verified_query(query))
    except ValueError as exc:
        logger.error("Passive proof disabled: %s", exc)
        return None


def run_passive_observation(
    session: AuthenticatedSession, observer: PassiveWidgetObserver, timeout: int
) -> PassiveObservation:
    """Validation only: never raises, never sends a request, never affects the application value."""
    try:
        if not observer.candidates():
            try:
                session.context.wait_for_event(
                    "requestfinished", predicate=observer.is_widget_request, timeout=timeout
                )
            except PlaywrightTimeoutError:
                pass
        observer.stop()
        observation = observe_passive_value(observer)
    except Exception as exc:
        observer.stop()
        observation = PassiveObservation(
            observer.widget_requests_seen, None, None, False, None, f"Passive observation error ({type(exc).__name__})"
        )
    logger.info(
        "Passive proof: %d dashboard JAQL POST(s) seen, %d for the verified widget; parse %s",
        observer.target_requests_seen, observer.widget_requests_seen, "PASS" if observation.parsed else "FAIL",
    )
    return observation


def print_passive_comparison(comparison: PassiveComparison) -> None:
    """Validation output only; the application value stays authoritative."""
    print()
    print("Passive JAQL proof (validation only — application value is authoritative)")
    for line in comparison_lines(comparison):
        print(line)
    print()
    print(RULE)
    logger.info("Passive Validation: %s", comparison.status)


def create_discovery_observer(settings: Settings, query: JaqlQuery) -> DiscoveryObserver | None:
    if not (settings.discovery or settings.runtime_validation) or settings.diagnostics_only:
        return None
    try:
        verified_widget_id = ExpectedWidgetQuery.from_verified_query(query).widget_id
    except ValueError:
        verified_widget_id = None
    return DiscoveryObserver(
        dashboard_id=settings.dashboard_id,
        datasource_id=VERIFIED_DATASOURCE["id"],
        target_labels=[target.label for target in DISCOVERY_TARGETS],
        verified_widget_id=verified_widget_id,
    )


def wait_for_dashboard_quiet(session: AuthenticatedSession, observer: DiscoveryObserver, timeouts) -> None:
    """Passively wait until no dashboard JAQL POST completes for a quiet period (bounded)."""
    quiet_ms = min(DISCOVERY_QUIET_MS, timeouts.default)
    deadline = time.monotonic() + timeouts.very_slow / 1000
    for _ in range(DISCOVERY_MAX_EVENTS):
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return
        try:
            session.context.wait_for_event(
                "requestfinished", predicate=observer.is_target, timeout=min(quiet_ms, remaining_ms)
            )
        except PlaywrightTimeoutError:
            return


@dataclass(frozen=True)
class RuntimeCapture:
    """Phase 3D values held in memory until the application control value is known."""

    rows: tuple[RuntimeValue, ...]
    passive_control_value: int | None
    control_reason: str | None


@dataclass(frozen=True)
class DiscoveryOutcome:
    report_path: Path | None
    counts: dict[str, int]
    runtime: RuntimeCapture | None = None
    identification: tuple = ()


def run_discovery(
    session: AuthenticatedSession, observer: DiscoveryObserver, query: JaqlQuery, settings: Settings
) -> DiscoveryOutcome | None:
    """Discovery only: never raises, never sends a request, never touches history or the executive report."""
    try:
        wait_for_dashboard_quiet(session, observer, settings.timeouts)
        observer.stop()
        try:
            expected = ExpectedWidgetQuery.from_verified_query(query)
        except ValueError:
            expected = None
        result = evaluate_discovery(observer, expected)
        metrics = resolve_targets(result)
        counts = {state: sum(m.state == state for m in metrics)
                  for state in (STATE_VERIFIED, STATE_DISCOVERED, STATE_PENDING)}
        logger.info(
            "Wave 1 discovery: %d dashboard JAQL POST(s), %d widget(s); %s",
            result.target_requests_seen, len(result.widgets),
            ", ".join(f"{state} {count}" for state, count in counts.items()),
        )
        path = None
        if settings.reports_dir is not None:
            path = write_discovery_report(metrics, result, settings.reports_dir, datetime.now(timezone.utc))
            logger.info("Wave 1 discovery report: %s", path)
        runtime = capture_runtime_values(observer, metrics, expected) if settings.runtime_validation else None
        identification = identify_cancellation_sources(result)
        if identification:
            logger.info(
                "Cancellation source identification: %s",
                ", ".join(f"{c.semantic_role}/{c.confidence} ({c.widget_id})" for c in identification),
            )
        return DiscoveryOutcome(path, counts, runtime, identification)
    except Exception as exc:
        observer.stop()
        logger.error("Wave 1 discovery failed: %s", type(exc).__name__)
        return None


def capture_runtime_values(
    observer: DiscoveryObserver, metrics, expected: ExpectedWidgetQuery | None
) -> RuntimeCapture | None:
    """Phase 3D: read DISCOVERED values from already-returned responses (validation only, never raises)."""
    try:
        rows = validate_runtime_values(observer, metrics)
        passive_value, reason = passive_control_value(observer, expected)
    except Exception as exc:
        logger.error("Wave 1 runtime validation failed: %s", type(exc).__name__)
        return None
    logger.info(
        "Wave 1 runtime validation: %s",
        ", ".join(f"{status} {sum(r.status == status for r in rows)}"
                  for status in (RV_VALUE_EXTRACTED, RV_SERIES_EXTRACTED, RV_NOT_AVAILABLE,
                                 RV_VALIDATION_FAILED, RV_PENDING)),
    )
    return RuntimeCapture(rows, passive_value, reason)


def finish_runtime_validation(
    outcome: DiscoveryOutcome | None, settings: Settings, application_value: int | None
) -> None:
    """Write the validation artifact and print the summary; never affects the production result."""
    if outcome is None or outcome.runtime is None:
        return
    capture = outcome.runtime
    reason = capture.control_reason or (None if application_value is not None else "Application request failed")
    control = ControlComparison(capture.passive_control_value, application_value, reason)
    now = datetime.now(timezone.utc)
    evidence = None
    try:
        log_path = settings.history_db_path.parent / OBSERVATIONS_FILE if settings.history_db_path else None
        evidence = collect_evidence(capture.rows, now.strftime("%Y-%m-%dT%H:%M:%SZ"), log_path,
                                    control.status == "PASS")
    except Exception as exc:
        logger.error("Could not collect promotion evidence: %s", type(exc).__name__)
    path = None
    if settings.reports_dir is not None:
        try:
            path = write_validation_report(capture.rows, control, settings.reports_dir, now, evidence,
                                          outcome.identification)
        except Exception as exc:
            logger.error("Could not write the runtime validation report: %s", type(exc).__name__)
    print()
    print("Wave 1 runtime validation (developer review only — not stored, not in the executive report)")
    for row in capture.rows:
        value = format_runtime_value(row) if row.value is not None else ""
        print(f"  {row.label:<28}{row.status:<20}{value}")
    print(f"  {'Currently Enrolled Control':<28}{control.status}")
    if evidence is not None:
        print("  Promotion evidence (validation-only candidates; nothing is promoted automatically)")
        for e in evidence.evidence:
            eligible = "ELIGIBLE" if e.eligible else f"NOT ELIGIBLE ({e.eligibility_reason})"
            print(f"    {e.label:<26}{evidence.comparison.results.get(e.metric_id, ''):<14}{eligible}")
        for s in evidence.shared:
            print(f"    Shared widget {', '.join(s.metric_ids)}: "
                  f"{'ELIGIBLE TOGETHER' if s.eligible_together else 'NOT ELIGIBLE'}")
        print("  Cancellation / termination evidence (validation only; not production)")
        for e in evidence.cancellation:
            eligible = "ELIGIBLE" if e.eligible else f"NOT ELIGIBLE ({e.eligibility_reason})"
            print(f"    {e.label:<26}{evidence.comparison.results.get(e.metric_id, ''):<14}{eligible}")
        for s in evidence.cancellation_shared:
            print(f"    Shared widget {', '.join(s.metric_ids)}: "
                  f"{'ELIGIBLE TOGETHER' if s.eligible_together else 'NOT ELIGIBLE'}")
        print(f"    Fingerprint observation: "
              f"{'RECORDED' if evidence.observation_recorded else evidence.observation_note}")
    if outcome.identification:
        print("  Cancellation source identification (structure only; not production)")
        for c in outcome.identification:
            print(f"    {c.widget_id:<24}{c.semantic_role:<32}{c.confidence}")
    print(f"  Report: {path if path else 'not written'}")
    print()
    print(RULE)
    logger.info("Currently Enrolled control: %s", control.status)


def print_discovery_summary(outcome: DiscoveryOutcome) -> None:
    print()
    print("Wave 1 discovery (developer review only — not used for snapshots or the executive report)")
    for state, count in outcome.counts.items():
        print(f"  {state:<12}{count}")
    if outcome.identification:
        print(f"  Identification {len(outcome.identification)} cancellation source candidate(s)")
    print(f"  Report      {outcome.report_path if outcome.report_path else 'not written (no reports directory)'}")
    print()
    print(RULE)


def create_reporting(settings: Settings) -> ReportingPipeline | None:
    if settings.diagnostics_only or settings.history_db_path is None or settings.reports_dir is None:
        return None
    return ReportingPipeline(
        CURRENTLY_ENROLLED,
        history_db_path=settings.history_db_path,
        reports_dir=settings.reports_dir,
        granularity=settings.snapshot_granularity,
    )


def report_failure(reporting: ReportingPipeline | None, run_id: str, exc: BaseException,
                   metrics=()) -> None:
    """Record the failed run (no snapshot) and regenerate the report from stored history."""
    if reporting is None:
        return
    try:
        summary = reporting.complete_failure(run_id, type(exc).__name__, metrics or passive_metrics())
    except Exception as report_exc:
        logger.error("Could not update the report after failure: %s", type(report_exc).__name__)
        return
    logger.info("Report regenerated from stored history (%s): %s", summary.data_status, summary.report_path)


def print_reporting_summary(summary: ReportingSummary, result: MetricResult) -> None:
    snapshot = "NOT WRITTEN"
    if summary.snapshot_write is not None:
        write = summary.snapshot_write
        snapshot = "SAVED" if write.saved else (
            f"SKIPPED ({write.status}: kept existing snapshot from {fmt_time(write.snapshot.retrieved_at_utc)})"
        )
    rows = [
        ("Currently Enrolled", fmt_int(result.value)),
        ("Snapshot", snapshot),
        ("Historical Snapshots", str(summary.stored_snapshot_count)),
        ("Data Status", summary.data_status),
    ]
    rows += [(c.label, fmt_comparison(c)) for c in summary.analytics.comparisons]
    rows.append(("Overall Data Status", summary.overall_data_status or summary.data_status))
    rows.append(("Report", str(summary.report_path)))
    for label, value in rows:
        print(f"{label:<22}{value}")
    print()
    print(RULE)


if __name__ == "__main__":
    sys.exit(main())
