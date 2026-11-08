"""pyodbc connection factory for the enrollment database source.

No pool. The previous Georgia Access SQL Server code used pyodbc as the
driver (wrapped there by SQLAlchemy, which pools). This project does not use
a pool, so connections are opened and closed directly.

Authentication is ActiveDirectoryInteractive. The string contains UID and
no password. It is never logged. Driver exceptions are rewritten before they
leave this module; SQL error codes are kept.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

from database.config import DatabaseConfig
from database.models import DatabaseConnectionError

# A connector is ``connect(connection_string, timeout=seconds) -> raw connection``.
Connector = Callable[..., object]

_CREDENTIAL_ATTR = re.compile(
    r"(?i)\b(PWD|PASSWORD|UID)\s*=\s*(?:\{(?:\}\}|[^}])*}|[^;]*)"
)
_FULL_ODBC_STRING = re.compile(r"(?i)\bDRIVER\s*=.*", re.DOTALL)


class DatabaseConnection:
    """Open pyodbc connection. Use as a context manager so ``close`` always runs."""

    def __init__(self, raw: object, *, redact: Callable[[str], str]):
        self._raw = raw
        self._redact = redact
        self._closed = False

    def cursor(self):
        if self._closed:
            raise DatabaseConnectionError("Connection is closed")
        return self._raw.cursor()

    def sanitize(self, message: str) -> str:
        return self._redact(message)

    def set_query_timeout(self, seconds: int) -> None:
        """Set the driver query timeout. This is not the login timeout."""
        if self._closed:
            raise DatabaseConnectionError("Connection is closed")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1:
            raise DatabaseConnectionError("Query timeout must be a positive integer")
        if hasattr(self._raw, "timeout"):
            self._raw.timeout = seconds

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._raw.close()
        except Exception as exc:
            raise DatabaseConnectionError(self._redact(str(exc))) from None

    def __enter__(self) -> DatabaseConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.close()
        except DatabaseConnectionError:
            if exc_type is None:
                raise
        return False


def database_connection(
    config: DatabaseConfig,
    *,
    connector: Connector | None = None,
) -> DatabaseConnection:
    """Open one connection. Pass ``connector`` only from tests."""
    connect = connector or _pyodbc_connect
    redact = _redactor(config.username)
    try:
        raw = connect(config.odbc_connection_string(), timeout=config.timeout_seconds)
    except Exception as exc:
        raise DatabaseConnectionError(redact(str(exc))) from None
    return DatabaseConnection(raw, redact=redact)


@contextmanager
def open_database(
    config: DatabaseConfig,
    *,
    connector: Connector | None = None,
) -> Iterator[DatabaseConnection]:
    """Open a connection and close it when the block ends, including on error."""
    connection = database_connection(config, connector=connector)
    with connection:
        yield connection


def sanitize_database_message(message: str, *secrets: str) -> str:
    """Remove credentials and full ODBC strings. Keep surrounding error text."""
    redacted = message or ""
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _CREDENTIAL_ATTR.sub(lambda match: f"{match.group(1).upper()}=[REDACTED]", redacted)
    if re.search(r"(?i)\bDRIVER\s*=", redacted) and re.search(r"(?i)\bSERVER\s*=", redacted):
        redacted = _FULL_ODBC_STRING.sub("Connection details were omitted.", redacted, count=1)
    return redacted or "Database error."


def _redactor(username: str) -> Callable[[str], str]:
    def redact(message: str) -> str:
        return sanitize_database_message(message, username)

    return redact


def _pyodbc_connect(connection_string: str, timeout: int = 300):
    """Lazy import so unit tests never need a live ODBC driver."""
    import pyodbc

    return pyodbc.connect(connection_string, timeout=timeout)
