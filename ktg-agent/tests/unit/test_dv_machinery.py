"""Contract tests for the read-only device ledger tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.dv_machinery import (
    DETAIL_NAME, LIST_NAME, WORKSHOP_NAME, build_dv_machinery_tools,
    dv_machinery_factories, dv_machinery_manifests,
)
from ktg_agent.ktg.dv_machinery_writes import (
    DvMachineryPreviewStore, PREVIEW_CREATE, PREVIEW_UPDATE, build_dv_machinery_write_tools,
)
from ktg_agent.tools import ToolContext, ToolRegistry, ToolRunner, ToolStatus
from ktg_agent.tools.catalog import ToolCatalog
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import ToolResult


MACHINES = [
    {"machineryId": 10, "machineryCode": "EQ-10", "machineryName": "压缩机 A",
     "machineryBrand": "Acme", "machinerySpec": "X-1", "machineryTypeId": 201,
     "machineryTypeCode": "T-1", "machineryTypeName": "压缩机", "workshopId": 5,
     "workshopCode": "WS-5", "workshopName": "一车间", "status": "STOP", "remark": ""},
    {"machineryId": 11, "machineryCode": "EQ-11", "machineryName": "包装机",
     "machineryBrand": "Acme", "machinerySpec": "P-1", "machineryTypeId": 202,
     "machineryTypeCode": "T-2", "machineryTypeName": "包装", "workshopId": 6,
     "workshopCode": "WS-6", "workshopName": "二车间", "status": "WORKING", "remark": ""},
]
WORKSHOPS = [
    {"workshopId": 5, "workshopCode": "WS-5", "workshopName": "一车间", "enableFlag": "Y"},
    {"workshopId": 6, "workshopCode": "WS-6", "workshopName": "二车间", "enableFlag": "Y"},
    {"workshopId": 7, "workshopCode": "WS-7", "workshopName": "停用车间", "enableFlag": "N"},
]


def _client(responses: Mapping[str, object], calls: list[tuple[str, Mapping[str, object]]]) -> KTGClient:
    async def transport(url: str, _headers: Mapping[str, str], params: Mapping[str, object], _timeout: float) -> object:
        calls.append((url, dict(params)))
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected URL: {url}")
    return KTGClient(base_url="http://localhost", authorization="Bearer test", transport=transport)


def _run(name: str, args: dict[str, object], responses: Mapping[str, object]) -> tuple[ToolResult, list[tuple[str, Mapping[str, object]]]]:
    calls: list[tuple[str, Mapping[str, object]]] = []
    client = _client(responses, calls)
    tool = next(item for item in build_dv_machinery_tools() if item.name == name)
    async def invoke() -> ToolResult:
        return await tool.execute(ToolContext(run_id="r", tool_call_id="c", ktg_client=client), args)
    return asyncio.run(invoke()), calls


def _list_payload(rows: list[dict[str, object]] | None = None) -> dict[str, object]:
    rows = rows if rows is not None else MACHINES
    return {"code": 200, "total": len(rows), "rows": rows}


def test_tools_register_and_permissions_are_scoped() -> None:
    definitions = build_dv_machinery_tools()
    factories = dv_machinery_factories()
    manifests = dv_machinery_manifests()
    assert {item.name for item in definitions} == {LIST_NAME, DETAIL_NAME, WORKSHOP_NAME}
    catalog = ToolCatalog(factories)
    for manifest in manifests:
        catalog.register_factory(manifest, factories[manifest.handler_ref])
        assert manifest.expose_to_model and manifest.confirmation_policy == "none"
    assert isinstance(catalog.manifest(LIST_NAME).permission_policy, PermissionLeaf)
    assert isinstance(catalog.manifest(DETAIL_NAME).permission_policy, PermissionAll)
    assert catalog.manifest(WORKSHOP_NAME).permission_policy.permission == "mes:md:workshop:list"


def test_list_maps_supported_filters_and_pagination() -> None:
    result, calls = _run(LIST_NAME, {
        "machinery_name": "压缩", "machinery_type_id": 201,
        "workshop_name": "一车间", "status": "STOP", "page": 2, "page_size": 10,
    }, {"/mes/dv/machinery/list": _list_payload([MACHINES[0]])})
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details["total"] == 1
    assert result.details["rows"][0]["machineryId"] == 10
    assert calls[0][1] == {"machineryName": "压缩", "machineryTypeId": 201,
                           "workshopName": "一车间", "status": "STOP", "pageNum": 2, "pageSize": 10}


@pytest.mark.parametrize("args", [
    {"status": "BROKEN"}, {"page": True}, {"page_size": 101},
    {"workshop_id": 0}, {"machinery_name": "  "}, {"unknown": "x"},
])
def test_list_rejects_invalid_input_without_http(args: dict[str, object]) -> None:
    result, calls = _run(LIST_NAME, args, {})
    assert result.error_code == "VALIDATION_ERROR"
    assert not calls


def test_detail_by_id_and_exact_code() -> None:
    detail = {"code": 200, "data": MACHINES[0]}
    by_id, id_calls = _run(DETAIL_NAME, {"machinery_id": 10}, {"/mes/dv/machinery/10": detail})
    assert by_id.status is ToolStatus.SUCCEEDED
    assert by_id.details["machinery"]["machineryCode"] == "EQ-10"
    assert id_calls[0][0].endswith("/mes/dv/machinery/10")

    by_code, code_calls = _run(DETAIL_NAME, {"machinery_code": "EQ-10"}, {
        "/mes/dv/machinery/list": _list_payload([MACHINES[0]]),
        "/mes/dv/machinery/10": detail,
    })
    assert by_code.status is ToolStatus.SUCCEEDED
    assert len(code_calls) == 2


def test_detail_rejects_missing_selector_ambiguous_and_not_found() -> None:
    missing, missing_calls = _run(DETAIL_NAME, {}, {})
    assert missing.error_code == "VALIDATION_ERROR" and not missing_calls
    ambiguous, _ = _run(DETAIL_NAME, {"machinery_code": "EQ-10"}, {
        "/mes/dv/machinery/list": _list_payload([MACHINES[0], {**MACHINES[0], "machineryId": 12}]),
    })
    assert ambiguous.error_code == "AMBIGUOUS_MATCH"
    absent, _ = _run(DETAIL_NAME, {"machinery_id": 999}, {"/mes/dv/machinery/999": {"code": 200, "data": None}})
    assert absent.error_code == "MACHINERY_NOT_FOUND"


@pytest.mark.parametrize("arguments", [
    {}, {"machinery_id": 10, "machinery_code": "EQ-10"},
    {"machinery_id": "10"}, {"machineryId": 10}, {"machinery_code": "  "},
])
def test_detail_runner_rejects_invalid_locator_before_mes(arguments: JsonObject) -> None:
    calls: list[tuple[str, Mapping[str, object]]] = []
    runner = ToolRunner(ToolRegistry(build_dv_machinery_tools()))
    result = asyncio.run(runner.run(ToolCall("c", DETAIL_NAME, arguments),
                                    ToolContext("r", "c", ktg_client=_client({}, calls))))
    assert result.error_code == "VALIDATION_ERROR"
    assert result.details is not None and "path" in result.details
    assert calls == []


def test_workshop_options_filter_disabled_rows_and_bound_results() -> None:
    result, calls = _run(WORKSHOP_NAME, {"workshop_name": "车间", "limit": 1}, {
        "/mes/md/workshop/listAll": {"code": 200, "data": WORKSHOPS},
    })
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details["total"] == 2 and result.details["truncated"] is True
    assert result.details["rows"][0]["enableFlag"] == "Y"
    assert calls[0][0].endswith("/mes/md/workshop/listAll")


def test_preview_declarations_distinguish_blank_create_and_clear_update() -> None:
    definitions = {tool.name: tool for tool in build_dv_machinery_write_tools(DvMachineryPreviewStore())}
    create = Draft202012Validator(definitions[PREVIEW_CREATE].input_schema)
    update = Draft202012Validator(definitions[PREVIEW_UPDATE].input_schema)
    new_device = {"machinery_code": "EQ-20", "machinery_name": "新设备",
                  "machinery_type_id": 201, "workshop_id": 5}
    assert create.is_valid(new_device)
    assert not create.is_valid({**new_device, "machinery_brand": ""})
    assert not create.is_valid({**new_device, "machinery_type_id": "T-1"})
    assert update.is_valid({"machinery_id": 10, "machinery_brand": ""})
    assert not update.is_valid({"machinery_id": 10, "machinery_name": " "})
    assert not update.is_valid({"machinery_id": 10, "machinery_code": "EQ-20"})


def test_malformed_list_response_is_rejected() -> None:
    result, _ = _run(LIST_NAME, {}, {"/mes/dv/machinery/list": {"code": 200, "rows": "bad", "total": 0}})
    assert result.error_code == "KTG_INVALID_RESPONSE"
