"""Read-only tools for MES tool fixture types."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_machinery import _bounded_integer, _factory, _client, _failure
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult


LIST_NAME = "ktg_tm_tooltype_list"
DETAIL_NAME = "ktg_tm_tooltype_detail"
OPTIONS_NAME = "ktg_tm_tooltype_options"
TOOLTYPE_PATH = "/mes/tm/tooltype"
TOOLTYPE_LIST = f"{TOOLTYPE_PATH}/list"
MAX_PERIOD = 2147483647
MAINTENANCE_TYPES = {"REGULAR", "USAGE"}
YES_NO = {"Y", "N"}
FILTER_FIELDS = {
    "tool_type_code": ("toolTypeCode", 64), "tool_type_name": ("toolTypeName", 100),
    "code_flag": ("codeFlag", 1), "mainten_type": ("maintenType", 16),
}
_OUTPUT_FIELDS = ("toolTypeId", "toolTypeCode", "toolTypeName", "codeFlag", "maintenType", "maintenPeriod", "remark")


def _schema(properties: dict[str, JsonValue], required: tuple[str, ...] = ()) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _output_schema(fields: tuple[str, ...]) -> JsonObject:
    return {"type": "object", "properties": {field: {} for field in fields},
            "required": list(fields), "additionalProperties": False}


def _text(value: object, field: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise KTGError("VALIDATION_ERROR", f"{field} 必须是长度不超过 {maximum} 的{'非空' if required else ''}文本。")
    return value.strip()


def project_tooltype(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具类型详情格式无效。")
    tool_type_id = value.get("toolTypeId")
    if not isinstance(tool_type_id, int) or isinstance(tool_type_id, bool) or tool_type_id <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具类型ID无效。")
    row: JsonObject = {"toolTypeId": tool_type_id}
    for field in _OUTPUT_FIELDS[1:]:
        item = value.get(field)
        if field == "maintenPeriod" and item is not None and (isinstance(item, bool) or not isinstance(item, int)):
            raise KTGError("KTG_INVALID_RESPONSE", "工装夹具维护周期格式无效。")
        if field != "maintenPeriod" and item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"工装夹具类型字段 {field} 格式无效。")
        row[field] = item
    if not isinstance(row["toolTypeCode"], str) or not row["toolTypeCode"].strip():
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具类型编码缺失。")
    return row


def tooltype_page(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(total, int)
            or isinstance(total, bool) or total < len(rows)):
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具类型分页结果格式无效。")
    return [project_tooltype(row) for row in rows], total


async def fetch_tooltype(client: KTGClient, value: object) -> JsonObject:
    tool_type_id = _bounded_integer(value, "tool_type_id", 1, 9223372036854775807)
    payload = await client.get(f"{TOOLTYPE_PATH}/{tool_type_id}")
    if payload.get("data") is None:
        raise KTGError("TOOLTYPE_NOT_FOUND", f"未找到工装夹具类型ID {tool_type_id}。")
    row = project_tooltype(payload["data"])
    if row["toolTypeId"] != tool_type_id:
        raise KTGError("KTG_INVALID_RESPONSE", "工装夹具类型详情ID不一致。")
    return row


async def read_tooltype_list(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    allowed = set(FILTER_FIELDS) | {"page", "page_size"}
    if set(arguments) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的工装夹具类型筛选字段。")
    params: dict[str, object] = {}
    filters: JsonObject = {}
    for key, (wire, maximum) in FILTER_FIELDS.items():
        if key not in arguments:
            continue
        value = _text(arguments[key], key, maximum, required=True)
        if key == "code_flag" and value not in YES_NO:
            raise KTGError("VALIDATION_ERROR", "code_flag 只允许 Y 或 N。")
        if key == "mainten_type" and value not in MAINTENANCE_TYPES:
            raise KTGError("VALIDATION_ERROR", "mainten_type 只允许 REGULAR 或 USAGE。")
        filters[key] = value
        params[wire] = value
    page = _bounded_integer(arguments.get("page", 1), "page", 1, 1000000)
    size = _bounded_integer(arguments.get("page_size", 20), "page_size", 1, 100)
    params.update(pageNum=page, pageSize=size)
    rows, total = tooltype_page(await client.get(TOOLTYPE_LIST, params))
    return {"kind": "tm_tooltype_list", "rows": cast(list[JsonValue], rows), "total": total,
            "page": page, "page_size": size, "filters": filters, "queried_at": datetime.now(UTC).isoformat()}


async def read_tooltype_detail(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    if set(arguments) - {"tool_type_id", "tool_type_code"} or len(arguments) != 1:
        raise KTGError("VALIDATION_ERROR", "必须且只能提供 tool_type_id 或 tool_type_code。")
    if "tool_type_id" in arguments:
        row = await fetch_tooltype(client, arguments["tool_type_id"])
    else:
        code = _text(arguments["tool_type_code"], "tool_type_code", 64, required=True)
        rows, total = tooltype_page(await client.get(TOOLTYPE_LIST, {"toolTypeCode": code, "pageNum": 1, "pageSize": 100}))
        if total == 0:
            raise KTGError("TOOLTYPE_NOT_FOUND", "未找到该编码的工装夹具类型。")
        if total != 1 or len(rows) != 1:
            matches = "；".join(f"[{item['toolTypeId']}] {item['toolTypeName'] or ''}" for item in rows[:10])
            raise KTGError("AMBIGUOUS_MATCH", f"该编码命中多条记录，请明确ID：{matches}")
        row = await fetch_tooltype(client, rows[0]["toolTypeId"])
        if cast(str, row["toolTypeCode"]).casefold() != code.casefold():
            raise KTGError("KTG_INVALID_RESPONSE", "按编码定位后详情已变化，请按ID重新查询。")
    return {"kind": "tm_tooltype_detail", "tool_type": row, "queried_at": datetime.now(UTC).isoformat()}


async def read_tooltype_options(client: KTGClient) -> JsonObject:
    code_flags: list[JsonObject] = []
    maintenance_types: list[JsonObject] = []
    for dictionary, expected, target in (("sys_yes_no", YES_NO, code_flags),
                                         ("mes_mainten_type", MAINTENANCE_TYPES, maintenance_types)):
        payload = await client.get(f"/system/dict/data/type/{dictionary}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise KTGError("KTG_INVALID_RESPONSE", f"字典 {dictionary} 返回格式无效。")
        for item in data:
            if not isinstance(item, Mapping):
                raise KTGError("KTG_INVALID_RESPONSE", f"字典 {dictionary} 条目格式无效。")
            value, label = item.get("dictValue"), item.get("dictLabel")
            if not isinstance(value, str):
                raise KTGError("KTG_INVALID_RESPONSE", f"字典 {dictionary} 值格式无效。")
            if value in expected:
                if not isinstance(label, str):
                    raise KTGError("KTG_INVALID_RESPONSE", f"字典 {dictionary} 值格式无效。")
                target.append({"value": value, "label": label})
        if {row["value"] for row in target} != expected:
            raise KTGError("KTG_INVALID_RESPONSE", f"字典 {dictionary} 缺少页面所需选项。")
    return {"kind": "tm_tooltype_options", "options": {
        "code_flags": cast(list[JsonValue], code_flags),
        "maintenance_types": cast(list[JsonValue], maintenance_types),
        "period": {"minimum": 1, "maximum": MAX_PERIOD, "integer": True,
                   "unit_by_type": {"REGULAR": "天", "USAGE": "次"}},
    }, "queried_at": datetime.now(UTC).isoformat()}


def build_tm_tooltype_tools() -> tuple[ToolDefinition, ...]:
    async def execute_list(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _run_read(context, lambda client: read_tooltype_list(client, arguments), "查询工装夹具类型")

    async def execute_detail(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _run_read(context, lambda client: read_tooltype_detail(client, arguments), "查询工装夹具类型详情")

    async def execute_options(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        if arguments:
            return _failure("选项查询不接受筛选参数。", "VALIDATION_ERROR")
        return await _run_read(context, read_tooltype_options, "查询工装夹具类型选项")

    return (
        ToolDefinition(name=LIST_NAME, version="1.0", label="查询工装夹具类型",
            description="分页查看有哪些工装夹具类型，返回类型系统 ID、编码、名称和维护配置。编码精确匹配，名称模糊匹配。",
            input_schema=_schema({
                "tool_type_code": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"\S",
                                   "description": "完整类型业务编码，精确筛选。"},
                "tool_type_name": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"\S",
                                   "description": "类型名称关键字，模糊筛选。"},
                "code_flag": {"type": "string", "enum": ["Y", "N"],
                              "description": "类型的编码管理方式。Y 一件一码，N 非独立编码管理。"},
                "mainten_type": {"type": "string", "enum": ["REGULAR", "USAGE"],
                                 "description": "类型维护方式。REGULAR 定期维护，USAGE 按使用次数维护。"},
                "page": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 1,
                         "description": "页码，从 1 开始，省略时查第 1 页。"},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                              "description": "每页记录数，省略时为 20。"},
            }),
            output_schema=_output_schema(("kind", "rows", "total", "page", "page_size", "filters", "queried_at")),
            required_permissions=("mes:tm:tooltype:list",), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=execute_list),
        ToolDefinition(name=DETAIL_NAME, version="1.0", label="查询工装夹具类型详情",
            description=(
                "读取一条工装夹具类型记录，查看编码管理、维护方式及类型维护周期。"
                "必须且只能提供类型系统 ID 或完整类型编码；编码命中多条时请用系统 ID。"
            ),
            input_schema={
                **_schema({
                    "tool_type_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807,
                                     "description": "类型系统 ID，取自类型查询或台账选项中的 toolTypeId。"},
                    "tool_type_code": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"\S",
                                       "description": "类型的完整业务编码，精确匹配。"},
                }),
                "oneOf": [{"required": ["tool_type_id"]}, {"required": ["tool_type_code"]}],
            },
            output_schema=_output_schema(("kind", "tool_type", "queried_at")),
            required_permissions=("mes:tm:tooltype:query", "mes:tm:tooltype:list"), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=execute_detail),
        ToolDefinition(name=OPTIONS_NAME, version="1.0", label="查询工装夹具类型选项",
            description=(
                "读取工装夹具类型表单的编码管理和维护方式字典，以及类型维护周期规则。"
                "REGULAR 周期单位为天，USAGE 周期单位为次。需要已有类型记录和真实类型 ID 时查询工装夹具类型列表。"
            ),
            input_schema=_schema({}), output_schema=_output_schema(("kind", "options", "queried_at")),
            required_permissions=("mes:tm:tooltype:list",), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=execute_options),
    )


async def _run_read(context: ToolContext, operation: Callable[[KTGClient], Awaitable[JsonObject]], label: str) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        data = await operation(client)
        content = _read_content(label, data)
        return ToolResult(content=content[:4000], details=data)
    except KTGError as exc:
        return _failure(str(exc), exc.code)


def _read_content(label: str, data: JsonObject) -> str:
    if data.get("kind") == "tm_tooltype_list":
        rows = cast(list[JsonObject], data["rows"])
        summary = f"{label}：共{data['total']}条，本页{len(rows)}条（第{data['page']}页）。"
        if not rows:
            return summary + "\n没有匹配的工装夹具类型。"
        lines: list[str] = []
        used = len(summary) + 1
        for row in rows:
            line = _tooltype_summary(row)
            if len(lines) >= 30 or used + len(line) + 1 > 3500:
                break
            lines.append(line)
            used += len(line) + 1
        omitted = len(rows) - len(lines)
        if omitted:
            lines.append(f"本页共{len(rows)}条，以上展示{len(rows) - omitted}条，另有{omitted}条未展开；请缩小page_size或继续分页查询。")
        return "\n".join([summary, *lines])
    if data.get("kind") == "tm_tooltype_detail":
        row = cast(JsonObject, data["tool_type"])
        return f"{label}：{_tooltype_summary(row)}；备注{row['remark'] or '无'}"
    options = cast(JsonObject, data["options"])
    flags = "、".join(f"{item['value']}={item['label']}" for item in cast(list[JsonObject], options["code_flags"]))
    types = "、".join(f"{item['value']}={item['label']}" for item in cast(list[JsonObject], options["maintenance_types"]))
    period = cast(JsonObject, options["period"])
    return _bounded_content(f"{label}：编码管理 {flags}；维护类型 {types}；周期{period['minimum']}至{period['maximum']}整数，REGULAR用天、USAGE用次。")


def _bounded_content(value: str, maximum: int = 4000) -> str:
    if len(value) <= maximum:
        return value
    suffix = "\n（结果较长，内容已截断；可缩小查询范围。）"
    return value[:maximum - len(suffix)] + suffix


def _tooltype_summary(row: Mapping[str, object]) -> str:
    code_flag = row.get("codeFlag")
    maintenance_type = row.get("maintenType")
    period = row.get("maintenPeriod")
    if code_flag == "N":
        maintenance_label, period_label = "无", "无"
    else:
        type_value = maintenance_type if isinstance(maintenance_type, str) else ""
        maintenance_label = {"REGULAR": "定期维护", "USAGE": "按使用次数维护"}.get(type_value, "未设置")
        unit = {"REGULAR": "天", "USAGE": "次"}.get(type_value, "")
        period_label = "未设置" if period is None else f"{period}{unit}"
    return (f"[{row['toolTypeId']}] {row['toolTypeCode']} {row['toolTypeName'] or ''}；"
            f"编码管理{code_flag}；维护类型{maintenance_label}；周期{period_label}")


def tm_tooltype_factories() -> dict[str, ToolFactory]:
    return {f"tm_tooltype:{item.name}": _factory(item) for item in build_tm_tooltype_tools()}


def tm_tooltype_manifests() -> tuple[ToolManifest, ...]:
    permission: dict[str, PermissionLeaf | PermissionAll] = {LIST_NAME: PermissionLeaf("mes:tm:tooltype:list"),
                  DETAIL_NAME: PermissionAll((PermissionLeaf("mes:tm:tooltype:query"), PermissionLeaf("mes:tm:tooltype:list"))),
                  OPTIONS_NAME: PermissionLeaf("mes:tm:tooltype:list")}
    return tuple(ToolManifest(name=item.name, version=item.version, module="tm", domain="tooltype",
        label=item.label, description=item.description, permission_policy=permission[item.name],
        effect=item.effect, execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=True, confirmation_policy="none",
        proposal_store_effect=False, handler_ref=f"tm_tooltype:{item.name}")
        for item in build_tm_tooltype_tools())
