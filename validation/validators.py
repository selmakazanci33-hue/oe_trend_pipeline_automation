"""Validation for outgoing JAQL payloads and incoming JAQL responses."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from config.settings import VERIFIED_DATASOURCE
from sisense.models import InvalidJaqlPayloadError, ResponseValidationError

_DATASOURCE_NAMES = {VERIFIED_DATASOURCE["title"], VERIFIED_DATASOURCE["fullname"]}
_DATASOURCE_IDENTITY_KEYS = ("title", "fullname", "id")


def validate_jaql_payload(body: Any) -> None:
    """Reject payloads that do not target the verified MAIN datasource."""
    if not isinstance(body, Mapping):
        raise InvalidJaqlPayloadError("JAQL payload must be a JSON object")

    datasource = body.get("datasource")
    if isinstance(datasource, str):
        if datasource not in _DATASOURCE_NAMES:
            raise InvalidJaqlPayloadError(f"Payload datasource {datasource!r} is not the verified MAIN datasource")
    elif isinstance(datasource, Mapping):
        if not any(key in datasource for key in _DATASOURCE_IDENTITY_KEYS):
            raise InvalidJaqlPayloadError("Payload datasource does not identify a datasource")
        for key, expected in VERIFIED_DATASOURCE.items():
            if key in datasource and datasource[key] != expected:
                raise InvalidJaqlPayloadError(
                    f"Payload datasource.{key}={datasource[key]!r} does not match verified value {expected!r}"
                )
    else:
        raise InvalidJaqlPayloadError("Payload is missing a datasource")

    metadata = body.get("metadata")
    if not isinstance(metadata, list) or not metadata:
        raise InvalidJaqlPayloadError("Payload metadata must be a non-empty list")
    if not all(isinstance(item, Mapping) for item in metadata):
        raise InvalidJaqlPayloadError("Every payload metadata item must be an object")


def parse_json_body(text: str) -> dict[str, Any]:
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ResponseValidationError(
            "JAQL response is not valid JSON (an HTML login page would also cause this)"
        ) from exc
    if not isinstance(body, dict):
        raise ResponseValidationError(f"JAQL response must be a JSON object, got {type(body).__name__}")
    return body


def _value_cells(values: list[Any]) -> list[Any]:
    if all(isinstance(item, Mapping) for item in values):
        return values
    if all(isinstance(item, list) for item in values):
        return [cell for row in values for cell in row]
    raise ResponseValidationError("JAQL 'values' mixes rows and cells")


def count_value_cells(body: Mapping[str, Any]) -> int:
    values = body.get("values")
    if not isinstance(values, list):
        return 0
    try:
        return len(_value_cells(values))
    except ResponseValidationError:
        return 0


def result_measure_titles(payload: Mapping[str, Any]) -> list[str]:
    """Titles of the result columns, in payload order.

    Scope-panel entries are filters and produce no result column. Any other
    entry must be an enabled, titled measure; anything else (e.g. a dimension,
    which would produce multiple rows) cannot be reconciled with a
    single-row indicator response.
    """
    metadata = payload.get("metadata")
    if not isinstance(metadata, list):
        raise InvalidJaqlPayloadError("Payload metadata must be a list")

    titles: list[str] = []
    for index, item in enumerate(metadata):
        if not isinstance(item, Mapping):
            raise InvalidJaqlPayloadError(f"metadata[{index}] is not an object")
        if item.get("panel") == "scope":
            continue
        jaql = item.get("jaql")
        if not isinstance(jaql, Mapping) or jaql.get("type") != "measure":
            raise InvalidJaqlPayloadError(f"metadata[{index}] is neither a scope filter nor a measure")
        if item.get("disabled") is True:
            raise InvalidJaqlPayloadError(f"metadata[{index}] is a disabled measure")
        title = jaql.get("title")
        if not isinstance(title, str) or not title:
            raise InvalidJaqlPayloadError(f"metadata[{index}] measure has no title")
        titles.append(title)
    return titles


def measure_column_index(payload: Mapping[str, Any], title: str) -> int:
    titles = result_measure_titles(payload)
    occurrences = titles.count(title)
    if occurrences == 0:
        raise InvalidJaqlPayloadError(f"Measure {title!r} is not present in the payload")
    if occurrences > 1:
        raise InvalidJaqlPayloadError(f"Measure {title!r} occurs {occurrences} times in the payload")
    return titles.index(title)


def _single_result_row(values: list[Any]) -> list[Any]:
    if all(isinstance(item, Mapping) for item in values):
        return values
    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    raise ResponseValidationError("Expected exactly one result row for an indicator query")


def _check_headers(headers: Any, titles: list[str], title: str, index: int) -> None:
    if not isinstance(headers, list) or not headers or not all(isinstance(h, str) for h in headers):
        return
    if len(headers) != len(titles):
        raise ResponseValidationError(
            f"Response has {len(headers)} headers but the payload defines {len(titles)} measures"
        )
    if headers.count(title) > 1:
        raise ResponseValidationError(f"Response header {title!r} occurs more than once")
    if title in headers and headers.index(title) != index:
        raise ResponseValidationError(
            f"Response header {title!r} is at position {headers.index(title)}, payload order expects {index}"
        )


def _numeric_cell(cell: Any, title: str) -> int | float:
    if not isinstance(cell, Mapping) or "data" not in cell:
        raise ResponseValidationError(f"Response cell for {title!r} is missing 'data'")
    data = cell["data"]
    if isinstance(data, bool) or not isinstance(data, (int, float)):
        raise ResponseValidationError(f"Response cell for {title!r} is not numeric: {type(data).__name__}")
    if not math.isfinite(data):
        raise ResponseValidationError(f"Response cell for {title!r} is not finite")
    if data < 0:
        raise ResponseValidationError(f"Response cell for {title!r} is negative: {data}")
    if isinstance(data, float) and data.is_integer():
        return int(data)
    return data


def extract_measure_value(
    response: Mapping[str, Any], payload: Mapping[str, Any], title: str
) -> int | float:
    """Return the value of the measure titled ``title``, located by its position in ``payload``."""
    titles = result_measure_titles(payload)
    index = measure_column_index(payload, title)

    if response.get("error"):
        raise ResponseValidationError("JAQL response reports an error")
    values = response.get("values")
    if not isinstance(values, list):
        raise ResponseValidationError("JAQL response is missing a 'values' list")
    if not values:
        raise ResponseValidationError("JAQL response 'values' is empty")

    row = _single_result_row(values)
    if len(row) != len(titles):
        raise ResponseValidationError(
            f"Response row has {len(row)} cells but the payload defines {len(titles)} measures {titles}"
        )
    _check_headers(response.get("headers"), titles, title, index)
    return _numeric_cell(row[index], title)


def extract_count_measure_value(
    response: Mapping[str, Any], payload: Mapping[str, Any], title: str
) -> int:
    value = extract_measure_value(response, payload, title)
    if not isinstance(value, int):
        raise ResponseValidationError(f"Count measure {title!r} returned a non-integer value: {value}")
    return value
