"""Contract tests for preview/confirm device classification writes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.dv_machinerytype_writes import (
    APPLY_CREATE,
    APPLY_UPDATE,
    PREVIEW_CREATE,
    PREVIEW_UPDATE,
    DvMachineryTypePreviewStore,
    build_dv_machinerytype_write_tools,
    dv_machinerytype_write_manifests,
)
from ktg_agent.server import Settings, create_service
from ktg_agent.tools import CatalogIdentity, ToolContext, ToolStatus
from ktg_agent.tools.registry import ToolDefinition, ToolResult


class FakeMes:
    def __init__(self, nodes: list[dict[str, object]]) -> None:
        self.nodes = [dict(node) for node in nodes]
        self.reads: list[tuple[str, Mapping[str, object]]] = []
        self.writes: list[tuple[str, str, dict[str, object]]] = []

    async def transport(
        self, url: str, _headers: Mapping[str, str], params: Mapping[str, object], _timeout: float
    ) -> object:
        self.reads.append((url, params))
        if url.endswith("/mes/dv/machinerytype/list"):
            return {"code": 200, "data": [dict(node) for node in self.nodes]}
        if url.endswith("/mes/dv/machinery/list"):
            return {"code": 200, "total": 0, "rows": []}
        if url.endswith("/mes/dv/repair/list"):
            return {"code": 200, "total": 0, "rows": []}
        raise AssertionError(f"unexpected read: {url}")

    async def write_transport(
        self, method: str, url: str, _headers: Mapping[str, str],
        body: Mapping[str, object], _timeout: float,
    ) -> dict[str, object]:
        payload = dict(body)
        self.writes.append((method, url, payload))
        if method == "POST":
            new_id = max(int(cast(int, node["machineryTypeId"])) for node in self.nodes) + 1
            self.nodes.append({
                "machineryTypeId": new_id,
                "machineryTypeCode": f"M_TYPE_{new_id:03d}",
                "machineryTypeName": payload["machineryTypeName"],
                "parentTypeId": payload["parentTypeId"],
                "ancestors": "0,200",
                "enableFlag": payload.get("enableFlag", "Y"),
                "remark": payload.get("remark", ""),
            })
            return {"code": 200, "msg": "操作成功", "data": None}
        if method == "PUT":
            target = next(node for node in self.nodes
                          if node["machineryTypeId"] == payload["machineryTypeId"])
            for key in ("machineryTypeName", "enableFlag", "remark"):
                if key in payload:
                    target[key] = payload[key]
            return {"code": 200, "msg": "操作成功", "data": None}
        raise AssertionError(f"unexpected write: {method}")

    def client(self) -> KTGClient:
        return KTGClient(
            base_url="http://localhost", authorization="Bearer test",
            transport=self.transport, write_transport=self.write_transport,
        )


NODES: list[dict[str, object]] = [
    {"machineryTypeId": 200, "machineryTypeCode": "M_TYPE_000", "machineryTypeName": "设备分类",
     "parentTypeId": 0, "ancestors": "0", "enableFlag": "Y", "remark": ""},
    {"machineryTypeId": 201, "machineryTypeCode": "M_TYPE_025", "machineryTypeName": "工艺压缩设备",
     "parentTypeId": 200, "ancestors": "0,200", "enableFlag": "Y", "remark": ""},
]


def _definitions(store: DvMachineryTypePreviewStore) -> dict[str, ToolDefinition]:
    return {tool.name: tool for tool in build_dv_machinerytype_write_tools(store)}


def _run(
    definition: ToolDefinition,
    client: KTGClient,
    arguments: dict[str, object],
    *,
    operation_id: str | None = None,
) -> ToolResult:
    context = ToolContext(
        run_id="r", tool_call_id="c", operation_id=operation_id,
        identity={"userId": 7}, ktg_client=client,
    )

    async def invoke() -> ToolResult:
        return await definition.execute(context, arguments)

    return asyncio.run(invoke())


def test_preview_tools_are_visible_and_apply_tools_are_hidden() -> None:
    store = DvMachineryTypePreviewStore()
    definitions = _definitions(store)
    assert set(definitions) == {PREVIEW_CREATE, PREVIEW_UPDATE, APPLY_CREATE, APPLY_UPDATE}
    assert definitions[PREVIEW_CREATE].expose_to_model is True
    assert definitions[PREVIEW_UPDATE].expose_to_model is True
    assert definitions[APPLY_CREATE].expose_to_model is False
    assert definitions[APPLY_UPDATE].expose_to_model is False
    assert "preview_id" in definitions[APPLY_CREATE].output_schema["properties"]
    assert "preview_id" in definitions[APPLY_UPDATE].output_schema["properties"]
    assert all(definition.effect.value == "read" for name, definition in definitions.items()
               if name.startswith("ktg_preview_"))
    assert all(definition.effect.value == "write" for name, definition in definitions.items()
               if not name.startswith("ktg_preview_"))
    manifests = {manifest.name: manifest for manifest in dv_machinerytype_write_manifests(
        tuple(definitions.values()))}
    assert manifests[PREVIEW_CREATE].confirmation_policy == "none"
    assert manifests[PREVIEW_CREATE].proposal_store_effect is True
    assert manifests[APPLY_CREATE].confirmation_policy == "trusted_confirmation"
    assert manifests[APPLY_CREATE].expose_to_model is False
    assert manifests[APPLY_UPDATE].confirmation_policy == "trusted_confirmation"


def test_preview_create_does_not_write_and_rejects_duplicates() -> None:
    mes = FakeMes(NODES)
    store = DvMachineryTypePreviewStore()
    tools = _definitions(store)
    result = _run(tools[PREVIEW_CREATE], mes.client(), {
        "parent_type_id": 200, "machinery_type_name": "包装设备", "enable_flag": "Y",
    })
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["kind"] == "dv_machinerytype_preview"
    assert result.details["action"] == "create"
    assert result.details["after"]["parentPath"] == "设备分类"
    assert not mes.writes

    duplicate = _run(tools[PREVIEW_CREATE], mes.client(), {
        "parent_type_id": 200, "machinery_type_name": " 工艺压缩设备 ",
    })
    assert duplicate.error_code == "DUPLICATE_NAME"

    missing = _run(tools[PREVIEW_CREATE], mes.client(), {
        "parent_type_id": 999, "machinery_type_name": "新分类",
    })
    assert missing.error_code == "PARENT_NOT_FOUND"

    root = _run(tools[PREVIEW_CREATE], mes.client(), {
        "parent_type_id": 0, "machinery_type_name": "根分类",
    })
    assert root.error_code == "VALIDATION_ERROR"


def test_preview_update_detects_no_change_and_duplicates() -> None:
    mes = FakeMes([*NODES, {
        "machineryTypeId": 202, "machineryTypeCode": "M_TYPE_026",
        "machineryTypeName": "固原岩压缩设备1型", "parentTypeId": 200,
        "ancestors": "0,200", "enableFlag": "Y", "remark": "",
    }])
    store = DvMachineryTypePreviewStore()
    tools = _definitions(store)
    ok = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_type_id": 201, "machinery_type_name": "工艺气体压缩设备",
    })
    assert ok.status is ToolStatus.SUCCEEDED
    assert ok.details is not None
    assert ok.details["before"]["machineryTypeName"] == "工艺压缩设备"
    assert ok.details["after"]["machineryTypeName"] == "工艺气体压缩设备"
    assert ok.details["after"]["path"] == "设备分类/工艺气体压缩设备"
    assert not mes.writes

    no_change = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_type_id": 201, "machinery_type_name": "工艺压缩设备",
    })
    assert no_change.error_code == "NO_CHANGE"

    duplicate = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_type_id": 201, "machinery_type_name": "固原岩压缩设备1型",
    })
    assert duplicate.error_code == "DUPLICATE_NAME"

    missing = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_type_id": 999, "machinery_type_name": "x",
    })
    assert missing.error_code == "TYPE_NOT_FOUND"


def test_create_preview_then_apply_writes_once_and_verifies() -> None:
    mes = FakeMes(NODES)
    store = DvMachineryTypePreviewStore()
    tools = _definitions(store)
    preview = _run(tools[PREVIEW_CREATE], mes.client(), {
        "parent_type_id": 200, "machinery_type_name": "包装设备", "enable_flag": "Y",
        "remark": "验收用",
    })
    assert isinstance(preview.operation_id, str)
    applied = _run(tools[APPLY_CREATE], mes.client(),
                   {"operation_id": preview.operation_id},
                   operation_id=preview.operation_id)
    assert applied.status is ToolStatus.SUCCEEDED, applied
    assert applied.details is not None
    assert applied.details["outcome"] == "succeeded"
    assert applied.details["verified"] is True
    created = cast(dict[str, object], applied.details["created"])
    assert created["machineryTypeName"] == "包装设备"
    assert created["parentTypeId"] == 200
    assert len(mes.writes) == 1
    method, path, body = mes.writes[0]
    assert (method, path.endswith("/mes/dv/machinerytype")) == ("POST", True)
    assert body == {"parentTypeId": 200, "machineryTypeName": "包装设备",
                    "enableFlag": "Y", "remark": "验收用"}

    again = _run(tools[APPLY_CREATE], mes.client(),
                 {"operation_id": preview.operation_id},
                 operation_id=preview.operation_id)
    assert again.content == applied.content
    assert len(mes.writes) == 1


def test_update_preview_then_apply_only_sends_changed_fields() -> None:
    mes = FakeMes(NODES)
    store = DvMachineryTypePreviewStore()
    tools = _definitions(store)
    preview = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_type_id": 201, "machinery_type_name": "工艺气体压缩设备",
        "enable_flag": "N",
    })
    assert preview.operation_id
    applied = _run(tools[APPLY_UPDATE], mes.client(),
                   {"operation_id": preview.operation_id},
                   operation_id=preview.operation_id)
    assert applied.status is ToolStatus.SUCCEEDED, applied
    assert applied.details is not None
    assert applied.details["updated"]["machineryTypeName"] == "工艺气体压缩设备"
    assert applied.details["updated"]["enableFlag"] == "N"
    method, path, body = mes.writes[0]
    assert (method, path.endswith("/mes/dv/machinerytype")) == ("PUT", True)
    assert body == {"machineryTypeId": 201, "machineryTypeName": "工艺气体压缩设备",
                    "enableFlag": "N"}
    assert "parentTypeId" not in body and "ancestors" not in body


def test_apply_rejects_stale_preview() -> None:
    mes = FakeMes(NODES)
    store = DvMachineryTypePreviewStore()
    tools = _definitions(store)
    preview = _run(tools[PREVIEW_UPDATE], mes.client(), {
        "machinery_type_id": 201, "machinery_type_name": "工艺气体压缩设备",
    })
    mes.nodes[1]["machineryTypeName"] = "被人改过的名称"
    stale = _run(tools[APPLY_UPDATE], mes.client(),
                 {"operation_id": preview.operation_id},
                 operation_id=preview.operation_id)
    assert stale.error_code == "PREVIEW_STALE"
    assert not mes.writes


def test_apply_requires_matching_operation_id() -> None:
    mes = FakeMes(NODES)
    store = DvMachineryTypePreviewStore()
    tools = _definitions(store)
    preview = _run(tools[PREVIEW_CREATE], mes.client(), {
        "parent_type_id": 200, "machinery_type_name": "包装设备",
    })
    mismatch = _run(tools[APPLY_CREATE], mes.client(),
                    {"operation_id": "other"}, operation_id="other")
    assert mismatch.error_code == "PREVIEW_NOT_FOUND"
    assert not mes.writes
    assert preview.operation_id


def test_store_binds_preview_to_user_and_expires() -> None:
    store = DvMachineryTypePreviewStore(ttl_seconds=0.05)
    preview_id = store.create(user_key="id:7", action="create", before=None,
                              after={"parentTypeId": 200, "machineryTypeName": "x"})
    with pytest.raises(Exception):
        store.action_for(preview_id=preview_id, user_key="id:8")
    time.sleep(0.1)
    with pytest.raises(Exception):
        store.action_for(preview_id=preview_id, user_key="id:7")


def test_write_definitions_are_registered_only_when_enabled(tmp_path: Path) -> None:
    common = dict(
        model_api_key="test", model_base_url="https://example.test/v1",
        model_id="test-model", ktg_base_url="http://127.0.0.1:8080",
        session_db_path=str(tmp_path / "sessions.sqlite3"),
        workorder_db_path=str(tmp_path / "workorders.sqlite3"),
        inventory_db_path=str(tmp_path / "inventory.sqlite3"),
        enable_item_receipt=True,
        enable_production_issue=True,
    )
    disabled = create_service(Settings(**common))
    candidates = disabled._catalog.candidate_snapshot(
        CatalogIdentity(frozenset({"*:*:*"}), "run"), disabled._switches_provider())
    assert len(disabled._catalog._manifests) == 21
    assert len(candidates.candidates) == 16

    enabled = create_service(Settings(
        **common,
        enable_dv_machinerytype_write=True,
        dv_machinerytype_db_path=str(tmp_path / "dv.sqlite3"),
    ))
    enabled_candidates = enabled._catalog.candidate_snapshot(
        CatalogIdentity(frozenset({"*:*:*"}), "run"), enabled._switches_provider())
    assert len(enabled._catalog._manifests) == 25
    assert len(enabled_candidates.candidates) == 18
    assert PREVIEW_CREATE in enabled_candidates.candidates
    assert PREVIEW_UPDATE in enabled_candidates.candidates
    assert APPLY_CREATE not in enabled_candidates.candidates
    assert APPLY_UPDATE not in enabled_candidates.candidates
