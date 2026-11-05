"""JAQL client bound to the single verified Sisense endpoint."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from playwright.sync_api import BrowserContext

from config.settings import VERIFIED_JAQL_URL, Timeouts
from sisense.models import (
    AuthenticationError,
    AuthorizationError,
    InvalidJaqlPayloadError,
    JaqlQuery,
    JaqlResponse,
    TransientSisenseError,
    UnexpectedResponseError,
    UnverifiedEndpointError,
)
from sisense.retry import TRANSIENT_HTTP_STATUSES, RetryPolicy, run_with_retry
from validation.validators import count_value_cells, parse_json_body, validate_jaql_payload

logger = logging.getLogger(__name__)


def assert_verified_endpoint(url: str) -> None:
    if url != VERIFIED_JAQL_URL:
        raise UnverifiedEndpointError(
            f"Refusing to call unverified endpoint {url!r}; only {VERIFIED_JAQL_URL!r} is allowed"
        )


def _error_summary(text: str) -> str:
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "non-JSON error body"
    if isinstance(body, dict):
        detail = body.get("details") or body.get("error") or body.get("message")
        if isinstance(detail, (str, int, float)):
            return str(detail)[:300]
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"][:300]
    return "no error detail"


class SisenseJaqlClient:
    """Executes verified JAQL queries through an authenticated Playwright context."""

    def __init__(
        self,
        context: BrowserContext,
        *,
        timeouts: Timeouts,
        retry_policy: RetryPolicy,
        endpoint: str = VERIFIED_JAQL_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        assert_verified_endpoint(endpoint)
        self._context = context
        self._endpoint = endpoint
        self._timeouts = timeouts
        self._retry_policy = retry_policy
        self._sleep = sleep

    def execute(self, query: JaqlQuery) -> JaqlResponse:
        if not isinstance(query, JaqlQuery):
            raise InvalidJaqlPayloadError("execute() only accepts JaqlQuery objects from verified payload builders")
        assert_verified_endpoint(self._endpoint)
        validate_jaql_payload(query.body)

        if query.raw_body is not None:
            if json.loads(query.raw_body) != query.body:
                raise InvalidJaqlPayloadError("Verified raw payload text does not match the validated payload")
            data = query.raw_body
        else:
            data = json.dumps(query.body)
        logger.info("Metric: %s", query.metric)
        started = time.monotonic()
        attempts_used = 0

        def attempt_once(attempt: int) -> tuple[int, dict[str, Any]]:
            nonlocal attempts_used
            attempts_used = attempt
            return self._post_once(data, attempt)

        status, body = run_with_retry(
            attempt_once,
            policy=self._retry_policy,
            description=f"JAQL {query.metric}",
            sleep=self._sleep,
        )
        duration = time.monotonic() - started
        logger.info("Value cells: %d", count_value_cells(body))
        logger.info("Duration: %.2f sec", duration)
        return JaqlResponse(status=status, body=body, duration_seconds=duration, attempts=attempts_used)

    def _post_once(self, data: str, attempt: int) -> tuple[int, dict[str, Any]]:
        timeout = self._timeouts.for_attempt(attempt)
        logger.info(
            "JAQL attempt %d/%d (timeout %.0f sec)",
            attempt, self._retry_policy.max_attempts, timeout / 1000,
        )
        attempt_started = time.monotonic()
        response = self._context.request.post(
            self._endpoint,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
            fail_on_status_code=False,
            max_redirects=0,
        )
        try:
            status = response.status
            text = response.text()
        finally:
            response.dispose()

        logger.info("HTTP %d (%.2f sec)", status, time.monotonic() - attempt_started)

        if status == 200:
            return status, parse_json_body(text)
        if status in TRANSIENT_HTTP_STATUSES:
            raise TransientSisenseError(f"HTTP {status} from JAQL endpoint")
        if status == 401 or 300 <= status < 400:
            raise AuthenticationError(f"HTTP {status}: JAQL request is not authenticated")
        if status == 403:
            raise AuthorizationError("HTTP 403: account is not authorized for this JAQL query")
        if status == 400:
            raise InvalidJaqlPayloadError(f"HTTP 400: Sisense rejected the payload ({_error_summary(text)})")
        raise UnexpectedResponseError(f"HTTP {status} from JAQL endpoint ({_error_summary(text)})")
