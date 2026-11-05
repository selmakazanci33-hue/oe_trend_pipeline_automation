import copy
import json

from playwright.sync_api import Error as PlaywrightError

from config.settings import VERIFIED_JAQL_URL
from sisense.payloads.currently_enrolled import build_currently_enrolled_query

QUERY = build_currently_enrolled_query()
VERIFIED = json.loads(QUERY.raw_body)


def payload(mutate=None, query_guid=None):
    """A dashboard request body: the verified payload, optionally altered."""
    body = copy.deepcopy(VERIFIED)
    if query_guid is not None:
        body["queryGuid"] = query_guid
    if mutate is not None:
        mutate(body)
    return body


def scope(body, column):
    return next(m for m in body["metadata"] if m.get("panel") == "scope" and m["jaql"]["column"] == column)


def measure(body, title):
    return next(m for m in body["metadata"] if m.get("panel") != "scope" and m["jaql"]["title"] == title)


def response_text(value, extra=None):
    body = {
        "headers": ["Currently Enrolled People", "% Effectuated"],
        "values": [{"data": value, "text": str(value)}, {"data": 0.97, "text": "0.97"}],
    }
    body.update(extra or {})
    return json.dumps(body)


class PassiveResponse:
    def __init__(self, status, text, body_error=False):
        self.status = status
        self._text = text
        self._body_error = body_error
        self.body_reads = 0

    def body(self):
        self.body_reads += 1
        if self._body_error:
            raise PlaywrightError("Response body is unavailable")
        return self._text.encode("utf-8")


class PassiveRequest:
    """A completed dashboard JAQL request exposing only what Playwright already holds."""

    def __init__(self, body, status=200, text=None, body_error=False, no_response=False,
                 method="POST", url=VERIFIED_JAQL_URL, headers=None):
        self.method = method
        self.url = url
        self.post_data = json.dumps(body) if body is not None else None
        self._response = None if no_response else PassiveResponse(status, text or response_text(1), body_error)
        self._headers = headers or {}
        self.response_calls = 0

    def response(self):
        self.response_calls += 1
        return self._response

    def all_headers(self):
        return dict(self._headers)
