import copy
import json
import tempfile
import unittest
from pathlib import Path

from config.settings import VERIFIED_DATASOURCE, Timeouts
from sisense.client import SisenseJaqlClient
from sisense.models import InvalidJaqlPayloadError, ResponseValidationError, VerifiedJaqlPayloadRequired
from sisense.payloads.currently_enrolled import (
    METRIC_NAME,
    PRIMARY_MEASURE_TITLE,
    VERIFIED_JAQL_PAYLOAD_REQUIRED,
    VERIFIED_PAYLOAD_FILE,
    build_currently_enrolled_query,
)
from sisense.retry import RetryPolicy
from validation.validators import (
    count_value_cells,
    extract_count_measure_value,
    extract_measure_value,
    measure_column_index,
    parse_json_body,
    result_measure_titles,
    validate_jaql_payload,
)

ENROLLED = "Currently Enrolled People"
EFFECTUATED = "% Effectuated"

VERIFIED_PAYLOAD = json.loads(VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))

# Synthetic response values: an enrolled count and a fractional percentage.
COUNT = 812345
PERCENT = 0.9731


def measure(title):
    return {"jaql": {"type": "measure", "formula": "x", "title": title}}


def scope(title):
    return {"jaql": {"dim": f"[t.{title}]", "title": title}, "panel": "scope"}


def payload_with(*items):
    return {"datasource": dict(VERIFIED_DATASOURCE), "metadata": list(items)}


def cells(*data):
    return [{"data": d, "text": str(d)} for d in data]


class ValidatePayloadTests(unittest.TestCase):
    def test_accepts_verified_payload(self):
        validate_jaql_payload(VERIFIED_PAYLOAD)

    def test_accepts_datasource_name_string(self):
        validate_jaql_payload(dict(VERIFIED_PAYLOAD, datasource="MAIN"))

    def test_rejects_other_datasource(self):
        payload = dict(VERIFIED_PAYLOAD, datasource=dict(VERIFIED_DATASOURCE, title="OTHER"))
        with self.assertRaises(InvalidJaqlPayloadError):
            validate_jaql_payload(payload)
        with self.assertRaises(InvalidJaqlPayloadError):
            validate_jaql_payload(dict(VERIFIED_PAYLOAD, datasource="OTHER"))

    def test_rejects_missing_datasource(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            validate_jaql_payload({"metadata": VERIFIED_PAYLOAD["metadata"]})

    def test_rejects_empty_metadata(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            validate_jaql_payload(dict(VERIFIED_PAYLOAD, metadata=[]))

    def test_rejects_non_object(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            validate_jaql_payload([VERIFIED_PAYLOAD])


class VerifiedPayloadStructureTests(unittest.TestCase):
    def test_measure_titles_in_order(self):
        self.assertEqual(result_measure_titles(VERIFIED_PAYLOAD), [ENROLLED, EFFECTUATED])

    def test_primary_is_metadata_and_column_zero(self):
        self.assertEqual(VERIFIED_PAYLOAD["metadata"][0]["jaql"]["title"], ENROLLED)
        self.assertEqual(measure_column_index(VERIFIED_PAYLOAD, ENROLLED), 0)
        self.assertEqual(measure_column_index(VERIFIED_PAYLOAD, EFFECTUATED), 1)

    def test_scope_filters_produce_no_columns(self):
        self.assertEqual(len(VERIFIED_PAYLOAD["metadata"]), 8)
        self.assertEqual(sum(1 for m in VERIFIED_PAYLOAD["metadata"] if m.get("panel") == "scope"), 6)

    def test_absent_primary_title_fails(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            measure_column_index(payload_with(measure(EFFECTUATED)), ENROLLED)

    def test_duplicate_primary_title_fails(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            measure_column_index(payload_with(measure(ENROLLED), measure(ENROLLED)), ENROLLED)

    def test_non_scope_dimension_fails(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            result_measure_titles(payload_with(measure(ENROLLED), {"jaql": {"dim": "[t.c]", "title": "c"}}))

    def test_disabled_measure_fails(self):
        with self.assertRaises(InvalidJaqlPayloadError):
            result_measure_titles(payload_with(dict(measure(ENROLLED), disabled=True)))


class PrimaryMeasureExtractionTests(unittest.TestCase):
    """The enrolled count must be returned, never the % Effectuated value."""

    def extract(self, response, payload=VERIFIED_PAYLOAD):
        return extract_count_measure_value(response, payload, ENROLLED)

    def test_flat_row_returns_count_not_percentage(self):
        value = self.extract({"values": cells(COUNT, PERCENT)})
        self.assertEqual(value, COUNT)
        self.assertNotEqual(value, PERCENT)

    def test_nested_row_returns_count_not_percentage(self):
        self.assertEqual(self.extract({"values": [cells(COUNT, PERCENT)]}), COUNT)

    def test_matching_headers_accepted(self):
        response = {"headers": [ENROLLED, EFFECTUATED], "values": [cells(COUNT, PERCENT)]}
        self.assertEqual(self.extract(response), COUNT)

    def test_percentage_measure_is_separately_addressable(self):
        value = extract_measure_value({"values": cells(COUNT, PERCENT)}, VERIFIED_PAYLOAD, EFFECTUATED)
        self.assertEqual(value, PERCENT)

    def test_mapping_follows_payload_order(self):
        reversed_payload = payload_with(measure(EFFECTUATED), measure(ENROLLED), scope("coverage_year"))
        self.assertEqual(self.extract({"values": cells(PERCENT, COUNT)}, reversed_payload), COUNT)

    def test_swapped_headers_fail(self):
        response = {"headers": [EFFECTUATED, ENROLLED], "values": [cells(COUNT, PERCENT)]}
        with self.assertRaises(ResponseValidationError):
            self.extract(response)

    def test_header_count_mismatch_fails(self):
        response = {"headers": [ENROLLED], "values": [cells(COUNT, PERCENT)]}
        with self.assertRaises(ResponseValidationError):
            self.extract(response)

    def test_cell_count_mismatch_fails(self):
        for values in (cells(COUNT), cells(COUNT, PERCENT, 1), [cells(COUNT)], [cells(COUNT, PERCENT, 1)]):
            with self.subTest(values=values), self.assertRaises(ResponseValidationError):
                self.extract({"values": values})

    def test_multiple_rows_fail(self):
        with self.assertRaises(ResponseValidationError):
            self.extract({"values": [cells(COUNT, PERCENT), cells(COUNT, PERCENT)]})

    def test_non_numeric_primary_fails_without_falling_back_to_percentage(self):
        for bad in ("812345", None, True, float("nan"), -1):
            with self.subTest(bad=bad), self.assertRaises(ResponseValidationError):
                self.extract({"values": [{"data": bad}, {"data": PERCENT}]})

    def test_missing_primary_data_fails(self):
        with self.assertRaises(ResponseValidationError):
            self.extract({"values": [{"text": "812345"}, {"data": PERCENT}]})

    def test_fractional_value_in_primary_position_fails(self):
        with self.assertRaises(ResponseValidationError):
            self.extract({"values": cells(PERCENT, COUNT)})

    def test_integral_float_count_becomes_int(self):
        self.assertEqual(self.extract({"values": cells(float(COUNT), PERCENT)}), COUNT)

    def test_missing_or_empty_values_fail(self):
        for response in ({}, {"values": None}, {"values": []}):
            with self.subTest(response=response), self.assertRaises(ResponseValidationError):
                self.extract(response)

    def test_error_response_fails(self):
        with self.assertRaises(ResponseValidationError):
            self.extract({"error": True, "values": cells(COUNT, PERCENT)})

    def test_value_cell_count(self):
        self.assertEqual(count_value_cells({"values": [cells(COUNT, PERCENT)]}), 2)


class ParseJsonTests(unittest.TestCase):
    def test_parses_object(self):
        self.assertEqual(parse_json_body('{"values": []}'), {"values": []})

    def test_rejects_html(self):
        with self.assertRaises(ResponseValidationError):
            parse_json_body("<html><body>Sign in</body></html>")

    def test_rejects_non_object(self):
        with self.assertRaises(ResponseValidationError):
            parse_json_body("[1, 2]")


class CurrentlyEnrolledPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "currently_enrolled.verified.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_file_builds(self):
        query = build_currently_enrolled_query()
        self.assertEqual(query.metric, METRIC_NAME)
        self.assertEqual(query.plan_year, 2027)
        self.assertEqual(query.primary_measure_title, PRIMARY_MEASURE_TITLE)
        self.assertEqual(query.raw_body, VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
        self.assertEqual(query.body, VERIFIED_PAYLOAD)

    def test_verified_identifiers_untouched(self):
        body = build_currently_enrolled_query().body
        self.assertEqual(body["queryGuid"], "922AA-9317-FC38-636B-491F-3F4A-ECF7-A360-2")
        self.assertEqual(body["dashboard"], "6a98048ccef60720fe7f9b30;PY 2027 - OE Executive Dashboard")
        self.assertEqual(body["widget"], "6a98048ccef60720fe7f9b47;Currently Enrolled People")
        self.assertEqual(body["count"], 50000)
        self.assertEqual(body["metadata"][0]["instanceid"], "33E76-48BD-D4")
        self.assertEqual(body["metadata"][0]["jaql"]["formula"], "Count([17769-998]) ")

    def test_missing_file_stops(self):
        with self.assertRaises(VerifiedJaqlPayloadRequired) as ctx:
            build_currently_enrolled_query(self.path)
        self.assertIn(VERIFIED_JAQL_PAYLOAD_REQUIRED, str(ctx.exception))

    def test_placeholder_content_stops(self):
        self.path.write_text(json.dumps({"payload": VERIFIED_JAQL_PAYLOAD_REQUIRED}))
        with self.assertRaises(VerifiedJaqlPayloadRequired):
            build_currently_enrolled_query(self.path)

    def test_rejects_payload_without_verified_measure_field(self):
        self.path.write_text(json.dumps(payload_with(measure(ENROLLED))))
        with self.assertRaises(InvalidJaqlPayloadError):
            build_currently_enrolled_query(self.path)

    def test_rejects_payload_without_primary_title(self):
        payload = copy.deepcopy(VERIFIED_PAYLOAD)
        payload["metadata"][0]["jaql"]["title"] = "Something Else"
        self.path.write_text(json.dumps(payload))
        with self.assertRaises(InvalidJaqlPayloadError):
            build_currently_enrolled_query(self.path)


class RawBodySentUnchangedTests(unittest.TestCase):
    class _Response:
        status = 200
        headers = {"content-type": "application/json"}

        def text(self):
            return json.dumps({"values": cells(COUNT, PERCENT)})

        def dispose(self):
            pass

    class _Request:
        def __init__(self, response):
            self.response = response
            self.sent = []

        def post(self, url, **kwargs):
            self.sent.append((url, kwargs["data"]))
            return self.response

    def test_client_sends_verified_file_bytes(self):
        query = build_currently_enrolled_query()
        context = type("Ctx", (), {})()
        context.request = self._Request(self._Response())
        client = SisenseJaqlClient(
            context, timeouts=Timeouts(), retry_policy=RetryPolicy(max_attempts=1), sleep=lambda _: None
        )
        response = client.execute(query)
        url, data = context.request.sent[0]
        self.assertEqual(url, "https://gahixreports.getinsured.com/api/datasources/MAIN/jaql")
        self.assertEqual(data, VERIFIED_PAYLOAD_FILE.read_text(encoding="utf-8"))
        self.assertEqual(extract_count_measure_value(response.body, query.body, ENROLLED), COUNT)

    def test_tampered_raw_body_rejected(self):
        query = build_currently_enrolled_query()
        tampered = type(query)(**{**query.__dict__, "raw_body": query.raw_body.replace("50000", "1")})
        context = type("Ctx", (), {})()
        context.request = self._Request(self._Response())
        client = SisenseJaqlClient(
            context, timeouts=Timeouts(), retry_policy=RetryPolicy(max_attempts=1), sleep=lambda _: None
        )
        with self.assertRaises(InvalidJaqlPayloadError):
            client.execute(tampered)
        self.assertEqual(context.request.sent, [])


if __name__ == "__main__":
    unittest.main()
