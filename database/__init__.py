"""Enrollment Database source.

This package is independent of the Sisense source in ``sisense/``.

Sisense source
    reads dashboard metrics through the existing JAQL client.

Enrollment Database source
    reads FFM/enrollment records from SQL Server / Azure SQL.

The two clients are not combined. Later phases compare their outputs.
``main.py`` does not open this connection.

Future pipeline (not implemented in this phase):

Enrollment DB
→ authoritative source discovery
→ PY2026 records
→ PY2027 records
→ consistent business-rule normalization
→ month
→ issuer
→ Enrollment Count
→ Enrollee Count
→ PY2026 vs PY2027 same-month comparison
→ variance / percentage change
→ validated Sisense comparison
→ trend charts

Historical table names from earlier work (``dbo.Enrollments_PY2526_NOV30``,
``dbo.Enrollments_TEST``, ``dbo.Enrollments_PY2026``) are evidence only. They
are not assumed to be the authoritative table that contains both PY2026 and
PY2027. Column existence is also not assumed; the next phase validates the
schema before extracting.
"""

from database.config import load_database_config
from database.connection import database_connection
from database.models import (
    CANDIDATE_ENROLLMENT_COLUMNS,
    DatabaseConfigurationError,
    DatabaseConnectionError,
    QueryResult,
)
from database.query_runner import DatabaseClient, ReadOnlyQueryError

__all__ = [
    "CANDIDATE_ENROLLMENT_COLUMNS",
    "DatabaseClient",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "QueryResult",
    "ReadOnlyQueryError",
    "database_connection",
    "load_database_config",
]
