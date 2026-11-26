"""Bounded read of workorders and authoritative single-unit execution summaries."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import (
    ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus,
)


NAME = "ktg_query_workorder_overview"
PERMISSIONS = ("mes:pro:workorder:list", "mes:pro:execution:query")
_STATUSES = ("PREPARE", "CONFIRMED", "APPROVING", "APPROVED", "FINISHED")
_MAX_SCAN_PAGES = 10
_SCAN_PAGE_SIZE = 20
_MAX_TASKS = 8
_MAX_ID = 2**63 - 1
_PROGRESS_COUNTS = (
    "planned", "allocated", "assembled", "waitingAssembly", "waitingInspection",
    "pendingDisposition", "reworkInProgress", "waitingReinspection", "qualified",
    "packable", "packed", "scrapped",
)
_LIST_PATH = "/mes/pro/workorder/list"
_EXECUTION_PATH = "/mes/pro/execution/workorders/"


def _text_schema(max_length: int, description: str) -> JsonObject:
    return {"type": "string", "minLength": 1, "maxLength": max_length,
            "pattern": r"\S", "description": description}


_DATE_SCHEMA: JsonObject = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_INPUT_SCHEMA: JsonObject = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "workorder_name": _text_schema(255, "工单名称片段按字面匹配，% 和 _ 不作为通配符。"),
        "workorder_code": _text_schema(64, "准确工单编码；与 workorder_codes 二选一。"),
        "workorder_codes": {
            "type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
            "items": _text_schema(64, "完整工单编码。"), "description": "比较多个明确工单。"
        },
        "product_code": _text_schema(64, "准确产品编码。"),
        "statuses": {"type": "array", "minItems": 1, "maxItems": len(_STATUSES), "uniqueItems": True,
                     "items": {"enum": list(_STATUSES)},
                     "description": "按一个或多个 MES 工单状态筛选。PREPARE草稿，CONFIRMED已下达，"
                                    "APPROVING审批中，APPROVED已审批，FINISHED已完成。"},
        "unfinished_only": {"type": "boolean", "default": False,
                            "description": "只查未完结工单（排除 FINISHED），不可与 statuses 同时启用。"},
        "request_date": {**_DATE_SCHEMA, "description": "需求日期精确匹配，不是创建/实际生产日期。"},
        "request_date_from": {**_DATE_SCHEMA, "description": "需求日期范围起点（含）。"},
        "request_date_to": {**_DATE_SCHEMA, "description": "需求日期范围终点（含）。"},
        "page": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        "include_progress": {"type": "boolean", "default": False,
                             "description": "查询工序任务、任务顺序或实际执行状态时必须为true；同时返回各工单的任务和SN全单执行摘要，不返回SN明细。"},
    },
    "allOf": [
        {"not": {"required": ["workorder_code", "workorder_codes"]}},
        {"not": {"required": ["statuses", "unfinished_only"],
                 "properties": {"unfinished_only": {"const": True}}}},
        {"not": {"required": ["request_date", "request_date_from"]}},
        {"not": {"required": ["request_date", "request_date_to"]}},
    ],
}
_INPUT_VALIDATOR = Draft202012Validator(_INPUT_SCHEMA, format_checker=FormatChecker())


def _strict_schema(properties: JsonObject, required: tuple[str, ...]) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


_TASK_SCHEMA = _strict_schema(
    {"taskId": {"type": "string"}, "stepKind": {"type": "string"},
     "taskKind": {"type": "string"}, "status": {"type": "string"},
     "processName": {"type": "string"}, "taskCode": {"type": "string"},
     "normalStepNo": {"type": "integer", "minimum": 1}},
    ("taskId", "stepKind", "taskKind", "status"),
)
_PROGRESS_SCHEMA = _strict_schema({
    **{key: {"type": "integer", "minimum": 0} for key in _PROGRESS_COUNTS},
    "completionAllowed": {"type": "boolean"},
    "completionReasons": {"type": "array", "items": {"type": "string"}},
    "tasks": {"type": "array", "items": _TASK_SCHEMA, "maxItems": _MAX_TASKS},
    "tasks_truncated": {"type": "boolean"},
}, (*_PROGRESS_COUNTS, "completionAllowed", "completionReasons"))
_ROW_SCHEMA = _strict_schema({
    "workorderId": {"type": "string"}, "workorderCode": {"type": "string"},
    "workorderName": {"type": "string"}, "productCode": {"type": "string"},
    "productName": {"type": "string"}, "quantity": {"type": "string"},
    "unitOfMeasure": {"type": "string"}, "status": {"enum": list(_STATUSES)},
    "requestDate": _DATE_SCHEMA,
    "progress_state": {"enum": ["not_requested", "draft", "available", "unavailable"]},
    "progress": {"anyOf": [_PROGRESS_SCHEMA, {"type": "null"}]},
    "progress_reason": {"type": "string"},
}, ("workorderId", "workorderCode", "workorderName", "productCode", "productName", "quantity",
    "unitOfMeasure", "status", "requestDate", "progress_state", "progress"))
_OUTPUT_SCHEMA = _strict_schema({
    "kind": {"const": "workorder_overview"}, "filters": {"type": "object"},
    "rows": {"type": "array", "items": _ROW_SCHEMA, "maxItems": 20},
    "total": {"type": ["integer", "null"], "minimum": 0},
    "page": {"type": "integer", "minimum": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 20},
    "coverage": {"enum": ["complete", "partial"]}, "queried_at": {"type": "string"},
    "warnings": {"type": "array", "items": {"type": "string"}},
}, ("kind", "filters", "rows", "total", "page", "page_size", "coverage", "queried_at"))


def build_workorder_overview_tool() -> ToolDefinition:
    return ToolDefinition(
        name=NAME, version="1.0", label="查询工单和单件生产进度",
        description=(
            "分页查询、比较生产工单。可用工单名称、准确单号（支持多个）、产品编码、"
            "状态和需求日期来筛选；查在制或未完结工单可用 unfinished_only。"
            "查询哪些工单卡住、已装配、待检、待处置、返工、合格、已包装、报废及完结条件时，"
            "启用 include_progress，读取 MES 的 SN 全单统计而非旧报工总量。"
            "查询工单有哪些工序任务、工序顺序及实际执行状态也使用本工具，include_progress=true，读取progress.tasks；无需另找工序明细工具。"
            "草稿无执行进度；没有权限或查询失败不推断为零或未开始。已完结不属于阻塞。"
            "多状态或日期范围有界扫描，coverage=partial 不提供完整总数。"
            "需求日期不是开工日期；不返回单个 SN 明细，也不执行或修改工单。"
        ),
        input_schema=_INPUT_SCHEMA, output_schema=_OUTPUT_SCHEMA,
        required_permissions=PERMISSIONS, effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=45, execute=_execute,
    )


def _invalid(message: str) -> KTGError:
    return KTGError("KTG_INVALID_RESPONSE", message)


def _normalize(arguments: Mapping[str, object]) -> tuple[JsonObject, int, int]:
    if not _INPUT_VALIDATOR.is_valid(dict(arguments)):
        raise KTGError("VALIDATION_ERROR", "工单查询参数格式、范围或互斥条件无效。", status=400)
    page, page_size = arguments.get("page", 1), arguments.get("page_size", 10)
    # JSON Schema treats 1.0 as an integer value, but Java's page parameters
    # require integer text. Do not forward fractional JSON number representations.
    if type(page) is not int or type(page_size) is not int:
        raise KTGError("VALIDATION_ERROR", "页码与每页数量必须是整数。", status=400)
    filters: JsonObject = {}
    for key, value in arguments.items():
        if key in {"page", "page_size"}:
            continue
        if key == "workorder_codes":
            codes = [str(code).strip() for code in cast(list[str], value)]
            if len({code.casefold() for code in codes}) != len(codes):
                raise KTGError("VALIDATION_ERROR", "工单编码不可重复。", status=400)
            filters[key] = cast(list[JsonValue], codes)
        elif isinstance(value, str):
            filters[key] = value.strip()
        else:
            filters[key] = cast(JsonValue, value)
    start, end = filters.get("request_date_from"), filters.get("request_date_to")
    if isinstance(start, str) and isinstance(end, str) and start > end:
        raise KTGError("VALIDATION_ERROR", "需求日期起点不能晚于终点。", status=400)
    filters.setdefault("include_progress", False)
    return filters, page, page_size


def _id(value: object) -> str:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
    else:
        raise _invalid("MES 工单或任务 ID 无效。")
    if not 1 <= number <= _MAX_ID:
        raise _invalid("MES 工单或任务 ID 超出合法范围。")
    return str(number)


def _text(value: object, field: str, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid(f"MES 工单字段 {field} 缺失或无效。")
    return value


def _quantity(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float | str) or len(str(value)) > 64:
        raise _invalid("MES 工单计划量缺失或无效。")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise _invalid("MES 工单计划量缺失或无效。") from exc
    if not number.is_finite() or number < 0 or not -16 <= number.adjusted() <= 17:
        raise _invalid("MES 工单计划量超出有效范围。")
    return format(number, "f")


@dataclass(slots=True)
class _Workorder:
    row: JsonObject
    version: int | None


def _project(value: object) -> _Workorder:
    if not isinstance(value, dict):
        raise _invalid("MES 工单列表包含无效记录。")
    row: JsonObject = {"workorderId": _id(value.get("workorderId"))}
    for key in ("workorderName", "productName", "unitOfMeasure"):
        row[key] = _text(value.get(key), key)
    for key in ("workorderCode", "productCode"):
        row[key] = _text(value.get(key), key, 64)
    status = value.get("status")
    if status not in _STATUSES:
        raise _invalid("MES 工单状态缺失或不在已核对的业务字典中。")
    row["status"] = cast(str, status)
    raw_date = _text(value.get("requestDate"), "requestDate", 10)
    try:
        if date.fromisoformat(raw_date).isoformat() != raw_date:
            raise ValueError
    except ValueError as exc:
        raise _invalid("MES 工单需求日期格式无效。") from exc
    row["requestDate"] = raw_date
    row["quantity"] = _quantity(value.get("quantity"))
    row["progress_state"] = "not_requested"
    row["progress"] = None
    version = value.get("version")
    if version is not None and (type(version) is not int or version < 0):
        raise _invalid("MES 工单版本格式无效。")
    return _Workorder(row, version)


def _native_filters(filters: JsonObject) -> dict[str, object]:
    params: dict[str, object] = {target: filters[source] for source, target in (
        ("workorder_name", "workorderName"), ("workorder_code", "workorderCode"),
        ("product_code", "productCode"), ("request_date", "requestDate"),
    ) if source in filters}
    statuses = filters.get("statuses")
    if isinstance(statuses, list) and len(statuses) == 1:
        params["status"] = statuses[0]
    name = filters.get("workorder_name")
    if isinstance(name, str) and _has_like_metacharacters(name):
        # Java has no explicit LIKE escape contract. Use an ordinary fragment to
        # narrow candidates, then match the complete literal below; never forward
        # user-controlled LIKE wildcards or escape characters.
        parts = [part.strip() for part in re.split(r"[%_\\]", name)]
        plain = max(parts, key=len, default="")
        if plain:
            params["workorderName"] = plain
        else:
            params.pop("workorderName", None)
    return params


async def _read_page(client: KTGClient, native: Mapping[str, object], page: int,
                     page_size: int) -> tuple[list[_Workorder], int]:
    response = await client.get(_LIST_PATH, {
        **native, "pageNum": page, "pageSize": page_size,
        "orderByColumn": "workorderId", "isAsc": "asc", "reasonable": "false",
    })
    rows, raw_total = response.get("rows"), response.get("total")
    if response.get("code") != 200 or not isinstance(rows, list) or type(raw_total) is not int:
        raise _invalid("MES 工单列表未返回可信的分页数据和总数。")
    total = raw_total
    if total < 0 or len(rows) != max(0, min(page_size, total - (page - 1) * page_size)):
        raise _invalid("MES 工单数量与页码、实际行数不一致。")
    orders = [_project(row) for row in rows]
    ids = [int(cast(str, item.row["workorderId"])) for item in orders]
    if ids != sorted(set(ids)):
        raise _invalid("MES 工单页未按唯一系统 ID 升序返回。")
    for item in orders:
        for key in ("workorderCode", "productCode", "status", "requestDate"):
            if key in native and str(item.row[key]).casefold() != str(native[key]).casefold():
                raise _invalid("MES 工单返回记录不符合服务端精确筛选条件。")
    return orders, total


def _needs_scan(filters: JsonObject) -> bool:
    statuses = filters.get("statuses")
    name = filters.get("workorder_name")
    return (isinstance(statuses, list) and len(statuses) > 1
            or filters.get("unfinished_only") is True
            or "request_date_from" in filters or "request_date_to" in filters
            or isinstance(name, str) and _has_like_metacharacters(name))


def _has_like_metacharacters(value: str) -> bool:
    return any(char in value for char in "%_\\")


def _literal_name(value: str) -> str:
    # Keep punctuation/whitespace literal while accommodating ordinary case and
    # accent equivalence used by the MES database collation.
    return "".join(char for char in unicodedata.normalize("NFD", value.casefold())
                   if not unicodedata.combining(char))


def _matches(row: JsonObject, filters: JsonObject) -> bool:
    name = filters.get("workorder_name")
    if (isinstance(name, str) and _has_like_metacharacters(name)
            and _literal_name(name) not in _literal_name(cast(str, row["workorderName"]))):
        return False
    statuses = filters.get("statuses")
    if isinstance(statuses, list) and row["status"] not in statuses:
        return False
    if filters.get("unfinished_only") is True and row["status"] == "FINISHED":
        return False
    start, end = filters.get("request_date_from"), filters.get("request_date_to")
    day = cast(str, row["requestDate"])
    return not ((isinstance(start, str) and day < start) or (isinstance(end, str) and day > end))


async def _select(client: KTGClient, filters: JsonObject, page: int, page_size: int
                  ) -> tuple[list[_Workorder], int | None]:
    native = _native_filters(filters)
    codes = filters.get("workorder_codes")
    if not isinstance(codes, list) and not _needs_scan(filters):
        return await _read_page(client, native, page, page_size)
    observed: list[_Workorder] = []
    complete = True
    if isinstance(codes, list):
        # Exact lookups avoid an unfiltered search for explicitly named workorders.
        for code in codes:
            batch, total = await _read_page(client, {**native, "workorderCode": code}, 1, _SCAN_PAGE_SIZE)
            observed.extend(batch)
            complete = complete and len(batch) == total
        observed.sort(key=lambda item: int(cast(str, item.row["workorderId"])))
    else:
        source_total: int | None = None
        for source_page in range(1, _MAX_SCAN_PAGES + 1):
            batch, total = await _read_page(client, native, source_page, _SCAN_PAGE_SIZE)
            if source_total is None:
                source_total = total
            if total != source_total:
                raise _invalid("多页读取期间 MES 工单总数发生变化，请重新查询。")
            observed.extend(batch)
            if len(observed) == total:
                break
        complete = len(observed) == source_total
    ids = [int(cast(str, item.row["workorderId"])) for item in observed]
    if ids != sorted(set(ids)):
        raise _invalid("多页或多单号查询未返回稳定的唯一工单集合。")
    matching = [item for item in observed if _matches(item.row, filters)]
    offset = (page - 1) * page_size
    return matching[offset:offset + page_size], len(matching) if complete else None


def _parse_progress(response: Mapping[str, object], selected: _Workorder) -> JsonObject:
    data = response.get("data")
    if response.get("code") != 200 or not isinstance(data, dict):
        raise _invalid("MES 未返回有效的工单执行摘要。")
    order, progress = data.get("order"), data.get("progress")
    if not isinstance(order, dict) or not isinstance(progress, dict):
        raise _invalid("MES 工单执行摘要缺失订单或统计。")
    row = selected.row
    if (_id(order.get("workorderId")) != row["workorderId"]
            or _id(progress.get("workorderId")) != row["workorderId"]
            or order.get("workorderCode") != row["workorderCode"]
            or order.get("status") != row["status"] or progress.get("status") != row["status"]):
        raise _invalid("列表与执行详情的工单身份或状态发生变化，需重新核实。")
    version = order.get("version")
    if selected.version is not None and (type(version) is not int or version != selected.version):
        raise _invalid("列表与执行详情的工单版本不同，需重新核实。")
    result: JsonObject = {}
    for key in _PROGRESS_COUNTS:
        value = progress.get(key)
        if type(value) is not int or value < 0:
            raise _invalid(f"MES 执行统计 {key} 缺失或无效。")
        result[key] = value
    if (Decimal(cast(str, row["quantity"])) != result["planned"]
            or ("plannedUnits" in order and
                (type(order["plannedUnits"]) is not int or order["plannedUnits"] != result["planned"]))):
        raise _invalid("列表与执行详情的计划量不同，需重新核实。")
    allowed, reasons = progress.get("completionAllowed"), progress.get("completionReasons")
    if type(allowed) is not bool or not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in reasons
    ) or allowed != (not reasons):
        raise _invalid("MES 完结条件缺失或无效。")
    result["completionAllowed"] = allowed
    result["completionReasons"] = cast(list[JsonValue], reasons)
    tasks = progress.get("tasks")
    if "tasks" in progress and not isinstance(tasks, list):
        raise _invalid("MES 执行任务摘要格式无效。")
    if isinstance(tasks, list):
        projected: list[JsonValue] = []
        for task in tasks[:_MAX_TASKS]:
            if not isinstance(task, dict):
                raise _invalid("MES 执行任务摘要格式无效。")
            item: JsonObject = {
                "taskId": _id(task.get("taskId")),
                **{key: _text(task.get(key), key, 64) for key in ("stepKind", "taskKind", "status")},
            }
            # Names and sequence come from the same execution response, joined by ID.
            details = data.get("tasks", [])
            if isinstance(details, list):
                matches = [detail for detail in details if isinstance(detail, dict)
                           and str(detail.get("taskId")) == item["taskId"]]
                if len(matches) == 1:
                    detail = matches[0]
                    if (_id(detail.get("workorderId")) != row["workorderId"]
                            or any(detail.get(key) != item[key] for key in ("stepKind", "taskKind", "status"))):
                        raise _invalid("MES 工序任务明细与摘要不一致。")
                    for key in ("processName", "taskCode"):
                        if detail.get(key) is not None:
                            item[key] = _text(detail[key], key, 255)
                    sequence = detail.get("normalStepNo")
                    if type(sequence) is int and sequence > 0:
                        item["normalStepNo"] = sequence
            projected.append(item)
        result["tasks"] = projected
        result["tasks_truncated"] = len(tasks) > _MAX_TASKS
    return result


async def _attach_progress(client: KTGClient, orders: list[_Workorder]) -> None:
    semaphore = asyncio.Semaphore(4)

    async def attach(item: _Workorder) -> None:
        row = item.row
        if row["status"] == "PREPARE":
            row["progress_state"] = "draft"
            return
        async with semaphore:
            try:
                response = await asyncio.wait_for(
                    client.get(_EXECUTION_PATH + cast(str, row["workorderId"])), timeout=5,
                )
                row["progress"] = _parse_progress(response, item)
                row["progress_state"] = "available"
            except TimeoutError:
                row["progress_state"] = "unavailable"
                row["progress_reason"] = "执行进度查询超时，待核实。"
            except KTGError as exc:
                if exc.code == "AUTH_EXPIRED":
                    raise
                row["progress_state"] = "unavailable"
                row["progress_reason"] = (
                    "当前身份无法核实执行进度。" if exc.code == "PERMISSION_DENIED"
                    else "执行进度不可用或数据已变化，待核实；不代表尚未下达或数量为零。"
                )

    await asyncio.gather(*(attach(item) for item in orders))


async def _execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        return ToolResult("KTG 查询客户端不可用。", status=ToolStatus.FAILED, error_code="KTG_CLIENT_MISSING")
    try:
        filters, page, page_size = _normalize(arguments)
        async with asyncio.timeout(15):
            orders, total = await _select(client, filters, page, page_size)
        if filters["include_progress"] is True:
            await _attach_progress(client, orders)
    except KTGError as exc:
        return ToolResult(str(exc), status=ToolStatus.FAILED, error_code=exc.code)
    except TimeoutError:
        return ToolResult("工单列表读取超出时间预算，结果待核实。", status=ToolStatus.FAILED,
                          error_code="SOURCE_INCOMPLETE")
    rows = [item.row for item in orders]
    warnings: list[JsonValue] = []
    if total is None:
        warnings.append("有限范围扫描未覆盖全部记录，当前页仅是已核实的匹配结果；总数未知。")
    if _needs_scan(filters) or "workorder_codes" in filters or filters["include_progress"] is True:
        warnings.append("多次查询不是跨工单的一致数据库快照；每个可用执行摘要来自 MES 同一详情响应。")
    if any(row["progress_state"] == "unavailable" for row in rows):
        warnings.append("部分工单执行进度待核实；不得视作零产量、无异常或未开始。")
    details: JsonObject = {
        "kind": "workorder_overview", "filters": filters, "rows": cast(list[JsonValue], rows),
        "total": total, "page": page, "page_size": page_size,
        "coverage": "complete" if total is not None else "partial",
        "queried_at": datetime.now(UTC).isoformat(),
        "warnings": warnings,
    }
    content = (
        ("工单筛选完整计数已核实。" if total is not None else "仅有部分工单筛选结果，总数未知。")
        + ("没有匹配的工单记录。" if total == 0 else "")
        + ("当前页没有记录，但筛选总数非零。" if not rows and total else "")
        + ("已完成工单的再次完结不合法，不是生产阻塞；completionReasons 仅为服务端完结条件。"
           if any(row["status"] == "FINISHED" for row in rows) else "")
        + json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    )
    return ToolResult(content, details=details)
