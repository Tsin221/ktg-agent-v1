"""Read-only point inspection/maintenance subject tools."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_machinery import _bounded_integer, _schema, _output_schema, _factory, _client, _failure
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult

LIST_NAME = "ktg_dv_subject_list"
DETAIL_NAME = "ktg_dv_subject_detail"
SUBJECT_PATH = "/mes/dv/dvsubject"
TYPE_VALUES = {"CHECK", "MAINTEN"}  # mes_dvsubject_type
ENABLE_VALUES = {"Y", "N"}  # sys_yes_no
FIELDS = {"subject_code": ("subjectCode", 64), "subject_name": ("subjectName", 100),
          "subject_type": ("subjectType", 64), "enable_flag": ("enableFlag", 1),
          "subject_content": ("subjectContent", 250), "subject_standard": ("subjectStandard", 255),
          "remark": ("remark", 500)}


def subject_value(key: str, value: object, *, required: bool = False) -> str:
    maximum = FIELDS[key][1]
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise KTGError("VALIDATION_ERROR", f"{key} 必须是长度不超过 {maximum} 的{'非空' if required else ''}文本。")
    clean = value.strip()
    if key == "subject_type" and clean not in TYPE_VALUES:
        raise KTGError("VALIDATION_ERROR", "subject_type 只允许 CHECK（点检）或 MAINTEN（保养）。")
    if key == "enable_flag" and clean not in ENABLE_VALUES:
        raise KTGError("VALIDATION_ERROR", "enable_flag 只允许 Y（启用）或 N（停用）。")
    return clean


def project_subject(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "项目详情格式无效。")
    sid = value.get("subjectId")
    if not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "项目 ID 无效。")
    row: JsonObject = {"subjectId": sid}
    for field, _limit in FIELDS.values():
        item = value.get(field)
        if item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"项目字段 {field} 格式无效。")
        row[field] = item
    if not isinstance(row["subjectCode"], str) or not row["subjectCode"].strip():
        raise KTGError("KTG_INVALID_RESPONSE", "项目编码缺失。")
    return row


def subject_page(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or len(rows) > 100 or not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "项目分页结果格式无效。")
    return [project_subject(row) for row in rows], total


async def fetch_subject(client: KTGClient, sid: object) -> JsonObject:
    subject_id = _bounded_integer(sid, "subject_id", 1, 9223372036854775807)
    payload = await client.get(f"{SUBJECT_PATH}/{subject_id}")
    if payload.get("data") is None:
        raise KTGError("SUBJECT_NOT_FOUND", f"未找到项目 ID {subject_id}。")
    row = project_subject(payload["data"])
    if row["subjectId"] != subject_id:
        raise KTGError("KTG_INVALID_RESPONSE", "项目详情 ID 不一致。")
    return row


async def read_subject_list(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    allowed = {"subject_code", "subject_name", "subject_type", "enable_flag", "page", "page_size"}
    if set(arguments) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的项目筛选字段。")
    filters: JsonObject = {}
    params: dict[str, object] = {}
    for key in allowed - {"page", "page_size"}:
        if key in arguments:
            clean = subject_value(key, arguments[key], required=True)
            filters[key], params[FIELDS[key][0]] = clean, clean
    page = _bounded_integer(arguments.get("page", 1), "page", 1, 1000000)
    size = _bounded_integer(arguments.get("page_size", 20), "page_size", 1, 100)
    params.update(pageNum=page, pageSize=size)
    rows, total = subject_page(await client.get(f"{SUBJECT_PATH}/list", params))
    return {"kind": "dv_subject_list", "rows": cast(list[JsonValue], rows), "total": total,
            "page": page, "page_size": size, "filters": filters, "queried_at": datetime.now(UTC).isoformat()}


async def read_subject_detail(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    if set(arguments) - {"subject_id", "subject_code"} or len(arguments) != 1:
        raise KTGError("VALIDATION_ERROR", "必须且只能提供 subject_id 或 subject_code。")
    if "subject_id" in arguments:
        row = await fetch_subject(client, arguments["subject_id"])
    else:
        code = subject_value("subject_code", arguments["subject_code"], required=True)
        rows, total = subject_page(await client.get(f"{SUBJECT_PATH}/list", {"subjectCode": code, "pageNum": 1, "pageSize": 100}))
        if total == 0:
            raise KTGError("SUBJECT_NOT_FOUND", "未找到该编码的项目。")
        if total != 1 or len(rows) != 1:
            candidates = "；".join(f"[{r['subjectId']}] {r['subjectName'] or ''}" for r in rows[:10])
            raise KTGError("AMBIGUOUS_MATCH", f"编码可能有历史重复，请明确项目ID：{candidates}")
        row = await fetch_subject(client, rows[0]["subjectId"])
        if cast(str, row["subjectCode"]).casefold() != code.casefold():
            raise KTGError("KTG_INVALID_RESPONSE", "编码定位后详情已变化，请按ID重新查询。")
    return {"kind": "dv_subject_detail", "subject": row, "queried_at": datetime.now(UTC).isoformat()}


def build_dv_subject_tools() -> tuple[ToolDefinition, ...]:
    result = []
    for detail in (False, True):
        async def execute(context: ToolContext, arguments: Mapping[str, object], *, detail: bool = detail) -> ToolResult:
            client = _client(context)
            if client is None:
                return _failure("KTG查询客户端未配置。", "KTG_CLIENT_MISSING")
            try:
                data = await (read_subject_detail(client, arguments) if detail else read_subject_list(client, arguments))
                if detail:
                    row = cast(JsonObject, data["subject"])
                    text = f"项目 [{row['subjectId']}] {row['subjectCode']} {row['subjectName'] or ''}；类型 {row['subjectType']}；启用 {row['enableFlag']}。"
                else:
                    rows = cast(list[JsonObject], data["rows"])
                    text = f"点检保养项目共 {data['total']} 条，第 {data['page']} 页。\n" + "\n".join(
                        f"[{r['subjectId']}] {r['subjectCode']} {r['subjectName'] or ''}；{r['subjectType']}；启用{r['enableFlag']}" for r in rows[:20])
                return ToolResult(content=text[:4000], details=data)
            except KTGError as exc:
                return _failure(str(exc), exc.code)
        properties: dict[str, JsonValue] = {"subject_code": {"type": "string", "minLength": 1, "maxLength": 64}}
        if detail:
            properties["subject_id"] = {"type": "integer", "minimum": 1}
        else:
            properties.update({"subject_name": {"type": "string", "maxLength": 100},
                "subject_type": {"type": "string", "enum": cast(JsonValue, sorted(TYPE_VALUES))},
                "enable_flag": {"type": "string", "enum": cast(JsonValue, sorted(ENABLE_VALUES))},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}})
        result.append(ToolDefinition(name=DETAIL_NAME if detail else LIST_NAME, version="1.0",
            label="查询点检保养项目详情" if detail else "查询点检保养项目",
            description="按明确ID或精确编码读取项目；同编码多条时必须让用户选择ID。" if detail else
                "分页查询点检保养项目；编码精确匹配、名称模糊匹配，类型CHECK/MAINTEN，启用Y/N。不涉及计划。",
            input_schema=_schema(properties), output_schema=_output_schema(("kind", "subject", "queried_at") if detail else
                ("kind", "rows", "total", "page", "page_size", "filters", "queried_at")),
            required_permissions=("mes:dv:dvsubject:query", "mes:dv:dvsubject:list") if detail else ("mes:dv:dvsubject:list",),
            effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
            timeout_seconds=20, execute=execute))
    return tuple(result)


def dv_subject_factories() -> dict[str, ToolFactory]:
    return {f"dv:{t.name}": _factory(t) for t in build_dv_subject_tools()}


def dv_subject_manifests() -> tuple[ToolManifest, ...]:
    return tuple(ToolManifest(name=t.name, version=t.version, module="dv", domain="subject", label=t.label,
        description=t.description, permission_policy=PermissionAll(tuple(PermissionLeaf(p) for p in t.required_permissions)),
        effect=t.effect, execution_mode=t.execution_mode, retry_policy=t.retry_policy, timeout_seconds=t.timeout_seconds,
        expose_to_model=True, confirmation_policy="none", proposal_store_effect=False, handler_ref=f"dv:{t.name}")
        for t in build_dv_subject_tools())
