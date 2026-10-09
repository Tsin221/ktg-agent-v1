"""Allow-listed read-only KTG business query tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import (
    ExecutionMode,
    RetryPolicy,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolExecute,
    ToolResult,
    ToolStatus,
)


def build_query_tools() -> tuple[ToolDefinition, ...]:
    common_properties: JsonObject = {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
    }
    return (
        _tool(
            name="ktg_search_items",
            label="查询产品",
            description="按产品或物料编码、名称查询 KTG 产品资料。至少提供一个筛选条件。",
            properties={
                "item_code": {"type": "string", "minLength": 1},
                "item_name": {"type": "string", "minLength": 1},
                **common_properties,
            },
            execute=_search_items,
        ),
        _tool(
            name="ktg_query_stock",
            label="查询库存",
            description="按物料编码查询当前用户有权查看的分仓库存。",
            properties={
                "item_code": {"type": "string", "minLength": 1},
                "warehouse_id": {"type": "integer", "minimum": 1},
                **common_properties,
            },
            execute=_query_stock,
            required=("item_code",),
        ),
        _tool(
            name="ktg_list_workorders",
            label="查询工单",
            description="按工单编号、产品编码、工单类型或状态查询当前用户有权查看的生产工单。",
            properties={
                "workorder_code": {"type": "string", "minLength": 1},
                "product_code": {"type": "string", "minLength": 1},
                "workorder_type": {
                    "type": "string",
                    "enum": ["SELF", "OUTSOURCE", "PURCHASE"],
                },
                "status": {"type": "string", "minLength": 1},
                **common_properties,
            },
            execute=_list_workorders,
        ),
    )


def _tool(
    *,
    name: str,
    label: str,
    description: str,
    properties: JsonObject,
    execute: ToolExecute,
    required: tuple[str, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1.0",
        description=description,
        label=label,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "rows": {"type": "array"},
                "total": {"type": "integer"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
                "queried_at": {"type": "string"},
            },
            "required": ["kind", "rows", "total", "page", "page_size", "queried_at"],
            "additionalProperties": False,
        },
        required_permissions=(),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=20,
        execute=execute,
    )


async def _search_items(
    context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    params = _page_params(arguments)
    _copy_text(arguments, params, "item_code", "itemCode")
    _copy_text(arguments, params, "item_name", "itemName")
    if "itemCode" not in params and "itemName" not in params:
        return _failure("请提供产品编码或产品名称。", "VALIDATION_ERROR")
    return await _query(context, "/mes/md/mditem/list", params, "items", _item_row)


async def _query_stock(
    context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    params = _page_params(arguments)
    _copy_text(arguments, params, "item_code", "itemCode")
    warehouse_id = arguments.get("warehouse_id")
    if isinstance(warehouse_id, int):
        params["warehouseId"] = warehouse_id
    return await _query(context, "/mes/wm/wmstock/list", params, "stock", _stock_row)


async def _list_workorders(
    context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    params = _page_params(arguments)
    _copy_text(arguments, params, "workorder_code", "workorderCode")
    _copy_text(arguments, params, "product_code", "productCode")
    _copy_text(arguments, params, "workorder_type", "workorderType")
    _copy_text(arguments, params, "status", "status")
    return await _query(
        context, "/mes/pro/workorder/list", params, "workorders", _workorder_row
    )


async def _query(
    context: ToolContext,
    path: str,
    params: Mapping[str, object],
    kind: str,
    projector: object,
) -> ToolResult:
    if not isinstance(context.ktg_client, KTGClient):
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        payload = await context.ktg_client.get(path, params)
    except KTGError as exc:
        return _failure(str(exc), exc.code)
    raw_rows = payload.get("rows", [])
    if not isinstance(raw_rows, list):
        return _failure("KTG 查询结果格式无效。", "KTG_INVALID_RESPONSE")
    projected = [
        cast(JsonObject, projector(row))  # type: ignore[operator]
        for row in raw_rows
        if isinstance(row, Mapping)
    ]
    total = payload.get("total", len(projected))
    if not isinstance(total, int):
        total = len(projected)
    page = cast(int, params["pageNum"])
    page_size = cast(int, params["pageSize"])
    details: JsonObject = {
        "kind": kind,
        "rows": cast(list[JsonValue], projected),
        "total": total,
        "page": page,
        "page_size": page_size,
        "queried_at": datetime.now(UTC).isoformat(),
    }
    return ToolResult(
        content=(
            f"{kind} 查询返回 {len(projected)} 条，本次分页总数为 {total}。"
            f"结构化结果：{projected}"
        ),
        details=details,
    )


def _page_params(arguments: Mapping[str, object]) -> dict[str, object]:
    page = arguments.get("page", 1)
    page_size = arguments.get("page_size", 10)
    return {
        "pageNum": page if isinstance(page, int) else 1,
        "pageSize": page_size if isinstance(page_size, int) else 10,
    }


def _copy_text(
    source: Mapping[str, object], target: dict[str, object], source_key: str, target_key: str
) -> None:
    value = source.get(source_key)
    if isinstance(value, str) and value.strip():
        target[target_key] = value.strip()


def _pick(row: Mapping[object, object], *keys: str) -> JsonObject:
    return {key: cast(JsonValue, row.get(key)) for key in keys}


def _item_row(row: Mapping[object, object]) -> JsonObject:
    return _pick(
        row,
        "itemId",
        "itemCode",
        "itemName",
        "specification",
        "unitOfMeasure",
        "itemOrProduct",
        "enableFlag",
    )


def _stock_row(row: Mapping[object, object]) -> JsonObject:
    return _pick(
        row,
        "materialStockId",
        "itemCode",
        "itemName",
        "specification",
        "unitOfMeasure",
        "quantityOnhand",
        "warehouseId",
        "warehouseCode",
        "warehouseName",
        "locationName",
        "areaName",
        "batchCode",
    )


def _workorder_row(row: Mapping[object, object]) -> JsonObject:
    return _pick(
        row,
        "workorderId",
        "workorderCode",
        "workorderName",
        "workorderType",
        "productCode",
        "productName",
        "unitOfMeasure",
        "quantity",
        "quantityProduced",
        "requestDate",
        "status",
    )


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(
        content=message,
        status=ToolStatus.FAILED,
        error_code=code,
    )

