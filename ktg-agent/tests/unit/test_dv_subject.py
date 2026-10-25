"""Isolated subject CRUD contracts; never connects to a business MES."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import sleep
from pathlib import Path
from typing import cast
from threading import Thread
from http.client import HTTPConnection
import json

import pytest

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.dv_subject import LIST_NAME, DETAIL_NAME, build_dv_subject_tools
from ktg_agent.ktg.dv_subject_writes import (
    APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE, PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE,
    DvSubjectPreviewStore, build_dv_subject_write_tools,
)
from ktg_agent.server import Settings, create_service, AgentHTTPServer
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.tools import CatalogIdentity, ToolContext, ToolStatus
from ktg_agent.tools.registry import ToolChannel, ToolDefinition, ToolResult

SUBJECT = {"subjectId": 10, "subjectCode": "S10", "subjectName": "检查油位", "subjectType": "CHECK",
           "enableFlag": "Y", "subjectContent": "查看油位", "subjectStandard": "正常", "remark": ""}


class FakeMes:
    def __init__(self) -> None:
        self.rows = [dict(SUBJECT)]
        self.reads: list[tuple[str, dict[str, object]]] = []
        self.writes: list[tuple[str, dict[str, object]]] = []
        self.permissions = [f"mes:dv:dvsubject:{p}" for p in ("list", "query", "add", "edit", "remove")]
        self.user_id = 7
        self.failure = False

    async def read(self, url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        self.reads.append((url, dict(params)))
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": self.user_id}, "permissions": self.permissions}
        if url.endswith("/list"):
            rows = [dict(r) for r in self.rows if all(
                (str(v) in str(r.get(k, "")) if k == "subjectName" else r.get(k) == v)
                for k, v in params.items() if k not in {"pageNum", "pageSize"})]
            size, page = int(params.get("pageSize", 20)), int(params.get("pageNum", 1))
            return {"code": 200, "rows": rows[(page-1)*size:page*size], "total": len(rows)}
        sid = int(url.rsplit("/", 1)[-1])
        return {"code": 200, "data": next((dict(r) for r in self.rows if r["subjectId"] == sid), None)}

    async def write(self, method: str, url: str, headers: Mapping[str, str], body: Mapping[str, object], timeout: float) -> object:
        self.writes.append((method, dict(body)))
        if self.failure:
            return {"code": 500, "msg": "项目编码已存在！"}
        if method == "POST":
            self.rows.append({**body, "subjectId": 11})
        elif method == "PUT":
            next(r for r in self.rows if r["subjectId"] == body["subjectId"]).update(body)
        else:
            self.rows = [r for r in self.rows if r["subjectId"] != int(url.rsplit("/", 1)[-1])]
        return {"code": 200}

    def client(self) -> KTGClient:
        return KTGClient(base_url="http://localhost", authorization="Bearer fixture",
                         transport=self.read, write_transport=self.write)


def run(tool: ToolDefinition, mes: FakeMes, args: dict[str, object], *, trusted: bool = False,
        user: int = 7) -> ToolResult:
    async def invoke() -> ToolResult:
        return await tool.execute(ToolContext(run_id="r", tool_call_id="c", identity={"userId": user},
            ktg_client=mes.client(), channel=ToolChannel.TRUSTED_CONFIRMATION if trusted else ToolChannel.MODEL,
            operation_id=str(args.get("operation_id")) if trusted else None,
            confirmation_id="confirm" if trusted else None), args)
    return asyncio.run(invoke())


def tools(store: DvSubjectPreviewStore) -> dict[str, ToolDefinition]:
    return {t.name: t for t in (*build_dv_subject_tools(), *build_dv_subject_write_tools(store))}


def test_read_filters_paging_and_code_ambiguity() -> None:
    mes, ts = FakeMes(), tools(DvSubjectPreviewStore())
    result = run(ts[LIST_NAME], mes, {"subject_code": "S10", "subject_name": "油", "subject_type": "CHECK",
                                     "enable_flag": "Y", "page": 1, "page_size": 5})
    assert result.details["total"] == 1
    assert mes.reads[-1][1] == {"subjectCode": "S10", "subjectName": "油", "subjectType": "CHECK",
                                      "enableFlag": "Y", "pageNum": 1, "pageSize": 5}
    assert run(ts[DETAIL_NAME], mes, {"subject_id": 10}).details["subject"]["subjectId"] == 10
    mes.rows.append({**SUBJECT, "subjectId": 12})
    assert run(ts[DETAIL_NAME], mes, {"subject_code": "S10"}).error_code == "AMBIGUOUS_MATCH"


@pytest.mark.parametrize("args", [{"page": True}, {"page_size": 101}, {"subject_type": "BAD"}, {"enable_flag": "yes"}, {"unknown": 1}])
def test_invalid_reads_do_not_call_backend(args: dict[str, object]) -> None:
    mes = FakeMes()
    assert run(tools(DvSubjectPreviewStore())[LIST_NAME], mes, args).error_code == "VALIDATION_ERROR"
    assert not mes.reads


def test_create_preview_is_pure_and_confirmation_is_idempotent() -> None:
    mes, store = FakeMes(), DvSubjectPreviewStore()
    ts = tools(store)
    preview = run(ts[PREVIEW_CREATE], mes, {"subject_code": "NEW", "subject_type": "MAINTEN", "subject_content": "保养"})
    assert preview.status is ToolStatus.SUCCEEDED and not mes.writes
    args = {"operation_id": preview.operation_id}
    assert run(ts[APPLY_CREATE], mes, args).error_code == "CONFIRMATION_REQUIRED"
    assert run(ts[APPLY_CREATE], mes, args, trusted=True).details["verified"] is True
    assert run(ts[APPLY_CREATE], mes, args, trusted=True).status is ToolStatus.SUCCEEDED
    assert len(mes.writes) == 1


@pytest.mark.parametrize("args", [{"subject_code": "X"}, {"subject_code": "X", "subject_type": "CHECK", "subject_content": ""},
    {"subject_code": "X", "subject_type": "BAD", "subject_content": "X"},
    {"subject_code": "X", "subject_type": "CHECK", "subject_content": "X", "subjectId": 1}])
def test_create_validation_and_allowlist(args: dict[str, object]) -> None:
    mes = FakeMes()
    assert run(tools(DvSubjectPreviewStore())[PREVIEW_CREATE], mes, args).error_code == "VALIDATION_ERROR"
    assert not mes.writes


def test_update_conflict_duplicate_code_and_backend_failure() -> None:
    mes, ts = FakeMes(), tools(DvSubjectPreviewStore())
    p = run(ts[PREVIEW_UPDATE], mes, {"subject_id": 10, "subject_name": "新名称"})
    mes.rows[0]["subjectContent"] = "别人改了"
    assert run(ts[APPLY_UPDATE], mes, {"operation_id": p.operation_id}, trusted=True).error_code == "PREVIEW_STALE"
    assert not mes.writes
    mes.rows.append({**SUBJECT, "subjectId": 12})
    assert run(ts[PREVIEW_UPDATE], mes, {"subject_id": 10, "subject_name": "新名称"}).error_code == "SUBJECT_CODE_EXISTS"
    mes.rows.pop()
    p = run(ts[PREVIEW_UPDATE], mes, {"subject_id": 10, "subject_name": "新名称"})
    mes.failure = True
    assert run(ts[APPLY_UPDATE], mes, {"operation_id": p.operation_id}, trusted=True).status is ToolStatus.FAILED


def test_delete_identity_ttl_and_single_record_readback(tmp_path) -> None:
    mes, store = FakeMes(), DvSubjectPreviewStore(path=tmp_path / "subjects.sqlite3", ttl_seconds=.02)
    ts = tools(store)
    p = run(ts[PREVIEW_DELETE], mes, {"subject_id": 10})
    assert p.details["warnings"] and not mes.writes
    assert run(ts[APPLY_DELETE], mes, {"operation_id": p.operation_id}, trusted=True, user=8).error_code == "PREVIEW_IDENTITY_MISMATCH"
    sleep(.03)
    assert run(ts[APPLY_DELETE], mes, {"operation_id": p.operation_id}, trusted=True).error_code == "PREVIEW_NOT_FOUND"
    p = run(ts[PREVIEW_DELETE], mes, {"subject_id": 10})
    assert run(ts[APPLY_DELETE], mes, {"operation_id": p.operation_id}, trusted=True).details["verified"] is True
    assert not mes.rows


def service_settings(tmp_path: Path, *, enable_dv_subject_write: bool = False,
                     dv_subject_db_path: str = "") -> Settings:
    return Settings(model_api_key="fixture", model_base_url="https://example.test/v1", model_id="fixture",
        ktg_base_url="http://localhost", session_db_path=str(tmp_path / "sessions.sqlite3"),
        workorder_db_path=str(tmp_path / "workorders.sqlite3"), enable_dv_subject_write=enable_dv_subject_write,
        dv_subject_db_path=dv_subject_db_path)


def test_registration_hidden_boundary_and_trusted_reauthorization(tmp_path) -> None:
    assert create_service(service_settings(tmp_path))._dv_subject_store is None
    mes = FakeMes()
    service = create_service(service_settings(tmp_path, enable_dv_subject_write=True))
    service._ktg_client_factory = lambda auth: mes.client()
    store = service._dv_subject_store
    assert store and store.path.endswith("agent-dv-subject.sqlite3")
    snapshot = service._catalog.candidate_snapshot(CatalogIdentity(frozenset({"*:*:*"}), "run"), service._switches_provider())
    assert {LIST_NAME, DETAIL_NAME, PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE} <= set(snapshot.candidates)
    assert not {APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE} & set(snapshot.candidates)
    p = run(tools(store)[PREVIEW_DELETE], mes, {"subject_id": 10})
    mes.permissions = ["mes:dv:dvsubject:query"]
    denied = asyncio.run(service.confirm_dv_subject(preview_id=p.operation_id, authorization="Bearer fixture"))
    assert denied["result"]["outcome"] == "failed" and not mes.writes


def test_confirm_http_ignores_claimed_identity_and_replays_without_second_delete(tmp_path: Path) -> None:
    mes = FakeMes()
    service = create_service(service_settings(tmp_path, enable_dv_subject_write=True))
    service._ktg_client_factory = lambda auth: mes.client()
    assert service._dv_subject_store
    p = run(tools(service._dv_subject_store)[PREVIEW_DELETE], mes, {"subject_id": 10})
    sessions = SessionService(SessionRepository(tmp_path / "http.sqlite3"), lambda auth: mes.client())
    server = AgentHTTPServer(("127.0.0.1", 0), service, sessions)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def confirm() -> dict[str, object]:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("POST", "/agent-api/v1/dv-subject/confirm",
            body=json.dumps({"preview_id": p.operation_id, "userId": 7, "permissions": ["*:*:*"], "channel": "trusted_confirmation"}),
            headers={"Authorization": "Bearer fixture", "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        assert response.status == 200
        return cast(dict[str, object], payload)

    try:
        mes.user_id = 8
        assert cast(dict[str, object], confirm()["result"])["error_code"] == "PREVIEW_IDENTITY_MISMATCH"
        assert not mes.writes
        mes.user_id = 7
        assert cast(dict[str, object], confirm()["result"])["outcome"] == "succeeded"
        assert cast(dict[str, object], confirm()["result"])["outcome"] == "succeeded"
        assert len(mes.writes) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_readback_failure_after_write_is_unknown_and_never_reposts() -> None:
    mes, ts = FakeMes(), tools(DvSubjectPreviewStore())
    p = run(ts[PREVIEW_CREATE], mes, {"subject_code": "NEW", "subject_type": "CHECK", "subject_content": "x"})
    original = mes.read

    async def broken_read(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        if mes.writes:
            return {"code": 200, "rows": "invalid", "total": 1}
        return await original(url, headers, params, timeout)

    mes.read = broken_read  # type: ignore[method-assign]
    args = {"operation_id": p.operation_id}
    assert run(ts[APPLY_CREATE], mes, args, trusted=True).status is ToolStatus.UNKNOWN
    assert run(ts[APPLY_CREATE], mes, args, trusted=True).status is ToolStatus.UNKNOWN
    assert len(mes.writes) == 1


def test_store_path_cannot_alias_sessions(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must differ"):
        create_service(service_settings(tmp_path, enable_dv_subject_write=True,
            dv_subject_db_path=str(tmp_path / "sessions.sqlite3")))
