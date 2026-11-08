"""Enrollment database source. Mocks only — no driver connection and no Sisense."""

import subprocess
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from database.check_connection import main as check_connection_main
from database.config import ENV_FILE, PROJECT_ROOT, load_database_config
from database.connection import (
    database_connection,
    open_database,
    sanitize_database_message,
)
from database.models import (
    CANDIDATE_ENROLLMENT_COLUMNS,
    DatabaseConfigurationError,
    QueryResult,
)
from database.query_runner import (
    DatabaseClient,
    ReadOnlyQueryError,
    assert_read_only,
    load_query_file,
)
from sisense.payloads.currently_enrolled import METRIC_NAME, PLAN_YEAR

USERNAME = "reader@example.invalid"
ENV = {
    "DB_SERVER": "sql.example.internal",
    "DB_DATABASE": "enrollment",
    "DB_USERNAME": USERNAME,
    "DB_DRIVER": "ODBC Driver 17 for SQL Server",
}


class FakeCursor:
    def __init__(self, *, fail_message=None):
        self.fail_message = fail_message
        self.executed = None
        self.description = [("connection_ok", None), ("label", None)]
        self.closed = False

    def execute(self, sql, params=None):
        self.executed = (sql, params)
        if self.fail_message is not None:
            raise RuntimeError(self.fail_message)

    def fetchall(self):
        return [(1, "ok")]

    def close(self):
        self.closed = True


class FakeRaw:
    def __init__(self, *, fail_message=None):
        self.cursor_obj = FakeCursor(fail_message=fail_message)
        self.closed = False
        self.fail_close = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError(f"close failed for {USERNAME} (40532)")


def _config():
    return load_database_config(environ=ENV)


class ConfigurationTests(unittest.TestCase):
    def test_loads_required_fields(self):
        config = _config()
        self.assertEqual(config.server, "sql.example.internal")
        self.assertEqual(config.database, "enrollment")
        self.assertEqual(config.username, USERNAME)
        self.assertEqual(config.driver, "ODBC Driver 17 for SQL Server")
        self.assertEqual(config.timeout_seconds, 300)
        self.assertFalse(hasattr(config, "password"))
        self.assertFalse(hasattr(config, "port"))

    def test_loads_from_env_file_without_touching_process_environment(self):
        import os

        os.environ["DB_SOURCE_TEST_SENTINEL"] = "untouched"
        try:
            with mock.patch("database.config.dotenv_values", return_value=dict(ENV)) as reader:
                config = load_database_config(env_file=Path("ignored.env"))
            reader.assert_called_once()
            self.assertEqual(config.database, "enrollment")
            self.assertEqual(os.environ["DB_SOURCE_TEST_SENTINEL"], "untouched")
        finally:
            os.environ.pop("DB_SOURCE_TEST_SENTINEL", None)

    def test_missing_configuration_names_the_variable_only(self):
        incomplete = {key: value for key, value in ENV.items() if key != "DB_SERVER"}
        with self.assertRaises(DatabaseConfigurationError) as caught:
            load_database_config(environ=incomplete)
        message = str(caught.exception)
        self.assertEqual(message, "Missing required database configuration: DB_SERVER")
        self.assertNotIn(USERNAME, message)
        self.assertNotIn("sql.example.internal", message)

    def test_missing_database_and_username_fail_safely(self):
        for name in ("DB_DATABASE", "DB_USERNAME"):
            incomplete = {key: value for key, value in ENV.items() if key != name}
            with self.assertRaises(DatabaseConfigurationError) as caught:
                load_database_config(environ=incomplete)
            self.assertEqual(str(caught.exception), f"Missing required database configuration: {name}")
            self.assertNotIn(USERNAME, str(caught.exception))

    def test_missing_several_variables_are_listed_by_name(self):
        with self.assertRaises(DatabaseConfigurationError) as caught:
            load_database_config(environ={"DB_DRIVER": "ODBC Driver 17 for SQL Server"})
        self.assertEqual(
            str(caught.exception),
            "Missing required database configuration: DB_SERVER, DB_DATABASE, DB_USERNAME",
        )

    def test_password_and_port_are_not_required_and_do_not_change_the_server(self):
        source = dict(ENV)
        source["DB_PASSWORD"] = "not-used"
        source["DB_PORT"] = "1433"
        config = load_database_config(environ=source)
        connection_string = config.odbc_connection_string()
        self.assertEqual(config.server, "sql.example.internal")
        self.assertIn("SERVER=sql.example.internal;", connection_string)
        self.assertNotIn(",1433", connection_string)
        self.assertNotIn("PWD=", connection_string)
        self.assertNotIn("not-used", connection_string)
        self.assertNotIn("not-used", repr(config))

    def test_driver_and_timeout_default_when_omitted(self):
        source = {key: ENV[key] for key in ("DB_SERVER", "DB_DATABASE", "DB_USERNAME")}
        config = load_database_config(environ=source)
        self.assertEqual(config.driver, "ODBC Driver 17 for SQL Server")
        self.assertEqual(config.timeout_seconds, 300)

    def test_custom_timeout(self):
        config = load_database_config(environ=dict(ENV, DB_CONNECTION_TIMEOUT="90"))
        self.assertEqual(config.timeout_seconds, 90)
        self.assertIn("Connection Timeout=90;", config.odbc_connection_string())

    def test_invalid_timeout_does_not_echo_the_value(self):
        bad = dict(ENV, DB_CONNECTION_TIMEOUT=USERNAME)
        with self.assertRaises(DatabaseConfigurationError) as caught:
            load_database_config(environ=bad)
        self.assertEqual(str(caught.exception), "DB_CONNECTION_TIMEOUT must be an integer")
        self.assertNotIn(USERNAME, str(caught.exception))

    def test_repr_omits_username_and_connection_string(self):
        config = _config()
        rendered = repr(config)
        self.assertNotIn(USERNAME, rendered)
        self.assertNotIn("PWD=", rendered)
        self.assertNotIn("Authentication=", rendered)


class SecretRedactionTests(unittest.TestCase):
    def test_connection_string_is_removed_and_error_code_remains(self):
        leaked = (
            "Login failed (40532) DRIVER={ODBC Driver 17 for SQL Server};"
            f"SERVER=sql.example.internal;UID={USERNAME};Authentication=ActiveDirectoryInteractive;"
        )
        message = sanitize_database_message(leaked, USERNAME)
        self.assertIn("(40532)", message)
        self.assertNotIn(USERNAME, message)
        self.assertNotIn("PWD=", message)
        self.assertNotIn("UID=", message)
        self.assertNotIn("DRIVER=", message)
        self.assertIn("Connection details were omitted", message)

    def test_example_file_has_placeholders_only(self):
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for name in ("DB_SERVER", "DB_DATABASE", "DB_USERNAME", "DB_DRIVER", "DB_CONNECTION_TIMEOUT"):
            self.assertIn(name, example)
        self.assertNotIn("DB_PASSWORD", example)
        self.assertNotIn("DB_PORT", example)
        self.assertIn("DB_CONNECTION_TIMEOUT=300", example)
        self.assertNotIn(USERNAME, example)


class ConnectionFactoryTests(unittest.TestCase):
    def test_factory_passes_timeout_and_does_not_log_the_connection_string(self):
        seen = {}

        def connector(connection_string, timeout):
            seen["connection_string"] = connection_string
            seen["timeout"] = timeout
            return FakeRaw()

        config = _config()
        connection = database_connection(config, connector=connector)
        connection.close()
        text = seen["connection_string"]
        self.assertEqual(seen["timeout"], 300)
        self.assertIn("SERVER=sql.example.internal;", text)
        self.assertNotIn(",1433", text)
        self.assertIn(f"UID={USERNAME};", text)
        self.assertIn("Authentication=ActiveDirectoryInteractive;", text)
        self.assertIn("Encrypt=yes;", text)
        self.assertIn("TrustServerCertificate=yes;", text)
        self.assertIn("Connection Timeout=300;", text)
        self.assertNotIn("PWD=", text)
        self.assertNotIn("Trusted_Connection", text)
        self.assertNotIn("Integrated Security", text)
        self.assertEqual(text.count("Authentication="), 1)
        self.assertNotIn(text, repr(config))
        self.assertNotIn(USERNAME, repr(config))

    def test_open_failure_is_sanitized(self):
        def connector(connection_string, timeout):
            raise RuntimeError(f"(40532) {connection_string}")

        with self.assertRaises(Exception) as caught:
            database_connection(_config(), connector=connector)
        message = str(caught.exception)
        self.assertIn("(40532)", message)
        self.assertNotIn(USERNAME, message)
        self.assertNotIn("PWD=", message)
        self.assertNotIn("DRIVER=", message)
        self.assertNotIn("Authentication=", message)

    def test_context_manager_closes_on_success_and_on_error(self):
        raw = FakeRaw()
        with database_connection(_config(), connector=lambda *args, **kwargs: raw):
            self.assertFalse(raw.closed)
        self.assertTrue(raw.closed)

        raw_error = FakeRaw()

        def connector(connection_string, timeout):
            return raw_error

        with self.assertRaises(RuntimeError):
            with open_database(_config(), connector=connector):
                raise RuntimeError("query failed")
        self.assertTrue(raw_error.closed)

    def test_close_error_is_sanitized(self):
        raw = FakeRaw()
        raw.fail_close = True
        connection = database_connection(_config(), connector=lambda *args, **kwargs: raw)
        with self.assertRaises(Exception) as caught:
            connection.close()
        self.assertIn("(40532)", str(caught.exception))
        self.assertNotIn(USERNAME, str(caught.exception))
        self.assertNotIn("PWD=", str(caught.exception))


class QueryTests(unittest.TestCase):
    def _client(self, raw=None):
        raw = raw or FakeRaw()
        connection = database_connection(_config(), connector=lambda *args, **kwargs: raw)
        return DatabaseClient(connection), raw

    def test_probe_file_loads_and_is_read_only(self):
        sql = load_query_file("discovery/probe.sql")
        self.assertIn("SELECT 1 AS probe", sql)
        assert_read_only(sql)
        self.assertNotIn("Enrollments_", sql)

    def test_extraction_contract_is_not_executable_and_names_no_from_table(self):
        sql = load_query_file("enrollment/extraction_contract.sql")
        self.assertIn("coverage_year", sql)
        self.assertIn("enrollment_confirmation_date", sql)
        self.assertNotIn("FROM ", sql.upper())
        with self.assertRaises(ReadOnlyQueryError):
            assert_read_only(sql)
        for column in CANDIDATE_ENROLLMENT_COLUMNS:
            self.assertIn(column, sql)

    def test_select_accepted_and_writes_rejected(self):
        assert_read_only("SELECT 1 AS probe")
        assert_read_only("WITH src AS (SELECT 1 AS n) SELECT n FROM src")
        assert_read_only("SELECT 'INSERT' AS example")  # word is inside a string
        for statement in (
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "ALTER TABLE t ADD a INT",
            "TRUNCATE TABLE t",
            "MERGE INTO t USING s ON 1 = 1",
            "CREATE TABLE t (a INT)",
            "SELECT * INTO t FROM src",
            "SELECT 1; DROP TABLE t",
        ):
            with self.assertRaises(ReadOnlyQueryError):
                assert_read_only(statement)

    def test_parameters_are_forwarded_not_interpolated(self):
        client, raw = self._client()
        try:
            result = client.execute_query(
                "SELECT 1 AS connection_ok, ? AS label",
                params=("issuer-1",),
            )
        finally:
            client._connection.close()
        self.assertEqual(raw.cursor_obj.executed, ("SELECT 1 AS connection_ok, ? AS label", ("issuer-1",)))
        self.assertNotIn("issuer-1", raw.cursor_obj.executed[0])
        self.assertIsInstance(result, QueryResult)
        self.assertEqual(result.columns, ("connection_ok", "label"))
        self.assertEqual(result.rows, ((1, "ok"),))
        self.assertEqual(result.row_count, 1)
        self.assertGreaterEqual(result.duration_seconds, 0)

    def test_query_file_execution_uses_the_loaded_text(self):
        client, raw = self._client()
        try:
            result = client.execute_query_file("discovery/probe.sql")
        finally:
            client._connection.close()
        self.assertIn("SELECT 1 AS probe", raw.cursor_obj.executed[0])
        self.assertIsNone(raw.cursor_obj.executed[1])
        self.assertEqual(result.row_count, 1)
        self.assertTrue(raw.cursor_obj.closed)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(Exception) as caught:
            load_query_file("../config/settings.py")
        self.assertNotIn(USERNAME, str(caught.exception))

    def test_driver_error_keeps_the_code_and_hides_the_username(self):
        raw = FakeRaw(fail_message=f"login failed for {USERNAME} (40532)")
        client, _raw = self._client(raw)
        try:
            with self.assertRaises(Exception) as caught:
                client.execute_query("SELECT 1 AS connection_ok")
        finally:
            client._connection.close()
        message = str(caught.exception)
        self.assertIn("(40532)", message)
        self.assertNotIn(USERNAME, message)
        self.assertNotIn("PWD=", message)


class CheckCommandTests(unittest.TestCase):
    def test_pass_line_uses_a_mock_connection(self):
        raw = FakeRaw()
        config = _config()
        with mock.patch("database.check_connection.load_database_config", return_value=config), \
                mock.patch("database.check_connection.database_connection") as opener, \
                mock.patch("sys.stdout", new_callable=StringIO) as stdout:
            opener.return_value = database_connection(config, connector=lambda *args, **kwargs: raw)
            code = check_connection_main()
        self.assertEqual(code, 0)
        self.assertIn("Database connection: PASS", stdout.getvalue())
        self.assertNotIn(USERNAME, stdout.getvalue())
        self.assertNotIn("PWD=", stdout.getvalue())
        self.assertTrue(raw.closed)

    def test_failure_line_names_a_missing_variable_and_not_a_secret(self):
        with mock.patch(
            "database.check_connection.load_database_config",
            side_effect=DatabaseConfigurationError("Missing required database configuration: DB_SERVER"),
        ), mock.patch("sys.stdout", new_callable=StringIO) as stdout:
            code = check_connection_main()
        text = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Database connection: FAILED", text)
        self.assertIn("DB_SERVER", text)
        self.assertNotIn(USERNAME, text)
        self.assertNotIn("PWD=", text)


class IsolationTests(unittest.TestCase):
    def test_env_is_gitignored_and_example_is_not(self):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=PROJECT_ROOT,
            check=False,
        )
        example = subprocess.run(
            ["git", "check-ignore", "-q", ".env.example"],
            cwd=PROJECT_ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertNotEqual(example.returncode, 0)
        self.assertTrue(ENV_FILE.name == ".env")

    def test_main_does_not_reference_the_database_source(self):
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("database", source)
        self.assertNotIn("DB_PASSWORD", source)
        self.assertNotIn("check_connection", source)
        self.assertNotIn("pyodbc", source)

    def test_importing_main_does_not_import_the_connection_check(self):
        sys.modules.pop("database.check_connection", None)
        import main  # noqa: F401
        self.assertNotIn("database.check_connection", sys.modules)

    def test_approved_sisense_metric_identity_is_unchanged(self):
        self.assertEqual(METRIC_NAME, "currently_enrolled_people")
        self.assertEqual(PLAN_YEAR, 2027)

    def test_candidate_columns_are_not_queried(self):
        discovery = load_query_file("discovery/probe.sql")
        for column in ("hios_issuer_id", "enrollee_id", "coverage_year"):
            self.assertNotIn(column, discovery)


if __name__ == "__main__":
    unittest.main()
