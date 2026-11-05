import unittest

from config.settings import VERIFIED_DATASOURCE, VERIFIED_JAQL_URL, Timeouts
from sisense.client import SisenseJaqlClient, assert_verified_endpoint
from sisense.models import InvalidJaqlPayloadError, JaqlQuery, UnverifiedEndpointError
from sisense.retry import RetryPolicy

UNVERIFIED = [
    "https://gahixreports.getinsured.com/api/datasources/OTHER/jaql",
    "https://gahixreports.getinsured.com/api/v1/datasources/MAIN/jaql",
    "https://gahixreports.getinsured.com/api/datasources/MAIN/jaql/",
    "https://gahixreports.getinsured.com/api/datasources/MAIN/jaql?x=1",
    "http://gahixreports.getinsured.com/api/datasources/MAIN/jaql",
    "https://evil.example.com/api/datasources/MAIN/jaql",
    "https://gahixreports.getinsured.com.evil.example/api/datasources/MAIN/jaql",
    "/api/datasources/MAIN/jaql",
    "",
]


class RecordingRequest:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        raise AssertionError("request must not be sent")


class RecordingContext:
    def __init__(self):
        self.request = RecordingRequest()


def make_client(context, endpoint=VERIFIED_JAQL_URL):
    return SisenseJaqlClient(
        context,
        timeouts=Timeouts(),
        retry_policy=RetryPolicy(max_attempts=1),
        endpoint=endpoint,
    )


class EndpointGuardTests(unittest.TestCase):
    def test_verified_endpoint_value(self):
        self.assertEqual(VERIFIED_JAQL_URL, "https://gahixreports.getinsured.com/api/datasources/MAIN/jaql")
        assert_verified_endpoint(VERIFIED_JAQL_URL)

    def test_rejects_unverified_endpoints(self):
        for url in UNVERIFIED:
            with self.subTest(url=url), self.assertRaises(UnverifiedEndpointError):
                assert_verified_endpoint(url)

    def test_client_constructor_rejects_unverified_endpoint(self):
        for url in UNVERIFIED:
            with self.subTest(url=url), self.assertRaises(UnverifiedEndpointError):
                make_client(RecordingContext(), endpoint=url)

    def test_execute_rechecks_endpoint_before_sending(self):
        context = RecordingContext()
        client = make_client(context)
        client._endpoint = UNVERIFIED[0]
        query = JaqlQuery("m", 2027, "w", {"datasource": dict(VERIFIED_DATASOURCE), "metadata": [{}]})
        with self.assertRaises(UnverifiedEndpointError):
            client.execute(query)
        self.assertEqual(context.request.calls, [])

    def test_execute_rejects_raw_dict_payload(self):
        context = RecordingContext()
        client = make_client(context)
        with self.assertRaises(InvalidJaqlPayloadError):
            client.execute({"datasource": "MAIN", "metadata": [{}]})
        self.assertEqual(context.request.calls, [])

    def test_execute_rejects_unverified_datasource_before_sending(self):
        context = RecordingContext()
        client = make_client(context)
        query = JaqlQuery("m", 2027, "w", {"datasource": "OTHER", "metadata": [{}]})
        with self.assertRaises(InvalidJaqlPayloadError):
            client.execute(query)
        self.assertEqual(context.request.calls, [])


if __name__ == "__main__":
    unittest.main()
