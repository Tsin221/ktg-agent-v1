import asyncio
from collections.abc import Mapping
from typing import cast

import pytest

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.production_queries import (
    build_production_query_tools, production_query_factories,
    production_query_manifests, read_production_list,
)
from ktg_agent.tools.catalog import ToolCatalog
from ktg_agent.tools.registry import ToolContext, ToolRegistry, ToolStatus
from ktg_agent.tools.registry import ToolResult
from ktg_agent.tools.permissions import PermissionLeaf
from ktg_agent.tools.runner import ToolRunner


def _client(payload: dict[str, object], calls: list[tuple[str, Mapping[str, object]]]) -> KTGClient:
    async def transport(url: str, _headers: Mapping[str, str], params: Mapping[str, object], _timeout: float) -> object:
        calls.append((url, params))
        return payload
    return KTGClient(base_url="http://localhost", authorization="Bearer test", transport=transport)


def _run(name: str, args: dict[str, object], payload: dict[str, object]) -> tuple[ToolResult, list[tuple[str, Mapping[str, object]]]]:
    calls: list[tuple[str, Mapping[str, object]]] = []
    client = _client(payload, calls)
    tool = next(item for item in build_production_query_tools() if item.name == name)
    async def invoke() -> ToolResult:
        return await tool.execute(ToolContext(run_id="r", tool_call_id="c", ktg_client=client), args)
    result = asyncio.run(invoke())
    return result, calls


def test_five_manifest_factories_resolve() -> None:
    factories = production_query_factories()
    manifests = production_query_manifests()
    catalog = ToolCatalog(factories)
    for manifest in manifests:
        catalog.register_factory(manifest, factories[manifest.handler_ref])
        definition = catalog.resolve_definition(manifest.name, manifest.version)
        assert isinstance(manifest.permission_policy, PermissionLeaf)
        assert definition.required_permissions == (manifest.permission_policy.permission,)
    assert len(manifests) == len(factories) == 5


@pytest.mark.parametrize("name,field,path", [
    ("ktg_pro_workorder_get", "workorderId", "workorder"),
    ("ktg_pro_task_get", "taskId", "protask"),
    ("ktg_pro_feedback_get", "recordId", "feedback"),
])
def test_detail_reads_exact_id_and_preserves_zero(name: str, field: str, path: str) -> None:
    arg = {"workorderId": "workorder_id", "taskId": "task_id", "recordId": "record_id"}[field]
    result, calls = _run(name, {arg: 7}, {"code": 200, "data": {field: 7, "quantity": 0}})
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert cast(dict[str, object], result.details["data"])["quantity"] == "0"
    assert calls[0][0].endswith(f"/mes/pro/{path}/7")


def test_list_uses_server_total_and_marks_more_pages() -> None:
    result, calls = _run("ktg_pro_feedback_list", {"task_id": 2, "page": 2, "page_size": 1},
                         {"code": 200, "total": 4, "rows": [{"recordId": 3, "quantityFeedback": "1.50"}]})
    assert result.details is not None
    assert result.details["total"] == 4
    assert result.details["truncated"] is True
    assert cast(list[dict[str, object]], result.details["rows"])[0]["quantityFeedback"] == "1.50"
    assert calls[0][1] == {"pageNum": 2, "pageSize": 1, "taskId": 2}


@pytest.mark.parametrize(("arguments", "expected_filters"), [
    ({}, None),
    ({"status": "FINISHED"}, None),
    ({"workorder_id": 1}, {"workorderId": 1}),
    ({"task_id": 2}, {"taskId": 2}),
    ({"workorder_id": 1, "task_id": 2}, {"workorderId": 1, "taskId": 2}),
])
def test_feedback_runner_requires_at_least_one_scope(
    arguments: JsonObject, expected_filters: dict[str, object] | None,
) -> None:
    calls: list[tuple[str, Mapping[str, object]]] = []
    client = _client({"code": 200, "total": 0, "rows": []}, calls)
    runner = ToolRunner(ToolRegistry(build_production_query_tools()))
    call = ToolCall("c", "ktg_pro_feedback_list", arguments)
    result = asyncio.run(runner.run(call, ToolContext("r", call.id, ktg_client=client)))
    if expected_filters is None:
        assert result.error_code == "VALIDATION_ERROR"
        assert result.details is not None and "path" in result.details
        assert calls == []
    else:
        assert result.status is ToolStatus.SUCCEEDED
        assert calls == [("http://localhost/mes/pro/feedback/list",
                          {"pageNum": 1, "pageSize": 10, **expected_filters})]


@pytest.mark.parametrize("args", [{"task_id": -1}, {"task_id": 1, "page_size": 21},
                                      {"task_id": 1, "page": 0}, {"task_id": 1, "unknown": "x"}])
def test_invalid_list_input_never_calls_mes(args: dict[str, object]) -> None:
    result, calls = _run("ktg_pro_feedback_list", args, {})
    assert result.status is ToolStatus.FAILED
    assert not calls


def test_time_range_is_forwarded_with_offsets() -> None:
    result, calls = _run("ktg_pro_feedback_list", {
        "task_id": 1, "from_time": "2026-01-01T00:00:00Z",
        "to_time": "2026-01-02T08:00:00+08:00",
    }, {"code": 200, "total": 0, "rows": []})
    assert result.status is ToolStatus.SUCCEEDED
    assert calls[0][1]["fromTime"] == "2026-01-01T00:00:00+00:00"
    assert calls[0][1]["toTime"] == "2026-01-02T08:00:00+08:00"


@pytest.mark.parametrize("args", [
    {"from_time": "2026-01-01"},
    {"from_time": "2026-01-01T00:00:00"},
    {"from_time": "invalid"},
    {"from_time": "2026-01-02T00:00:00Z", "to_time": "2026-01-01T00:00:00Z"},
    {"from_time": "2026-01-01T00:00:00Z", "to_time": "2026-01-01T08:00:00+08:00"},
])
def test_invalid_time_range_fails_before_http(args: dict[str, object]) -> None:
    result, calls = _run("ktg_pro_feedback_list", {"task_id": 1, **args}, {})
    assert result.error_code == "VALIDATION_ERROR"
    assert not calls


@pytest.mark.parametrize("payload,code", [
    ({"code": 403, "msg": "denied"}, "PERMISSION_DENIED"),
    ({"code": 200, "data": None}, "KTG_NOT_FOUND"),
    ({"code": 200, "data": []}, "KTG_INVALID_RESPONSE"),
    ({"code": 200, "data": {"taskId": 9}}, "KTG_INVALID_RESPONSE"),
])
def test_detail_failure_mapping(payload: dict[str, object], code: str) -> None:
    result, _ = _run("ktg_pro_task_get", {"task_id": 7}, payload)
    assert result.error_code == code


def test_list_rejects_missing_total_and_bad_rows() -> None:
    for payload in ({"code": 200, "rows": []}, {"code": 200, "total": 1, "rows": [{}]}):
        result, _ = _run("ktg_pro_task_list", {"workorder_id": 1}, payload)
        assert result.error_code == "KTG_INVALID_RESPONSE"
