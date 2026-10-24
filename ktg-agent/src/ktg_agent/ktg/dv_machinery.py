"""Read-only tools for the MES equipment ledger."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)


LIST_NAME = "ktg_dv_machinery_list"
DETAIL_NAME = "ktg_dv_machinery_detail"
WORKSHOP_NAME = "ktg_md_machinery_workshop_options"

_MACHINERY_PATH = "/mes/dv/machinery"
_MACHINERY_LIST_PATH = f"{_MACHINERY_PATH}/list"
_MACHINERY_TYPE_LIST_PATH = "/mes/dv/machinerytype/list"
_WORKSHOP_LIST_PATH = "/mes/md/workshop/listAll"
_PAGE_SIZE_MAX = 100
_STATUS = {"STOP", "WORKING", "REPAIR"}

_FILTER_FIELDS = {
    "machinery_code": ("machineryCode", 64),
    "machinery_name": ("machineryName", 255),
    "machinery_brand": ("machineryBrand", 255),
    "machinery_spec": ("machinerySpec", 255),
    "machinery_type_id": ("machineryTypeId", None),
    "machinery_type_code": ("machineryTypeCode", 64),
    "machinery_type_name": ("machineryTypeName", 255),
    "workshop_id": ("workshopId", None),
    "workshop_code": ("workshopCode", 64),
    "workshop_name": ("workshopName", 255),
    "status": ("status", 64),
}


def _schema(properties: dict[str, JsonValue], required: tuple[str, ...] = ()) -> JsonObject:
    return {
        "type": "object", "properties": properties, "required": list(required),
        "additionalProperties": False,
    }


def _output_schema(fields: tuple[str, ...]) -> JsonObject:
    return {"type": "object", "properties": {key: {} for key in fields},
            "required": list(fields), "additionalProperties": False}


def _list_definition() -> ToolDefinition:
    properties: dict[str, JsonValue] = {
        key: {"type": "string", "minLength": 1, "maxLength": limit,
              "description": f"筛选字段 {wire}；编码/品牌/规格/分类编码/车间编码精确匹配，名称字段按页面规则模糊匹配"}
        for key, (wire, limit) in _FILTER_FIELDS.items() if limit is not None
    }
    for key in ("machinery_type_id", "workshop_id"):
        properties[key] = {"type": "integer", "minimum": 1}
    properties["status"] = {"type": "string", "enum": cast(JsonValue, sorted(_STATUS))}
    properties["page"] = {"type": "integer", "minimum": 1, "default": 1}
    properties["page_size"] = {"type": "integer", "minimum": 1, "maximum": _PAGE_SIZE_MAX, "default": 20}

    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        client = _client(context)
        if client is None:
            return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
        try:
            details = await read_machinery_list(client, arguments)
        except KTGError as exc:
            return _failure(str(exc), exc.code)
        rows = cast(list[JsonObject], details["rows"])
        content = f"设备台账共 {details['total']} 条，本页 {len(rows)} 条（第 {details['page']} 页）。"
        if not rows:
            content += "未找到匹配设备。"
        else:
            content += "\n" + "\n".join(
                f"- [{row['machineryId']}] {row['machineryCode']} {row['machineryName']}；"
                f"{row['machineryTypeName'] or '未分类'}；{row['workshopName'] or '未指定车间'}；"
                f"状态 {row['status']}" for row in rows[:20]
            )
        return ToolResult(content=content[:4000], details=details)

    return ToolDefinition(
        name=LIST_NAME, version="1.0", label="查询设备台账",
        description=("按页面支持的筛选条件查询设备台账。编码、品牌、规格和分类/车间编码精确匹配，"
                     "名称字段按后端模糊匹配。分页有上限；名称命中多个设备时只列出候选，不代替用户选择。"),
        input_schema=_schema(properties),
        output_schema=_output_schema(("kind", "rows", "total", "page", "page_size", "filters", "queried_at")),
        required_permissions=("mes:dv:machinery:list",), effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=20, execute=execute,
    )


def _detail_definition() -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        client = _client(context)
        if client is None:
            return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
        try:
            details = await read_machinery_detail(client, arguments)
        except KTGError as exc:
            return _failure(str(exc), exc.code)
        row = cast(JsonObject, details["machinery"])
        content = (f"设备 [{row['machineryId']}] {row['machineryCode']} {row['machineryName']}；"
                   f"分类 {row['machineryTypeName'] or '未指定'}；车间 {row['workshopName'] or '未指定'}；"
                   f"状态 {row['status']}。")
        return ToolResult(content=content, details=details)

    return ToolDefinition(
        name=DETAIL_NAME, version="1.0", label="查询设备详情",
        description=("按设备 ID 或精确设备编码查询详情。必须且只能提供其中一个；编码查找若有多条结果会返回歧义，"
                     "请改用 ID，不要按名称猜测目标。"),
        input_schema=_schema({
            "machinery_id": {"type": "integer", "minimum": 1},
            "machinery_code": {"type": "string", "minLength": 1, "maxLength": 64},
        }),
        output_schema=_output_schema(("kind", "machinery", "queried_at")),
        required_permissions=("mes:dv:machinery:query", "mes:dv:machinery:list"),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=execute,
    )


def _workshop_definition() -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        client = _client(context)
        if client is None:
            return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
        try:
            details = await read_workshop_options(client, arguments)
        except KTGError as exc:
            return _failure(str(exc), exc.code)
        rows = cast(list[JsonObject], details["rows"])
        content = "启用车间候选：" + ("；".join(
            f"[{row['workshopId']}] {row['workshopName']}（{row['workshopCode']}）" for row in rows[:30]
        ) or "无匹配车间。")
        return ToolResult(content=content[:4000], details=details)

    return ToolDefinition(
        name=WORKSHOP_NAME, version="1.0", label="查询启用车间候选",
        description="查询设备台账可选的启用车间。返回车间 ID、编码和名称；同名时须按编码/ID确认。",
        input_schema=_schema({
            "workshop_name": {"type": "string", "minLength": 1, "maxLength": 255},
            "workshop_code": {"type": "string", "minLength": 1, "maxLength": 64},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        }),
        output_schema=_output_schema(("kind", "rows", "total", "truncated", "queried_at")),
        required_permissions=("mes:md:workshop:list",), effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=20, execute=execute,
    )


def build_dv_machinery_tools() -> tuple[ToolDefinition, ...]:
    return (_list_definition(), _detail_definition(), _workshop_definition())


def dv_machinery_factories() -> dict[str, ToolFactory]:
    return {f"dv:{definition.name}": _factory(definition)
            for definition in build_dv_machinery_tools()}


def dv_machinery_manifests() -> tuple[ToolManifest, ...]:
    definitions = build_dv_machinery_tools()
    permission_by_name: dict[str, PermissionPolicy] = {
        LIST_NAME: PermissionLeaf("mes:dv:machinery:list"),
        DETAIL_NAME: PermissionAll((PermissionLeaf("mes:dv:machinery:query"),
                                     PermissionLeaf("mes:dv:machinery:list"))),
        WORKSHOP_NAME: PermissionLeaf("mes:md:workshop:list"),
    }
    return tuple(ToolManifest(
        name=item.name, version=item.version, module="dv", domain="machinery",
        label=item.label, description=item.description,
        permission_policy=permission_by_name[item.name], effect=item.effect,
        execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=True,
        confirmation_policy="none", proposal_store_effect=False,
        handler_ref=f"dv:{item.name}") for item in definitions)


async def read_machinery_list(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    allowed = set(_FILTER_FIELDS) | {"page", "page_size"}
    if set(arguments) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的设备筛选字段。")
    params: dict[str, object] = {}
    filters: JsonObject = {}
    for key, (wire, limit) in _FILTER_FIELDS.items():
        value = arguments.get(key)
        if value is None:
            continue
        if key in {"machinery_type_id", "workshop_id"}:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise KTGError("VALIDATION_ERROR", f"{key} 必须是正整数。")
        elif not isinstance(value, str) or not 1 <= len(value.strip()) <= cast(int, limit):
            raise KTGError("VALIDATION_ERROR", f"{key} 长度无效。")
        elif key == "status" and value not in _STATUS:
            raise KTGError("VALIDATION_ERROR", "status 只允许 STOP、WORKING、REPAIR。")
        clean = value.strip() if isinstance(value, str) else value
        params[wire] = clean
        filters[key] = cast(JsonValue, clean)
    page = _bounded_integer(arguments.get("page", 1), "page", 1, 1000000)
    page_size = _bounded_integer(arguments.get("page_size", 20), "page_size", 1, _PAGE_SIZE_MAX)
    params.update({"pageNum": page, "pageSize": page_size})
    payload = await client.get(_MACHINERY_LIST_PATH, params)
    rows, total = _page_payload(payload)
    return {"kind": "dv_machinery_list", "rows": cast(list[JsonValue], rows),
            "total": total, "page": page, "page_size": page_size,
            "filters": filters, "queried_at": datetime.now(UTC).isoformat()}


async def read_machinery_detail(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    allowed = {"machinery_id", "machinery_code"}
    if set(arguments) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的详情字段。")
    machinery_id = arguments.get("machinery_id")
    code = _text(arguments, "machinery_code", 64)
    if (machinery_id is None) == (code is None):
        raise KTGError("VALIDATION_ERROR", "必须且只能提供 machinery_id 或 machinery_code。")
    if machinery_id is not None:
        if isinstance(machinery_id, bool) or not isinstance(machinery_id, int) or machinery_id <= 0:
            raise KTGError("VALIDATION_ERROR", "machinery_id 必须是正整数。")
        payload = await client.get(f"{_MACHINERY_PATH}/{machinery_id}")
        data = payload.get("data")
        if data is None:
            raise KTGError("MACHINERY_NOT_FOUND", f"未找到 ID 为 {machinery_id} 的设备。")
        row = _project_machinery(data)
        if row["machineryId"] != machinery_id:
            raise KTGError("KTG_INVALID_RESPONSE", "设备详情 ID 与请求不一致。")
    else:
        payload = await client.get(_MACHINERY_LIST_PATH, {"machineryCode": code, "pageNum": 1, "pageSize": 100})
        rows, _ = _page_payload(payload)
        matches = [row for row in rows if cast(str, row["machineryCode"]).casefold() == cast(str, code).casefold()]
        if not matches:
            raise KTGError("MACHINERY_NOT_FOUND", f"未找到编码为 {code} 的设备。")
        if len(matches) != 1:
            candidates = "；".join(f"[{row['machineryId']}] {row['machineryName']}" for row in matches[:10])
            raise KTGError("AMBIGUOUS_MATCH", f"该编码对应多台设备，请按 ID 确认：{candidates}")
        row = matches[0]
        if len(matches) == 1:
            detail_payload = await client.get(f"{_MACHINERY_PATH}/{row['machineryId']}")
            if detail_payload.get("data") is not None:
                row = _project_machinery(detail_payload["data"])
    return {"kind": "dv_machinery_detail", "machinery": row,
            "queried_at": datetime.now(UTC).isoformat()}


async def read_workshop_options(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    allowed = {"workshop_name", "workshop_code", "limit"}
    if set(arguments) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的车间筛选字段。")
    name = _text(arguments, "workshop_name", 255)
    code = _text(arguments, "workshop_code", 64)
    limit = _bounded_integer(arguments.get("limit", 50), "limit", 1, 100)
    payload = await client.get(_WORKSHOP_LIST_PATH)
    data = payload.get("data")
    if not isinstance(data, list):
        raise KTGError("KTG_INVALID_RESPONSE", "车间候选列表格式无效。")
    rows: list[JsonObject] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise KTGError("KTG_INVALID_RESPONSE", "车间候选不是对象。")
        row = _project_workshop(item)
        if row["enableFlag"] != "Y":
            continue
        if code and str(row["workshopCode"]).casefold() != code.casefold():
            continue
        if name and name.casefold() not in str(row["workshopName"]).casefold():
            continue
        rows.append(row)
    total = len(rows)
    return {"kind": "md_machinery_workshop_options",
            "rows": cast(list[JsonValue], rows[:limit]), "total": total,
            "truncated": total > limit, "queried_at": datetime.now(UTC).isoformat()}


async def load_machinery_types(client: KTGClient) -> list[JsonObject]:
    """Return validated flat type rows for write-preview association checks."""
    payload = await client.get(_MACHINERY_TYPE_LIST_PATH)
    data = payload.get("data")
    if not isinstance(data, list):
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类列表格式无效。")
    rows: list[JsonObject] = []
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise KTGError("KTG_INVALID_RESPONSE", "设备分类数据不是对象。")
        type_id, parent_id = item.get("machineryTypeId"), item.get("parentTypeId", 0)
        code, name, flag = item.get("machineryTypeCode"), item.get("machineryTypeName"), item.get("enableFlag")
        if not isinstance(type_id, int) or isinstance(type_id, bool) or type_id <= 0 or type_id in seen:
            raise KTGError("KTG_INVALID_RESPONSE", "设备分类 ID 无效或重复。")
        if parent_id is None:
            parent_id = 0
        if not isinstance(parent_id, int) or isinstance(parent_id, bool) or parent_id < 0:
            raise KTGError("KTG_INVALID_RESPONSE", "设备分类父 ID 无效。")
        if not isinstance(code, str) or not code.strip() or not isinstance(name, str) or not name.strip():
            raise KTGError("KTG_INVALID_RESPONSE", "设备分类名称或编码无效。")
        if flag not in {"Y", "N"}:
            raise KTGError("KTG_INVALID_RESPONSE", "设备分类启用状态无效。")
        seen.add(type_id)
        rows.append({"machineryTypeId": type_id, "parentTypeId": parent_id,
                     "machineryTypeCode": code, "machineryTypeName": name, "enableFlag": flag})
    return rows


def _page_payload(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise KTGError("KTG_INVALID_RESPONSE", "设备分页结果格式无效。")
    projected = [_project_machinery(row) for row in rows]
    if len(projected) > _PAGE_SIZE_MAX:
        raise KTGError("KTG_INVALID_RESPONSE", "设备分页结果超过安全上限。")
    return projected, total


def _project_machinery(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "设备数据不是对象。")
    machinery_id = value.get("machineryId")
    if not isinstance(machinery_id, int) or isinstance(machinery_id, bool) or machinery_id <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "设备 ID 无效。")
    fields = ("machineryCode", "machineryName", "machineryBrand", "machinerySpec",
              "machineryTypeCode", "machineryTypeName", "workshopCode", "workshopName", "status", "remark")
    result: JsonObject = {"machineryId": machinery_id}
    for key in fields:
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"设备字段 {key} 格式无效。")
        result[key] = item
    for key in ("machineryCode", "machineryName"):
        item = result[key]
        if not isinstance(item, str) or not item.strip():
            raise KTGError("KTG_INVALID_RESPONSE", f"设备字段 {key} 缺失或为空。")
    for key in ("machineryTypeId", "workshopId"):
        item = value.get(key)
        if item is not None and (not isinstance(item, int) or isinstance(item, bool)):
            raise KTGError("KTG_INVALID_RESPONSE", f"设备关联字段 {key} 格式无效。")
        result[key] = item
    return result


def _project_workshop(value: Mapping[str, object]) -> JsonObject:
    workshop_id, code, name, flag = (value.get("workshopId"), value.get("workshopCode"),
                                     value.get("workshopName"), value.get("enableFlag"))
    if not isinstance(workshop_id, int) or isinstance(workshop_id, bool) or workshop_id <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "车间 ID 无效。")
    if not isinstance(code, str) or not isinstance(name, str) or flag not in {"Y", "N"}:
        raise KTGError("KTG_INVALID_RESPONSE", "车间候选字段无效。")
    return {"workshopId": workshop_id, "workshopCode": code,
            "workshopName": name, "enableFlag": cast(str, flag)}


def _bounded_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise KTGError("VALIDATION_ERROR", f"{name} 必须在 {minimum}-{maximum} 之间。")
    return value


def _text(arguments: Mapping[str, object], key: str, maximum: int) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise KTGError("VALIDATION_ERROR", f"{key} 长度必须为 1-{maximum}。")
    return value.strip()


def _factory(definition: ToolDefinition) -> ToolFactory:
    return lambda: definition


def _client(context: ToolContext) -> KTGClient | None:
    return context.ktg_client if isinstance(context.ktg_client, KTGClient) else None


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code)
