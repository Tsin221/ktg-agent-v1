"""Read-only tools for the MES tool ledger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import cast
import json

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_machinery import _bounded_integer, _client, _factory, _failure
from ktg_agent.ktg.tm_tooltype import read_tooltype_options
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import (
    ExecutionMode,
    RetryPolicy,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)

LIST_NAME = "ktg_tm_tool_list"
DETAIL_NAME = "ktg_tm_tool_detail"
OPTIONS_NAME = "ktg_tm_tool_options"
TOOL_PATH = "/mes/tm/tool"
TOOL_LIST = f"{TOOL_PATH}/list"
MAINTENANCE_TYPES = {"REGULAR", "USAGE"}
TOOL_STATUSES = {"STORE", "ISSUE", "SCRAP", "REPARE"}
FIELDS = (
    "toolId", "toolCode", "toolName", "brand", "spec", "toolTypeId", "toolTypeCode",
    "toolTypeName", "codeFlag", "quantity", "quantityAvail", "maintenType",
    "nextMaintenPeriod", "nextMaintenDate", "status", "remark",
)
FILTERS = {
    "tool_code": ("toolCode", 64),
    "tool_name": ("toolName", 100),
    "brand": ("brand", 100),
    "spec": ("spec", 100),
    "tool_type_id": ("toolTypeId", None),
    "status": ("status", 16),
    "mainten_type": ("maintenType", 16),
}


def _schema(properties: dict[str, JsonValue], required: tuple[str, ...] = ()) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _output_schema(fields: tuple[str, ...]) -> JsonObject:
    return {
        "type": "object",
        "properties": {field: {} for field in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise KTGError("VALIDATION_ERROR", f"{field} 必须是1至{maximum}字符的非空文本。")
    return value.strip()


def project_tool(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具记录格式无效。")
    tool_id = value.get("toolId")
    if not isinstance(tool_id, int) or isinstance(tool_id, bool) or tool_id <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具ID无效。")

    row: JsonObject = {"toolId": tool_id}
    integer_fields = {"toolTypeId", "quantity", "quantityAvail", "nextMaintenPeriod"}
    for field in FIELDS[1:]:
        item = value.get(field)
        if field in integer_fields and item is not None:
            if not isinstance(item, int) or isinstance(item, bool):
                raise KTGError("KTG_INVALID_RESPONSE", f"{field} 数值格式无效。")
        elif field not in integer_fields and item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"{field} 字段格式无效。")
        row[field] = item

    tool_code = row["toolCode"]
    if not isinstance(tool_code, str) or not tool_code.strip():
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具编码缺失。")
    return row


def tool_page(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if (
        not isinstance(rows, list)
        or len(rows) > 100
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < len(rows)
    ):
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具分页结果无效。")
    return [project_tool(row) for row in rows], total


async def fetch_tool(client: KTGClient, value: object) -> JsonObject:
    tool_id = _bounded_integer(value, "tool_id", 1, 9223372036854775807)
    payload = await client.get(f"{TOOL_PATH}/{tool_id}")
    if payload.get("data") is None:
        raise KTGError("TOOL_NOT_FOUND", f"未找到工装夹具ID {tool_id}。")
    row = project_tool(payload["data"])
    if row["toolId"] != tool_id:
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具详情ID不匹配。")
    return row


async def read_tool_list(client: KTGClient, args: Mapping[str, object]) -> JsonObject:
    if set(args) - set(FILTERS) - {"page", "page_size"}:
        raise KTGError("VALIDATION_ERROR", "包含不支持的工装夹具筛选字段。")
    params: dict[str, object] = {}
    filters: JsonObject = {}
    for key, (wire_name, maximum) in FILTERS.items():
        if key not in args:
            continue
        value = args[key]
        if key == "tool_type_id":
            value = _bounded_integer(value, key, 1, 9223372036854775807)
        else:
            value = _text(value, key, cast(int, maximum))
        if key == "mainten_type" and value not in MAINTENANCE_TYPES:
            raise KTGError("VALIDATION_ERROR", "mainten_type只允许REGULAR或USAGE。")
        if key == "status" and value not in TOOL_STATUSES:
            raise KTGError("VALIDATION_ERROR", "status不是工装台账支持的状态值。")
        filters[key] = cast(JsonValue, value)
        params[wire_name] = value

    page = _bounded_integer(args.get("page", 1), "page", 1, 1000000)
    page_size = _bounded_integer(args.get("page_size", 20), "page_size", 1, 100)
    params.update({"pageNum": page, "pageSize": page_size})
    rows, total = tool_page(await client.get(TOOL_LIST, params))
    return {
        "kind": "tm_tool_list",
        "rows": cast(list[JsonValue], rows),
        "total": total,
        "page": page,
        "page_size": page_size,
        "filters": filters,
        "queried_at": datetime.now(UTC).isoformat(),
    }


async def read_tool_detail(client: KTGClient, args: Mapping[str, object]) -> JsonObject:
    if set(args) - {"tool_id", "tool_code"} or len(args) != 1:
        raise KTGError("VALIDATION_ERROR", "必须且只能提供 tool_id 或 tool_code。")
    if "tool_id" in args:
        row = await fetch_tool(client, args["tool_id"])
    else:
        code = _text(args["tool_code"], "tool_code", 64)
        rows, total = tool_page(
            await client.get(TOOL_LIST, {"toolCode": code, "pageNum": 1, "pageSize": 100})
        )
        matches = [
            item for item in rows
            if cast(str, item["toolCode"]).casefold() == code.casefold()
        ]
        if total == 0 or not matches:
            raise KTGError("TOOL_NOT_FOUND", "未找到该工装夹具编码。")
        if total != 1 or len(matches) != 1:
            raise KTGError("AMBIGUOUS_MATCH", "该工装夹具编码匹配多条记录，请改用明确ID。")
        row = await fetch_tool(client, matches[0]["toolId"])
        if cast(str, row["toolCode"]).casefold() != code.casefold():
            raise KTGError("KTG_INVALID_RESPONSE", "按编码定位后记录发生变化。")
    return {"kind": "tm_tool_detail", "tool": row, "queried_at": datetime.now(UTC).isoformat()}


async def read_tool_options(client: KTGClient) -> JsonObject:
    base = await read_tooltype_options(client)
    type_payload = await client.get("/mes/tm/tooltype/list", {"pageNum": 1, "pageSize": 100})
    type_rows, type_total = type_payload.get("rows"), type_payload.get("total")
    if (
        not isinstance(type_rows, list)
        or not isinstance(type_total, int)
        or isinstance(type_total, bool)
        or type_total < len(type_rows)
    ):
        raise KTGError("KTG_INVALID_RESPONSE", "工装类型候选格式无效。")
    tool_types: list[JsonValue] = []
    for item in type_rows[:100]:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("toolTypeId"), int)
            or isinstance(item.get("toolTypeId"), bool)
            or not isinstance(item.get("toolTypeCode"), str)
            or not isinstance(item.get("toolTypeName"), str)
            or item.get("codeFlag") not in ("Y", "N")
        ):
            raise KTGError("KTG_INVALID_RESPONSE", "工装类型候选字段无效。")
        tool_types.append({
            "toolTypeId": cast(int, item["toolTypeId"]),
            "toolTypeCode": cast(str, item["toolTypeCode"]),
            "toolTypeName": cast(str, item["toolTypeName"]),
            "codeFlag": cast(str, item["codeFlag"]),
        })

    status_payload = await client.get("/system/dict/data/type/mes_tool_status")
    status_rows = status_payload.get("data")
    if not isinstance(status_rows, list):
        raise KTGError("KTG_INVALID_RESPONSE", "工装状态字典格式无效。")
    statuses: list[JsonValue] = []
    for item in status_rows:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("dictValue"), str)
            or not isinstance(item.get("dictLabel"), str)
        ):
            raise KTGError("KTG_INVALID_RESPONSE", "工装状态选项格式无效。")
        statuses.append({"value": cast(str, item["dictValue"]), "label": cast(str, item["dictLabel"])})

    base_options = cast(JsonObject, base["options"])
    return {
        "kind": "tm_tool_options",
        "options": {
            "tool_types": tool_types,
            "tool_types_total": type_total,
            "tool_types_truncated": type_total > len(tool_types),
            "maintenance_types": base_options["maintenance_types"],
            "statuses": statuses,
            "quantity": {"minimum": 0, "maximum": 99999999, "integer": True},
            "maintenance_period": {
                "minimum": 1,
                "maximum": 2147483647,
                "integer": True,
                "unit_by_type": {"REGULAR": "天", "USAGE": "次"},
            },
        },
        "queried_at": datetime.now(UTC).isoformat(),
    }


async def _run(
    context: ToolContext,
    operation: Callable[[KTGClient], Awaitable[JsonObject]],
    label: str,
) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        details = await operation(client)
        return ToolResult(content=_read_content(label, details), details=details)
    except KTGError as exc:
        return _failure(str(exc), exc.code)


def _read_content(label: str, data: JsonObject) -> str:
    if data.get("kind") == "tm_tool_list":
        rows = cast(list[JsonObject], data["rows"])
        parts = [f"{label}：共{data['total']}条，本页{len(rows)}条（第{data['page']}页）。"]
        for row in rows[:30]:
            parts.append(
                f"[{row['toolId']}] {row['toolCode']} {row['toolName']}；类型{row['toolTypeName']}；"
                f"数量{row['quantity']}，可用{row['quantityAvail']}；状态{row['status']}；维护{row['maintenType']}。"
            )
        if len(rows) > 30:
            parts.append(f"本页还有{len(rows) - 30}条未展示，请缩小page_size。")
        return "\n".join(parts)[:4000]
    if data.get("kind") == "tm_tool_detail":
        row = cast(JsonObject, data["tool"])
        return (
            f"{label}：ID={row['toolId']}；{row['toolCode']} {row['toolName']}；类型ID={row['toolTypeId']} {row['toolTypeName']}；品牌{row['brand'] or '无'}；型号{row['spec'] or '无'}；"
            f"数量{row['quantity']}，可用{row['quantityAvail']}；状态{row['status']}；"
            f"维护方式{row['maintenType']}；下次保养日期(nextMaintenDate)={row['nextMaintenDate'] or '未设置'}；"
            f"下次按使用次数保养周期(nextMaintenPeriod)={row['nextMaintenPeriod'] or '未设置'}次；备注{row['remark'] or '无'}。"
            "台账已有下次保养日期时直接据实回答，不需要从类型维护周期推算；次数周期为空不影响已保存日期。无日期时说明未设置，USAGE次数不能换算为日期。"
        )
    options = cast(JsonObject, data["options"])
    types = cast(list[JsonValue], options["tool_types"])
    statuses = cast(list[JsonValue], options["statuses"])
    truncation = "（已截断）" if options["tool_types_truncated"] else ""
    return (
        f"{label}：工装类型候选{len(types)}条{truncation}：{json.dumps(types, ensure_ascii=False)}；维护方式{options['maintenance_types']}；"
        f"状态{statuses}；数量0至99999999整数；codeFlag=Y为一件一码，数量必须1，用户指定2时说明冲突，不擅自改数量。类型名称不是ID，新增必传真实toolTypeId为tool_type_id；台账REGULAR用nextMaintenDate日期，USAGE用nextMaintenPeriod次数；类型维护周期是另一概念。"
    )[:4000]


def build_tm_tool_tools() -> tuple[ToolDefinition, ...]:
    async def listing(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _run(context, lambda client: read_tool_list(client, arguments), "工装夹具台账")

    async def detail(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _run(context, lambda client: read_tool_detail(client, arguments), "工装夹具详情")

    async def options(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        if arguments:
            return _failure("选项查询不接受参数。", "VALIDATION_ERROR")
        return await _run(context, read_tool_options, "工装夹具选项")

    return (
        ToolDefinition(
            name=LIST_NAME, version="1.0", label="查询工装夹具台账",
            description="分页查询工装夹具台账，编码/品牌/型号精确匹配，名称模糊匹配。",
            input_schema=_schema({
                "tool_code": {"type": "string", "minLength": 1, "maxLength": 64},
                "tool_name": {"type": "string", "minLength": 1, "maxLength": 100},
                "brand": {"type": "string", "minLength": 1, "maxLength": 100},
                "spec": {"type": "string", "minLength": 1, "maxLength": 100},
                "tool_type_id": {"type": "integer", "minimum": 1},
                "status": {"type": "string", "enum": ["STORE", "ISSUE", "SCRAP", "REPARE"]},
                "mainten_type": {"type": "string", "enum": ["REGULAR", "USAGE"]},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            }),
            output_schema=_output_schema(("kind", "rows", "total", "page", "page_size", "filters", "queried_at")),
            required_permissions=("mes:tm:tool:list",), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=listing,
        ),
        ToolDefinition(
            name=DETAIL_NAME, version="1.0", label="查询工装夹具详情",
            description="按明确ID或精确唯一业务编码读取工装夹具台账详情。",
            input_schema=_schema({
                "tool_id": {"type": "integer", "minimum": 1},
                "tool_code": {"type": "string", "minLength": 1, "maxLength": 64},
            }),
            output_schema=_output_schema(("kind", "tool", "queried_at")),
            required_permissions=("mes:tm:tool:query", "mes:tm:tool:list"), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=detail,
        ),
        ToolDefinition(
            name=OPTIONS_NAME, version="1.0", label="查询工装夹具选项",
            description="读取真实工装类型ID/名称/codeFlag、维护方式和状态选项。新增需要类型ID时可用本会话已查询选项，缺失、不完整、类型变化或用户明确重查时再读取；codeFlag=Y数量必须1。状态字典仅用于解释/查询，不代表支持修改状态；拒绝状态修改无需读取本工具。",
            input_schema=_schema({}),
            output_schema=_output_schema(("kind", "options", "queried_at")),
            required_permissions=("mes:tm:tooltype:list",), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=options,
        ),
    )


def tm_tool_factories() -> dict[str, ToolFactory]:
    return {f"tm_tool:{tool.name}": _factory(tool) for tool in build_tm_tool_tools()}


def tm_tool_manifests() -> tuple[ToolManifest, ...]:
    policies: dict[str, PermissionPolicy] = {
        LIST_NAME: PermissionLeaf("mes:tm:tool:list"),
        DETAIL_NAME: PermissionAll((
            PermissionLeaf("mes:tm:tool:query"), PermissionLeaf("mes:tm:tool:list"),
        )),
        OPTIONS_NAME: PermissionLeaf("mes:tm:tooltype:list"),
    }
    return tuple(
        ToolManifest(
            name=tool.name, version=tool.version, module="tm", domain="tool",
            label=tool.label, description=tool.description,
            permission_policy=policies[tool.name], effect=tool.effect,
            execution_mode=tool.execution_mode, retry_policy=tool.retry_policy,
            timeout_seconds=tool.timeout_seconds, expose_to_model=True,
            confirmation_policy="none", proposal_store_effect=False,
            handler_ref=f"tm_tool:{tool.name}",
        )
        for tool in build_tm_tool_tools()
    )
