"""Read scoped single-unit quality history without inventing quality causes."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus


NAME = "ktg_query_workorder_quality"
PERMISSIONS = ("mes:pro:workorder:list", "mes:pro:execution:query")
_ORDER = "/mes/pro/workorder/list"
_EXECUTION = "/mes/pro/execution"
_PAGE_SIZE = 25
_MAX_UNITS = 50
_MAX_ROUNDS = 20
_MAX_LINES = 50
_HISTORY_OUTPUT_BYTES = 8000  # per order, keeps the model and browser payload bounded
_STATE_COUNTS = {
    "WAIT_INSPECTION": "waitingInspection", "WAIT_DISPOSITION": "pendingDisposition",
    "REWORK_IN_PROGRESS": "reworkInProgress", "WAIT_REINSPECTION": "waitingReinspection",
    "WAIT_PACKING": "packable", "PACKED": "packed", "SCRAPPED": "scrapped",
}
_ABNORMAL = {"WAIT_DISPOSITION", "REWORK_IN_PROGRESS", "WAIT_REINSPECTION", "SCRAPPED"}
_SUMMARY_KEYS = ("planned", "allocated", "waitingAssembly", "waitingInspection", "pendingDisposition",
                 "reworkInProgress", "waitingReinspection", "qualified", "scrapped",
                 "firstInspectionRejected", "reinspectionCount")
_SUMMARY_OUTPUT_KEYS = (*_SUMMARY_KEYS, "firstInspectionCompleted", "firstInspectionAccepted")


def _object(properties: JsonObject, required: tuple[str, ...]) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STRING: JsonObject = {"type": "string"}
_NULLABLE_STRING: JsonObject = {"type": ["string", "null"]}
_NULLABLE_COUNT: JsonObject = {"type": ["integer", "null"], "minimum": 0}
_STRINGS: JsonObject = {"type": "array", "items": _STRING}
_CODE: JsonObject = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"\S"}
_INPUT = _object({
    "workorder_codes": {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True,
                        "items": _CODE, "description": "1–5个准确工单编码；名称先经工单概览定位。"},
    "sn_codes": {"type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True, "items": _CODE,
                 "description": "可选，准确SN编码；仅在指定工单可见检验历史成员中查找，否则查历史异常单件。"},
}, ("workorder_codes",))
_VALIDATOR = Draft202012Validator(_INPUT)
_INDEX = _object({
    **{key: _STRING for key in ("indexCode", "indexName", "indexType")},
    **{key: _NULLABLE_STRING for key in ("value", "thresholdMin", "thresholdMax", "unitOfMeasure",
                                        "defectName", "defectLevel", "reason")},
    "passed": {"type": ["boolean", "null"]},
}, ("indexCode", "indexName", "indexType", "value", "passed", "thresholdMin", "thresholdMax",
    "unitOfMeasure", "defectName", "defectLevel", "reason"))
_INSPECTION = _object({
    "inspectionId": _STRING, "round_no": {"type": "integer", "minimum": 1},
    "result": {"enum": ["ACCEPT", "REJECT"]}, "inspect_date": _NULLABLE_STRING,
    "failed_indices": {"type": "array", "items": _INDEX, "maxItems": _MAX_LINES},
}, ("inspectionId", "round_no", "result", "inspect_date", "failed_indices"))
_UNIT = _object({
    "snId": _STRING, "snCode": _STRING, "unitState": {"enum": list(_STATE_COUNTS)},
    "inspection_count": _NULLABLE_COUNT, "first_result": {"enum": ["ACCEPT", "REJECT", None]},
    "latest_result": {"enum": ["ACCEPT", "REJECT", None]}, "ever_rejected": {"type": ["boolean", "null"]},
    "history_state": {"enum": ["verified", "unavailable"]},
    "inspections": {"type": "array", "items": _INSPECTION, "maxItems": _MAX_ROUNDS}, "warnings": _STRINGS,
}, ("snId", "snCode", "unitState", "inspection_count", "first_result", "latest_result", "ever_rejected",
    "history_state", "inspections"))
_SUMMARY = _object({key: {"type": "integer", "minimum": 0} for key in _SUMMARY_OUTPUT_KEYS}, _SUMMARY_OUTPUT_KEYS)
_ORDER_OUTPUT = _object({
    **{key: _STRING for key in ("workorderId", "workorderCode", "workorderName", "status")},
    "summary": {"anyOf": [_SUMMARY, {"type": "null"}]}, "history_coverage": {"enum": ["complete", "partial", "not_applicable"]},
    "scanned_units": {"type": "integer", "minimum": 0}, "total_units": _NULLABLE_COUNT,
    "units": {"type": "array", "items": _UNIT, "maxItems": _MAX_UNITS}, "missing_sn_codes": _STRINGS, "warnings": _STRINGS,
}, ("workorderId", "workorderCode", "workorderName", "status", "summary", "history_coverage", "scanned_units", "total_units", "units"))
_OUTPUT = _object({
    "kind": {"const": "workorder_quality"}, "filters": _INPUT,
    "orders": {"type": "array", "items": _ORDER_OUTPUT, "maxItems": 5}, "missing_codes": _STRINGS,
    "coverage": {"enum": ["complete", "partial"]}, "queried_at": _STRING, "warnings": _STRINGS,
}, ("kind", "filters", "orders", "missing_codes", "coverage", "queried_at", "warnings"))


def build_workorder_quality_tool() -> ToolDefinition:
    return ToolDefinition(
        name=NAME, version="1.0", label="查询工单质量异常与单件检验历史",
        description=("按准确工单编码查询全单质量统计与历史异常SN，区分待首检、当前待处置、返工中、"
                     "待复检、已报废及复检后合格。读取实际检验轮次、失败指标、测量与阈值、记录缺陷及备注。"
                     "可选准确SN编码查看指定单件（包括无失败或尚未检验）；仅查询当前账号检验任务范围。"
                     "首检完成件数由全单统计直接提供；历史核实件数不等于已检验件数。"
                     "默认每工单至多核实50个历史成员，部分覆盖或历史不可用时不声称无异常。"
                     "MES不合格处置只支持返工和报废，不支持降级放行或让步接收。"
                     "缺陷备注只是检验事实，不证明根因；不查原料追溯、不执行任何业务写入。"),
        input_schema=_INPUT, output_schema=_OUTPUT, required_permissions=PERMISSIONS,
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=45, execute=_execute,
    )


def _invalid(message: str) -> KTGError:
    return KTGError("KTG_INVALID_RESPONSE", message)


def _id(value: object) -> str:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
    else:
        raise _invalid("MES 主键缺失或格式异常。")
    if not 0 < number < 2**63:
        raise _invalid("MES 主键超出范围。")
    return str(number)


def _text(value: object, limit: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _invalid("MES 文本字段缺失或无效。")
    return value


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise _invalid("MES 计数字段缺失或无效。")
    return value


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 64:
        raise _invalid("检验测量值或阈值格式无效。")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise _invalid("检验测量值或阈值非数值。") from exc
    exponent = result.as_tuple().exponent
    if not result.is_finite() or not -20 <= result.adjusted() <= 20 or not isinstance(exponent, int) or exponent < -20:
        raise _invalid("检验测量值或阈值超出核实范围。")
    return result


def _number_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid("MES 返回非对象记录。")
    return cast(dict[str, object], value)


def _rows(value: object, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid("MES 明细缺失或超过核实范围。")
    return [_dict(row) for row in value]


def _note(target: JsonObject, message: str) -> None:
    notes = cast(list[JsonValue], target.setdefault("warnings", []))
    if message not in notes:
        notes.append(message)


async def _page(client: KTGClient, path: str, params: Mapping[str, object], field: str,
                cap: int = _MAX_UNITS) -> tuple[list[dict[str, object]], int, bool]:
    output: list[dict[str, object]] = []
    total: int | None = None
    previous = 0
    for page in range(1, (cap + _PAGE_SIZE - 1) // _PAGE_SIZE + 1):
        raw = await client.get(path, {**params, "pageNum": page, "pageSize": _PAGE_SIZE})
        current = _count(raw.get("total"))
        if raw.get("code") != 200 or (total is not None and current != total):
            raise _invalid("分页总数变化或来源异常。")
        total = current
        rows = _rows(raw.get("rows"), _PAGE_SIZE)
        if len(rows) != min(_PAGE_SIZE, max(0, total - (page - 1) * _PAGE_SIZE)):
            raise _invalid("分页记录与总数不匹配。")
        for row in rows:
            row_id = int(_id(row.get(field)))
            if row_id <= previous:
                raise _invalid("分页记录重复或顺序异常。")
            previous = row_id
        output.extend(rows)
        if len(output) == total:
            break
    assert total is not None
    return output[:cap], total, total <= cap


async def _lookup(client: KTGClient, code: str) -> dict[str, object] | None:
    rows, total, complete = await _page(client, _ORDER, {"workorderCode": code, "orderByColumn": "workorderId",
                                                     "isAsc": "asc", "reasonable": "false"}, "workorderId", 1)
    if total == 0:
        return None
    if not complete or len(rows) != 1 or _text(rows[0].get("workorderCode"), 64).casefold() != code.casefold():
        raise _invalid("工单编码无法准确唯一定位。")
    return rows[0]


def _identity(header: Mapping[str, object]) -> tuple[str, str, str, int, Decimal]:
    return (_id(header.get("workorderId")), _text(header.get("workorderCode"), 64),
            _text(header.get("status"), 64), _count(header.get("version")), _decimal(header.get("quantity")))


async def _progress(client: KTGClient, identity: tuple[str, str, str, int, Decimal]
                    ) -> tuple[dict[str, int], str | None]:
    order_id, code, status, version, quantity = identity
    raw = await client.get(f"{_EXECUTION}/workorders/{order_id}")
    if raw.get("code") != 200:
        raise _invalid("工单执行详情不可核实。")
    data = _dict(raw.get("data"))
    order, progress = _dict(data.get("order")), _dict(data.get("progress"))
    if (_id(order.get("workorderId")) != order_id or order.get("workorderCode") != code or order.get("status") != status
            or _count(order.get("version")) != version or _id(progress.get("workorderId")) != order_id
            or progress.get("status") != status):
        raise _invalid("工单详情身份、状态或版本不一致。")
    counts = {key: _count(progress.get(key)) for key in (*_SUMMARY_KEYS, "assembled", "packable", "packed")}
    if (counts["planned"] <= 0 or counts["planned"] != _count(order.get("plannedUnits")) or counts["planned"] != quantity
            or counts["allocated"] != counts["planned"] or counts["qualified"] != counts["packable"] + counts["packed"]
            or counts["assembled"] != counts["allocated"] - counts["waitingAssembly"]
            or counts["assembled"] != sum(counts[key] for key in _STATE_COUNTS.values())
            or counts["firstInspectionRejected"] > counts["assembled"] - counts["waitingInspection"]
            or (status == "FINISHED" and counts["packed"] + counts["scrapped"] != counts["allocated"])):
        raise _invalid("单件质量统计口径不一致。")
    tasks = _rows(progress.get("tasks"), 100)
    inspection = [_id(task.get("taskId")) for task in tasks
                  if task.get("stepKind") == "INSPECTION" and task.get("taskKind") == "NORMAL"]
    return counts, inspection[0] if len(inspection) == 1 else None


def _unit(raw: Mapping[str, object]) -> JsonObject:
    state = _text(raw.get("unitState"))
    if state not in _STATE_COUNTS:
        raise _invalid("检验任务返回无效单件状态。")
    _count(raw.get("version"))
    _id(raw.get("currentTaskId"))
    return {"snId": _id(raw.get("snId")), "snCode": _text(raw.get("snCode"), 64), "unitState": state,
            "history_state": "unavailable", "inspection_count": None, "first_result": None,
            "latest_result": None, "ever_rejected": None, "inspections": []}


async def _members(client: KTGClient, task: str, sn_codes: list[str]
                   ) -> tuple[list[dict[str, object]], int, bool, list[str]]:
    path = f"{_EXECUTION}/tasks/{task}/units"
    params = {"includeHistory": "true"}
    if not sn_codes:
        rows, total, complete = await _page(client, path, params, "snId")
        return rows, total, complete, []
    found: list[dict[str, object]] = []
    missing: list[str] = []
    complete = True
    for code in sn_codes:
        rows, _total, covered = await _page(client, path, {**params, "snCode": code}, "snId")
        exact = [row for row in rows if _text(row.get("snCode"), 64).casefold() == code.casefold()]
        if len(exact) > 1:
            raise _invalid("SN 编码无法唯一定位。")
        if not covered:
            complete = False
        if exact:
            found.extend(exact)
        else:
            missing.append(code)
    return found, len(found), complete and not missing, missing


def _failed_indices(record: Mapping[str, object], ipqc: str, rejected: bool) -> list[JsonObject]:
    lines, defects = _rows(record.get("lines"), _MAX_LINES), _rows(record.get("defects"), _MAX_LINES)
    if not lines:
        raise _invalid("检验指标明细缺失。")
    by_line: dict[str, dict[str, object]] = {}
    records: set[str] = set()
    for defect in defects:
        line_id, record_id = _id(defect.get("lineId")), _id(defect.get("recordId"))
        if (line_id in by_line or record_id in records or defect.get("qcType") != "IPQC"
                or _id(defect.get("qcId")) != ipqc or _decimal(defect.get("defectQuantity")) != 1):
            raise _invalid("缺陷记录与本轮检验不一致。")
        by_line[line_id] = defect
        records.add(record_id)
    failures: list[JsonObject] = []
    line_ids, index_ids = set[str](), set[str]()
    for line in lines:
        line_id, index_id = _id(line.get("lineId")), _id(line.get("indexId"))
        if line_id in line_ids or index_id in index_ids or _id(line.get("ipqcId")) != ipqc:
            raise _invalid("检验指标身份不一致。")
        line_ids.add(line_id)
        index_ids.add(index_id)
        try:
            measured = _dict(json.loads(_text(line.get("remark"), 500), parse_float=Decimal))
        except (ValueError, RecursionError) as exc:
            raise _invalid("实际检验测量记录不可核实。") from exc
        kind = line.get("indexType")
        actual, passed = measured.get("value"), measured.get("passed")
        lower: Decimal | None = None
        upper: Decimal | None = None
        value: Decimal | None = None
        if kind == "QUANTITATIVE" and passed is None:
            lower, upper, value = (_decimal(line.get("thresholdMin")), _decimal(line.get("thresholdMax")), _decimal(actual))
            if lower > upper:
                raise _invalid("检验指标阈值无效。")
            failed = not lower <= value <= upper
        elif kind == "QUALITATIVE" and type(passed) is bool and actual is None:
            failed = not passed
        else:
            raise _invalid("检验指标实际测量类型无效。")
        counts = {level: _decimal(line.get(f"{level.lower()}Quantity")) for level in ("CR", "MAJ", "MIN")}
        linked_defect = by_line.get(line_id)
        if linked_defect is not None:
            del by_line[line_id]
        if failed:
            if linked_defect is None or measured.get("defectId") is None:
                raise _invalid("失败指标未关联真实缺陷记录。")
            _id(measured["defectId"])
            level = _text(linked_defect.get("defectLevel"))
            if level not in counts or any(counts[key] != (1 if key == level else 0) for key in counts):
                raise _invalid("指标缺陷数量与等级不一致。")
            unit = line.get("unitOfMeasure")
            failures.append({"indexCode": _text(line.get("indexCode"), 64), "indexName": _text(line.get("indexName")),
                             "indexType": cast(str, kind), "value": _number_text(value) if value is not None else None,
                             "passed": passed, "thresholdMin": _number_text(lower) if lower is not None else None,
                             "thresholdMax": _number_text(upper) if upper is not None else None,
                             "unitOfMeasure": _text(unit, 64) if unit else None,
                             "defectName": _text(linked_defect.get("defectName"), 500), "defectLevel": level,
                             "reason": _text(linked_defect.get("remark"), 500)})
        elif linked_defect is not None or measured.get("defectId") is not None or any(counts.values()):
            raise _invalid("合格指标却出现失败缺陷记录。")
    if by_line or bool(failures) != rejected:
        raise _invalid("指标明细与本轮检验结果不一致。")
    return failures


async def _history(client: KTGClient, task: str, unit: JsonObject) -> None:
    raw = await client.get(f"{_EXECUTION}/units/{unit['snId']}/inspections")
    if raw.get("code") != 200:
        raise _invalid("单件检验历史不可核实。")
    history = _rows(raw.get("data"), _MAX_ROUNDS)
    observations: list[JsonValue] = []
    results: list[str] = []
    seen: set[str] = set()
    receipts: set[str] = set()
    for number, record in enumerate(history, 1):
        result = _text(record.get("checkResult"))
        identifier, receipt = _id(record.get("inspectionId")), _id(record.get("ipqcId"))
        reject = result == "REJECT"
        if (identifier in seen or receipt in receipts or _id(record.get("snId")) != unit["snId"]
                or _id(record.get("taskId")) != task or _count(record.get("roundNo")) != number
                or result not in {"ACCEPT", "REJECT"} or (results and results[-1] != "REJECT")
                or _decimal(record.get("quantityCheck")) != 1 or _decimal(record.get("quantityQualified")) != (0 if reject else 1)
                or _decimal(record.get("quantityUnqualified")) != (1 if reject else 0)
                or (number == 1) != (record.get("sourceDispositionId") is None)
                or reject != (record.get("produceLineId") is None)):
            raise _invalid("检验身份、轮次、数量或结果不一致。")
        if number > 1:
            _id(record.get("sourceDispositionId"))
        if not reject:
            _id(record.get("produceLineId"))
        seen.add(identifier)
        receipts.add(receipt)
        results.append(result)
        date = record.get("inspectDate")
        observations.append({"inspectionId": identifier, "round_no": number, "result": result,
                             "inspect_date": _text(date, 64) if date is not None else None,
                             "failed_indices": cast(list[JsonValue], _failed_indices(record, receipt, reject))})
    state = unit["unitState"]
    latest = results[-1] if results else None
    expected = None if state == "WAIT_INSPECTION" else "REJECT" if state in _ABNORMAL else "ACCEPT"
    if latest != expected:
        raise _invalid("当前单件状态与最新检验事实不一致。")
    unit.update({"history_state": "verified", "inspection_count": len(results),
                 "first_result": results[0] if results else None, "latest_result": latest,
                 "ever_rejected": "REJECT" in results, "inspections": observations})


async def _read_quality(client: KTGClient, order: JsonObject, identity: tuple[str, str, str, int, Decimal],
                        sn_codes: list[str]) -> None:
    counts, task = await _progress(client, identity)
    first_completed = counts["allocated"] - counts["waitingAssembly"] - counts["waitingInspection"]
    order["summary"] = {**{key: counts[key] for key in _SUMMARY_KEYS},
                        "firstInspectionCompleted": first_completed,
                        "firstInspectionAccepted": first_completed - counts["firstInspectionRejected"]}
    if first_completed == 0:
        _note(order, f"本工单尚无首检结果，首检实际完成0件；当前{counts['waitingInspection']}件待检、{counts['waitingAssembly']}件待装配，不能称为已经检验或合格。")
    if task is None:
        _note(order, "正常检验任务不可见或无法唯一定位，质量统计为全工单，单件历史待核实。")
        return
    try:
        raw, total, complete, missing = await _members(client, task, sn_codes)
        units = [_unit(row) for row in raw]
        if (len({unit["snId"] for unit in units}) != len(units)
                or len({cast(str, unit["snCode"]).casefold() for unit in units}) != len(units)):
            raise _invalid("单件列表出现重复身份。")
    except KTGError as exc:
        if exc.code == "AUTH_EXPIRED":
            raise
        _note(order, "检验任务内单件历史范围无权访问或未核实，不代表没有异常。")
        return
    order["total_units"] = total
    if sn_codes:
        _note(order, "单件历史仅核实指定SN在本工单可见检验任务的范围，统计仍为全工单。")
        if missing:
            order["missing_sn_codes"] = cast(list[JsonValue], missing)
            _note(order, "指定SN未在本工单可见检验范围匹配；可能尚未进入检验、不可见或不存在，无法据此断言。")
    else:
        if not complete or total != counts["assembled"]:
            _note(order, "检验范围受分页上限或可见性限制，未完全覆盖全单已装配成员。")
            complete = False
        state_counts = Counter(cast(str, entry["unitState"]) for entry in units)
        if any(state_counts[state] != counts[key] for state, key in _STATE_COUNTS.items()):
            complete = False
            _note(order, "单件列表和全工单质量统计未完全一致，异常明细不完整。")

    async def read(unit: JsonObject) -> None:
        try:
            await _history(client, task, unit)
        except KTGError as exc:
            if exc.code == "AUTH_EXPIRED":
                raise
            _note(unit, "检验历史缺失、不可见或与状态不一致，不能判断具体失败指标或历史轮次。")

    # Small batches provide bounded parallelism without leaving requests pending
    # when one reply expires authentication.
    for i in range(0, len(units), 4):
        jobs = await asyncio.gather(*(read(unit) for unit in units[i:i + 4]), return_exceptions=True)
        for job in jobs:
            if isinstance(job, BaseException):
                raise job
    scanned = [unit for unit in units if unit["history_state"] == "verified"]
    order["scanned_units"] = len(scanned)
    if len(scanned) != len(units):
        complete = False
        _note(order, "部分单件历史待核实；不能把未读历史当作没有异常。")
    if not sn_codes and complete:
        if (sum(entry["first_result"] == "REJECT" for entry in scanned) != counts["firstInspectionRejected"]
                or sum(max(0, cast(int, entry["inspection_count"]) - 1) for entry in scanned) != counts["reinspectionCount"]):
            complete = False
            _note(order, "逐件历史与工单汇总的首检不合格或复检次数不一致，需复核。")
    selected = [entry for entry in units if sn_codes or entry["ever_rejected"] is True or entry["unitState"] in _ABNORMAL]
    kept: list[JsonValue] = []
    used = 2
    for entry in selected:
        size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if used + size <= _HISTORY_OUTPUT_BYTES:
            kept.append(entry)
            used += size
    if len(kept) != len(selected):
        complete = False
        _note(order, f"历史输出超过本次明细上限，省略{len(selected) - len(kept)}个单件的完整记录；可按准确SN缩小查询范围。已核实历史数仍包含省略项。")
    order["history_coverage"] = "complete" if complete else "partial"
    order["units"] = kept


def _invalidate(order: JsonObject) -> None:
    order.update({"summary": None, "units": [], "history_coverage": "partial", "total_units": None, "scanned_units": 0})
    order.pop("missing_sn_codes", None)
    order.pop("warnings", None)
    _note(order, "工单状态/版本、权限或查询时限无法完成复核，已撤回本次质量统计及单件历史，需重新查询。")


async def _execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        return ToolResult("KTG 查询客户端不可用。", status=ToolStatus.FAILED, error_code="KTG_CLIENT_MISSING")
    if not _VALIDATOR.is_valid(dict(arguments)):
        return ToolResult("工单质量查询参数无效。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
    codes = [code.strip() for code in cast(list[str], arguments["workorder_codes"])]
    sn_codes = [code.strip() for code in cast(list[str], arguments.get("sn_codes", []))]
    if len(set(code.casefold() for code in codes)) != len(codes) or len(set(code.casefold() for code in sn_codes)) != len(sn_codes):
        return ToolResult("工单或 SN 编码不可重复。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
    filters: JsonObject = {"workorder_codes": cast(list[JsonValue], codes)}
    if sn_codes:
        filters["sn_codes"] = cast(list[JsonValue], sn_codes)
    orders: list[JsonValue] = []
    missing: list[JsonValue] = []
    warnings: list[JsonValue] = ["历史核实件数不等于已检验件数，包含已核实无检验记录的待检单件。",
                                "当前MES不合格处置只支持返工和报废，不支持降级放行、让步接收或其他放行方式。本工具只读，不执行处置。",
                                "缺陷与备注为已记录的质检事实，不能直接确定真实根因。",
                                "统计与明细为多接口读取，不是跨接口一致事务快照。"]
    partial = False
    for code in codes:
        order: JsonObject | None = None
        try:
            async with asyncio.timeout(8):
                header = await _lookup(client, code)
                if header is None:
                    missing.append(code)
                    partial = True
                    continue
                order = {"workorderId": _id(header.get("workorderId")), "workorderCode": _text(header.get("workorderCode"), 64),
                         "workorderName": _text(header.get("workorderName")), "status": _text(header.get("status")),
                         "summary": None, "units": [], "history_coverage": "partial", "total_units": None, "scanned_units": 0}
                orders.append(order)
                if order["status"] == "PREPARE":
                    order["history_coverage"] = "not_applicable"
                    _note(order, "草稿尚未下达，不能视为质量统计为零。")
                    if sn_codes:
                        order["missing_sn_codes"] = cast(list[JsonValue], sn_codes)
                        partial = True
                    continue
                if order["status"] not in {"CONFIRMED", "FINISHED"}:
                    _note(order, "该工单状态不适用本期单件质量历史查询。")
                else:
                    identity = _identity(header)
                    await _read_quality(client, order, identity, sn_codes)
                    latest = await _lookup(client, code)
                    if latest is None or _identity(latest) != identity:
                        raise _invalid("工单状态或版本在读取期间变化。")
        except (KTGError, TimeoutError) as exc:
            if isinstance(exc, KTGError) and exc.code == "AUTH_EXPIRED":
                return ToolResult("登录状态已过期，请重新登录后查询。", status=ToolStatus.FAILED, error_code="AUTH_EXPIRED")
            partial = True
            if order is None:
                warnings.append(f"工单 {code} 未核实或不可访问，不代表不存在。")
            else:
                _invalidate(order)
        if order is not None and order["history_coverage"] == "partial":
            partial = True
    if missing:
        warnings.append("以下工单编码未在当前权限范围匹配，不代表系统不存在记录。")
    details: JsonObject = {"kind": "workorder_quality", "filters": filters, "orders": orders, "missing_codes": missing,
                           "coverage": "partial" if partial else "complete", "queried_at": datetime.now(UTC).isoformat(), "warnings": warnings}
    return ToolResult("仅依据已核实的全工单质量统计与单件检验历史；待核实不代表没有异常。"
                      "首检完成和首检合格件数分别读取 summary.firstInspectionCompleted 和 summary.firstInspectionAccepted；scanned_units、total_units 不是已检件数。"
                      "inspect_date 检验时间无时区时按MES原样报告，不自行认定UTC或换算；queried_at 是独立查询时点。"
                      + json.dumps(details, ensure_ascii=False, separators=(",", ":")), details=details)
