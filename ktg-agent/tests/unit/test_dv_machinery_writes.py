"""Mock-only contract tests for device ledger preview/confirmation writes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import sleep
from typing import cast

import pytest

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.dv_machinery_writes import (
    APPLY_CREATE, APPLY_DELETE, APPLY_UPDATE, PREVIEW_CREATE, PREVIEW_DELETE,
    PREVIEW_UPDATE, DvMachineryPreviewStore, build_dv_machinery_write_tools,
    dv_machinery_write_manifests,
)
from ktg_agent.server import Settings, create_service
from ktg_agent.tools import CatalogIdentity, ToolContext, ToolStatus
from ktg_agent.tools.registry import ToolChannel, ToolDefinition, ToolResult


TYPES = [
    {"machineryTypeId": 200, "machineryTypeCode": "ROOT", "machineryTypeName": "设备分类",
     "parentTypeId": 0, "enableFlag": "Y"},
    {"machineryTypeId": 201, "machineryTypeCode": "TYPE-1", "machineryTypeName": "压缩机",
     "parentTypeId": 200, "enableFlag": "Y"},
    {"machineryTypeId": 202, "machineryTypeCode": "TYPE-2", "machineryTypeName": "包装机",
     "parentTypeId": 200, "enableFlag": "Y"},
    {"machineryTypeId": 203, "machineryTypeCode": "TYPE-X", "machineryTypeName": "停用分类",
     "parentTypeId": 200, "enableFlag": "N"},
]
WORKSHOPS = [
    {"workshopId": 5, "workshopCode": "WS-5", "workshopName": "一车间", "enableFlag": "Y"},
    {"workshopId": 6, "workshopCode": "WS-6", "workshopName": "二车间", "enableFlag": "Y"},
    {"workshopId": 7, "workshopCode": "WS-X", "workshopName": "停用车间", "enableFlag": "N"},
]
DEVICE = {"machineryId": 10, "machineryCode": "EQ-10", "machineryName": "压缩机A",
          "machineryBrand": "Acme", "machinerySpec": "X-1", "machineryTypeId": 201,
          "machineryTypeCode": "TYPE-1", "machineryTypeName": "压缩机", "workshopId": 5,
          "workshopCode": "WS-5", "workshopName": "一车间", "status": "STOP", "remark": "原备注"}


class FakeMes:
    def __init__(self) -> None:
        self.devices = [dict(DEVICE)]
        self.reads: list[tuple[str, Mapping[str, object]]] = []
        self.writes: list[tuple[str, str, Mapping[str, object]]] = []
        self.write_response: dict[str, object] = {"code": 200, "msg": "ok"}

    async def read(self, url: str, _headers: Mapping[str, str], params: Mapping[str, object], _timeout: float) -> object:
        path = url.split("localhost", 1)[-1]
        self.reads.append((path, dict(params)))
        if path == "/getInfo":
            return {"code": 200, "user": {"userId": 7}, "permissions": [
                "mes:dv:machinery:add", "mes:dv:machinery:edit", "mes:dv:machinery:remove",
                "mes:dv:machinery:list", "mes:dv:machinery:query",
                "mes:dv:machinerytype:list", "mes:md:workshop:list",
            ]}
        if path == "/mes/dv/machinerytype/list":
            return {"code": 200, "data": [dict(item) for item in TYPES]}
        if path == "/mes/md/workshop/listAll":
            return {"code": 200, "data": [dict(item) for item in WORKSHOPS if item["enableFlag"] == "Y"]}
        if path == "/mes/dv/machinery/list":
            code = params.get("machineryCode")
            rows = [dict(item) for item in self.devices
                    if code is None or item["machineryCode"] == code]
            return {"code": 200, "total": len(rows), "rows": rows[:int(params.get("pageSize", 20))]}
        if path.startswith("/mes/dv/machinery/"):
            device_id = int(path.rsplit("/", 1)[-1])
            row = next((dict(item) for item in self.devices if item["machineryId"] == device_id), None)
            return {"code": 200, "data": row}
        raise AssertionError(f"unexpected mock GET: {path}")

    async def write(self, method: str, url: str, _headers: Mapping[str, str],
                    body: Mapping[str, object], _timeout: float) -> dict[str, object]:
        path = url.split("localhost", 1)[-1]
        payload = dict(body)
        self.writes.append((method, path, payload))
        if self.write_response.get("code", 200) != 200:
            return dict(self.write_response)
        if method == "POST":
            device = {**payload, "machineryId": max([int(row["machineryId"]) for row in self.devices] + [0]) + 1}
            self.devices.append(device)
            return {"code": 200, "data": device["machineryId"]}
        if method == "PUT":
            target = next(row for row in self.devices if row["machineryId"] == payload["machineryId"])
            target.update({key: value for key, value in payload.items() if key != "machineryId"})
            return {"code": 200, "data": 1}
        if method == "DELETE":
            device_id = int(path.rsplit("/", 1)[-1])
            self.devices = [row for row in self.devices if row["machineryId"] != device_id]
            return {"code": 200, "data": 1}
        raise AssertionError(f"unexpected mock write: {method}")

    def client(self) -> KTGClient:
        return KTGClient(base_url="http://localhost", authorization="Bearer test",
                         transport=self.read, write_transport=self.write)


def _defs(store: DvMachineryPreviewStore) -> dict[str, ToolDefinition]:
    return {item.name: item for item in build_dv_machinery_write_tools(store)}


def _run(tool: ToolDefinition, client: KTGClient, args: dict[str, object], *,
         operation_id: str | None = None, channel: ToolChannel = ToolChannel.MODEL) -> ToolResult:
    context = ToolContext(run_id="r", tool_call_id="c", operation_id=operation_id,
                          identity={"userId": 7}, ktg_client=client, channel=channel,
                          confirmation_id="trusted-confirm-id" if channel is ToolChannel.TRUSTED_CONFIRMATION else None)
    async def invoke() -> ToolResult:
        return await tool.execute(context, args)
    return asyncio.run(invoke())


def _apply(tool: ToolDefinition, mes: FakeMes, preview: ToolResult) -> ToolResult:
    assert preview.operation_id
    return _run(tool, mes.client(), {"operation_id": preview.operation_id},
                operation_id=preview.operation_id, channel=ToolChannel.TRUSTED_CONFIRMATION)


def test_preview_and_hidden_manifests_keep_confirmation_boundary() -> None:
    store = DvMachineryPreviewStore()
    tools = _defs(store)
    assert set(tools) == {PREVIEW_CREATE, APPLY_CREATE, PREVIEW_UPDATE, APPLY_UPDATE,
                          PREVIEW_DELETE, APPLY_DELETE}
    assert all(tools[name].expose_to_model for name in (PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE))
    assert all(not tools[name].expose_to_model for name in (APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE))
    manifests = {item.name: item for item in dv_machinery_write_manifests(tuple(tools.values()))}
    for name in (APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE):
        assert manifests[name].confirmation_policy == "trusted_confirmation"
        assert manifests[name].expose_to_model is False
    assert all(manifests[name].proposal_store_effect for name in (PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE))


def test_create_preview_validates_refs_and_apply_writes_once() -> None:
    mes, store = FakeMes(), DvMachineryPreviewStore()
    tools = _defs(store)
    args = {"machinery_code": "EQ-20", "machinery_name": "新压缩机", "machinery_type_id": 201,
            "workshop_id": 6, "machinery_brand": "Acme", "machinery_spec": "X-2", "status": "STOP"}
    preview = _run(tools[PREVIEW_CREATE], mes.client(), args)
    assert preview.status is ToolStatus.SUCCEEDED and not mes.writes
    assert preview.details["after"]["machineryTypeName"] == "压缩机"
    assert not any(path.startswith("/system/autocode") for path, _ in mes.reads)
    applied = _apply(tools[APPLY_CREATE], mes, preview)
    assert applied.status is ToolStatus.SUCCEEDED and applied.details["verified"] is True
    assert [item[0] for item in mes.writes] == ["POST"]
    repeated = _apply(tools[APPLY_CREATE], mes, preview)
    assert repeated.status is ToolStatus.SUCCEEDED and len(mes.writes) == 1


@pytest.mark.parametrize("args,code", [
    ({"machinery_code": "EQ-10", "machinery_name": "重复", "machinery_type_id": 201, "workshop_id": 5}, "MACHINERY_CODE_EXISTS"),
    ({"machinery_code": "EQ-20", "machinery_name": "新设备", "machinery_type_id": 200, "workshop_id": 5}, "TYPE_NOT_LEAF"),
    ({"machinery_code": "EQ-20", "machinery_name": "新设备", "machinery_type_id": 203, "workshop_id": 5}, "TYPE_DISABLED"),
    ({"machinery_code": "EQ-20", "machinery_name": "新设备", "machinery_type_id": 201, "workshop_id": 7}, "WORKSHOP_NOT_FOUND"),
    ({"machinery_code": "EQ-20", "machinery_name": "新设备", "machinery_type_id": 201, "workshop_id": 5, "status": "BAD"}, "VALIDATION_ERROR"),
])
def test_create_preview_rejects_invalid_or_ambiguous_business_fields(args: dict[str, object], code: str) -> None:
    result = _run(_defs(DvMachineryPreviewStore())[PREVIEW_CREATE], FakeMes().client(), args)
    assert result.error_code == code


def test_update_preview_canonicalizes_relation_ids_and_apply_checks_stale_snapshot() -> None:
    mes, store = FakeMes(), DvMachineryPreviewStore()
    tools = _defs(store)
    preview = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_id": 10, "machinery_type_id": 202, "workshop_id": 6, "status": "REPAIR",
    })
    assert preview.status is ToolStatus.SUCCEEDED and not mes.writes
    assert preview.details["after"]["machineryTypeName"] == "包装机"
    applied = _apply(tools[APPLY_UPDATE], mes, preview)
    assert applied.status is ToolStatus.SUCCEEDED
    assert mes.devices[0]["machineryTypeId"] == 202 and mes.devices[0]["status"] == "REPAIR"

    stale = _run(tools[PREVIEW_UPDATE], mes.client(), {"machinery_id": 10, "machinery_name": "新名称"})
    mes.devices[0]["status"] = "STOP"
    result = _apply(tools[APPLY_UPDATE], mes, stale)
    assert result.status is ToolStatus.FAILED and result.error_code == "PREVIEW_STALE"
    assert len(mes.writes) == 1


def test_update_rejects_code_change_unknown_fields_empty_name_and_noop() -> None:
    mes, tools = FakeMes(), _defs(DvMachineryPreviewStore())
    for args, code in (
        ({"machinery_id": 10, "machinery_code": "X"}, "VALIDATION_ERROR"),
        ({"machinery_id": 10, "machinery_name": ""}, "VALIDATION_ERROR"),
        ({"machinery_id": 10, "status": "BAD"}, "VALIDATION_ERROR"),
        ({"machinery_id": 10, "machinery_name": "压缩机A"}, "NO_CHANGE"),
    ):
        assert _run(tools[PREVIEW_UPDATE], mes.client(), args).error_code == code


def test_delete_preview_is_single_device_and_apply_verifies_absence() -> None:
    mes, tools = FakeMes(), _defs(DvMachineryPreviewStore())
    preview = _run(tools[PREVIEW_DELETE], mes.client(), {"machinery_id": 10})
    assert preview.details["warnings"]
    assert not mes.writes
    result = _apply(tools[APPLY_DELETE], mes, preview)
    assert result.status is ToolStatus.SUCCEEDED and result.details["verified"] is True
    assert mes.writes == [("DELETE", "/mes/dv/machinery/10", {})]
    assert not mes.devices


def test_apply_rejects_model_channel_and_expired_previews_without_writes() -> None:
    mes, store = FakeMes(), DvMachineryPreviewStore(ttl_seconds=0.01)
    tools = _defs(store)
    preview = _run(tools[PREVIEW_DELETE], mes.client(), {"machinery_id": 10})
    assert preview.operation_id
    model_apply = _run(tools[APPLY_DELETE], mes.client(), {"operation_id": preview.operation_id},
                       operation_id=preview.operation_id)
    assert model_apply.error_code == "CONFIRMATION_REQUIRED"
    sleep(0.02)
    expired = _apply(tools[APPLY_DELETE], mes, preview)
    assert expired.status is ToolStatus.FAILED and expired.error_code == "PREVIEW_NOT_FOUND"
    assert not mes.writes


def test_backend_business_error_is_not_reported_as_success() -> None:
    mes, tools = FakeMes(), _defs(DvMachineryPreviewStore())
    preview = _run(tools[PREVIEW_UPDATE], mes.client(), {"machinery_id": 10, "status": "REPAIR"})
    mes.write_response = {"code": 403, "msg": "denied"}
    result = _apply(tools[APPLY_UPDATE], mes, preview)
    assert result.status is ToolStatus.FAILED and result.error_code == "PERMISSION_DENIED"
    assert mes.devices[0]["status"] == "STOP"


def test_service_registers_visible_tools_but_hides_apply_and_creates_separate_store(tmp_path) -> None:
    common = dict(model_api_key="test", model_base_url="https://example.test/v1", model_id="test",
                  ktg_base_url="http://localhost", session_db_path=str(tmp_path / "session.sqlite3"),
                  workorder_db_path=str(tmp_path / "workorder.sqlite3"),
                  inventory_db_path=str(tmp_path / "inventory.sqlite3"))
    disabled = create_service(Settings(**common))
    assert disabled._dv_machinery_store is None

    enabled = create_service(Settings(**common, enable_dv_machinery_write=True))
    assert enabled._dv_machinery_store.path == str(tmp_path / "agent-dv-machinery.sqlite3")
    candidates = enabled._catalog.candidate_snapshot(
        CatalogIdentity(frozenset({"*:*:*"}), "run"), enabled._switches_provider())
    names = set(candidates.candidates)
    assert {PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE} <= names
    assert not {APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE} & names


def test_service_confirmation_runs_hidden_apply_with_fresh_identity_and_permissions(tmp_path) -> None:
    mes = FakeMes()
    settings = Settings(
        model_api_key="test", model_base_url="https://example.test/v1", model_id="test",
        ktg_base_url="http://localhost", session_db_path=str(tmp_path / "session.sqlite3"),
        workorder_db_path=str(tmp_path / "workorder.sqlite3"),
        inventory_db_path=str(tmp_path / "inventory.sqlite3"), enable_dv_machinery_write=True,
    )
    service = create_service(settings)
    service._ktg_client_factory = lambda _authorization: mes.client()
    store = service._dv_machinery_store
    assert store is not None
    preview = _run(_defs(store)[PREVIEW_CREATE], mes.client(), {
        "machinery_code": "EQ-20", "machinery_name": "新压缩机", "machinery_type_id": 201,
        "workshop_id": 6, "status": "STOP",
    })
    assert preview.status is ToolStatus.SUCCEEDED and preview.operation_id

    response = asyncio.run(service.confirm_dv_machinery(
        preview_id=preview.operation_id, authorization="Bearer trusted-session",
    ))
    assert response["result"]["outcome"] == "succeeded"
    assert response["result"]["kind"] == "dv_machinery_create_result"
    assert [item[0] for item in mes.writes] == ["POST"]


def test_service_confirmation_rejects_untrusted_user_identity(tmp_path) -> None:
    mes = FakeMes()
    settings = Settings(
        model_api_key="test", model_base_url="https://example.test/v1", model_id="test",
        ktg_base_url="http://localhost", session_db_path=str(tmp_path / "session.sqlite3"),
        workorder_db_path=str(tmp_path / "workorder.sqlite3"),
        inventory_db_path=str(tmp_path / "inventory.sqlite3"), enable_dv_machinery_write=True,
    )
    service = create_service(settings)
    service._ktg_client_factory = lambda _authorization: mes.client()
    store = service._dv_machinery_store
    assert store is not None
    preview = _run(_defs(store)[PREVIEW_DELETE], mes.client(), {"machinery_id": 10})
    assert preview.operation_id
    # Change the server-derived identity after the preview; caller input cannot claim ownership.
    original_read = mes.read

    async def other_identity(url, headers, params, timeout):
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": 8}, "permissions": [
                "mes:dv:machinery:remove", "mes:dv:machinery:query",
            ]}
        return await original_read(url, headers, params, timeout)

    mes.read = other_identity
    response = asyncio.run(service.confirm_dv_machinery(
        preview_id=preview.operation_id, authorization="Bearer another-session",
    ))
    assert response["result"]["error_code"] == "PREVIEW_IDENTITY_MISMATCH"
    assert not mes.writes
