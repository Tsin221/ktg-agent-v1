"""AST-authorized MySQL 5.7 reads over caller-authorized views only.

The connection factory is trusted server configuration: it must supply a
dedicated SELECT-only account and finite driver connect/read/write timeouts.
No model-provided host, credentials, database or grants enter this module.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Protocol, cast
from uuid import uuid4

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from ktg_agent.core.messages import JsonObject, JsonValue


class SQLPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedSQL:
    sql: str
    parameters: Mapping[str, object]
    referenced_views: tuple[str, ...]


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXECUTION_SLOTS = threading.BoundedSemaphore(2)
_FUNCTIONS = frozenset({
    "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "IF", "NULLIF", "ABS",
    "ROUND", "FLOOR", "CEIL", "CEILING", "LENGTH", "CHAR_LENGTH", "LOWER", "UPPER",
    "TRIM", "LTRIM", "RTRIM", "CONCAT", "CONCAT_WS", "SUBSTRING", "SUBSTRING_INDEX",
    "DATE", "YEAR", "MONTH", "DAY", "DAY_OF_MONTH", "DATE_ADD", "DATE_SUB", "DATEDIFF",
    "DATE_DIFF", "TIMESTAMPDIFF", "TIMESTAMP_DIFF", "CAST", "TRY_CAST", "IFNULL",
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "TIME_TO_STR", "STR_TO_DATE",
    "AND", "OR", "EXISTS",
})
_FORBIDDEN = frozenset({
    "Into", "Lock", "Parameter", "SessionParameter", "PropertyEQ", "Window", "With",
    "CTE", "Qualify", "Lateral", "Pivot", "Unpivot", "TableSample", "Hint",
    "Command", "Set", "Insert", "Update", "Delete", "Create", "Drop", "Alter",
    "Merge", "Grant", "Revoke", "Transaction", "Commit", "Rollback", "Use",
    "Copy", "LoadData", "Fetch", "OffsetFetch", "Connect", "MatchAgainst",
})


def _reject(message: str, code: str = "SQL_POLICY_DENIED") -> SQLPolicyError:
    return SQLPolicyError(code, message)


def validate_sql(
    sql: str, parameters: Mapping[str, object], *,
    views: Mapping[str, tuple[str, ...]], database: str = "ktg_demo",
    max_rows: int | None = None,
) -> PreparedSQL:
    """Validate every AST scope; qualify/expand against authorized columns.

    Parameters are :name placeholders. Output is DB-API pyformat SQL; percent
    signs are escaped for that binding protocol, including in string literals.
    """
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 40000:
        raise _reject("SQL 为空或超过长度上限。")
    if not _NAME.fullmatch(database) or not views:
        raise _reject("没有可查询的授权视图。", "PERMISSION_DENIED")
    if any(not _NAME.fullmatch(view) or not columns or any(not _NAME.fullmatch(c) for c in columns)
           for view, columns in views.items()):
        raise _reject("授权视图配置无效。")
    try:
        statements = sqlglot.parse(sql, read="mysql")
    except (ParseError, SqlglotError, RecursionError) as exc:
        raise _reject("无法解析为受支持的 MySQL 5.7 查询。", "UNSUPPORTED_SQL_DIALECT") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise _reject("只允许单条 SELECT 查询。")
    tree = statements[0]
    names: set[str] = set()
    referenced: set[str] = set()
    nodes = list(tree.walk())
    if len(nodes) > 3000:
        raise _reject("查询表达式过于复杂。", "QUERY_TOO_EXPENSIVE")
    for node in nodes:
        if node.comments or type(node).__name__ in _FORBIDDEN:
            raise _reject("不允许注释、变量、副作用语法或 MySQL 5.7 不支持的结构。")
        if isinstance(node, exp.Query) and not isinstance(node, (exp.Select, exp.Union, exp.Subquery)):
            raise _reject("查询集合操作不受支持。", "UNSUPPORTED_SQL_DIALECT")
        if isinstance(node, exp.Select) and node.args.get("operation_modifiers"):
            raise _reject("不允许改变执行行为的 SELECT 修饰符。")
        if isinstance(node, exp.Distinct) and node.args.get("on"):
            raise _reject("DISTINCT ON 不属于 MySQL 5.7 方言。", "UNSUPPORTED_SQL_DIALECT")
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
            if name not in _FUNCTIONS:
                raise _reject("查询包含未授权函数。")
        if isinstance(node, exp.Table):
            if (not isinstance(node.this, exp.Identifier) or node.catalog
                    or node.db and node.db != database or node.name not in views
                    or any(value for key, value in node.args.items() if key not in {"this", "alias", "db", "catalog"})):
                raise _reject("查询引用了未授权视图或数据库。", "PERMISSION_DENIED")
            referenced.add(node.name)
            node.set("db", None)
        if isinstance(node, exp.Placeholder):
            name = node.name
            if not _NAME.fullmatch(name):
                raise _reject("只允许具名参数 :name。")
            names.add(name)
    if names != set(parameters):
        raise _reject("绑定参数必须与查询中的具名参数完全一致。", "VALIDATION_ERROR")
    for value in parameters.values():
        if not isinstance(value, (str, int, float, Decimal, date, datetime, type(None))):
            raise _reject("参数只能是标量、日期或空值。", "VALIDATION_ERROR")
        if isinstance(value, float) and not math.isfinite(value) or isinstance(value, Decimal) and not value.is_finite():
            raise _reject("数值参数必须有限。", "VALIDATION_ERROR")
    schema: dict[str, object] = {view: {column: "UNKNOWN" for column in columns} for view, columns in views.items()}
    try:
        qualified = qualify(tree, dialect="mysql", schema=schema, expand_stars=True,
                            validate_qualify_columns=True, allow_partial_qualification=False)
    except (OptimizeError, SqlglotError, RecursionError) as exc:
        raise _reject("列不存在、无权读取或来源不明确。", "PERMISSION_DENIED") from exc
    if max_rows is not None:
        limit = qualified.args.get("limit")
        existing = limit.expression if isinstance(limit, exp.Limit) else None
        if existing is not None and (not isinstance(existing, exp.Literal) or not existing.is_int):
            raise _reject("LIMIT 必须是非负整数字面量。", "VALIDATION_ERROR")
        amount = min(int(existing.this), max_rows + 1) if existing is not None else max_rows + 1
        if amount < 0:
            raise _reject("LIMIT 不能为负数。", "VALIDATION_ERROR")
        qualified = qualified.limit(amount)
    markers: dict[str, str] = {}
    for placeholder in list(qualified.find_all(exp.Placeholder)):
        marker = "ktg_bind_" + uuid4().hex
        markers[marker] = placeholder.name
        placeholder.replace(exp.Var(this=marker))
    bound_sql = qualified.sql(dialect="mysql").replace("%", "%%")
    for marker, name in markers.items():
        bound_sql = bound_sql.replace(marker, f"%({name})s")
    return PreparedSQL(bound_sql, dict(parameters), tuple(sorted(referenced)))


class SQLCursor(Protocol):
    @property
    def description(self) -> Sequence[Sequence[object]] | None: ...

    def execute(self, sql: str, parameters: Mapping[str, object] | None = None) -> object: ...
    def fetchone(self) -> Sequence[object] | None: ...
    def fetchmany(self, size: int) -> Sequence[Sequence[object]]: ...
    def close(self) -> None: ...


class SQLConnection(Protocol):
    def cursor(self) -> SQLCursor: ...
    def close(self) -> None: ...


def _json_cell(value: object, business_timezone: tzinfo) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _reject("数据库返回无效数值。", "INVALID_RESPONSE")
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, datetime):
        return (value if value.tzinfo is not None else value.replace(tzinfo=business_timezone)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise _reject("数据库返回不支持的列类型。", "INVALID_RESPONSE")


class SQLExecutor:
    def __init__(
        self, connection_factory: Callable[[], SQLConnection], *, database: str = "ktg_demo",
        max_rows: int = 200, timeout_seconds: float = 5, max_bytes: int = 1000000,
        business_timezone: tzinfo = timezone(timedelta(hours=8)),
    ) -> None:
        if not 1 <= max_rows <= 10000 or not 0 < timeout_seconds <= 60 or max_bytes < 1:
            raise ValueError("Invalid SQL execution limits")
        self._factory = connection_factory
        self._database = database
        self._rows = max_rows
        self._timeout = timeout_seconds
        self._bytes = max_bytes
        self._timezone = business_timezone

    async def execute(
        self, sql: str, parameters: Mapping[str, object], *, views: Mapping[str, tuple[str, ...]],
        mode: str = "execute",
    ) -> JsonObject:
        if not isinstance(mode, str) or mode not in {"execute", "explain"}:
            raise _reject("查询模式只能为 execute 或 explain。", "VALIDATION_ERROR")
        prepared = validate_sql(sql, parameters, views=views, database=self._database, max_rows=self._rows)
        # Only the server adds EXPLAIN, after validating the complete SELECT AST.
        # User-supplied EXPLAIN/ANALYZE prefixes never enter this execution path.
        applied_sql = "EXPLAIN " + prepared.sql if mode == "explain" else prepared.sql
        lock = threading.Lock()
        connections: list[SQLConnection] = []
        cancelled = threading.Event()

        def read() -> JsonObject:
            if cancelled.is_set():
                raise _reject("查询已取消。", "QUERY_CANCELLED")
            connection = self._factory()
            cursor: SQLCursor | None = None
            with lock:
                connections.append(connection)
            try:
                if cancelled.is_set():
                    raise _reject("查询已取消。", "QUERY_CANCELLED")
                cursor = connection.cursor()
                cursor.execute("SELECT DATABASE()")
                current = cursor.fetchone()
                if current is None or current[0] != self._database:
                    raise _reject("连接的数据库与授权目标数据库不一致。", "PERMISSION_DENIED")
                # Match the parser/generator's MySQL quote and escape rules.
                # In particular NO_BACKSLASH_ESCAPES cannot reinterpret a
                # safely serialized literal as executable SQL.
                cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,ONLY_FULL_GROUP_BY,NO_ENGINE_SUBSTITUTION'")
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {max(1, int(self._timeout * 1000))}")
                if cancelled.is_set():
                    raise _reject("查询已取消。", "QUERY_CANCELLED")
                cursor.execute(applied_sql, prepared.parameters)
                columns = [str(column[0]) for column in cursor.description or ()]
                raw = cursor.fetchmany(self._rows + 1)
                truncated = len(raw) > self._rows
                rows: list[JsonValue] = []
                size = 0
                for row in raw[:self._rows]:
                    if len(row) != len(columns):
                        raise _reject("数据库返回行与列不一致。", "INVALID_RESPONSE")
                    cells = [_json_cell(cell, self._timezone) for cell in row]
                    size += len(json.dumps(cells, ensure_ascii=False).encode("utf-8"))
                    if size > self._bytes:
                        truncated = True
                        break
                    rows.append(cells)
                return {
                    "kind": "readonly_sql", "mode": mode, "columns": cast(JsonValue, columns), "rows": rows,
                    "truncated": truncated, "coverage": "partial" if truncated else "complete",
                    "applied_sql": applied_sql, "source": {"database": self._database,
                        "views": cast(JsonValue, list(prepared.referenced_views))},
                    "warnings": (["这是优化器估算计划，不是实际执行结果或业务数量。"] if mode == "explain"
                                 else ["自定义SQL结果不自动认证为权威业务指标；范围以实际SQL为准。"]),
                }
            finally:
                try:
                    if cursor is not None:
                        cursor.close()
                finally:
                    connection.close()
                    with lock:
                        connections.clear()

        def work() -> JsonObject:
            if not _EXECUTION_SLOTS.acquire(timeout=self._timeout):
                raise _reject("查询并发已达到上限。", "QUERY_TOO_EXPENSIVE")
            try:
                return read()
            except SQLPolicyError:
                raise
            except Exception as exc:
                if (mode == "explain" and exc.args and isinstance(exc.args[0], int)
                        and exc.args[0] in {1142, 1143, 1227, 1345}):
                    raise _reject("当前只读身份不能解释该视图查询；未扩大权限或生成替代计划。",
                                  "SQL_EXPLAIN_UNAVAILABLE") from None
                # Driver exceptions can contain hosts, credentials or raw rows.
                raise _reject("数据库读取失败，请核对授权字段、MySQL 5.7 语法或查询成本。", "SQL_READ_FAILED") from None
            finally:
                _EXECUTION_SLOTS.release()

        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except (TimeoutError, asyncio.CancelledError):
            cancelled.set()
            with lock:
                active = tuple(connections)
            for connection in active:
                try:
                    await asyncio.to_thread(connection.close)
                except Exception:
                    pass
            # Retrieve late worker exceptions; close/read timeouts and database
            # SELECT timeout, not coroutine cancellation alone, bound the worker.
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            raise
