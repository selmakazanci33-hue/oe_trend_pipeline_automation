import random
import unittest

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config.settings import VERIFIED_DATASOURCE, Timeouts
from sisense.client import SisenseJaqlClient
from sisense.models import (
    AuthenticationError,
    AuthorizationError,
    InvalidJaqlPayloadError,
    JaqlQuery,
    TransientSisenseError,
)
from sisense.retry import RetryPolicy, is_transient, run_with_retry

POLICY = RetryPolicy(max_attempts=3, backoff_seconds=(2.0, 5.0, 10.0), jitter_ratio=0.2)


class Operation:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.attempts = []

    def __call__(self, attempt):
        self.attempts.append(attempt)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ClassificationTests(unittest.TestCase):
    def test_transient(self):
        for exc in (
            TransientSisenseError("503"),
            PlaywrightTimeoutError("Timeout 30000ms exceeded"),
            PlaywrightError("read ECONNRESET"),
            PlaywrightError("net::ERR_CONNECTION_RESET at https://example"),
        ):
            with self.subTest(exc=exc):
                self.assertTrue(is_transient(exc))

    def test_not_transient(self):
        for exc in (
            AuthenticationError("401"),
            AuthorizationError("403"),
            InvalidJaqlPayloadError("400"),
            PlaywrightError("Target page, context or browser has been closed"),
            ValueError("bug"),
            KeyError("bug"),
        ):
            with self.subTest(exc=exc):
                self.assertFalse(is_transient(exc))


class RunWithRetryTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def run_op(self, op, policy=POLICY):
        return run_with_retry(op, policy=policy, description="test", sleep=self.sleeps.append, rng=random.Random(0))

    def test_success_first_attempt(self):
        op = Operation(["ok"])
        self.assertEqual(self.run_op(op), "ok")
        self.assertEqual(op.attempts, [1])
        self.assertEqual(self.sleeps, [])

    def test_retries_transient_then_succeeds(self):
        op = Operation([TransientSisenseError("502"), PlaywrightTimeoutError("t"), "ok"])
        self.assertEqual(self.run_op(op), "ok")
        self.assertEqual(op.attempts, [1, 2, 3])
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(1.6 <= self.sleeps[0] <= 2.4)
        self.assertTrue(4.0 <= self.sleeps[1] <= 6.0)

    def test_gives_up_after_max_attempts(self):
        op = Operation([TransientSisenseError("503")] * 3)
        with self.assertRaises(TransientSisenseError):
            self.run_op(op)
        self.assertEqual(op.attempts, [1, 2, 3])
        self.assertEqual(len(self.sleeps), 2)

    def test_does_not_retry_non_transient(self):
        for exc in (AuthenticationError("401"), AuthorizationError("403"), InvalidJaqlPayloadError("400"), TypeError("bug")):
            with self.subTest(exc=exc):
                self.sleeps.clear()
                op = Operation([exc, "ok"])
                with self.assertRaises(type(exc)):
                    self.run_op(op)
                self.assertEqual(op.attempts, [1])
                self.assertEqual(self.sleeps, [])

    def test_max_attempts_is_configurable(self):
        op = Operation([TransientSisenseError("504")] * 5 + ["ok"])
        self.assertEqual(self.run_op(op, RetryPolicy(max_attempts=6)), "ok")
        self.assertEqual(len(op.attempts), 6)

    def test_delay_is_bounded_by_last_tier(self):
        policy = RetryPolicy(max_attempts=10, backoff_seconds=(2.0, 5.0, 10.0), jitter_ratio=0.2)
        for attempt in range(1, 10):
            self.assertLessEqual(policy.delay_for(attempt, random.Random(attempt)), 12.0)

    def test_invalid_policy(self):
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(backoff_seconds=())


class TimeoutEscalationTests(unittest.TestCase):
    def test_tiers_escalate_per_attempt(self):
        t = Timeouts(short=1, default=10, slow=20, very_slow=30)
        self.assertEqual([t.for_attempt(n) for n in (1, 2, 3, 4)], [10, 20, 30, 30])


class FakeResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text
        self.disposed = False

    def text(self):
        return self._text

    def dispose(self):
        self.disposed = True


class FakeRequest:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeContext:
    def __init__(self, responses):
        self.request = FakeRequest(responses)


QUERY = JaqlQuery(
    metric="currently_enrolled_people",
    plan_year=2027,
    widget_title="Currently Enrolled People",
    body={"datasource": dict(VERIFIED_DATASOURCE), "metadata": [{"jaql": {}}]},
)
OK_BODY = '{"values": [{"data": 791826, "text": "791826"}]}'


class ClientRetryTests(unittest.TestCase):
    def make_client(self, responses):
        context = FakeContext(responses)
        client = SisenseJaqlClient(
            context,
            timeouts=Timeouts(short=1, default=10, slow=20, very_slow=30),
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=(0.0,), jitter_ratio=0.0),
            sleep=lambda _: None,
        )
        return client, context

    def test_success(self):
        client, context = self.make_client([FakeResponse(200, OK_BODY)])
        response = client.execute(QUERY)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.attempts, 1)
        self.assertEqual(response.body["values"][0]["data"], 791826)
        _, kwargs = context.request.calls[0]
        self.assertEqual(kwargs["max_redirects"], 0)
        self.assertEqual(kwargs["timeout"], 10)

    def test_retries_503_with_escalating_timeout(self):
        client, context = self.make_client([FakeResponse(503, ""), FakeResponse(504, ""), FakeResponse(200, OK_BODY)])
        response = client.execute(QUERY)
        self.assertEqual(response.attempts, 3)
        self.assertEqual([kw["timeout"] for _, kw in context.request.calls], [10, 20, 30])

    def test_terminal_statuses_not_retried(self):
        cases = {
            401: AuthenticationError,
            302: AuthenticationError,
            403: AuthorizationError,
            400: InvalidJaqlPayloadError,
        }
        for status, error in cases.items():
            with self.subTest(status=status):
                client, context = self.make_client([FakeResponse(status, '{"error": true}'), FakeResponse(200, OK_BODY)])
                with self.assertRaises(error):
                    client.execute(QUERY)
                self.assertEqual(len(context.request.calls), 1)


if __name__ == "__main__":
    unittest.main()
