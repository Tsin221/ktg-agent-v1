"""Read actual, authorized production consumption in both trace directions."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus


NAME = "ktg_query_production_trace"
PERMISSIONS = ("mes:pro:execution:trace", "mes:pro:execution:query", "mes:md:mditem:list")
_UNIT_PATH = "/mes/pro/execution/units/trace"
_MATERIAL_PATH = "/mes/pro/execution/materials/trace"
_ITEM_PATH = "/mes/md/mditem/list"
_PAGE_SIZE = 20
_MAX_RESULTS = 50
_MAX_ITEM_LOOKUP = 250
_ITEM_PAGE = 50
_MAX_UNIT_BYTES = 12_000
_MAX_BATCH_BYTES = 24_000
_MAX_ID = 2**63 - 1
_STATE_COUNTS = {"WAIT_INSPECTION": "waitingInspection", "WAIT_DISPOSITION": "pendingDisposition",
                 "REWORK_IN_PROGRESS": "reworkInProgress", "WAIT_REINSPECTION": "waitingReinspection",
                 "WAIT_PACKING": "waitingPacking", "PACKED": "packed", "SCRAPPED": "scrapped"}
_STATES = ("WAIT_ASSEMBLY", *_STATE_COUNTS)


def _object(properties: JsonObject, required: tuple[str, ...]) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STR: JsonObject = {"type": "string"}
_NULL_STR: JsonObject = {"type": ["string", "null"]}
_COUNT: JsonObject = {"type": "integer", "minimum": 0}
_NULL_COUNT: JsonObject = {"type": ["integer", "null"], "minimum": 0}
_CODE: JsonObject = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"\S"}
_BATCH_CODE: JsonObject = {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"\S"}
_WARNINGS: JsonObject = {"type": "array", "items": _STR}
_BATCH_FILTER = _object({"item_code": _CODE, "batch_code": _BATCH_CODE}, ("item_code", "batch_code"))
_DIRECTION: JsonObject = {"enum": ["unit_to_material", "material_to_unit"]}
_INPUT = _object({
    "direction": {**_DIRECTION, "description": "unit_to_material: 成品SN查实际投入原料；material_to_unit: 原料物料编码+批次查实际使用成品。"},
    "sn_codes": {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True,
                 "items": _CODE, "description": "仅成品反查模式，1–5个完整精确SN，不去除空格或转换大小写。"},
    "batches": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                "items": _BATCH_FILTER, "description": "仅原料正查模式，1–3组完整物料编码和精确原料批次，不以批次号单独认定物料。"},
    "page": {"type": "integer", "minimum": 1, "maximum": 2147483647, "default": 1,
             "description": "原料正查每批次页码，默认1。"},
    "page_size": {"type": "integer", "minimum": 1, "maximum": _MAX_RESULTS, "default": _PAGE_SIZE,
                  "description": "原料正查每批次每页成品数，默认20，最多50。"},
}, ("direction",))
_INPUT["oneOf"] = [
    {"properties": {"direction": {"const": "unit_to_material"}}, "required": ["sn_codes"],
     "not": {"anyOf": [{"required": [field]} for field in ("batches", "page", "page_size")]}},
    {"properties": {"direction": {"const": "material_to_unit"}}, "required": ["batches"],
     "not": {"required": ["sn_codes"]}},
]
_VALIDATOR = Draft202012Validator(_INPUT)
_INPUT_FIELDS = ("inputId", "itemId", "itemCode", "itemName", "sourceBatchCode", "quantity", "unitOfMeasure", "usedAt")
_RECORD = _object({key: _STR for key in _INPUT_FIELDS}, _INPUT_FIELDS)
_IDENTITY = ("snId", "snCode", "itemId", "itemCode", "itemName", "workorderId", "workorderCode", "workorderName", "orderStatus", "unitState")
_UNIT = _object({**{key: _NULL_STR for key in _IDENTITY}, "snCode": _STR,
                 "trace_state": {"enum": ["tracked", "legacy", "unavailable"]},
                 "input_state": {"enum": ["recorded", "waiting_assembly", "legacy", "unavailable"]},
                 "inputs": {"type": "array", "items": _RECORD, "maxItems": _MAX_RESULTS},
                 "warnings": _WARNINGS}, (*_IDENTITY, "trace_state", "input_state", "inputs", "warnings"))
_ROW = _object({**{key: _STR for key in (*_IDENTITY, "firstUsedAt", "lastUsedAt")},
               "inputCount": {"type": "integer", "minimum": 1}}, (*_IDENTITY, "firstUsedAt", "lastUsedAt", "inputCount"))
_COUNT_KEYS = (*_STATE_COUNTS.values(), "qualified")
_SUMMARY = _object({key: _COUNT for key in _COUNT_KEYS}, _COUNT_KEYS)
_BATCH = _object({"itemId": _NULL_STR, "itemCode": _STR, "itemName": _NULL_STR, "batchCode": _STR,
                  "total_units": _NULL_COUNT, "returned_units": _COUNT,
                  "rows": {"type": "array", "items": _ROW, "maxItems": _MAX_RESULTS},
                  "state_counts": {"anyOf": [_SUMMARY, {"type": "null"}]},
                  "page": {"type": "integer", "minimum": 1}, "page_size": {"type": "integer", "minimum": 1},
                  "coverage": {"enum": ["complete", "partial", "unavailable"]}, "warnings": _WARNINGS},
                 ("itemId", "itemCode", "itemName", "batchCode", "total_units", "returned_units", "rows", "state_counts",
                  "page", "page_size", "coverage", "warnings"))
_OUTPUT = _object({"kind": {"const": "production_trace"}, "direction": _DIRECTION, "filters": _INPUT,
                   "units": {"type": "array", "items": _UNIT, "maxItems": 5},
                   "batches": {"type": "array", "items": _BATCH, "maxItems": 3},
                   "coverage": {"enum": ["complete", "partial"]}, "queried_at": _STR, "warnings": _WARNINGS},
                  ("kind", "direction", "filters", "units", "batches", "coverage", "queried_at", "warnings"))


def build_production_trace_tool() -> ToolDefinition:
    return ToolDefinition(
        name=NAME, version="1.0", label="原料与成品双向追溯",
        description=("查询实际投入的原料物料编码、批次、单件数量及由同批原料实际生产的成品SN、所属工单及当前状态。"
                     "unit_to_material输入精确SN，可核实SN身份、所属工单和当前状态并查询实际投入；material_to_unit输入已核实的itemCode+sourceBatchCode（批次可能跨物料重名），支持分页。"
                     "与工单质量查询组合分析同批成品质量；此处待检不等于不合格，合格状态不代表历史无失败。"
                     "total_units是单物料单批次当前权限范围内实际使用成品去重总数，returned_units、state_counts只统计返回页，跨批次不可相加。"
                     "不查BOM计划、领料或库存推算，不推断根因，不执行召回、隔离或任何业务写入。"),
        input_schema=_INPUT, output_schema=_OUTPUT, required_permissions=PERMISSIONS, effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=45,
        execute=_execute,
    )


def _invalid(message: str) -> KTGError:
    return KTGError("KTG_INVALID_RESPONSE", message)


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid("MES record is not an object")
    return cast(dict[str, object], value)


def _text(value: object, limit: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _invalid("MES text field invalid")
    return value


def _id(value: object) -> str:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
    else:
        raise _invalid("MES ID invalid")
    if not 0 < number <= _MAX_ID:
        raise _invalid("MES ID out of range")
    return str(number)


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_ID:
        raise _invalid("MES count invalid")
    return value


def _quantity(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 32:
        raise _invalid("MES quantity invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise _invalid("MES quantity invalid") from exc
    if not number.is_finite() or number <= 0 or number < Decimal("0.01") or number.adjusted() >= 16:
        raise _invalid("MES quantity invalid")
    # Actual input column is DECIMAL(18,2), not a float aggregation. Do not round.
    if number != number.quantize(Decimal("0.01")):
        raise _invalid("MES quantity scale invalid")
    return format(number, "f")


def _records(value: object, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid("MES records missing or exceed bounded limit")
    return [_dict(row) for row in value]


def _note(target: JsonObject, message: str) -> None:
    cast(list[JsonValue], target["warnings"]).append(message)


def _bounded(value: object, maximum: int) -> bool:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= maximum


def _identity(data: Mapping[str, object], *, legacy: bool = False) -> JsonObject:
    identity: JsonObject = {"snId": _id(data.get("snId")), "snCode": _text(data.get("snCode"), 64),
                            "itemId": _id(data.get("itemId")), "itemCode": _text(data.get("itemCode"), 64),
                            "itemName": _text(data.get("itemName"))}
    if legacy:
        identity.update({key: None for key in _IDENTITY[5:]})
    else:
        state = _text(data.get("unitState"), 64)
        if state not in _STATES:
            raise _invalid("MES unit state invalid")
        order_status = _text(data.get("orderStatus"), 64)
        if order_status not in {"CONFIRMED", "FINISHED"}:
            raise _invalid("MES workorder state invalid for execution")
        if order_status == "FINISHED" and state not in {"PACKED", "SCRAPPED"}:
            raise _invalid("MES finished workorder has a nonterminal unit")
        identity.update({"workorderId": _id(data.get("workorderId")),
                         "workorderCode": _text(data.get("workorderCode"), 64),
                         "workorderName": _text(data.get("workorderName")),
                         "orderStatus": order_status, "unitState": state})
    return identity


def _input_record(data: Mapping[str, object]) -> JsonObject:
    return {"inputId": _id(data.get("inputId")), "itemId": _id(data.get("itemId")),
            "itemCode": _text(data.get("itemCode"), 64), "itemName": _text(data.get("itemName")),
            "sourceBatchCode": _text(data.get("sourceBatchCode")), "quantity": _quantity(data.get("quantity")),
            "unitOfMeasure": _text(data.get("unitOfMeasure"), 64), "usedAt": _text(data.get("usedAt"), 64)}


async def _unit(client: KTGClient, code: str) -> JsonObject:
    target: JsonObject = {**{key: None for key in _IDENTITY}, "snCode": code, "trace_state": "unavailable",
                          "input_state": "unavailable", "inputs": [], "warnings": []}
    try:
        async with asyncio.timeout(8):
            payload = await client.get(_UNIT_PATH, {"snCode": code})
            data = _dict(payload.get("data"))
            if payload.get("code") != 200 or data.get("snCode") != code:
                raise _invalid("MES did not match exact SN")
            trace_status = data.get("traceStatus")
            if trace_status == "LEGACY_NO_RECORD":
                if data.get("workorderId") is not None or data.get("inputs") != []:
                    raise _invalid("MES legacy trace inconsistent")
                target.update(_identity(data, legacy=True))
                target.update(trace_state="legacy", input_state="legacy")
                _note(target, "该旧SN无单件执行追溯记录，不能确认实际投入，不是零消耗。")
                return target
            if trace_status != "TRACKED":
                raise _invalid("MES trace status unknown")
            target.update(_identity(data))
            target["trace_state"] = "tracked"
            raw_inputs = _records(data.get("inputs"), _MAX_RESULTS)
            if target["unitState"] == "WAIT_ASSEMBLY":
                if raw_inputs or data.get("assemblyFeedbackId") is not None:
                    raise _invalid("MES unreported unit has consumption or assembly fact")
                target["input_state"] = "waiting_assembly"
                _note(target, "尚待装配/报工，当前没有经单件报工确认的实际投入，不等于BOM计划或已领料数量。")
                return target
            feedback = _id(data.get("assemblyFeedbackId"))
            if not raw_inputs or any(_id(raw.get("feedbackId")) != feedback for raw in raw_inputs):
                raise _invalid("MES assembly consumption missing or inconsistent")
            records = [_input_record(raw) for raw in raw_inputs]
            if len({entry["inputId"] for entry in records}) != len(records) or not _bounded(records, _MAX_UNIT_BYTES):
                raise _invalid("MES actual input records duplicate or exceed output limit")
            target.update(input_state="recorded", inputs=cast(list[JsonValue], records))
            return target
    except (KTGError, TimeoutError) as exc:
        if isinstance(exc, KTGError) and exc.code in {"AUTH_EXPIRED", "AUTH_REQUIRED"}:
            raise
        _note(target, "该SN未核实身份或实际投入（权限范围、不存在、源记录不一致或查询超限均可能），不能认定无消耗。")
        return target


def _checked_page(payload: Mapping[str, object], page: int, page_size: int) -> tuple[int, list[dict[str, object]]]:
    if payload.get("code") != 200:
        raise _invalid("MES page failed")
    total = _count(payload.get("total"))
    rows = _records(payload.get("rows"), page_size)
    if len(rows) != min(page_size, max(0, total - (page - 1) * page_size)):
        raise _invalid("MES page count inconsistent")
    return total, rows


async def _lookup_item(client: KTGClient, code: str) -> tuple[str, str]:
    # MD item list uses LIKE; only exhaustive exact-code matching may resolve an ID.
    params: dict[str, object] = {"itemCode": code, "pageSize": _ITEM_PAGE, "orderByColumn": "itemId", "isAsc": "asc"}
    seen: set[str] = set()
    matched: list[tuple[str, str]] = []
    expected: int | None = None
    last_id = 0
    for page in range(1, _MAX_ITEM_LOOKUP // _ITEM_PAGE + 1):
        payload = await client.get(_ITEM_PATH, {**params, "pageNum": page})
        total, rows = _checked_page(payload, page, _ITEM_PAGE)
        if total > _MAX_ITEM_LOOKUP or (expected is not None and total != expected):
            raise _invalid("MES item lookup coverage unavailable")
        expected = total
        for row in rows:
            key = _id(row.get("itemId"))
            if key in seen or int(key) <= last_id:
                raise _invalid("MES item lookup duplicated or unordered identity")
            seen.add(key)
            last_id = int(key)
            if row.get("itemCode") == code:
                matched.append((key, _text(row.get("itemName"))))
        if len(seen) == total:
            break
    if len(matched) != 1 or expected is None or len(seen) != expected:
        raise _invalid("MES item code not uniquely verified")
    return matched[0]


def _reverse_row(data: Mapping[str, object]) -> JsonObject:
    identity = _identity(data)
    if identity["unitState"] == "WAIT_ASSEMBLY":
        raise _invalid("MES actual consumption matched a waiting assembly unit")
    input_count = _count(data.get("inputCount"))
    if input_count == 0:
        raise _invalid("MES actual input count invalid")
    identity.update(firstUsedAt=_text(data.get("firstUsedAt"), 64), lastUsedAt=_text(data.get("lastUsedAt"), 64),
                    inputCount=input_count)
    return identity


async def _batch(client: KTGClient, item_code: str, batch_code: str, page: int, page_size: int) -> JsonObject:
    target: JsonObject = {"itemId": None, "itemCode": item_code, "itemName": None, "batchCode": batch_code,
                          "total_units": None, "returned_units": 0, "rows": [], "state_counts": None,
                          "page": page, "page_size": page_size, "coverage": "unavailable", "warnings": []}
    try:
        async with asyncio.timeout(12):
            item_id, item_name = await _lookup_item(client, item_code)
            target.update(itemId=item_id, itemName=item_name)
            payload = await client.get(_MATERIAL_PATH, {"itemId": item_id, "batchCode": batch_code,
                                                        "pageNum": page, "pageSize": page_size})
            total, rows = _checked_page(payload, page, page_size)
            projected = [_reverse_row(row) for row in rows]
            ids = [int(cast(str, row["snId"])) for row in projected]
            if (len(set(ids)) != len(ids) or len({row["snCode"] for row in projected}) != len(projected)
                    or ids != sorted(ids) or not _bounded(projected, _MAX_BATCH_BYTES)):
                raise _invalid("MES reverse trace page identities duplicate, unordered or exceed output limit")
            counts = dict.fromkeys(_COUNT_KEYS, 0)
            for row in projected:
                counts[_STATE_COUNTS[cast(str, row["unitState"])]] += 1
            counts["qualified"] = counts["waitingPacking"] + counts["packed"]
            complete = page == 1 and len(projected) == total
            target.update(total_units=total, returned_units=len(projected), rows=cast(list[JsonValue], projected),
                          state_counts=cast(JsonObject, counts), coverage="complete" if complete else "partial")
            if total == 0:
                _note(target, "当前权限范围未发现该物料+批次的实际投入关联；不证明批次不存在或其他范围未使用。")
            if not complete:
                _note(target, "仅返回本页SN及本页状态计数；总件数为当前可见批次去重总数，不得将本页状态当全批统计。")
            return target
    except (KTGError, TimeoutError) as exc:
        if isinstance(exc, KTGError) and exc.code in {"AUTH_EXPIRED", "AUTH_REQUIRED"}:
            raise
        _note(target, "物料精确身份或批次实际使用情况未核实（含权限、分页不一致或查询超限）；不可按零件处理。")
        return target


async def _execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        return ToolResult("KTG 查询客户端不可用。", status=ToolStatus.FAILED, error_code="KTG_CLIENT_MISSING")
    if not _VALIDATOR.is_valid(dict(arguments)):
        return ToolResult("双向追溯参数不符合要求，请选择一个方向并提供精确编码。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
    direction = cast(str, arguments["direction"])
    filters: JsonObject = {"direction": direction}
    units: list[JsonValue] = []
    batches: list[JsonValue] = []
    partial = False
    try:
        if direction == "unit_to_material":
            codes = cast(list[str], arguments["sn_codes"])
            filters["sn_codes"] = cast(list[JsonValue], codes)
            for code in codes:
                unit = await _unit(client, code)
                units.append(unit)
                partial = partial or unit["input_state"] not in {"recorded", "waiting_assembly"}
        else:
            batch_filters = cast(list[dict[str, str]], arguments["batches"])
            page, page_size = cast(int, arguments.get("page", 1)), cast(int, arguments.get("page_size", _PAGE_SIZE))
            # JSON Schema permits an integral float as integer. Require actual Python int for pagination.
            if type(page) is not int or type(page_size) is not int:
                return ToolResult("分页参数必须为整数。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
            filters.update(batches=cast(list[JsonValue], batch_filters), page=page, page_size=page_size)
            for batch in batch_filters:
                result = await _batch(client, batch["item_code"], batch["batch_code"], page, page_size)
                batches.append(result)
                partial = partial or result["coverage"] != "complete"
    except KTGError as exc:
        if exc.code in {"AUTH_EXPIRED", "AUTH_REQUIRED"}:
            return ToolResult("登录状态已过期，请重新登录后查询。", status=ToolStatus.FAILED, error_code=exc.code)
        raise
    details: JsonObject = {"kind": "production_trace", "direction": direction, "filters": filters, "units": units,
                           "batches": batches, "coverage": "partial" if partial else "complete",
                           "queried_at": datetime.now(UTC).isoformat(),
                           "warnings": ["只追溯单件实际报工投入，不是BOM计划用量、领料量或库存。",
                                        "同批次实际使用成品不等于存在质量问题；同批成品的状态和质量历史需区分，不证明缺陷根因。",
                                        "每组物料+批次的总件数独立去重，跨批次可能重复；状态计数只对应返回页。同批次只检查当前权限可见范围。",
                                        "不同目标分别查询，不是跨目标一致事务快照。源使用时间按MES原样报告，不自行假定时区。"]}
    return ToolResult("按实际单件投入追溯，未知不等于零。qualified=waitingPacking+packed，表示当前合格件数，不是首检合格数；state_counts是返回页内状态。"
                      + json.dumps(details, ensure_ascii=False, separators=(",", ":")), details=details)
