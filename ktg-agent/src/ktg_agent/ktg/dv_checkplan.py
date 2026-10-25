"""Read-only tools for point-inspection and maintenance plans."""
from __future__ import annotations

from collections.abc import Mapping
import asyncio
from datetime import UTC, datetime, date
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_machinery import (_bounded_integer, _schema, _output_schema, _factory,
    _client, _failure, _page_payload, _project_machinery)
from ktg_agent.ktg.dv_subject import project_subject, subject_page
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult

PLAN_PATH = "/mes/dv/checkplan"
PLAN_LIST = f"{PLAN_PATH}/list"
MACHINERY_LINK_PATH = "/mes/dv/checkmachinery"
SUBJECT_LINK_PATH = "/mes/dv/checksubject"
MACHINERY_PATH = "/mes/dv/machinery"
SUBJECT_PATH = "/mes/dv/dvsubject"
DICT_PATH = "/system/dict/data/type"
PLAN_TYPES = ("CHECK", "MAINTEN")
PLAN_STATUSES = ("PREPARE", "FINISHED")
BASELINE_CYCLES = ("HOUR", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR")
PLAN_FIELDS = ("planId", "planCode", "planName", "planType", "startDate", "endDate",
               "cycleType", "cycleCount", "status", "remark")
PLAN_FILTERS = {"plan_code": ("planCode", 64), "plan_name": ("planName", 100),
                "plan_type": ("planType", None), "status": ("status", None),
                "cycle_type": ("cycleType", 64), "start_date": ("startDate", 10),
                "end_date": ("endDate", 10)}


def _text(value: object, key: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise KTGError("VALIDATION_ERROR", f"{key} 必须是文本。")
    clean = value.strip()
    if (required and not clean) or len(clean) > maximum:
        raise KTGError("VALIDATION_ERROR", f"{key} 长度无效。")
    return clean


def _date(value: object, key: str) -> str:
    clean = _text(value, key, 10)
    try:
        parsed = date.fromisoformat(clean)
    except ValueError:
        raise KTGError("VALIDATION_ERROR", f"{key} 必须是真实日期 YYYY-MM-DD。") from None
    if parsed.isoformat() != clean:
        raise KTGError("VALIDATION_ERROR", f"{key} 必须使用 YYYY-MM-DD 格式。")
    return clean


def _positive_id(value: object, key: str) -> int:
    return _bounded_integer(value, key, 1, 9223372036854775807)


def project_plan(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "计划数据格式无效。")
    plan_id = value.get("planId")
    if not isinstance(plan_id, int) or isinstance(plan_id, bool) or plan_id <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "计划 ID 无效。")
    result: JsonObject = {"planId": plan_id}
    string_fields = {"planCode", "planName", "planType", "startDate", "endDate", "cycleType", "status", "remark"}
    for field in string_fields:
        item = value.get(field)
        if item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"计划字段 {field} 格式无效。")
        result[field] = item
    count = value.get("cycleCount")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
        raise KTGError("KTG_INVALID_RESPONSE", "计划周期次数格式无效。")
    result["cycleCount"] = count
    for field in ("planCode", "planName", "planType", "cycleType", "status"):
        if not isinstance(result[field], str):
            raise KTGError("KTG_INVALID_RESPONSE", f"计划字段 {field} 缺失。")
    return {key: result[key] for key in PLAN_FIELDS}


def plan_page(payload: Mapping[str, object]) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(total, int)
            or isinstance(total, bool) or total < len(rows)):
        raise KTGError("KTG_INVALID_RESPONSE", "计划分页结果格式无效。")
    return [project_plan(row) for row in rows], total


async def fetch_plan(client: KTGClient, value: object) -> JsonObject:
    plan_id = _positive_id(value, "plan_id")
    payload = await client.get(f"{PLAN_PATH}/{plan_id}")
    if payload.get("data") is None:
        raise KTGError("PLAN_NOT_FOUND", f"未找到计划 ID {plan_id}。", status=404)
    plan = project_plan(payload["data"])
    if plan["planId"] != plan_id:
        raise KTGError("KTG_INVALID_RESPONSE", "计划详情 ID 与请求不一致。")
    return plan


async def read_plan_list(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    allowed = set(PLAN_FILTERS) | {"page", "page_size"}
    if set(arguments) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的计划筛选字段。")
    page = _bounded_integer(arguments.get("page", 1), "page", 1, 1000000)
    size = _bounded_integer(arguments.get("page_size", 20), "page_size", 1, 100)
    params: dict[str, object] = {"pageNum": page, "pageSize": size}
    filters: JsonObject = {}
    for key, (wire, limit) in PLAN_FILTERS.items():
        if key not in arguments:
            continue
        value = arguments[key]
        if key == "plan_type" and value not in PLAN_TYPES:
            raise KTGError("VALIDATION_ERROR", "plan_type 只允许 CHECK 或 MAINTEN。")
        if key == "status" and value not in PLAN_STATUSES:
            raise KTGError("VALIDATION_ERROR", "status 只允许 PREPARE 或 FINISHED。")
        clean = _date(value, key) if key.endswith("_date") else _text(value, key, limit or 64)
        if key == "cycle_type":
            options = await read_plan_options(client)
            allowed_cycles = {row["value"] for row in cast(list[JsonObject], options["cycle_types"])}
            if clean not in allowed_cycles:
                raise KTGError("VALIDATION_ERROR", "cycle_type必须来自当前MES周期字典。")
        params[wire] = clean
        filters[key] = clean
    rows, total = plan_page(await client.get(PLAN_LIST, params))
    return {"kind": "dv_checkplan_list", "rows": cast(list[JsonValue], rows), "total": total,
            "page": page, "page_size": size, "filters": filters,
            "queried_at": datetime.now(UTC).isoformat()}


async def read_plan_detail(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    if set(arguments) not in ({"plan_id"}, {"plan_code"}):
        raise KTGError("VALIDATION_ERROR", "必须且只能提供 plan_id 或 plan_code。")
    if "plan_id" in arguments:
        plan = await fetch_plan(client, arguments["plan_id"])
    else:
        code = _text(arguments["plan_code"], "plan_code", 64)
        rows, total = plan_page(await client.get(PLAN_LIST, {"planCode": code, "pageNum": 1, "pageSize": 100}))
        if total == 0:
            raise KTGError("PLAN_NOT_FOUND", "未找到该编号的计划。", status=404)
        if total != 1 or len(rows) != 1:
            ids = "；".join(f"[{row['planId']}] {row['planName']}" for row in rows[:10])
            raise KTGError("AMBIGUOUS_MATCH", f"该计划编号可能重复，请按ID选择：{ids}")
        plan = await fetch_plan(client, rows[0]["planId"])
        if plan["planCode"] != code:
            raise KTGError("KTG_INVALID_RESPONSE", "编号定位后计划已变化，请按ID重新查询。")
    return {"kind": "dv_checkplan_detail", "plan": plan, "queried_at": datetime.now(UTC).isoformat()}


def _dict_rows(payload: Mapping[str, object], name: str) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise KTGError("KTG_INVALID_RESPONSE", f"{name}字典格式无效。")
    result: list[JsonObject] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise KTGError("KTG_INVALID_RESPONSE", f"{name}字典项格式无效。")
        label, value = item.get("dictLabel"), item.get("dictValue")
        if not isinstance(label, str) or not isinstance(value, str) or not value:
            raise KTGError("KTG_INVALID_RESPONSE", f"{name}字典项缺少标签或值。")
        if value not in seen:
            result.append({"label": label, "value": value})
            seen.add(value)
    return result


async def read_plan_options(client: KTGClient) -> JsonObject:
    types, cycles, statuses = await asyncio.gather(
        client.get(f"{DICT_PATH}/dv_plan_type"), client.get(f"{DICT_PATH}/mes_cycle_type"),
        client.get(f"{DICT_PATH}/mes_order_status"))
    return {"kind": "dv_checkplan_options", "plan_types": cast(list[JsonValue], _dict_rows(types, "计划类型")),
            "cycle_types": cast(list[JsonValue], _dict_rows(cycles, "周期类型")), "statuses": cast(list[JsonValue], _dict_rows(statuses, "状态")),
            "source_baseline": {"plan_types": cast(JsonValue, list(PLAN_TYPES)),
                "cycle_types": cast(JsonValue, list(BASELINE_CYCLES)),
                "supported_plan_statuses": cast(JsonValue, list(PLAN_STATUSES))},
            "queried_at": datetime.now(UTC).isoformat()}


async def read_plan_links(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    if set(arguments) - {"plan_id", "kind", "page", "page_size"} or "plan_id" not in arguments or "kind" not in arguments:
        raise KTGError("VALIDATION_ERROR", "必须提供 plan_id 和 kind，且不能提供其他字段。")
    plan_id = _positive_id(arguments["plan_id"], "plan_id")
    plan = await fetch_plan(client, plan_id)
    raw_kind = arguments["kind"]
    kind = cast(str, raw_kind)
    if kind not in {"machinery", "subject"}:
        raise KTGError("VALIDATION_ERROR", "kind 只允许 machinery 或 subject。")
    page = _bounded_integer(arguments.get("page", 1), "page", 1, 1000000)
    size = _bounded_integer(arguments.get("page_size", 20), "page_size", 1, 100)
    path = MACHINERY_LINK_PATH if kind == "machinery" else SUBJECT_LINK_PATH
    raw = await client.get(f"{path}/list", {"planId": plan_id, "pageNum": page, "pageSize": size})
    rows, total = _link_page(raw, kind)
    if any(row["planId"] != plan_id for row in rows):
        raise KTGError("KTG_INVALID_RESPONSE", "MES返回了其他计划的关联记录，已拒绝展示。")
    return {"kind": "dv_checkplan_links", "plan_id": plan_id, "plan": plan, "link_kind": kind,
            "rows": cast(list[JsonValue], rows), "total": total, "page": page,
            "page_size": size, "queried_at": datetime.now(UTC).isoformat()}


def _link_page(payload: Mapping[str, object], kind: str) -> tuple[list[JsonObject], int]:
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or len(rows) > 100 or not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "计划关联分页结果格式无效。")
    fields = (("recordId", "planId", "machineryId", "machineryCode", "machineryName", "machineryBrand", "machinerySpec")
              if kind == "machinery" else
              ("recordId", "planId", "subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent", "subjectStandard"))
    result = []
    for source in rows:
        if not isinstance(source, Mapping):
            raise KTGError("KTG_INVALID_RESPONSE", "计划关联数据不是对象。")
        row: JsonObject = {}
        for key in fields:
            value = source.get(key)
            if key in {"recordId", "planId", "machineryId", "subjectId"}:
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise KTGError("KTG_INVALID_RESPONSE", f"关联字段 {key} 无效。")
            elif value is not None and not isinstance(value, str):
                raise KTGError("KTG_INVALID_RESPONSE", f"关联字段 {key} 格式无效。")
            row[key] = value
        result.append(row)
    return result, total


async def read_candidates(client: KTGClient, arguments: Mapping[str, object], kind: str) -> JsonObject:
    allowed = {"plan_id", "page", "page_size", "machinery_code", "machinery_name"} if kind == "machinery" else {
        "plan_id", "page", "page_size", "subject_code", "subject_name"}
    if set(arguments) - allowed or "plan_id" not in arguments:
        raise KTGError("VALIDATION_ERROR", "候选查询必须提供 plan_id 且仅接受白名单筛选字段。")
    plan = await fetch_plan(client, arguments["plan_id"])
    plan_id = cast(int, plan["planId"])
    page = _bounded_integer(arguments.get("page", 1), "page", 1, 1000000)
    size = _bounded_integer(arguments.get("page_size", 20), "page_size", 1, 100)
    if kind == "machinery":
        params: dict[str, object] = {"pageNum": page, "pageSize": size}
        for key, wire, maxlen in (("machinery_code", "machineryCode", 64), ("machinery_name", "machineryName", 255)):
            if key in arguments:
                params[wire] = _text(arguments[key], key, maxlen)
        payload = await client.get(f"{MACHINERY_PATH}/list", params)
        rows, total = _page_payload(payload)
        rows = [{key: row.get(key) for key in ("machineryId", "machineryCode", "machineryName", "status", "machineryBrand", "machinerySpec")}
                for row in rows]
        note = "设备状态仅供参考；关联预览会检查所有同类型计划的冲突。"
        label = "dv_checkplan_machinery_candidates"
    else:
        params = {"pageNum": page, "pageSize": size, "subjectType": cast(str, plan["planType"]), "enableFlag": "Y"}
        for key, wire, maxlen in (("subject_code", "subjectCode", 64), ("subject_name", "subjectName", 100)):
            if key in arguments:
                params[wire] = _text(arguments[key], key, maxlen)
        payload = await client.get(f"{SUBJECT_PATH}/list", params)
        rows, total = subject_page(payload)
        if any(row["subjectType"] != plan["planType"] or row["enableFlag"] != "Y" for row in rows):
            raise KTGError("KTG_INVALID_RESPONSE", "MES返回了未启用或类型不匹配的项目候选。")
        note = "候选项目仅含启用且类型匹配的项目；请按明确ID选择。"
        label = "dv_checkplan_subject_candidates"
    return {"kind": label, "plan": plan, "rows": cast(list[JsonValue], rows), "total": total,
            "page": page, "page_size": size, "note": note, "queried_at": datetime.now(UTC).isoformat()}


_READ_CONFIG = (
    ("ktg_dv_checkplan_list", "查询点检保养计划", "分页查找计划；编号和枚举精确，名称按MES规则模糊匹配。", "list", ("mes:dv:checkplan:list",)),
    ("ktg_dv_checkplan_detail", "查询点检保养计划详情", "只能提供plan_id或plan_code；重码必须按ID选择。", "detail", ("mes:dv:checkplan:query", "mes:dv:checkplan:list")),
    ("ktg_dv_checkplan_options", "查询计划选项", "读取当前计划类型、周期类型和状态字典及源码基线。", "options", ("mes:dv:checkplan:list",)),
    ("ktg_dv_checkplan_links", "查询计划关联明细", "按计划和kind分页读取已保存的设备或项目快照。", "links", ("mes:dv:checkplan:query", "mes:dv:checkmachinery:list", "mes:dv:checksubject:list")),
    ("ktg_dv_checkplan_machinery_candidates", "查询计划设备候选", "列出设备候选，不写入；关联需预览确认。", "machinery", ("mes:dv:checkplan:query", "mes:dv:machinery:list")),
    ("ktg_dv_checkplan_subject_candidates", "查询计划项目候选", "只返回启用且与计划类型匹配的项目；按ID选择。", "subject", ("mes:dv:checkplan:query", "mes:dv:dvsubject:list")),
)


def build_dv_checkplan_tools() -> tuple[ToolDefinition, ...]:
    definitions = []
    for name, label, description, kind, perms in _READ_CONFIG:
        props: dict[str, JsonValue]
        if kind == "list":
            props = {}
            for key, (_wire, maximum) in PLAN_FILTERS.items():
                props[key] = (cast(JsonValue, {"type": "string", "format": "date"}) if key.endswith("_date") else
                              cast(JsonValue, {"type": "string", "minLength": 1, "maxLength": maximum or 64}))
            props["plan_type"] = {"type": "string", "enum": cast(JsonValue, list(PLAN_TYPES))}
            props["status"] = {"type": "string", "enum": cast(JsonValue, list(PLAN_STATUSES))}
            props.update({"page": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}})
        elif kind == "detail":
            id_schema: JsonValue = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
            code_schema: JsonValue = {"type": "string", "minLength": 1, "maxLength": 64}
            props = {"plan_id": id_schema, "plan_code": code_schema}
        elif kind == "options":
            props = {}
        elif kind == "links":
            props = {"plan_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}, "kind": {"type": "string", "enum": cast(JsonValue, ["machinery", "subject"])},
                     "page": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}
        else:
            props = {"plan_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
                     ("machinery_code" if kind == "machinery" else "subject_code"): {"type": "string", "minLength": 1, "maxLength": 64},
                     ("machinery_name" if kind == "machinery" else "subject_name"): {"type": "string", "minLength": 1, "maxLength": 255 if kind == "machinery" else 100},
                     "page": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}

        async def execute(context: ToolContext, arguments: Mapping[str, object], *, kind: str = kind) -> ToolResult:
            client = _client(context)
            if client is None:
                return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
            try:
                if kind == "list": data = await read_plan_list(client, arguments)
                elif kind == "detail": data = await read_plan_detail(client, arguments)
                elif kind == "options":
                    if arguments: raise KTGError("VALIDATION_ERROR", "选项查询不接受参数。")
                    data = await read_plan_options(client)
                elif kind == "links": data = await read_plan_links(client, arguments)
                else: data = await read_candidates(client, arguments, kind)
                return ToolResult(content=_read_content(data), details=data)
            except KTGError as exc:
                return _failure(str(exc), exc.code)

        fields = {"list": ("kind", "rows", "total", "page", "page_size", "filters", "queried_at"),
                  "detail": ("kind", "plan", "queried_at"), "options": ("kind", "plan_types", "cycle_types", "statuses", "source_baseline", "queried_at"),
                  "links": ("kind", "plan_id", "plan", "link_kind", "rows", "total", "page", "page_size", "queried_at")}.get(kind,
                  ("kind", "plan", "rows", "total", "page", "page_size", "note", "queried_at"))
        required: tuple[str, ...] = ()
        input_schema = _schema(props)
        if kind == "detail":
            input_schema = {"type": "object", "properties": props, "oneOf": [
                {"type": "object", "properties": {"plan_id": props["plan_id"]}, "required": ["plan_id"], "additionalProperties": False},
                {"type": "object", "properties": {"plan_code": props["plan_code"]}, "required": ["plan_code"], "additionalProperties": False}],
                "additionalProperties": False}
        elif kind == "links":
            required = ("plan_id", "kind")
        elif kind in {"machinery", "subject"}:
            required = ("plan_id",)
        if kind != "detail":
            input_schema = _schema(props, required)
        definitions.append(ToolDefinition(name=name, version="1.0", label=label, description=description,
            input_schema=input_schema, output_schema=_output_schema(fields), required_permissions=perms,
            effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
            retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=execute))
    return tuple(definitions)


def _read_content(data: JsonObject) -> str:
    kind = data["kind"]
    if kind == "dv_checkplan_detail":
        p = cast(JsonObject, data["plan"])
        return _limit_content(f"计划 [{p['planId']}] 编号 {p['planCode']}；名称 {p['planName']}；类型 {p['planType']}；"
            f"状态 {p['status']}；周期 {p['cycleType']}×{p['cycleCount']}；日期 {p.get('startDate') or '未设'} 至 {p.get('endDate') or '未设'}；备注 {p.get('remark') or '无'}。")
    if kind == "dv_checkplan_options":
        sections = []
        for key, label in (("plan_types", "计划类型"), ("cycle_types", "周期类型"), ("statuses", "状态")):
            rows = cast(list[JsonObject], data[key])
            sections.append(label + "：" + ("；".join(f"{r['value']}（{r['label']}）" for r in rows) or "无"))
        return _limit_content("当前MES字典选项：" + "\n".join(sections) + "\n源码基线仅供参考，不代表当前运行字典。")
    rows = cast(list[JsonObject], data.get("rows", []))
    total = cast(int, data.get("total", len(rows)))
    page, page_size = data.get("page", 1), data.get("page_size", 20)
    header = f"总计{total}条；当前第{page}页，每页上限{page_size}，本页返回{len(rows)}条。"
    lines: list[str] = []
    if kind == "dv_checkplan_list":
        lines = [f"计划ID[{r['planId']}] 编号{r['planCode']} 名称{r['planName']} 类型{r['planType']} 状态{r['status']} 周期{r['cycleType']}×{r['cycleCount']}"
                 for r in rows[:20]]
    elif kind == "dv_checkplan_links":
        source_id = "machineryId" if data["link_kind"] == "machinery" else "subjectId"
        if data["link_kind"] == "machinery":
            lines = [f"recordId[{r['recordId']}] planId[{r['planId']}] {source_id}[{r[source_id]}] 编号{r.get('machineryCode')} 名称{r.get('machineryName')} 品牌{r.get('machineryBrand') or '—'} 规格{r.get('machinerySpec') or '—'}"
                     for r in rows[:20]]
        else:
            lines = [f"recordId[{r['recordId']}] planId[{r['planId']}] {source_id}[{r[source_id]}] 编号{r.get('subjectCode')} 名称{r.get('subjectName')} 类型{r.get('subjectType')} 内容{r.get('subjectContent') or '—'} 标准{r.get('subjectStandard') or '—'}"
                     for r in rows[:20]]
        header = f"计划[{data['plan_id']}] 已关联{data['link_kind']}；总计{total}条；第{page}页返回{len(rows)}条。"
    elif kind == "dv_checkplan_machinery_candidates":
        lines = [f"machineryId[{r['machineryId']}] 编号{r['machineryCode']} 名称{r['machineryName']} 状态{r['status']} 规格{r.get('machinerySpec') or '—'}"
                 for r in rows[:20]]
    elif kind == "dv_checkplan_subject_candidates":
        lines = [f"subjectId[{r['subjectId']}] 编号{r['subjectCode']} 名称{r['subjectName']} 类型{r['subjectType']} 内容{r.get('subjectContent') or '—'} 标准{r.get('subjectStandard') or '—'} 启用{r['enableFlag']}"
                 for r in rows[:20]]
    if len(rows) > 20:
        header += "本页摘要只列前20条；其余行未列出，请继续分页查询。"
    note = data.get("note")
    if isinstance(note, str): header += " " + note
    return _limit_content(header + ("\n" + "\n".join(lines) if lines else ""))


def _limit_content(value: str) -> str:
    if len(value) <= 4000:
        return value
    marker = "\n……摘要已截断；请使用分页工具继续读取。"
    return value[:4000 - len(marker)] + marker


def dv_checkplan_factories() -> dict[str, ToolFactory]:
    return {f"dv:{tool.name}": _factory(tool) for tool in build_dv_checkplan_tools()}


def dv_checkplan_manifests() -> tuple[ToolManifest, ...]:
    return tuple(ToolManifest(name=tool.name, version=tool.version, module="dv", domain="checkplan",
        label=tool.label, description=tool.description,
        permission_policy=PermissionAll(tuple(PermissionLeaf(p) for p in tool.required_permissions)),
        effect=tool.effect, execution_mode=tool.execution_mode, retry_policy=tool.retry_policy,
        timeout_seconds=tool.timeout_seconds, expose_to_model=True, confirmation_policy="none",
        proposal_store_effect=False, handler_ref=f"dv:{tool.name}") for tool in build_dv_checkplan_tools())
