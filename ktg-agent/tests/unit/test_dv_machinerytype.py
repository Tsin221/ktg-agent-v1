"""Contract tests for the read-only device classification tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.dv_machinerytype import (
    DETAIL_NAME,
    TREE_NAME,
    build_dv_machinerytype_tools,
    dv_machinerytype_factories,
    dv_machinerytype_manifests,
)
from ktg_agent.tools import ToolContext, ToolStatus
from ktg_agent.tools.catalog import ToolCatalog
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import ToolResult


NODES = [
    {"machineryTypeId": 200, "machineryTypeCode": "M_TYPE_000", "machineryTypeName": "设备分类",
     "parentTypeId": 0, "ancestors": "0", "enableFlag": "Y", "remark": ""},
    {"machineryTypeId": 201, "machineryTypeCode": "M_TYPE_025", "machineryTypeName": "工艺压缩设备",
     "parentTypeId": 200, "ancestors": "0,200", "enableFlag": "Y", "remark": ""},
    {"machineryTypeId": 202, "machineryTypeCode": "M_TYPE_026", "machineryTypeName": "固原岩压缩设备1型",
     "parentTypeId": 201, "ancestors": "0,200,201", "enableFlag": "N", "remark": ""},
    {"machineryTypeId": 203, "machineryTypeCode": "M_TYPE_027", "machineryTypeName": "包装设备",
     "parentTypeId": 200, "ancestors": "0,200", "enableFlag": "Y", "remark": ""},
]


def _client(
    payloads: list[tuple[str, dict[str, object]]],
    calls: list[tuple[str, Mapping[str, object]]],
) -> KTGClient:
    async def transport(
        url: str, _headers: Mapping[str, str], params: Mapping[str, object], _timeout: float
    ) -> object:
        calls.append((url, params))
        for suffix, payload in payloads:
            if url.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected url: {url}")

    return KTGClient(base_url="http://localhost", authorization="Bearer test", transport=transport)


def _run(
    name: str,
    args: dict[str, object],
    payloads: list[tuple[str, dict[str, object]]] | None = None,
) -> tuple[ToolResult, list[tuple[str, Mapping[str, object]]]]:
    calls: list[tuple[str, Mapping[str, object]]] = []
    client = _client(payloads or [("/mes/dv/machinerytype/list", {"code": 200, "data": NODES})], calls)
    tool = next(item for item in build_dv_machinerytype_tools() if item.name == name)

    async def invoke() -> ToolResult:
        return await tool.execute(ToolContext(run_id="r", tool_call_id="c", ktg_client=client), args)

    return asyncio.run(invoke()), calls


def test_two_definitions_and_manifests_resolve() -> None:
    factories = dv_machinerytype_factories()
    manifests = dv_machinerytype_manifests()
    definitions = build_dv_machinerytype_tools()
    assert len(factories) == len(manifests) == len(definitions) == 2
    catalog = ToolCatalog(factories)
    for manifest in manifests:
        catalog.register_factory(manifest, factories[manifest.handler_ref])
        definition = catalog.resolve_definition(manifest.name, manifest.version)
        assert definition.required_permissions
        assert manifest.module == "dv"
        assert manifest.expose_to_model is True
        assert manifest.confirmation_policy == "none"
        assert manifest.proposal_store_effect is False
    tree_manifest = catalog.manifest(TREE_NAME)
    assert isinstance(tree_manifest.permission_policy, PermissionLeaf)
    assert tree_manifest.permission_policy.permission == "mes:dv:machinerytype:list"
    detail_manifest = catalog.manifest(DETAIL_NAME)
    assert isinstance(detail_manifest.permission_policy, PermissionAll)
    assert {leaf.permission for leaf in detail_manifest.permission_policy.all_of
            if isinstance(leaf, PermissionLeaf)} == {
        "mes:dv:machinerytype:query", "mes:dv:machinery:list"}
    detail_definition = next(item for item in definitions if item.name == DETAIL_NAME)
    assert "childrenTruncated" in detail_definition.output_schema["properties"]


def test_tree_no_filters_returns_all_with_paths() -> None:
    result, calls = _run(TREE_NAME, {})
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["total"] == 4
    assert result.details["returned"] == 4
    assert result.details["truncated"] is False
    rows = cast(list[dict[str, object]], result.details["rows"])
    assert rows[0]["path"] == "设备分类"
    assert rows[1]["path"] == "设备分类/工艺压缩设备"
    assert rows[2]["path"] == "设备分类/包装设备"
    assert rows[3]["path"] == "设备分类/工艺压缩设备/固原岩压缩设备1型"
    assert rows[3]["depth"] == 3
    assert rows[1]["childCount"] == 1
    assert calls[0][0].endswith("/mes/dv/machinerytype/list")
    assert "共 4 个设备分类" in result.content
    assert "[201] 设备分类/工艺压缩设备（M_TYPE_025，启用，有 1 个子分类）" in result.content
    assert "[203] 设备分类/包装设备（M_TYPE_027，启用，末级分类）" in result.content


@pytest.mark.parametrize("args,expected", [
    ({"machinery_type_name": "压缩"}, 2),
    ({"enable_flag": "N"}, 1),
    ({"parent_type_id": 200}, 2),
    ({"machinery_type_code": "M_TYPE_02"}, 3),
    ({"max_depth": 2}, 3),
])
def test_tree_filters(args: dict[str, object], expected: int) -> None:
    result, _ = _run(TREE_NAME, args)
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["total"] == expected


def test_tree_limit_marks_truncated() -> None:
    result, _ = _run(TREE_NAME, {"limit": 2})
    assert result.details is not None
    assert result.details["total"] == 4
    assert result.details["returned"] == 2
    assert result.details["truncated"] is True
    assert "还有" in result.content or "未返回" in result.content


@pytest.mark.parametrize("args", [
    {"enable_flag": "X"},
    {"limit": 0},
    {"limit": 201},
    {"max_depth": 6},
    {"parent_type_id": -1},
    {"machinery_type_name": ""},
    {"unknown": 1},
])
def test_tree_rejects_invalid_arguments_before_http(args: dict[str, object]) -> None:
    result, calls = _run(TREE_NAME, args)
    assert result.status is ToolStatus.FAILED
    assert result.error_code == "VALIDATION_ERROR"
    assert not calls


@pytest.mark.parametrize("payload", [
    {"code": 200},
    {"code": 200, "data": {}},
    {"code": 200, "data": [{"machineryTypeId": 1}]},
    {"code": 200, "data": [NODES[0], NODES[0]]},
])
def test_tree_rejects_bad_response(payload: dict[str, object]) -> None:
    result, _ = _run(TREE_NAME, {}, [("/mes/dv/machinerytype/list", payload)])
    assert result.status is ToolStatus.FAILED
    assert result.error_code == "KTG_INVALID_RESPONSE"


@pytest.mark.parametrize("arguments", [
    {"machinery_type_id": 201},
    {"machinery_type_id": 201, "machinery_type_code": "M_TYPE_027"},
])
def test_detail_by_id_includes_children_devices_and_repair(arguments: dict[str, object]) -> None:
    detail = next(tool for tool in build_dv_machinerytype_tools() if tool.name == DETAIL_NAME)
    assert Draft202012Validator(detail.input_schema).is_valid(arguments)
    result, calls = _run(
        DETAIL_NAME,
        arguments,
        [
            ("/mes/dv/machinerytype/list", {"code": 200, "data": NODES}),
            ("/mes/dv/machinery/list", {"code": 200, "total": 2,
             "rows": [{"machineryCode": "M0056", "machineryName": "阿尔法压缩装置", "status": "STOP"}]}),
            ("/mes/dv/repair/list", {"code": 200, "total": 1, "rows": [{}]}),
        ],
    )
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    node = cast(dict[str, object], result.details["node"])
    assert node["path"] == "设备分类/工艺压缩设备"
    assert node["parentName"] == "设备分类"
    children = cast(list[dict[str, object]], result.details["children"])
    assert [child["machineryTypeName"] for child in children] == ["固原岩压缩设备1型"]
    device = cast(dict[str, object], result.details["device"])
    assert device["subtreeTotal"] == 2
    assert result.details["repairTotal"] == 1
    assert result.details["checkPlanNote"].startswith("点检计划")
    assert "子树内设备 2 台" in result.content
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == ["list", "list", "list"]


def test_detail_by_name_and_ambiguity() -> None:
    result, _ = _run(DETAIL_NAME, {"machinery_type_name": "固原岩压缩设备1型"},
                     [("/mes/dv/machinerytype/list", {"code": 200, "data": NODES}),
                      ("/mes/dv/machinery/list", {"code": 200, "total": 0, "rows": []}),
                      ("/mes/dv/repair/list", {"code": 200, "total": 0, "rows": []})])
    assert result.status is ToolStatus.SUCCEEDED

    duplicated = [*NODES, {**NODES[1], "machineryTypeId": 204,
                           "machineryTypeCode": "M_TYPE_028"}]
    ambiguous, _ = _run(DETAIL_NAME, {"machinery_type_name": "工艺压缩设备"},
                        [("/mes/dv/machinerytype/list", {"code": 200, "data": duplicated})])
    assert ambiguous.error_code == "AMBIGUOUS_MATCH"
    assert "[201]" in ambiguous.content and "[204]" in ambiguous.content


@pytest.mark.parametrize("args,code", [
    ({"machinery_type_id": 999}, "TYPE_NOT_FOUND"),
    ({"machinery_type_code": "M_TYPE_999"}, "TYPE_NOT_FOUND"),
    ({"machinery_type_name": "不存在"}, "TYPE_NOT_FOUND"),
])
def test_detail_not_found(args: dict[str, object], code: str) -> None:
    result, _ = _run(DETAIL_NAME, args)
    assert result.error_code == code


def test_detail_degrades_when_repair_is_forbidden() -> None:
    result, _ = _run(
        DETAIL_NAME,
        {"machinery_type_code": "M_TYPE_025"},
        [
            ("/mes/dv/machinerytype/list", {"code": 200, "data": NODES}),
            ("/mes/dv/machinery/list", {"code": 200, "total": 0, "rows": []}),
            ("/mes/dv/repair/list", {"code": 403, "msg": "denied"}),
        ],
    )
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["repairTotal"] is None
    assert result.details["repairStatus"] == "PERMISSION_DENIED"
    assert "维修单引用：未核验" in result.content


def test_detail_requires_a_selector() -> None:
    result, calls = _run(DETAIL_NAME, {})
    assert result.error_code == "VALIDATION_ERROR"
    assert not calls


def test_detail_rejects_bad_device_response() -> None:
    result, _ = _run(
        DETAIL_NAME,
        {"machinery_type_id": 201},
        [
            ("/mes/dv/machinerytype/list", {"code": 200, "data": NODES}),
            ("/mes/dv/machinery/list", {"code": 200, "total": 1}),
            ("/mes/dv/repair/list", {"code": 200, "total": 0, "rows": []}),
        ],
    )
    assert result.error_code == "KTG_INVALID_RESPONSE"
