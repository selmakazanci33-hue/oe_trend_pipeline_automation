"""Phase 1: retrieve the live PY2027 "Currently Enrolled People" metric from Sisense."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone

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
from metrics.catalog import CURRENTLY_ENROLLED
from reporting.formatting import fmt_comparison, fmt_int, fmt_time
from reporting.pipeline import ReportingPipeline, ReportingSummary
from sisense.retry import RetryPolicy
from validation.validators import extract_count_measure_value

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_MISSING_VERIFIED_INFO = 2

RULE = "=" * 60
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

    passive_observer = create_passive_observer(settings, query)
    passive_observation = None
    try:
        with sync_playwright() as playwright:
            authenticator = SisenseAuthenticator(
                playwright, settings, extra_observers=(passive_observer,) if passive_observer else ()
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
                if passive_observer is not None:
                    passive_observation = run_passive_observation(session, passive_observer, settings.timeouts.slow)
                value = retrieve_value(session, settings, query, request_metadata)
            finally:
                session.close()
    except MissingVerifiedInformation as exc:
        logger.error("STOP: %s", exc)
        report_failure(reporting, run_id, exc)
        return EXIT_MISSING_VERIFIED_INFO
    except (SisenseError, PlaywrightError) as exc:
        logger.error("Retrieval failed: %s: %s", type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "")
        report_failure(reporting, run_id, exc)
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

    if reporting is None:
        return EXIT_OK
    try:
        summary = reporting.complete_success(run_id, result)
    except Exception as exc:
        logger.error("Snapshot/report step failed: %s", type(exc).__name__)
        return EXIT_FAILURE
    print_reporting_summary(summary, result)
    return EXIT_OK


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


def create_reporting(settings: Settings) -> ReportingPipeline | None:
    if settings.diagnostics_only or settings.history_db_path is None or settings.reports_dir is None:
        return None
    return ReportingPipeline(
        CURRENTLY_ENROLLED,
        history_db_path=settings.history_db_path,
        reports_dir=settings.reports_dir,
        granularity=settings.snapshot_granularity,
    )


def report_failure(reporting: ReportingPipeline | None, run_id: str, exc: BaseException) -> None:
    """Record the failed run (no snapshot) and regenerate the report from stored history."""
    if reporting is None:
        return
    try:
        summary = reporting.complete_failure(run_id, type(exc).__name__)
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
    rows.append(("Report", str(summary.report_path)))
    for label, value in rows:
        print(f"{label:<22}{value}")
    print()
    print(RULE)


if __name__ == "__main__":
    sys.exit(main())
