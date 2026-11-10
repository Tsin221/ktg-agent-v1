"""Isolated tool type Agent contracts using a fake MES transport."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from threading import Thread
from http.client import HTTPConnection
import json
from dataclasses import replace
from time import sleep
import sqlite3

import pytest

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.tm_tooltype import (
    LIST_NAME, DETAIL_NAME, OPTIONS_NAME, build_tm_tooltype_tools,
)
from ktg_agent.ktg.tm_tooltype_writes import (
    PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE, APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE,
    TmToolTypePreviewStore, build_tm_tooltype_write_tools,
)
from ktg_agent.server import AgentHTTPServer, Settings, create_service
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.tools.discovery import MODULES
from ktg_agent.tools import CatalogIdentity
from ktg_agent.tools import ToolContext, ToolRegistry, ToolRunner, ToolStatus
from ktg_agent.tools.registry import ToolChannel, ToolDefinition, ToolResult


ROW = {"toolTypeId": 10, "toolTypeCode": "TT001", "toolTypeName": "扭力扳手",
       "codeFlag": "Y", "maintenType": "REGULAR", "maintenPeriod": 30, "remark": "检定"}


def test_page_context_accepts_only_known_module_values() -> None:
    assert "tm" in MODULES
    assert SessionService._page_context({"route_name": "Tooltype", "module": "tm"}) == {
        "route_name": "Tooltype", "module": "tm",
    }
    with pytest.raises(ValueError):
        SessionService._page_context({"route_name": "Tooltype", "module": "unknown"})
    with pytest.raises(ValueError):
        SessionService._page_context({"route_name": "Tooltype", "module": ["tm"]})


class FakeMES:
    def __init__(self) -> None:
        self.rows = [dict(ROW)]
        self.tools: list[dict[str, object]] = []
        self.reads: list[tuple[str, dict[str, object]]] = []
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.permissions = ["mes:tm:tooltype:list", "mes:tm:tooltype:query", "mes:tm:tooltype:add",
            "mes:tm:tooltype:edit", "mes:tm:tooltype:remove", "mes:tm:tool:list"]
        self.user_id = 7
        self.invalid_write_response = False
        self.ignore_write_effect = False
        self.invalid_delete_readback = False
        self.fail_delete_list_readback = False

    async def read(self, url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        self.reads.append((url, dict(params)))
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": self.user_id}, "permissions": self.permissions}
        if url.endswith("/system/dict/data/type/sys_yes_no"):
            return {"code": 200, "data": [{"dictValue": "Y", "dictLabel": "是"}, {"dictValue": "N", "dictLabel": "否"}]}
        if url.endswith("/system/dict/data/type/mes_mainten_type"):
            return {"code": 200, "data": [{"dictValue": "REGULAR", "dictLabel": "定期"}, {"dictValue": "USAGE", "dictLabel": "按使用次数"}]}
        if url.endswith("/mes/tm/tool/list"):
            linked = [dict(row) for row in self.tools if row.get("toolTypeId") == params.get("toolTypeId")]
            return {"code": 200, "rows": linked, "total": len(linked)}
        if url.endswith("/mes/tm/tooltype/list"):
            if (self.fail_delete_list_readback and params.get("toolTypeCode") == ROW["toolTypeCode"]
                    and len(self.writes) and self.writes[-1][0] == "DELETE"):
                raise KTGError("KTG_HTTP_ERROR", "list unavailable")
            if (self.invalid_delete_readback and params.get("toolTypeCode") == ROW["toolTypeCode"]
                    and len(self.writes) and self.writes[-1][0] == "DELETE"):
                return {"rows": [], "total": "0"}
            filtered = [dict(row) for row in self.rows if all(
                str(value) in str(row.get(key, "")) if key == "toolTypeName" else
                (str(row.get(key, "")).casefold() == str(value).casefold() if key == "toolTypeCode" else row.get(key) == value)
                for key, value in params.items() if key not in {"pageNum", "pageSize"})]
            page = int(params.get("pageNum", 1))
            size = int(params.get("pageSize", 20))
            return {"code": 200, "rows": filtered[(page - 1) * size:page * size], "total": len(filtered)}
        tool_type_id = int(url.rsplit("/", 1)[-1])
        row = next((dict(row) for row in self.rows if row["toolTypeId"] == tool_type_id), None)
        return {"code": 200, "data": row} if row is not None else {"code": 200, "msg": "操作成功"}

    async def write(self, method: str, url: str, headers: Mapping[str, str], body: Mapping[str, object], timeout: float) -> object:
        self.writes.append((method, url, dict(body)))
        if method == "POST" and not self.ignore_write_effect:
            self.rows.append({**body, "toolTypeId": 20})
        elif method == "PUT" and not self.ignore_write_effect:
            next(row for row in self.rows if row["toolTypeId"] == body["toolTypeId"]).update(body)
        elif method == "DELETE" and not self.ignore_write_effect:
            tool_type_id = int(url.rsplit("/", 1)[-1])
            self.rows = [row for row in self.rows if row["toolTypeId"] != tool_type_id]
        return "not-json" if self.invalid_write_response else {"code": 200}

    def client(self) -> KTGClient:
        return KTGClient(base_url="http://localhost", authorization="Bearer fake",
                         transport=self.read, write_transport=self.write)


def run(tool: ToolDefinition, mes: FakeMES, args: dict[str, object], *, trusted: bool = False, user: int = 7) -> ToolResult:
    async def invoke() -> ToolResult:
        return await tool.execute(ToolContext(run_id="r", tool_call_id="c", identity={"userId": user},
            ktg_client=mes.client(), channel=ToolChannel.TRUSTED_CONFIRMATION if trusted else ToolChannel.MODEL,
            operation_id=str(args.get("operation_id")) if trusted else None,
            confirmation_id="confirm" if trusted else None), args)
    return asyncio.run(invoke())


def test_read_filters_paging_detail_and_dictionary_options() -> None:
    mes = FakeMES()
    tools = {tool.name: tool for tool in build_tm_tooltype_tools()}
    result = run(tools[LIST_NAME], mes, {"tool_type_code": "TT001", "tool_type_name": "扭力",
                                        "mainten_type": "REGULAR", "page": 1, "page_size": 5})
    assert result.status is ToolStatus.SUCCEEDED
    assert "TT001" in result.content and "扭力扳手" in result.content
    assert result.details["total"] == 1
    assert result.details["rows"][0]["toolTypeId"] == 10
    assert mes.reads[-1] == ("http://localhost/mes/tm/tooltype/list", {
        "toolTypeCode": "TT001", "toolTypeName": "扭力", "maintenType": "REGULAR",
        "pageNum": 1, "pageSize": 5,
    })
    assert run(tools[DETAIL_NAME], mes, {"tool_type_code": "TT001"}).details["tool_type"]["toolTypeId"] == 10
    assert "扭力扳手" in run(tools[DETAIL_NAME], mes, {"tool_type_code": "tt001"}).content
    options = run(tools[OPTIONS_NAME], mes, {})
    assert options.status is ToolStatus.SUCCEEDED
    assert {row["value"] for row in options.details["options"]["code_flags"]} == {"Y", "N"}
    assert {row["value"] for row in options.details["options"]["maintenance_types"]} == {"REGULAR", "USAGE"}
    assert "REGULAR" in options.content and "2147483647" in options.content


def test_detail_refuses_duplicate_codes_and_invalid_arguments_fail_closed() -> None:
    mes = FakeMES()
    tools = {tool.name: tool for tool in build_tm_tooltype_tools()}
    mes.rows.append({**ROW, "toolTypeId": 11})
    assert run(tools[DETAIL_NAME], mes, {"tool_type_code": "TT001"}).error_code == "AMBIGUOUS_MATCH"
    before = len(mes.reads)
    invalid = run(tools[LIST_NAME], mes, {"page_size": 101})
    assert invalid.error_code == "VALIDATION_ERROR"
    assert len(mes.reads) == before


def test_list_content_reports_when_page_rows_are_not_all_expanded() -> None:
    mes = FakeMES()
    mes.rows = [{**ROW, "toolTypeId": index, "toolTypeCode": f"TT{index:03d}"} for index in range(1, 51)]
    result = run({tool.name: tool for tool in build_tm_tooltype_tools()}[LIST_NAME], mes,
                 {"page_size": 100})
    assert "本页共50条，以上展示30条，另有20条未展开" in result.content
    assert "缩小page_size或继续分页查询" in result.content


@pytest.mark.parametrize(("name", "arguments"), [
    (DETAIL_NAME, {}),
    (DETAIL_NAME, {"tool_type_id": 10, "tool_type_code": "TT001"}),
    (DETAIL_NAME, {"tool_type_id": "10"}),
    (DETAIL_NAME, {"tool_type_id": 9223372036854775808}),
    (DETAIL_NAME, {"tool_type_code": " "}),
    (DETAIL_NAME, {"toolTypeId": 10}),
    (LIST_NAME, {"tool_type_name": " "}),
    (LIST_NAME, {"page": 1000001}),
    (LIST_NAME, {"code_flag": "YES"}),
    (OPTIONS_NAME, {"tool_type_id": 10}),
])
def test_read_runner_rejects_invalid_arguments_before_mes(name: str, arguments: JsonObject) -> None:
    mes = FakeMES()
    runner = ToolRunner(ToolRegistry(build_tm_tooltype_tools()))
    result = asyncio.run(runner.run(ToolCall("call", name, arguments),
                                    ToolContext("run", "call", ktg_client=mes.client())))
    assert result.error_code == "VALIDATION_ERROR"
    assert result.details is not None and "path" in result.details
    assert mes.reads == []


@pytest.mark.parametrize("arguments", [{"tool_type_id": 10}, {"tool_type_code": "TT001"}])
def test_detail_runner_accepts_either_locator(arguments: JsonObject) -> None:
    mes = FakeMES()
    runner = ToolRunner(ToolRegistry(build_tm_tooltype_tools()))
    result = asyncio.run(runner.run(ToolCall("call", DETAIL_NAME, arguments),
                                    ToolContext("run", "call", ktg_client=mes.client())))
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None and result.details["tool_type"] == ROW


def write_tools(store: TmToolTypePreviewStore) -> dict[str, ToolDefinition]:
    return {tool.name: tool for tool in build_tm_tooltype_write_tools(store)}


def test_create_preview_then_trusted_confirm_is_verified_and_idempotent(tmp_path) -> None:
    mes, store = FakeMES(), TmToolTypePreviewStore(path=tmp_path / "tooltype.sqlite3")
    tools = write_tools(store)
    args = {"tool_type_code": "LUNA-T10-A", "tool_type_name": "Luna测试类型",
            "code_flag": "Y", "mainten_type": "REGULAR", "mainten_period": 7, "remark": "本轮测试"}
    preview = run(tools[PREVIEW_CREATE], mes, args)
    assert preview.status is ToolStatus.SUCCEEDED and not mes.writes
    assert preview.details["after"]["toolTypeCode"] == "LUNA-T10-A"
    assert preview.operation_id in preview.content and "LUNA-T10-A" in preview.content and "7天" in preview.content
    assert run(tools[APPLY_CREATE], mes, {"operation_id": preview.operation_id}).error_code == "CONFIRMATION_REQUIRED"
    confirmed = run(tools[APPLY_CREATE], mes, {"operation_id": preview.operation_id}, trusted=True)
    replayed = run(tools[APPLY_CREATE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert confirmed.details["outcome"] == "succeeded" and confirmed.details["verified"] is True
    assert replayed.details == confirmed.details
    assert len(mes.writes) == 1


def test_create_validation_and_update_merge_only_whitelisted_fields() -> None:
    mes = FakeMES()
    mes.rows.append({**ROW, "toolTypeId": 11, "toolTypeCode": "TT011", "toolTypeName": "其他工具类型"})
    mes.rows.append({**ROW, "toolTypeId": 12, "toolTypeCode": "TT012", "toolTypeName": "其他工具类型副本"})
    tools = write_tools(TmToolTypePreviewStore())
    invalid = {"tool_type_code": "X", "tool_type_name": "新类型", "code_flag": "Y",
               "mainten_type": "REGULAR", "mainten_period": 1.5}
    assert run(tools[PREVIEW_CREATE], mes, invalid).error_code == "VALIDATION_ERROR"
    assert run(tools[PREVIEW_CREATE], mes, {"tool_type_code": "TT001", "tool_type_name": "另名", "code_flag": "N"}).error_code == "TOOLTYPE_DUPLICATE"
    assert run(tools[PREVIEW_CREATE], mes, {"tool_type_code": "TT013", "tool_type_name": "其他工具类型", "code_flag": "N"}).error_code == "TOOLTYPE_DUPLICATE"
    assert run(tools[PREVIEW_CREATE], mes, {"tool_type_code": "TT013", "tool_type_name": "新名称", "code_flag": []}).error_code == "VALIDATION_ERROR"
    assert run(tools[PREVIEW_CREATE], mes, {"tool_type_code": "TT013", "tool_type_name": "新名称", "code_flag": "N"}).status is ToolStatus.SUCCEEDED
    assert not mes.writes
    preview = run(tools[PREVIEW_UPDATE], mes, {"tool_type_id": 10, "tool_type_name": "更新扭力扳手"})
    assert preview.status is ToolStatus.SUCCEEDED
    assert preview.details["after"]["toolTypeCode"] == ROW["toolTypeCode"]
    assert preview.details["after"]["maintenPeriod"] == ROW["maintenPeriod"]
    assert run(tools[APPLY_UPDATE], mes, {"operation_id": preview.operation_id}, trusted=True).details["verified"] is True
    assert mes.rows[0]["toolTypeName"] == "更新扭力扳手"
    assert mes.rows[0]["maintenPeriod"] == 30


def test_n_flag_normalizes_maintenance_fields_to_null() -> None:
    mes = FakeMES()
    tools = write_tools(TmToolTypePreviewStore())
    args = {"tool_type_code": "LUNA-T10-N", "tool_type_name": "无编码管理", "code_flag": "N",
            "mainten_type": "REGULAR", "mainten_period": 9}
    preview = run(tools[PREVIEW_CREATE], mes, args)
    assert preview.status is ToolStatus.SUCCEEDED
    assert preview.details["after"]["maintenType"] is None
    assert preview.details["after"]["maintenPeriod"] is None
    assert "维护类型与周期均为无" in preview.content


def test_update_preserves_unprovided_text_exactly() -> None:
    mes = FakeMES()
    mes.rows[0]["toolTypeName"] = " 扭力扳手 "
    mes.rows[0]["remark"] = " 检定 "
    tools = write_tools(TmToolTypePreviewStore())
    preview = run(tools[PREVIEW_UPDATE], mes, {"tool_type_id": 10, "mainten_period": 45})
    assert preview.status is ToolStatus.SUCCEEDED
    assert preview.details["after"]["toolTypeName"] == " 扭力扳手 "
    assert preview.details["after"]["remark"] == " 检定 "
    result = run(tools[APPLY_UPDATE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert result.details["verified"] is True
    assert mes.rows[0]["toolTypeName"] == " 扭力扳手 "
    assert mes.rows[0]["remark"] == " 检定 "


def test_update_rejects_historical_invalid_code_flag_before_preview() -> None:
    mes = FakeMES()
    mes.rows[0]["codeFlag"] = "X"
    result = run(write_tools(TmToolTypePreviewStore())[PREVIEW_UPDATE], mes,
                 {"tool_type_id": 10, "remark": "修订"})
    assert result.error_code == "VALIDATION_ERROR"


@pytest.mark.parametrize("action,preview_name,apply_name,args", [
    ("create", PREVIEW_CREATE, APPLY_CREATE, {"tool_type_code": "LUNA-UNKNOWN", "tool_type_name": "待核新增",
        "code_flag": "Y", "mainten_type": "REGULAR", "mainten_period": 1}),
    ("update", PREVIEW_UPDATE, APPLY_UPDATE, {"tool_type_id": 10, "mainten_period": 31}),
    ("delete", PREVIEW_DELETE, APPLY_DELETE, {"tool_type_id": 10}),
])
def test_invalid_write_response_is_unknown_and_never_reposted(action, preview_name, apply_name, args) -> None:
    mes, store = FakeMES(), TmToolTypePreviewStore()
    tools = write_tools(store)
    preview = run(tools[preview_name], mes, args)
    mes.invalid_write_response = True
    first = run(tools[apply_name], mes, {"operation_id": preview.operation_id}, trusted=True)
    write_count = len(mes.writes)
    replay = run(tools[apply_name], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert first.status is ToolStatus.UNKNOWN
    assert replay.details == first.details
    assert len(mes.writes) == write_count == 1


def test_delete_is_blocked_by_tool_references_at_preview_and_confirmation() -> None:
    mes = FakeMES()
    tools = write_tools(TmToolTypePreviewStore())
    mes.tools.append({"toolId": 91, "toolTypeId": 10, "toolCode": "TOOL-91"})
    blocked = run(tools[PREVIEW_DELETE], mes, {"tool_type_id": 10})
    assert blocked.error_code == "TOOLTYPE_IN_USE"
    assert not mes.writes
    mes.tools.clear()
    preview = run(tools[PREVIEW_DELETE], mes, {"tool_type_id": 10})
    mes.tools.append({"toolId": 91, "toolTypeId": 10, "toolCode": "TOOL-91"})
    confirmed = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert confirmed.details["outcome"] == "failed"
    assert not mes.writes


def service_settings(tmp_path: Path, *, enable_tm_tooltype_write: bool = False, tm_tooltype_db_path: str = "") -> Settings:
    return Settings(model_api_key="fake", model_base_url="https://example.test/v1", model_id="fake",
        ktg_base_url="http://localhost", session_db_path=str(tmp_path / "sessions.sqlite3"),
        workorder_db_path=str(tmp_path / "workorders.sqlite3"),
        enable_tm_tooltype_write=enable_tm_tooltype_write, tm_tooltype_db_path=tm_tooltype_db_path)


def test_service_registers_tm_tools_in_module_with_hidden_confirmation_boundary(tmp_path: Path) -> None:
    default = create_service(service_settings(tmp_path))
    assert default._tm_tooltype_store is None
    mes = FakeMES()
    service = create_service(service_settings(tmp_path, enable_tm_tooltype_write=True))
    service._ktg_client_factory = lambda authorization: mes.client()
    assert service._tm_tooltype_store is not None
    assert service._tm_tooltype_store.path.endswith("agent-tm-tooltype.sqlite3")
    permissions = frozenset(mes.permissions + ["mes:tm:tooltype:add", "mes:tm:tooltype:edit",
        "mes:tm:tooltype:remove", "mes:tm:tool:list"])
    snapshot = service._catalog.candidate_snapshot(CatalogIdentity(permissions, "run"), service._switches_provider())
    assert {LIST_NAME, DETAIL_NAME, OPTIONS_NAME, PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE} <= set(snapshot.candidates)
    assert not {APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE} & set(snapshot.candidates)


def test_tooltype_preview_store_cannot_alias_sessions_or_other_preview_db(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must differ"):
        create_service(service_settings(tmp_path, enable_tm_tooltype_write=True,
            tm_tooltype_db_path=str(tmp_path / "sessions.sqlite3")))
    shared_path = str(tmp_path / "shared.sqlite3")
    settings = replace(service_settings(tmp_path, enable_tm_tooltype_write=True,
        tm_tooltype_db_path=shared_path), enable_dv_subject_write=True, dv_subject_db_path=shared_path)
    with pytest.raises(RuntimeError, match="must differ"):
        create_service(settings)


def test_http_confirmation_rechecks_identity_permission_and_replays_once(tmp_path: Path) -> None:
    mes = FakeMES()
    service = create_service(service_settings(tmp_path, enable_tm_tooltype_write=True))
    service._ktg_client_factory = lambda authorization: mes.client()
    assert service._tm_tooltype_store is not None
    preview = run(write_tools(service._tm_tooltype_store)[PREVIEW_CREATE], mes, {
        "tool_type_code": "LUNA-T10-HTTP", "tool_type_name": "HTTP确认测试", "code_flag": "N"})
    sessions = SessionService(SessionRepository(tmp_path / "http-sessions.sqlite3"), lambda authorization: mes.client())
    server = AgentHTTPServer(("127.0.0.1", 0), service, sessions)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def confirm() -> dict[str, object]:
        return confirm_id(preview.operation_id)

    def confirm_id(preview_id: str | None) -> dict[str, object]:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("POST", "/agent-api/v1/tm-tooltype/confirm",
            body=json.dumps({"preview_id": preview_id}),
            headers={"Authorization": "Bearer fake", "Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        assert response.status == 200
        return result["result"]

    try:
        mes.user_id = 8
        assert confirm()["error_code"] == "PREVIEW_IDENTITY_MISMATCH"
        assert not mes.writes
        mes.user_id = 7
        mes.permissions.remove("mes:tm:tooltype:add")
        assert confirm()["error_code"] == "PERMISSION_DENIED"
        assert not mes.writes
        mes.permissions.append("mes:tm:tooltype:add")
        first = confirm()
        replay = confirm()
        assert first["outcome"] == "succeeded" and first["verified"] is True
        assert replay == first and len(mes.writes) == 1
        delete_preview = run(write_tools(service._tm_tooltype_store)[PREVIEW_DELETE], mes,
                             {"tool_type_id": first["created"]["toolTypeId"]})
        mes.permissions.remove("mes:tm:tool:list")
        assert confirm_id(delete_preview.operation_id)["error_code"] == "PERMISSION_DENIED"
        assert len(mes.writes) == 1
        mes.permissions.append("mes:tm:tool:list")
        deleted = confirm_id(delete_preview.operation_id)
        assert deleted["outcome"] == "succeeded" and len(mes.writes) == 2
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_update_snapshot_change_expires_and_tampered_digest_fail_closed(tmp_path: Path) -> None:
    mes = FakeMES()
    expiring = TmToolTypePreviewStore(path=tmp_path / "expiry.sqlite3", ttl_seconds=0.01)
    update = write_tools(expiring)
    preview = run(update[PREVIEW_UPDATE], mes, {"tool_type_id": 10, "mainten_period": 31})
    sleep(0.02)
    assert run(update[APPLY_UPDATE], mes, {"operation_id": preview.operation_id}, trusted=True).error_code == "PREVIEW_NOT_FOUND"

    store = TmToolTypePreviewStore(path=tmp_path / "tamper.sqlite3")
    tools = write_tools(store)
    tampered = run(tools[PREVIEW_UPDATE], mes, {"tool_type_id": 10, "mainten_period": 31})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tm_tooltype_previews SET after_json='{}' WHERE preview_id=?", (tampered.operation_id,))
    assert run(tools[APPLY_UPDATE], mes, {"operation_id": tampered.operation_id}, trusted=True).error_code == "PREVIEW_STALE"

    stale = run(tools[PREVIEW_UPDATE], mes, {"tool_type_id": 10, "mainten_period": 31})
    mes.rows[0]["remark"] = "外部变更"
    result = run(tools[APPLY_UPDATE], mes, {"operation_id": stale.operation_id}, trusted=True)
    assert result.error_code == "PREVIEW_STALE"
    assert not mes.writes


def test_readback_mismatch_is_unknown_and_replay_does_not_write_twice() -> None:
    mes = FakeMES()
    tools = write_tools(TmToolTypePreviewStore())
    preview = run(tools[PREVIEW_CREATE], mes, {"tool_type_code": "LUNA-NO-READBACK",
        "tool_type_name": "回读失败", "code_flag": "N"})
    mes.ignore_write_effect = True
    first = run(tools[APPLY_CREATE], mes, {"operation_id": preview.operation_id}, trusted=True)
    replay = run(tools[APPLY_CREATE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert first.status is ToolStatus.UNKNOWN
    assert replay.details == first.details
    assert len(mes.writes) == 1


def test_delete_missing_malformed_readback_is_unknown_and_not_reissued() -> None:
    mes = FakeMES()
    tools = write_tools(TmToolTypePreviewStore())
    preview = run(tools[PREVIEW_DELETE], mes, {"tool_type_id": 10})
    mes.invalid_delete_readback = True
    first = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    replay = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert first.status is ToolStatus.UNKNOWN
    assert replay.details == first.details
    assert len(mes.writes) == 1


def test_delete_standard_ajax_success_without_data_is_verified_by_exact_code_list() -> None:
    mes = FakeMES()
    tools = write_tools(TmToolTypePreviewStore())
    preview = run(tools[PREVIEW_DELETE], mes, {"tool_type_id": 10})
    result = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    replay = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert result.details["outcome"] == "succeeded" and result.details["verified"] is True
    assert replay.details == result.details
    assert len(mes.writes) == 1
    assert any(url.endswith("/mes/tm/tooltype/list") and params.get("toolTypeCode") == ROW["toolTypeCode"]
               for url, params in mes.reads)


@pytest.mark.parametrize("failure_mode", ["malformed", "unavailable"])
def test_delete_list_readback_failure_is_unknown_and_not_reissued(failure_mode: str) -> None:
    mes = FakeMES()
    tools = write_tools(TmToolTypePreviewStore())
    preview = run(tools[PREVIEW_DELETE], mes, {"tool_type_id": 10})
    if failure_mode == "malformed":
        mes.invalid_delete_readback = True
    else:
        mes.fail_delete_list_readback = True
    first = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    replay = run(tools[APPLY_DELETE], mes, {"operation_id": preview.operation_id}, trusted=True)
    assert first.status is ToolStatus.UNKNOWN
    assert replay.details == first.details
    assert len(mes.writes) == 1
