"""Calendar-date labels for source series points."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def valid_date_label(value: Any) -> str | None:
    """ISO calendar date (YYYY-MM-DD) only when ``value`` is a provable ISO date/datetime; otherwise None.

    Dashboard display text is formatted by a widget mask and is not a calendar
    representation; strings such as '012orADaAe' are rejected, never guessed.
    """
    if not isinstance(value, str):
        return None
    match = _ISO_DATE.match(value.strip())
    if match is None:
        return None
    try:
        parsed = date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return parsed.isoformat() if 2000 <= parsed.year <= 2100 else None
