"""Read-only equipment repair tools."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_checkplan import _dict_rows, _positive_id, _text
from ktg_agent.ktg.dv_machinery import (_bounded_integer, _schema, _output_schema, _factory,
    _client, _failure, _page_payload)
from ktg_agent.ktg.dv_subject import subject_page
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult

REPAIR = "/mes/dv/repair"
LINE = "/mes/dv/repairline"
MACHINERY = "/mes/dv/machinery"
SUBJECT = "/mes/dv/dvsubject"
DICT = "/system/dict/data/type"
REPAIR_FIELDS = ("repairId", "repairCode", "repairName", "machineryId", "machineryCode", "machineryName",
    "machineryBrand", "machinerySpec", "machineryTypeId", "requireDate", "finishDate", "confirmDate",
    "repairResult", "acceptedBy", "acceptName", "confirmBy", "confirmName", "status", "remark")
LINE_FIELDS = ("lineId", "repairId", "subjectId", "subjectCode", "subjectName", "subjectType",
    "subjectContent", "subjectStandard", "malfunction", "malfunctionUrl", "repairDes", "remark")
FILTERS = {"repair_code": ("repairCode", 64), "repair_name": ("repairName", 100),
    "machinery_code": ("machineryCode", 64), "machinery_name": ("machineryName", 255),
    "repair_result": ("repairResult", 64), "status": ("status", 64)}
_FILTER_DESCRIPTIONS = {
    "repair_code": "维修单业务编号，精确筛选。",
    "repair_name": "维修单名称，模糊筛选。",
    "machinery_code": "维修单所关联设备的业务编码，精确筛选。",
    "machinery_name": "维修单所关联设备的名称，模糊筛选。",
    "repair_result": "维修结果字典值，取自维修选项中的 repair_results[].value，精确筛选。",
    "status": "维修单据状态字典值，取自维修选项中的 statuses[].value，精确筛选。",
}
_READ_DESCRIPTIONS = {
    "list": "分页筛选维修单列表，返回维修单头、总数和可用于后续查询的 repairId。可按维修单名称、设备、维修结果或单据状态筛选。",
    "detail": "读取一张维修单的单头详情，包括关联设备、报修日期、完工日期、验收和维修结果。必须且只能传 repair_id 或 repair_code 之一。",
    "lines": "分页读取指定维修单的维修内容行，包括维修项目、故障现象、维修描述和 lineId。先从维修单列表或详情取得 repair_id。",
    "options": "查询维修选项和字典值，返回维修结果、单据状态和当前支持的草稿操作。无需参数。",
    "machinery_candidates": "为草稿维修单选取关联设备，按设备编码或名称查询维修设备候选，返回可传入预览的 machineryId。",
    "subject_candidates": "为草稿维修内容选取关联项目，按项目编码或名称查询维修项目候选，返回可传入预览的 subjectId。",
}


def _project(value: object, fields: tuple[str, ...], id_key: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "维修记录格式无效。")
    row: JsonObject = {}
    ids = {id_key, "repairId", "machineryId", "machineryTypeId", "subjectId"}
    for key in fields:
        item = value.get(key)
        if key == id_key and (not isinstance(item, int) or isinstance(item, bool) or item <= 0):
            raise KTGError("KTG_INVALID_RESPONSE", f"{key} 无效。")
        if key in ids and item is not None and (not isinstance(item, int) or isinstance(item, bool) or item <= 0):
            raise KTGError("KTG_INVALID_RESPONSE", f"{key} 无效。")
        if key not in ids and item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"{key} 格式无效。")
        row[key] = item
    return row


def repair_page(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or len(rows) > 100 or not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "维修单分页格式无效。")
    return [_project(row, REPAIR_FIELDS, "repairId") for row in rows], total


def line_page(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or len(rows) > 100 or not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "维修内容分页格式无效。")
    return [_project(row, LINE_FIELDS, "lineId") for row in rows], total


async def fetch_repair(client: KTGClient, repair_id: object) -> JsonObject:
    rid = _positive_id(repair_id, "repair_id")
    raw = await client.get(f"{REPAIR}/{rid}")
    if raw.get("data") is None:
        raise KTGError("REPAIR_NOT_FOUND", f"维修单 {rid} 不存在。", status=404)
    row = _project(raw["data"], REPAIR_FIELDS, "repairId")
    if row["repairId"] != rid: raise KTGError("KTG_INVALID_RESPONSE", "维修单 ID 与请求不一致。")
    return row


async def fetch_line(client: KTGClient, line_id: object) -> JsonObject:
    lid = _positive_id(line_id, "line_id")
    raw = await client.get(f"{LINE}/{lid}")
    if raw.get("data") is None:
        raise KTGError("LINE_NOT_FOUND", f"维修内容 {lid} 不存在。", status=404)
    row = _project(raw["data"], LINE_FIELDS, "lineId")
    if row["lineId"] != lid: raise KTGError("KTG_INVALID_RESPONSE", "维修内容 ID 与请求不一致。")
    return row


def _paging(args: Mapping[str, object]) -> tuple[int, int]:
    return (_bounded_integer(args.get("page", 1), "page", 1, 1000000),
            _bounded_integer(args.get("page_size", 20), "page_size", 1, 100))


async def read_repair_list(client: KTGClient, args: Mapping[str, object]) -> JsonObject:
    if set(args) - (set(FILTERS) | {"page", "page_size"}): raise KTGError("VALIDATION_ERROR", "不支持的维修单筛选字段。")
    page, size = _paging(args)
    params: dict[str, object] = {"pageNum": page, "pageSize": size}
    filters: JsonObject = {}
    for key, (wire, limit) in FILTERS.items():
        if key in args:
            filters[key] = params[wire] = _text(args[key], key, limit)
    rows, total = repair_page(await client.get(f"{REPAIR}/list", params))
    return {"kind": "dv_repair_list", "rows": cast(list[JsonValue], rows), "total": total,
        "page": page, "page_size": size, "filters": filters, "queried_at": datetime.now(UTC).isoformat()}


async def read_repair_detail(client: KTGClient, args: Mapping[str, object]) -> JsonObject:
    if set(args) not in ({"repair_id"}, {"repair_code"}): raise KTGError("VALIDATION_ERROR", "只能提供 repair_id 或 repair_code。")
    if "repair_id" in args:
        row = await fetch_repair(client, args["repair_id"])
    else:
        code = _text(args["repair_code"], "repair_code", 64)
        rows, total = repair_page(await client.get(f"{REPAIR}/list", {"repairCode": code, "pageNum": 1, "pageSize": 100}))
        if total == 0: raise KTGError("REPAIR_NOT_FOUND", "该编号维修单不存在。", status=404)
        if total != 1 or len(rows) != 1:
            raise KTGError("AMBIGUOUS_MATCH", "维修单编号重复，请按 ID 指定：" + ", ".join(str(r["repairId"]) for r in rows[:10]))
        row = await fetch_repair(client, rows[0]["repairId"])
        if row["repairCode"] != code: raise KTGError("KTG_INVALID_RESPONSE", "维修单编号定位后已变化。")
    return {"kind": "dv_repair_detail", "repair": row, "queried_at": datetime.now(UTC).isoformat()}


async def read_repair_lines(client: KTGClient, args: Mapping[str, object]) -> JsonObject:
    if "repair_id" not in args or set(args) - {"repair_id", "page", "page_size"}:
        raise KTGError("VALIDATION_ERROR", "必须提供 repair_id。")
    rid = _positive_id(args["repair_id"], "repair_id")
    repair = await fetch_repair(client, rid)
    page, size = _paging(args)
    rows, total = line_page(await client.get(f"{LINE}/list", {"repairId": rid, "pageNum": page, "pageSize": size}))
    if any(row["repairId"] != rid for row in rows): raise KTGError("KTG_INVALID_RESPONSE", "MES返回其他维修单的维修内容。")
    return {"kind": "dv_repair_lines", "repair": repair, "rows": cast(list[JsonValue], rows),
        "total": total, "page": page, "page_size": size, "queried_at": datetime.now(UTC).isoformat()}


async def read_repair_options(client: KTGClient) -> JsonObject:
    results, statuses = await client.get(f"{DICT}/mes_repair_result"), await client.get(f"{DICT}/mes_order_status")
    return {"kind": "dv_repair_options", "repair_results": cast(list[JsonValue], _dict_rows(results, "维修结果")),
        "statuses": cast(list[JsonValue], _dict_rows(statuses, "单据状态")),
        "supported_writes": cast(JsonValue, ["草稿新增", "草稿修改", "草稿删除", "草稿维修内容增改删"]),
        "queried_at": datetime.now(UTC).isoformat()}


async def read_candidates(client: KTGClient, args: Mapping[str, object], kind: str) -> JsonObject:
    code_key, name_key = (("machinery_code", "machinery_name") if kind == "machinery" else ("subject_code", "subject_name"))
    if set(args) - {code_key, name_key, "page", "page_size"}: raise KTGError("VALIDATION_ERROR", "不支持的候选筛选字段。")
    page, size = _paging(args)
    params: dict[str, object] = {"pageNum": page, "pageSize": size}
    for key, wire, limit in ((code_key, "machineryCode" if kind == "machinery" else "subjectCode", 64),
                             (name_key, "machineryName" if kind == "machinery" else "subjectName", 255)):
        if key in args: params[wire] = _text(args[key], key, limit)
    raw = await client.get(f"{MACHINERY if kind == 'machinery' else SUBJECT}/list", params)
    rows, total = (_page_payload(raw) if kind == "machinery" else subject_page(raw))
    if kind == "machinery":
        rows = [{key: row.get(key) for key in ("machineryId", "machineryCode", "machineryName", "machineryBrand", "machinerySpec", "machineryTypeId", "status")} for row in rows]
    return {"kind": f"dv_repair_{kind}_candidates", "rows": cast(list[JsonValue], rows), "total": total,
        "page": page, "page_size": size, "queried_at": datetime.now(UTC).isoformat()}


_CONFIG = (
    ("list", "查询维修单", ("mes:dv:repair:list",)),
    ("detail", "查询维修单详情", ("mes:dv:repair:list", "mes:dv:repair:query")),
    ("lines", "查询维修内容", ("mes:dv:repair:query", "mes:dv:repairline:list")),
    ("options", "查询维修选项", ("mes:dv:repair:list",)),
    ("machinery_candidates", "查询维修设备候选", ("mes:dv:machinery:list",)),
    ("subject_candidates", "查询维修项目候选", ("mes:dv:dvsubject:list",)),
)


def build_dv_repair_tools() -> tuple[ToolDefinition, ...]:
    result = []
    for kind, label, perms in _CONFIG:
        if kind == "list":
            props: dict[str, JsonValue] = {
                key: {"type": "string", "minLength": 1, "maxLength": limit,
                      "description": _FILTER_DESCRIPTIONS[key]}
                for key, (_wire, limit) in FILTERS.items()
            }
        elif kind == "detail":
            props = {
                "repair_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807,
                              "description": "维修单系统 ID，取自维修单查询结果中的 repairId。"},
                "repair_code": {"type": "string", "minLength": 1, "maxLength": 64,
                                "description": "完整且唯一的维修单业务编号，按精确编号定位。"},
            }
        elif kind == "lines":
            props = {"repair_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807,
                                   "description": "内容行所属维修单的系统 ID，取自维修单查询结果中的 repairId。"}}
        elif kind.endswith("candidates"):
            key = "machinery" if kind.startswith("machinery") else "subject"
            entity = "设备" if key == "machinery" else "项目"
            props = {f"{key}_code": {"type": "string", "minLength": 1, "maxLength": 64,
                                    "description": f"{entity}业务编码，精确筛选。"},
                     f"{key}_name": {"type": "string", "minLength": 1, "maxLength": 255,
                                    "description": f"{entity}名称，模糊筛选。"}}
        else: props = {}
        if kind not in {"detail", "options"}:
            props.update(
                page={"type": "integer", "minimum": 1, "maximum": 1_000_000, "default": 1,
                      "description": "页码，从第 1 页开始。"},
                page_size={"type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                           "description": "每页记录数。"},
            )
        schema = _schema(props, ("repair_id",) if kind == "lines" else ())
        if kind == "detail":
            schema = {"type": "object", "properties": props, "oneOf": [
                _schema({"repair_id": props["repair_id"]}, ("repair_id",)),
                _schema({"repair_code": props["repair_code"]}, ("repair_code",))], "additionalProperties": False}

        async def execute(context: ToolContext, arguments: Mapping[str, object], *, kind: str = kind) -> ToolResult:
            client = _client(context)
            if client is None: return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
            try:
                if kind == "list": data = await read_repair_list(client, arguments)
                elif kind == "detail": data = await read_repair_detail(client, arguments)
                elif kind == "lines": data = await read_repair_lines(client, arguments)
                elif kind == "options":
                    if arguments: raise KTGError("VALIDATION_ERROR", "选项查询不接受参数。")
                    data = await read_repair_options(client)
                else: data = await read_candidates(client, arguments, kind.split("_")[0])
                rows = cast(list[JsonObject], data.get("rows", []))
                if kind == "detail":
                    row = cast(JsonObject, data["repair"])
                    content = (f"维修单[{row['repairId']}] 编号{row['repairCode']} 名称{row['repairName']}；设备"
                        f"[{row['machineryId']}] {row['machineryCode']} {row['machineryName']}；报修日期{row['requireDate']}；"
                        f"状态{row['status']}；维修结果{row['repairResult'] or '未设'}；完工日期{row['finishDate'] or '未设'}；"
                        f"验收日期{row['confirmDate'] or '未设'}；维修人员{row['acceptedBy'] or '未设'}；"
                        f"验收人员{row['confirmBy'] or '未设'}；备注{row['remark'] or '无'}。")
                elif kind == "options":
                    content = ("维修结果：" + "、".join(f"{r['value']}（{r['label']}）" for r in cast(list[JsonObject], data["repair_results"]))
                        + "；单据状态：" + "、".join(f"{r['value']}（{r['label']}）" for r in cast(list[JsonObject], data["statuses"]))
                        + "。本批写入仅支持草稿维修单及其维修内容增改删。")
                else:
                    header = f"共{data['total']}条；第{data['page']}页返回{len(rows)}条。"
                    if len(rows) > 20: header += "摘要仅列前20条，请继续分页查看。"
                    content = header + "\n" + "\n".join(str(row) for row in rows[:20])
                if len(content) > 4000:
                    content = content[:3950] + "\n摘要已截断，请继续分页或按ID查询。"
                return ToolResult(content=content[:4000], details=data)
            except KTGError as exc: return _failure(str(exc), exc.code)

        fields = ("kind", "repair", "queried_at") if kind == "detail" else (
            ("kind", "repair_results", "statuses", "supported_writes", "queried_at") if kind == "options" else
            ("kind", "rows", "total", "page", "page_size", "filters", "queried_at") if kind == "list" else
            ("kind", "rows", "total", "page", "page_size", "repair", "queried_at") if kind == "lines" else
            ("kind", "rows", "total", "page", "page_size", "queried_at"))
        result.append(ToolDefinition(name=f"ktg_dv_repair_{kind}", version="1.0", label=label,
            description=_READ_DESCRIPTIONS[kind], input_schema=schema,
            output_schema=_output_schema(fields), required_permissions=perms,
            effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
            retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=execute))
    return tuple(result)


def dv_repair_factories() -> dict[str, ToolFactory]:
    return {f"dv:{tool.name}": _factory(tool) for tool in build_dv_repair_tools()}


def dv_repair_manifests() -> tuple[ToolManifest, ...]:
    return tuple(ToolManifest(name=tool.name, version=tool.version, module="dv", domain="repair",
        label=tool.label, description=tool.description,
        permission_policy=PermissionAll(tuple(PermissionLeaf(p) for p in tool.required_permissions)),
        effect=tool.effect, execution_mode=tool.execution_mode, retry_policy=tool.retry_policy,
        timeout_seconds=tool.timeout_seconds, expose_to_model=True, confirmation_policy="none",
        proposal_store_effect=False, handler_ref=f"dv:{tool.name}") for tool in build_dv_repair_tools())
