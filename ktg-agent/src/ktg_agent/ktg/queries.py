"""Allow-listed read-only KTG business query tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import re
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


STOCK_SUMMARY_TOOL = "ktg_query_stock_summary"
_MAX_WAREHOUSE_ID = 2**63 - 1


def build_query_tools() -> tuple[ToolDefinition, ...]:
    return (
        _tool(
            name=STOCK_SUMMARY_TOOL,
            label="分仓库存汇总",
            description=("按完整物料编码查询分仓现存量，由 MES 一次汇总各仓库和各计量单位。"
                         "仅在对象已知为物料/产品且用户询问当前库存、各仓数量和库存总量时使用；未知类型的裸编码应先业务编码识别，不把成品SN、工单或批次当物料。"),
            properties={
                "item_code": {"type": "string", "minLength": 1, "maxLength": 64,
                              "description": "完整物料业务编码，精确匹配。"},
                "warehouse_id": {"type": "integer", "minimum": 1, "maximum": _MAX_WAREHOUSE_ID,
                                 "description": "可选仓库系统 ID，省略时覆盖所有可见仓库。"},
            },
            execute=_query_stock_summary,
            required=("item_code",),
            output_schema={
                "type": "object",
                "properties": {
                    "kind": {"const": "stock_summary"},
                    "filters": {"type": "object"},
                    "queried_at": {"type": "string"},
                    "no_records": {"type": "boolean"},
                    "groups": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["kind", "filters", "queried_at", "no_records", "groups"],
                "additionalProperties": False,
            },
        ),
        _tool(
            name="ktg_count_products",
            label="统计产品数量",
            description="统计当前用户可见的全部产品记录数，不需要编码或名称；只统计产品，不包含物料。适用于‘一共有多少产品’等总数问题。",
            properties={},
            execute=_count_products,
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
    output_schema: JsonObject | None = None,
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
        output_schema=output_schema or {
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
            STOCK_SUMMARY_TOOL: "mes:wm:wmstock:list",
        }[name],),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=20,
        execute=execute,
    )


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


async def _query_stock_summary(
    context: ToolContext, arguments: Mapping[str, object],
) -> ToolResult:
    item_code = arguments.get("item_code")
    warehouse_id = arguments.get("warehouse_id")
    if not isinstance(item_code, str) or not 1 <= len(item_code.strip()) <= 64:
        return _failure("请提供完整物料编码。", "VALIDATION_ERROR")
    item_code = item_code.strip()
    params: dict[str, object] = {"itemCode": item_code}
    if "warehouse_id" in arguments:
        if type(warehouse_id) is not int or not 1 <= warehouse_id <= _MAX_WAREHOUSE_ID:
            return _failure("仓库 ID 必须为有效正整数。", "VALIDATION_ERROR")
        params["warehouseId"] = warehouse_id

    if not isinstance(context.ktg_client, KTGClient):
        return _failure("KTG 查询客户端未配置，库存待核实。", "KTG_CLIENT_MISSING")
    try:
        payload = await context.ktg_client.get("/mes/wm/wmstock/summary", params)
    except KTGError as exc:
        return _failure(f"分仓库存待核实。{exc}", exc.code)

    data = payload.get("data")
    details = _stock_summary_result(data, item_code, warehouse_id)
    if details is None:
        return _failure("KTG 未返回有效的分仓库存汇总，库存待核实。", "KTG_INVALID_RESPONSE")
    meaning = "该物料在查询范围内无匹配库存记录，无库存记录不能等同于数量为零。" if details["no_records"] else "MES 分仓库存汇总。"
    return ToolResult(content=meaning + json.dumps(details, ensure_ascii=False), details=details)


def _stock_summary_result(data: object, item_code: str, warehouse_id: object) -> JsonObject | None:
    if not isinstance(data, dict):
        return None
    groups, has_records, queried_at = data.get("groups"), data.get("hasRecords"), data.get("queriedAt")
    if (data.get("itemCode") != item_code or data.get("warehouseId") != warehouse_id
            or not isinstance(groups, list) or has_records is not bool(groups)
            or not isinstance(queried_at, str)):
        return None
    try:
        if datetime.fromisoformat(queried_at).utcoffset() is None:
            return None
    except ValueError:
        return None

    projected = []
    keys: set[tuple[object, object, object]] = set()
    for group in groups:
        if not isinstance(group, dict):
            return None
        group_id, unit = group.get("warehouseId"), group.get("unitOfMeasure")
        count, quantity = group.get("sourceRowCount"), group.get("quantityOnhand")
        source_item_id, source_item_code = group.get("itemId"), group.get("itemCode")
        if (type(source_item_id) is not int or not isinstance(source_item_code, str)
                or (group_id is not None and type(group_id) is not int)
                or (warehouse_id is not None and group_id != warehouse_id)
                or (unit is not None and not isinstance(unit, str))
                or type(count) is not int or count <= 0
                or not isinstance(quantity, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", quantity)
                or any(group.get(field) is not None and not isinstance(group[field], str)
                       for field in ("warehouseCode", "warehouseName"))):
            return None
        key = (source_item_id, group_id, unit)
        if key in keys:
            return None
        keys.add(key)
        projected.append(_pick(group, "itemId", "itemCode", "warehouseId", "warehouseCode",
                               "warehouseName", "unitOfMeasure", "quantityOnhand", "sourceRowCount"))

    return {
        "kind": "stock_summary",
        "filters": {"item_code": item_code, "warehouse_id": cast(int | None, warehouse_id)},
        "queried_at": queried_at,
        "no_records": not groups,
        "groups": cast(list[JsonValue], projected),
    }


def _pick(row: Mapping[object, object], *keys: str) -> JsonObject:
    return {key: cast(JsonValue, row.get(key)) for key in keys}


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(
        content=message,
        status=ToolStatus.FAILED,
        error_code=code,
    )

