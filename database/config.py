"""Enrollment-database configuration loaded from the project ``.env``.

Values stay in the process environment. This module never logs them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from database.models import DatabaseConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Same default driver name used by the earlier Georgia Access SQL Server connection.
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
DEFAULT_PORT = 1433
DEFAULT_TIMEOUT_SECONDS = 30

_REQUIRED = ("DB_SERVER", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD")


@dataclass(frozen=True)
class DatabaseConfig:
    """Typed connection settings. ``password`` is omitted from ``repr``."""

    server: str
    database: str
    username: str
    password: str = field(repr=False)
    driver: str
    port: int
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def odbc_connection_string(self) -> str:
        """ODBC string for pyodbc. Callers must not log the return value."""
        server = self.server
        if "," not in server and "\\" not in server:
            server = f"{server},{self.port}"
        return (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={_odbc_value(server)};"
            f"DATABASE={_odbc_value(self.database)};"
            f"UID={_odbc_value(self.username)};"
            f"PWD={_odbc_value(self.password)};"
            "Encrypt=yes;"
            "TrustServerCertificate=yes;"
            f"Connection Timeout={self.timeout_seconds};"
        )


def load_database_config(
    env_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> DatabaseConfig:
    """Load ``DB_*`` settings.

    An explicit ``environ`` mapping is used as-is (tests). An explicit
    ``env_file`` is read without writing into ``os.environ``. The default
    path loads the project ``.env`` the same way the Sisense settings do
    (``override=False``).
    """
    if environ is not None:
        source: Mapping[str, str | None] = environ
    elif env_file is not None:
        source = dotenv_values(env_file)
    else:
        load_dotenv(ENV_FILE, override=False)
        source = os.environ

    missing = [name for name in _REQUIRED if not _text(source, name)]
    if missing:
        raise DatabaseConfigurationError(
            "Missing required database configuration: " + ", ".join(missing)
        )

    driver = _text(source, "DB_DRIVER") or DEFAULT_DRIVER
    return DatabaseConfig(
        server=_text(source, "DB_SERVER"),
        database=_text(source, "DB_DATABASE"),
        username=_text(source, "DB_USERNAME"),
        password=_text(source, "DB_PASSWORD"),
        driver=driver,
        port=_port(source),
    )


def _text(source: Mapping[str, str | None], name: str) -> str:
    raw = source.get(name)
    if raw is None:
        return ""
    return str(raw).strip()


def _port(source: Mapping[str, str | None]) -> int:
    raw = _text(source, "DB_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise DatabaseConfigurationError("DB_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise DatabaseConfigurationError("DB_PORT must be between 1 and 65535")
    return port


def _odbc_value(value: str) -> str:
    """Brace-escape values that would break an ODBC connection string."""
    if any(character in value for character in (";", "{", "}", "=")):
        return "{" + value.replace("}", "}}") + "}"
    return value
