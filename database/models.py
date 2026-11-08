"""Result and error types for the enrollment database source.

Candidate column names are a contract for the next phase. They are not a
statement that those columns exist on the authoritative table.
"""

from __future__ import annotations

from dataclasses import dataclass


# Unverified. The next phase must discover the schema before selecting these.
CANDIDATE_ENROLLMENT_COLUMNS: tuple[str, ...] = (
    "coverage_year",
    "hios_issuer_id",
    "enrollment_id",
    "enrollee_id",
    "enrollment_status_description",
    "enrollee_status_description",
    "application_type",
    "enrollment_confirmation_date",
)


class DatabaseError(Exception):
    """Base error for the enrollment database source. Messages must stay sanitized."""


class DatabaseConfigurationError(DatabaseError):
    """Required database configuration is missing or invalid. Names only, never values."""


class DatabaseConnectionError(DatabaseError):
    """The connection could not be opened or closed. No credentials in the message."""


class QueryExecutionError(DatabaseError):
    """A read-only query failed. The message must not contain credentials."""


@dataclass(frozen=True)
class QueryResult:
    """Rows returned by one read-only query. Nothing is written to disk."""

    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    row_count: int
    duration_seconds: float
