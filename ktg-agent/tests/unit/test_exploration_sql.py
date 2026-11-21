"""The policy rejects unsafe ASTs before obtaining a database connection."""
import asyncio
import threading
from collections.abc import Mapping
from decimal import Decimal

import pytest

from ktg_agent.exploration.sql import SQLExecutor, SQLPolicyError, validate_sql


VIEWS = {"agent_workorder": ("id", "code", "quantity"), "agent_task": ("id", "workorder_id")}


@pytest.mark.parametrize("sql", [
    "SELECT w.code, COUNT(t.id) AS n FROM agent_workorder w LEFT JOIN agent_task t ON w.id=t.workorder_id GROUP BY w.code",
    "SELECT code FROM agent_workorder WHERE id IN (SELECT workorder_id FROM agent_task)",
    "SELECT id FROM agent_workorder UNION ALL SELECT workorder_id FROM agent_task",
    "SELECT code FROM (SELECT code FROM agent_workorder) AS selected ORDER BY code",
    "SELECT COUNT(*) AS total FROM agent_workorder",
    "SELECT id FROM agent_workorder w WHERE EXISTS (SELECT id FROM agent_task t WHERE t.workorder_id=w.id) OR quantity > 5",
])
def test_legitimate_compositions_are_not_restricted_to_fixed_templates(sql: str) -> None:
    prepared = validate_sql(sql, {}, views=VIEWS)
    assert prepared.sql
    assert prepared.parameters == {}


def test_parameters_are_bound_and_wildcards_expand_only_authorized_columns() -> None:
    prepared = validate_sql("SELECT * FROM agent_workorder WHERE code=:code", {"code": "x' OR 1=1 --"}, views=VIEWS)
    assert "%(code)s" in prepared.sql
    assert " OR 1=1" not in prepared.sql
    assert "*" not in prepared.sql
    assert "quantity" in prepared.sql


@pytest.mark.parametrize("sql", [
    "DELETE FROM agent_workorder", "SELECT id FROM agent_workorder; SELECT 1",
    "SELECT id INTO OUTFILE '/tmp/leak' FROM agent_workorder",
    "SELECT LOAD_FILE('/etc/passwd')", "SELECT SLEEP(5)", "SELECT BENCHMARK(100000,1)",
    "SELECT @x:=1", "SELECT @@version", "SELECT id FROM agent_workorder FOR UPDATE",
    "SELECT id FROM information_schema.tables", "SELECT id FROM other.agent_workorder",
    "SELECT secret FROM agent_workorder", "SELECT w.secret FROM agent_workorder w",
    "SELECT id FROM agent_workorder WHERE id IN (SELECT secret FROM agent_task)",
    "SELECT id FROM agent_workorder UNION SELECT password FROM mysql.user",
    "SELECT w.* FROM missing_view w", "SELECT id FROM agent_workorder -- harmless",
    "SELECT /*!50000 SLEEP(5) */ 1", "WITH x AS (SELECT id FROM agent_task) SELECT * FROM x",
    "SELECT ROW_NUMBER() OVER () FROM agent_workorder", "SELECT custom_udf(id) FROM agent_workorder",
])
def test_unsafe_queries_fail_closed_before_execution(sql: str) -> None:
    with pytest.raises(SQLPolicyError):
        validate_sql(sql, {}, views=VIEWS)


def test_missing_extra_or_unsafe_parameter_types_are_rejected() -> None:
    for parameters in ({}, {"code": "x", "extra": 2}, {"code": {"nested": "x"}}):
        with pytest.raises(SQLPolicyError):
            validate_sql("SELECT code FROM agent_workorder WHERE code=:code", parameters, views=VIEWS)


class Cursor:
    description: tuple[tuple[object, ...], ...] | None = (("quantity",),)

    def __init__(self, rows: list[tuple[object, ...]], database: str = "ktg_demo") -> None:
        self.rows = rows
        self.database = database
        self.calls: list[tuple[str, Mapping[str, object] | None]] = []
        self.closed = False

    def execute(self, sql: str, parameters: Mapping[str, object] | None = None) -> int:
        self.calls.append((sql, parameters))
        return 0

    def fetchone(self) -> tuple[object, ...]:
        return (self.database,)

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        return self.rows[:size]

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self.value = cursor
        self.closed = False

    def cursor(self) -> Cursor:
        return self.value

    def close(self) -> None:
        self.closed = True


def test_execution_enforces_database_scope_limits_and_preserves_decimal() -> None:
    cursor = Cursor([(Decimal("1.23"),), (Decimal("2.34"),), (Decimal("3.45"),)])
    connection = Connection(cursor)
    result = asyncio.run(SQLExecutor(lambda: connection, max_rows=2).execute(
        "SELECT quantity FROM agent_workorder", {}, views=VIEWS
    ))
    assert result["rows"] == [["1.23"], ["2.34"]]
    assert result["coverage"] == "partial"
    assert result["truncated"] is True
    assert cursor.calls[0][0] == "SELECT DATABASE()"
    assert any("MAX_EXECUTION_TIME" in sql for sql, _ in cursor.calls)
    assert connection.closed and cursor.closed


def test_wrong_database_is_rejected_and_connection_closed() -> None:
    cursor = Cursor([], database="production")
    connection = Connection(cursor)
    with pytest.raises(SQLPolicyError, match="数据库"):
        asyncio.run(SQLExecutor(lambda: connection).execute("SELECT id FROM agent_workorder", {}, views=VIEWS))
    assert not any("FROM" in sql for sql, _ in cursor.calls)
    assert connection.closed


def test_policy_denial_never_opens_connection() -> None:
    def forbidden_factory() -> Connection:
        raise AssertionError("The rejected SQL reached the database")

    with pytest.raises(SQLPolicyError):
        asyncio.run(SQLExecutor(forbidden_factory).execute("SELECT SLEEP(5)", {}, views=VIEWS))


def test_timeout_closes_active_connection_and_stops_blocking_read() -> None:
    released = threading.Event()

    class BlockingCursor(Cursor):
        def fetchmany(self, size: int) -> list[tuple[object, ...]]:
            assert released.wait(timeout=2)
            return []

    class InterruptibleConnection(Connection):
        def close(self) -> None:
            super().close()
            released.set()

    connection = InterruptibleConnection(BlockingCursor([]))
    with pytest.raises(TimeoutError):
        asyncio.run(SQLExecutor(lambda: connection, timeout_seconds=0.05).execute(
            "SELECT quantity FROM agent_workorder", {}, views=VIEWS
        ))
    assert connection.closed and released.is_set()


def test_driver_errors_do_not_expose_private_diagnostics() -> None:
    def broken_factory() -> Connection:
        raise RuntimeError("password=secret host=private")

    with pytest.raises(SQLPolicyError) as error:
        asyncio.run(SQLExecutor(broken_factory).execute("SELECT id FROM agent_workorder", {}, views=VIEWS))
    assert "secret" not in str(error.value)


def test_literal_percent_is_escaped_for_pyformat_binding() -> None:
    prepared = validate_sql("SELECT code FROM agent_workorder WHERE code LIKE 'R%' AND id=:id", {"id": 1}, views=VIEWS)
    assert "R%%" in prepared.sql
    assert "%(id)s" in prepared.sql


def test_explain_prefix_is_created_only_after_policy_validation() -> None:
    cursor = Cursor([(1,)])
    connection = Connection(cursor)
    result = asyncio.run(SQLExecutor(lambda: connection).execute(
        "SELECT quantity FROM agent_workorder WHERE id=:id", {"id": 1}, views=VIEWS, mode="explain"))
    applied = cursor.calls[-1]
    assert applied[0].startswith("EXPLAIN SELECT ") and applied[1] == {"id": 1}
    assert result["mode"] == "explain" and result["applied_sql"] == applied[0]
    assert "估算" in str(result["warnings"])
    assert cursor.closed and connection.closed


@pytest.mark.parametrize("mode,sql", [("analyze", "SELECT id FROM agent_workorder"),
    ("explain", "EXPLAIN SELECT id FROM agent_workorder"),
    ("explain", "SELECT * FROM mysql.user"), ("explain", "DELETE FROM agent_workorder")])
def test_invalid_explain_requests_fail_before_connection(mode: str, sql: str) -> None:
    def forbidden() -> Connection:
        raise AssertionError("Invalid mode or SQL opened a connection")
    with pytest.raises(SQLPolicyError):
        asyncio.run(SQLExecutor(forbidden).execute(sql, {}, views=VIEWS, mode=mode))


def test_explain_permission_failure_is_explicit_without_driver_diagnostics() -> None:
    class ExplainCursor(Cursor):
        def execute(self, sql: str, parameters: Mapping[str, object] | None = None) -> int:
            if sql.startswith("EXPLAIN "):
                raise RuntimeError(1345, "private underlying table and password=secret")
            return super().execute(sql, parameters)

    cursor = ExplainCursor([])
    connection = Connection(cursor)
    with pytest.raises(SQLPolicyError) as error:
        asyncio.run(SQLExecutor(lambda: connection).execute(
            "SELECT id FROM agent_workorder", {}, views=VIEWS, mode="explain"))
    assert error.value.code == "SQL_EXPLAIN_UNAVAILABLE"
    assert "secret" not in str(error.value) and "private" not in str(error.value)
    assert connection.closed and cursor.closed
