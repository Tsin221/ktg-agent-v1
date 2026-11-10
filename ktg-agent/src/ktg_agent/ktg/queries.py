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
        "page": {"type": "integer", "minimum": 1, "default": 1,
                 "description": "页码，从第 1 页开始。"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10,
                      "description": "每页返回的记录数，默认 10 条，最多 20 条。"},
    }
    return (
        _tool(
            name="ktg_count_products",
            label="统计产品数量",
            description="统计当前用户可见的全部产品记录数，不需要编码或名称；只统计产品，不包含物料。适用于‘一共有多少产品’等总数问题。",
            properties={},
            execute=_count_products,
        ),
        _tool(
            name="ktg_search_items",
            label="查询产品",
            description=("分页查询产品或物料资料和列表明细。可按编码或名称模糊筛选；"
                         "省略筛选时只列当前用户可见的产品。产品总数用 ktg_count_products。"),
            properties={
                "item_code": {"type": "string", "minLength": 1,
                              "description": "产品或物料的业务编码，按包含该文本筛选。"},
                "item_name": {"type": "string", "minLength": 1,
                              "description": "产品或物料的业务名称，按包含该文本筛选。"},
                **common_properties,
            },
            execute=_search_items,
        ),
        _tool(
            name="ktg_query_stock",
            label="查询库存",
            description=("查询指定物料在当前用户可见仓库中的库存明细，须传完整物料编码。"
                         "省略仓库时查询该物料的各仓记录。库存单据执行回执用 ktg_wm_operation_result。"),
            properties={
                "item_code": {"type": "string", "minLength": 1,
                              "description": "完整物料业务编码，取自产品或物料查询结果的 itemCode，按精确编码筛选。"},
                "warehouse_id": {"type": "integer", "minimum": 1,
                                 "description": "仓库系统 ID，可取自库存结果的 warehouseId；省略时不限制仓库。"},
                **common_properties,
            },
            execute=_query_stock,
            required=("item_code",),
        ),
        _tool(
            name="ktg_list_workorders",
            label="查询生产工单列表",
            description=("分页筛选当前用户可见的生产工单列表。按工单编号、产品编码、类型或状态筛选。"
                         "读取单张工单详情用 ktg_pro_workorder_get，查看生产进度用已启用的工单进度工具。"),
            properties={
                "workorder_code": {"type": "string", "minLength": 1,
                                   "description": "生产工单完整业务编号，精确匹配。"},
                "product_code": {"type": "string", "minLength": 1,
                                 "description": "工单对应的完整产品业务编码，精确匹配。"},
                "workorder_type": {
                    "type": "string",
                    "enum": ["SELF", "OUTSOURCE", "PURCHASE"],
                    "description": "工单类型。SELF 为自制，OUTSOURCE 为委外，PURCHASE 为外购。",
                },
                "status": {"type": "string", "minLength": 1,
                           "description": "MES 工单状态的机器值，按精确状态筛选。"},
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
        required_permissions=({
            "ktg_count_products": "mes:md:mditem:list",
            "ktg_search_items": "mes:md:mditem:list",
            "ktg_query_stock": "mes:wm:wmstock:list",
            "ktg_list_workorders": "mes:pro:workorder:list",
        }[name],),
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
        params["itemOrProduct"] = "PRODUCT"
    return await _query(context, "/mes/md/mditem/list", params, "items", _item_row)


async def _count_products(
    context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    if not isinstance(context.ktg_client, KTGClient):
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        payload = await context.ktg_client.get(
            "/mes/md/mditem/list",
            {"itemOrProduct": "PRODUCT", "pageNum": 1, "pageSize": 1},
        )
    except KTGError as exc:
        return _failure(str(exc), exc.code)
    total = payload.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return _failure("KTG 未返回有效的产品总数。", "KTG_INVALID_RESPONSE")
    details: JsonObject = {
        "kind": "product_count", "rows": [], "total": total,
        "page": 1, "page_size": 1, "queried_at": datetime.now(UTC).isoformat(),
    }
    return ToolResult(
        content=f"按当前用户权限查询，产品记录总数为 {total} 条（不含物料）。",
        details=details,
    )


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
        "version",
    )


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(
        content=message,
        status=ToolStatus.FAILED,
        error_code=code,
    )

