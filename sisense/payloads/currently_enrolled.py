"""Verified JAQL payload for the "Currently Enrolled People" widget.

The request body is loaded verbatim from ``currently_enrolled.verified.json``,
captured from Chrome DevTools (POST /api/datasources/MAIN/jaql issued by the
widget). Do not edit, reconstruct, or reformat that file; it is sent
byte-for-byte. The widget payload also carries a secondary "% Effectuated"
measure, which is not part of this metric.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from sisense.models import InvalidJaqlPayloadError, JaqlQuery, VerifiedJaqlPayloadRequired
from validation.validators import measure_column_index, validate_jaql_payload

METRIC_NAME = "currently_enrolled_people"
WIDGET_TITLE = "Currently Enrolled People"
PRIMARY_MEASURE_TITLE = "Currently Enrolled People"
PLAN_YEAR = 2027
VERIFIED_MEASURE_FIELD = "exchg_indiv_identifier"

VERIFIED_JAQL_PAYLOAD_REQUIRED = "VERIFIED_JAQL_PAYLOAD_REQUIRED"
VERIFIED_PAYLOAD_FILE = Path(__file__).with_name("currently_enrolled.verified.json")


def build_currently_enrolled_query(payload_file: Path = VERIFIED_PAYLOAD_FILE) -> JaqlQuery:
    if not payload_file.is_file():
        raise VerifiedJaqlPayloadRequired(
            f"{VERIFIED_JAQL_PAYLOAD_REQUIRED}: the exact JAQL request body for the "
            f"'{WIDGET_TITLE}' widget has not been supplied. Save the captured request "
            f"body to {payload_file}"
        )

    raw = payload_file.read_text(encoding="utf-8")
    if VERIFIED_JAQL_PAYLOAD_REQUIRED in raw:
        raise VerifiedJaqlPayloadRequired(
            f"{VERIFIED_JAQL_PAYLOAD_REQUIRED}: {payload_file.name} still contains the placeholder"
        )

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidJaqlPayloadError(f"{payload_file.name} is not valid JSON: {exc}") from exc

    validate_jaql_payload(body)

    if VERIFIED_MEASURE_FIELD not in raw:
        raise InvalidJaqlPayloadError(
            f"{payload_file.name} does not reference the verified measure field "
            f"enrollee.{VERIFIED_MEASURE_FIELD}; it is probably not the '{WIDGET_TITLE}' request"
        )

    measure_column_index(body, PRIMARY_MEASURE_TITLE)

    return JaqlQuery(
        metric=METRIC_NAME,
        plan_year=PLAN_YEAR,
        widget_title=WIDGET_TITLE,
        body=copy.deepcopy(body),
        primary_measure_title=PRIMARY_MEASURE_TITLE,
        raw_body=raw,
    )
