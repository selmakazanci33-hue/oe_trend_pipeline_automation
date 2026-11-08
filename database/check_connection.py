"""Explicit enrollment-database connection check.

Run manually, after ``.env`` is filled in, from the project directory::

    python -m database.check_connection

This module is not imported by ``main.py``. It runs ``SELECT 1``. Opening the
connection uses ActiveDirectoryInteractive, so Entra may prompt in the browser.
Output is a pass/fail line plus, on failure, a sanitized reason. It never
prints the username or the connection string.
"""

from __future__ import annotations

import sys

from database.config import load_database_config
from database.connection import database_connection, sanitize_database_message
from database.models import DatabaseError
from database.query_runner import DatabaseClient

_PROBE = "SELECT 1 AS connection_ok"


def main() -> int:
    try:
        config = load_database_config()
        with database_connection(config) as connection:
            result = DatabaseClient(connection).execute_query(_PROBE)
        if result.row_count == 1 and result.rows and result.rows[0][0] == 1:
            print("Database connection: PASS")
            return 0
        print("Database connection: FAILED")
        return 1
    except DatabaseError as exc:
        print("Database connection: FAILED")
        print(sanitize_database_message(str(exc)))
        return 1
    except Exception as exc:
        print("Database connection: FAILED")
        print(sanitize_database_message(str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
