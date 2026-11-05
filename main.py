"""Phase 1: retrieve the live PY2027 "Currently Enrolled People" metric from Sisense."""

from __future__ import annotations

import json
import logging
import sys
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
from sisense.models import JaqlQuery, MetricResult, SisenseError, VerifiedJaqlPayloadRequired
from sisense.payloads.currently_enrolled import build_currently_enrolled_query
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


def retrieve_value(session: AuthenticatedSession, settings: Settings, query: JaqlQuery) -> int:
    """Send the verified JAQL through the same BrowserContext that authenticated."""
    client = SisenseJaqlClient(
        session.context,
        timeouts=settings.timeouts,
        retry_policy=RetryPolicy.from_settings(settings.retry),
    )
    response = client.execute(query)
    return extract_count_measure_value(response.body, query.body, query.primary_measure_title)


def run_session_diagnostics(
    session: AuthenticatedSession, observer: BrowserJaqlObserver | None, settings: Settings
) -> None:
    logger.info("[debug] JAQL will use the authenticated BrowserContext (reused saved state: %s)",
                session.reused_saved_state)
    if observer is not None and observer.request_count == 0:
        try:
            session.page.wait_for_event(
                "request",
                predicate=lambda r: r.method == "POST" and r.url == VERIFIED_JAQL_URL,
                timeout=settings.timeouts.default,
            )
        except PlaywrightTimeoutError:
            pass
    if observer is not None:
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

    try:
        with sync_playwright() as playwright:
            authenticator = SisenseAuthenticator(playwright, settings)
            session = authenticator.open_session()
            try:
                if settings.debug_session:
                    run_session_diagnostics(session, authenticator.jaql_observer, settings)
                value = retrieve_value(session, settings, query)
            finally:
                session.close()
    except MissingVerifiedInformation as exc:
        logger.error("STOP: %s", exc)
        return EXIT_MISSING_VERIFIED_INFO
    except (SisenseError, PlaywrightError) as exc:
        logger.error("Retrieval failed: %s: %s", type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "")
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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
