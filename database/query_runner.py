"""Read-only query execution for the enrollment database source.

Rejects obvious write statements. This is not a security boundary; database
permissions remain the authoritative protection. Runtime values are passed
as parameters and are not interpolated into the SQL text.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from pathlib import Path

from database.connection import DatabaseConnection
from database.models import QueryExecutionError, QueryResult

QUERIES_ROOT = Path(__file__).resolve().parent.parent / "queries"

_FORBIDDEN = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "MERGE",
    "CREATE",
)
_READ_START = frozenset({"SELECT", "WITH"})
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")


class ReadOnlyQueryError(QueryExecutionError):
    """The SQL text is empty or is not an obvious read."""


class DatabaseClient:
    """Executes read-only SQL on an already open enrollment-database connection."""

    def __init__(self, connection: DatabaseConnection):
        self._connection = connection

    def execute_query(
        self,
        sql: str,
        params: Sequence | None = None,
    ) -> QueryResult:
        """Run one read-only statement. ``params`` are bound, not concatenated."""
        assert_read_only(sql)
        cursor = self._connection.cursor()
        started = time.perf_counter()
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
            description = cursor.description
            if not description:
                raise QueryExecutionError("Query did not return a result set")
            columns = tuple(str(column[0]) for column in description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
        except QueryExecutionError:
            raise
        except Exception as exc:
            raise QueryExecutionError(self._connection.sanitize(str(exc))) from None
        finally:
            cursor.close()
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            duration_seconds=time.perf_counter() - started,
        )

    def execute_query_file(
        self,
        path: str | Path,
        params: Sequence | None = None,
    ) -> QueryResult:
        """Load a ``.sql`` file under ``queries/`` and execute it read-only."""
        return self.execute_query(load_query_file(path), params)


def load_query_file(path: str | Path) -> str:
    """Read a SQL file from ``queries/``. Does not execute it."""
    resolved = _resolve_query_path(path)
    return resolved.read_text(encoding="utf-8-sig")


def assert_read_only(sql: str) -> None:
    """Reject empty SQL and obvious non-read statements.

    Comments and single-quoted strings are ignored so a comment that mentions
    a forbidden word does not, by itself, fail the check. This is a textual
    guard, not a parser.
    """
    visible = _visible_sql(sql)
    statements = [part.strip() for part in visible.split(";") if part.strip()]
    if not statements:
        raise ReadOnlyQueryError("Query is empty")
    for statement in statements:
        match = re.match(r"([A-Za-z]+)", statement)
        keyword = match.group(1).upper() if match else ""
        if keyword not in _READ_START:
            raise ReadOnlyQueryError("Only read-only SELECT queries are allowed")
        if keyword == "SELECT" and re.search(r"\bINTO\b", statement, re.IGNORECASE):
            raise ReadOnlyQueryError("Rejected non-read SQL keyword: INTO")
        for forbidden in _FORBIDDEN:
            if re.search(rf"\b{forbidden}\b", statement, re.IGNORECASE):
                raise ReadOnlyQueryError(f"Rejected non-read SQL keyword: {forbidden}")


def _resolve_query_path(path: str | Path) -> Path:
    raw = Path(path)
    candidate = raw if raw.is_absolute() else QUERIES_ROOT / raw
    try:
        resolved = candidate.resolve()
        root = QUERIES_ROOT.resolve()
    except OSError as exc:
        raise QueryExecutionError("Query file could not be resolved") from exc
    if not resolved.is_relative_to(root):
        raise QueryExecutionError("Query file must be inside the queries directory")
    if resolved.suffix.lower() != ".sql":
        raise QueryExecutionError("Query file must be a .sql file")
    if not resolved.is_file():
        raise QueryExecutionError("Query file was not found")
    return resolved


def _visible_sql(sql: str) -> str:
    without_blocks = _BLOCK_COMMENT.sub(" ", sql)
    without_lines = _LINE_COMMENT.sub(" ", without_blocks)
    return _STRING_LITERAL.sub("''", without_lines)
