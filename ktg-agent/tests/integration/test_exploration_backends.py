"""Opt-in REAL MySQL 5.7 / Docker checks; isolated fixtures, never business schemas.

Set KTG_EXPLORATION_BACKENDS=1; requires local ktg-mes-mysql and prebuilt
ktg-analysis:local. Missing prerequisites fail rather than silently mock success.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from time import monotonic
from typing import Any, cast
from uuid import uuid4

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.core.agent import CancellationToken
from ktg_agent.exploration.compute import ComputeRunner
from ktg_agent.exploration.scope import SHOWCASE_PROFILE
from ktg_agent.exploration.sql import SQLConnection, SQLExecutor, SQLPolicyError
from ktg_agent.exploration.sql_views import SQLScopePolicy, available_views, view_statements
from ktg_agent.ktg.client import KTGError

pytestmark = pytest.mark.skipif(os.environ.get("KTG_EXPLORATION_BACKENDS") != "1",
                              reason="Explicit opt-in required for isolated backend fixtures")


def _docker(*args: str, data: str | None = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=data, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def _admin(sql: str) -> str:
    result = _docker("exec", "-i", "ktg-mes-mysql", "sh", "-c",
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4 --batch --skip-column-names',
        data=sql)
    if result.returncode:
        # Statements can contain generated passwords. Never include stdin/stderr.
        raise RuntimeError("Isolated SQL fixture operation failed; details suppressed")
    return result.stdout.strip()


@dataclass
class Database:
    schema: str
    username: str
    password: str = field(repr=False)
    views: dict[str, tuple[str, ...]] = field(default_factory=dict)
    connections: list[Any] = field(default_factory=list, repr=False)

    def connect(self) -> SQLConnection:
        driver = importlib.import_module("pymysql")
        connection: Any = driver.connect(host="127.0.0.1", port=3306, user=self.username,
            password=self.password, database=self.schema, charset="utf8mb4", autocommit=True,
            connect_timeout=3, read_timeout=3, write_timeout=3)
        self.connections.append(connection)
        return cast(SQLConnection, connection)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    suffix = uuid4().hex
    schema, username = "ktg_query_test_" + suffix, "ktg_qt_" + suffix[:20]
    password = secrets.token_urlsafe(32)
    assert schema.startswith("ktg_query_test_") and username.startswith("ktg_qt_")
    database_created = account_created = False
    try:
        assert _admin("SELECT VERSION();").startswith("5.7.")
        _admin(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4;")
        database_created = True
        _admin(f"CREATE USER '{username}'@'%' IDENTIFIED BY '{password}';")
        account_created = True
        _admin(f"""CREATE TABLE `{schema}`.`md_item` (
            item_id BIGINT PRIMARY KEY, item_code VARCHAR(64), item_name VARCHAR(255),
            specification VARCHAR(255), item_type_id BIGINT, item_type_code VARCHAR(64),
            item_type_name VARCHAR(255), item_or_product VARCHAR(16), unit_of_measure VARCHAR(32), enable_flag VARCHAR(8)
        );
        CREATE TABLE `{schema}`.`pro_workorder` (
            workorder_id BIGINT PRIMARY KEY, workorder_code VARCHAR(64), workorder_name VARCHAR(255),
            product_id BIGINT, product_code VARCHAR(64), product_name VARCHAR(255), product_spc VARCHAR(255),
            client_id BIGINT, client_code VARCHAR(64), client_name VARCHAR(255), quantity DECIMAL(16,4),
            quantity_produced DECIMAL(16,4), quantity_changed DECIMAL(16,4), quantity_scheduled DECIMAL(16,4),
            unit_of_measure VARCHAR(32), request_date DATE, status VARCHAR(32), version BIGINT
        );
        INSERT INTO `{schema}`.`md_item` (item_id,item_code,item_name) VALUES (1,'R-1','Rotor'),(2,'R-2','Valve');
        INSERT INTO `{schema}`.`pro_workorder` (workorder_id,workorder_code,product_id,quantity) VALUES
            (1,'WO-1',1,10.125),(2,'WO-2',1,20.250),(3,'WO-3',2,5.500);
        """)
        # A bounded fixture large enough for real expensive-query cancellation.
        values = ",".join(f"({key},'N-{key}','fixture')" for key in range(3, 1003))
        _admin(f"INSERT INTO `{schema}`.`md_item` (item_id,item_code,item_name) VALUES {values};")
        permissions = frozenset({"mes:md:mditem:list", "mes:pro:workorder:list"})
        policy = SQLScopePolicy(SHOWCASE_PROFILE, ("1",), frozenset({"md.item", "pro.workorder"}))
        specs = available_views(permissions, policy)
        statements = view_statements(schema, permissions, policy)
        for spec, statement in zip(specs, statements, strict=True):
            _admin(statement + f"; GRANT SELECT ON `{schema}`.`{spec.view_name}` TO '{username}'@'%';")
        fixture = Database(schema, username, password, {spec.view_name: spec.columns for spec in specs})
        yield fixture
        assert all(not connection.open for connection in fixture.connections)
    finally:
        # Exact generated names + prefix guards: never touch demo/acceptance schemas.
        if not schema.startswith("ktg_query_test_") or not username.startswith("ktg_qt_"):
            raise RuntimeError("Refusing cleanup outside the generated test namespace")
        if database_created:
            _admin(f"DROP DATABASE `{schema}`;")
        if account_created:
            _admin(f"DROP USER '{username}'@'%';")
        if database_created:
            assert _admin(f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{schema}';") == "0"
        if account_created:
            assert _admin(f"SELECT COUNT(*) FROM mysql.user WHERE User='{username}' AND Host='%';") == "0"


def test_real_sql_free_join_and_exact_decimal_aggregate(database: Database) -> None:
    executor = SQLExecutor(database.connect, database=database.schema)
    result = asyncio.run(executor.execute(
        "SELECT i.code,SUM(w.quantity) AS total FROM agent_md_item i "
        "JOIN agent_pro_workorder w ON i.id=w.product_id WHERE i.code LIKE :prefix "
        "GROUP BY i.code ORDER BY i.code", {"prefix": "R-%"}, views=database.views))
    assert result["columns"] == ["code", "total"]
    assert result["rows"] == [["R-1", "30.3750"], ["R-2", "5.5000"]]
    assert result["coverage"] == "complete"


def test_real_explain_never_expands_select_only_account_privileges(database: Database) -> None:
    executor = SQLExecutor(database.connect, database=database.schema)
    try:
        result = asyncio.run(executor.execute("SELECT code FROM agent_md_item WHERE id=:id",
            {"id": 1}, views=database.views, mode="explain"))
    except SQLPolicyError as unavailable:
        assert unavailable.code == "SQL_EXPLAIN_UNAVAILABLE"
        print("REAL_EXPLAIN: SELECT-only view account lacks required EXPLAIN privilege; explicitly unavailable, no grants added")
    else:
        assert result["mode"] == "explain" and str(result["applied_sql"]).startswith("EXPLAIN SELECT ")
        assert "select_type" in cast(list[str], result["columns"])
        assert isinstance(result["rows"], list) and result["rows"]
        print("REAL_EXPLAIN: actual MySQL optimizer plan returned with existing SELECT-only view grants")
    connection = database.connect()
    try:
        cursor: Any = connection.cursor()
        cursor.execute("SHOW GRANTS")
        grants = " ".join(str(row[0]) for row in cursor.fetchall())
        assert "SHOW VIEW" not in grants and "ALL PRIVILEGES" not in grants
        assert "FILE" not in grants and "WITH GRANT OPTION" not in grants
        cursor.close()
    finally:
        connection.close()


def test_real_account_has_view_select_only_and_cannot_access_base_or_system(database: Database) -> None:
    connection = database.connect()
    try:
        cursor: Any = connection.cursor()
        cursor.execute("SHOW GRANTS")
        grants = " ".join(str(row[0]) for row in cursor.fetchall())
        assert "ALL PRIVILEGES" not in grants and "WITH GRANT OPTION" not in grants and "FILE" not in grants
        assert "agent_md_item" in grants and "agent_pro_workorder" in grants
        for statement in ("SELECT * FROM md_item", "SELECT * FROM mysql.user",
                          "UPDATE md_item SET item_name='denied' WHERE item_id=1"):
            with pytest.raises(Exception) as denied:
                cursor.execute(statement)
            assert denied.value.args[0] in (1142, 1143)
        cursor.execute("SELECT LOAD_FILE('/etc/passwd')")
        assert cursor.fetchone()[0] is None
        cursor.close()
    finally:
        connection.close()


@pytest.mark.parametrize("query", ["SELECT * FROM md_item", "SELECT * FROM mysql.user",
    "DELETE FROM agent_md_item", "SELECT code INTO OUTFILE '/tmp/ktg_query_forbidden' FROM agent_md_item",
    "SELECT LOAD_FILE('/etc/passwd') FROM agent_md_item"])
def test_real_executor_rejects_escape_before_opening_connection(database: Database, query: str) -> None:
    before = len(database.connections)
    executor = SQLExecutor(database.connect, database=database.schema)
    with pytest.raises(SQLPolicyError):
        asyncio.run(executor.execute(query, {}, views=database.views))
    assert len(database.connections) == before


def test_real_sql_timeout_cancel_and_recovery(database: Database) -> None:
    heavy = "SELECT COUNT(*) FROM agent_md_item a JOIN agent_md_item b ON a.id<>b.id JOIN agent_md_item c ON b.id<>c.id"

    async def check() -> None:
        timed = SQLExecutor(database.connect, database=database.schema, timeout_seconds=0.1)
        started = monotonic()
        with pytest.raises((TimeoutError, SQLPolicyError)):
            await timed.execute(heavy, {}, views=database.views)
        assert monotonic() - started < 4
        executor = SQLExecutor(database.connect, database=database.schema, timeout_seconds=3)
        task = asyncio.create_task(executor.execute(heavy, {}, views=database.views))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Give the driver's closed read thread a chance to finish cleanup.
        for _ in range(50):
            if all(not connection.open for connection in database.connections):
                break
            await asyncio.sleep(0.02)
        assert all(not connection.open for connection in database.connections)
        # A closed client socket is not proof that MySQL stopped computing.
        deadline = monotonic() + 0.7
        active = "1"
        while active != "0" and monotonic() < deadline:
            active = _admin("SELECT COUNT(*) FROM information_schema.PROCESSLIST "
                            f"WHERE USER='{database.username}' AND COMMAND='Query';")
            if active != "0":
                await asyncio.sleep(0.05)
        assert active == "0", "Cancelled SQL is still executing on MySQL"
        recovered = await executor.execute("SELECT COUNT(*) AS n FROM agent_pro_workorder", {}, views=database.views)
        assert recovered["rows"] == [[3]]

    asyncio.run(check())


def _analysis_code(body: str) -> str:
    return "def analyze(datasets):\n" + "\n".join("    " + line for line in body.splitlines())


def test_real_compute_decimal_analysis() -> None:
    data: JsonObject = {"input": {"columns": [{"name": "cost", "type": "decimal"}],
                                 "rows": [["0.10"], ["0.20"]], "coverage": "complete"}}
    code = _analysis_code('from decimal import Decimal\ntotal=sum(Decimal(row[0]) for row in datasets["input"]["rows"])\n'
                          'return {"summary":str(total),"tables":[],"artifacts":[],"warnings":[]}')
    result = asyncio.run(ComputeRunner().analyze(data, code))
    assert result["summary"] == "0.30" and result["coverage"] == "complete"


def test_real_compute_cannot_read_host_secret_or_use_external_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "host-secret.txt"
    secret_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    monkeypatch.setenv("KTG_TEST_HOST_SECRET", secrets.token_urlsafe(32))
    code = _analysis_code('import os,socket,pathlib,json\n'
        'checks={"nonroot":os.getuid()==65532,"host_env_absent":os.environ.get("KTG_TEST_HOST_SECRET") is None}\n'
        f'checks["host_file_absent"]=not pathlib.Path({str(secret_file)!r}).exists()\n'
        'try:\n    socket.create_connection(("1.1.1.1",443),timeout=0.2)\n    checks["no_network"]=False\n'
        'except OSError:\n    checks["no_network"]=True\n'
        'try:\n    pathlib.Path("/opt/forbidden-output").write_text("blocked")\n    checks["rootfs_readonly"]=False\n'
        'except OSError:\n    checks["rootfs_readonly"]=True\n'
        'checks["root_mount_readonly"]=any(line.split()[1]=="/" and "ro" in line.split()[3].split(",") for line in pathlib.Path("/proc/mounts").read_text().splitlines())\n'
        'memory_path=next(pathlib.Path(p) for p in ("/sys/fs/cgroup/memory.max","/sys/fs/cgroup/memory/memory.limit_in_bytes") if pathlib.Path(p).is_file())\n'
        'pids_path=next(pathlib.Path(p) for p in ("/sys/fs/cgroup/pids.max","/sys/fs/cgroup/pids/pids.max") if pathlib.Path(p).is_file())\n'
        'checks["memory_limit"]=memory_path.read_text().strip()=="134217728"\n'
        'checks["process_limit"]=pids_path.read_text().strip()=="16"\n'
        'return {"summary":json.dumps(checks),"tables":[],"artifacts":[],"warnings":[]}')
    result = asyncio.run(ComputeRunner().analyze({}, code))
    assert isinstance(result["summary"], str)
    assert all(value is True for value in json.loads(result["summary"]).values())


def test_real_compute_timeout_cleanup_and_recovery() -> None:
    runner = ComputeRunner(timeout_seconds=0.8)
    started = monotonic()
    with pytest.raises(KTGError) as expired:
        asyncio.run(runner.analyze({}, _analysis_code("while True:\n    pass")))
    assert expired.value.code == "COMPUTE_TIMEOUT"
    assert monotonic() - started < 6
    recovered = asyncio.run(ComputeRunner().analyze({}, _analysis_code(
        'return {"summary":"recovered","tables":[],"artifacts":[],"warnings":[]}')))
    assert recovered["summary"] == "recovered"


def test_real_compute_memory_limit_is_enforced() -> None:
    with pytest.raises(KTGError) as limited:
        asyncio.run(ComputeRunner(timeout_seconds=6).analyze({}, _analysis_code(
            'memory=bytearray(256*1024*1024)\nreturn {"summary":"unbounded","tables":[],"artifacts":[],"warnings":[]}')))
    assert limited.value.code == "COMPUTE_FAILED"


def test_real_compute_cancellation_removes_container() -> None:
    names: list[str] = []

    class TrackedRunner(ComputeRunner):
        def command(self, name: str) -> list[str]:
            names.append(name)
            return super().command(name)

    async def check() -> None:
        cancellation = CancellationToken()
        task = asyncio.create_task(TrackedRunner().analyze({}, _analysis_code("while True:\n    pass"), cancellation))
        await asyncio.sleep(0.5)
        cancellation.cancel()
        with pytest.raises(KTGError) as cancelled:
            await task
        assert cancelled.value.code == "RUN_CANCELLED"

    asyncio.run(check())
    assert names
    for name in names:
        remaining = _docker("ps", "-aq", "--filter", "name=^" + name + "$")
        assert remaining.returncode == 0 and not remaining.stdout.strip()
