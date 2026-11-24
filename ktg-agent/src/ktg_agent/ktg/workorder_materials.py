"""Read-only workorder material requirements and scoped stock comparisons."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import cast

from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus


NAME = "ktg_query_workorder_materials"
PERMISSIONS = ("mes:pro:workorder:list", "mes:pro:workorderbom:list", "mes:pro:execution:query", "mes:wm:wmstock:list")
_ORDER_PATH = "/mes/pro/workorder/list"
_BOM_PATH = "/mes/pro/workorderbom/list"
_STOCK_PATH = "/mes/wm/wmstock/list"
_EXECUTION_PATH = "/mes/pro/execution/workorders/"
_VIRTUAL_WAREHOUSE = "XBK_VIRTUAL"
_MAX_ID = 2**63 - 1
_MAX_PAGES = 10
_PAGE_SIZE = 20
_MAX_MATERIALS = 20
_MAX_WAREHOUSES = 10
_MAX_BATCHES = 20


def _object(properties: JsonObject, required: tuple[str, ...]) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_INPUT = _object({
    "workorder_codes": {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"\S"},
                        "description": "1–5个准确生产工单编码；如仅有名称，先查询工单概览。"},
    "include_batches": {"type": "boolean", "default": False, "description": "是否显示已核实的批次数量明细。"},
}, ("workorder_codes",))
_INPUT_VALIDATOR = Draft202012Validator(_INPUT)
_STRING: JsonObject = {"type": "string"}
_NULLABLE_QUANTITY: JsonObject = {"type": ["string", "null"]}
_WARNINGS: JsonObject = {"type": "array", "items": _STRING}
_WAREHOUSE = _object({key: _STRING for key in ("warehouseId", "warehouseCode", "warehouseName", "quantity")},
                     ("warehouseId", "warehouseCode", "warehouseName", "quantity"))
_BATCH = _object({key: _STRING for key in ("batchCode", "quantity", "warehouseCode")},
                 ("batchCode", "quantity"))
_MATERIAL = _object({
    **{key: _STRING for key in ("itemId", "itemCode", "itemName", "unitOfMeasure", "planned_required")},
    "remaining_required": _NULLABLE_QUANTITY, "stock_quantity": _NULLABLE_QUANTITY,
    "shortage_quantity": _NULLABLE_QUANTITY, "stock_state": {"enum": ["verified", "unavailable"]},
    "warehouses": {"type": "array", "maxItems": _MAX_WAREHOUSES, "items": _WAREHOUSE},
    "batches": {"type": "array", "maxItems": _MAX_BATCHES, "items": _BATCH}, "warnings": _WARNINGS,
}, ("itemId", "itemCode", "itemName", "unitOfMeasure", "planned_required", "remaining_required",
    "stock_quantity", "shortage_quantity", "stock_state"))
_ORDER = _object({
    **{key: _STRING for key in ("workorderId", "workorderCode", "workorderName", "status", "quantity", "unitOfMeasure")},
    "material_scope": {"enum": ["warehouse_reference", "line_side", "unavailable"]},
    "waitingAssembly": {"type": ["integer", "null"], "minimum": 0},
    "assessment": {"enum": ["reference_sufficient", "reference_shortage", "line_side_sufficient",
                            "line_side_shortage", "no_remaining", "unavailable"]},
    "materials": {"type": "array", "maxItems": _MAX_MATERIALS, "items": _MATERIAL}, "warnings": _WARNINGS,
}, ("workorderId", "workorderCode", "workorderName", "status", "quantity", "unitOfMeasure",
    "material_scope", "waitingAssembly", "assessment", "materials"))
_OUTPUT = _object({
    "kind": {"const": "workorder_materials"}, "filters": _INPUT,
    "orders": {"type": "array", "maxItems": 5, "items": _ORDER},
    "missing_codes": {"type": "array", "items": _STRING, "maxItems": 5},
    "coverage": {"enum": ["complete", "partial"]}, "queried_at": _STRING, "warnings": _WARNINGS,
}, ("kind", "filters", "orders", "missing_codes", "coverage", "queried_at"))


def build_workorder_materials_tool() -> ToolDefinition:
    return ToolDefinition(
        name=NAME, version="1.0", label="查询工单用料需求与备料缺口",
        description=(
            "按准确工单编码查询工单 BOM、待装配用料需求和库存缺口。"
            "草稿仅比较工单BOM总需求与非虚拟仓、未绑定工单的同物料同单位账面库存，"
            "不代表已经预留、领料、保证齐套。已下达工单仅比较冻结BOM剩余装配需求与本工单"
            "合法已领入可耗线边库存，不将全仓库存算为线边物料。"
            "缺失、无权或不完整时注明待核实；无剩余装配需求不等于质量完工。"
            "每张工单独立评估，不作跨工单联合库存承诺，不执行任何库存或工单修改。"
        ),
        input_schema=_INPUT, output_schema=_OUTPUT, required_permissions=PERMISSIONS,
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=55, execute=_execute,
    )


def _invalid(message: str) -> KTGError:
    return KTGError("KTG_INVALID_RESPONSE", message)


def _id(value: object, *, allow_zero: bool = False) -> str:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
    else:
        raise _invalid("MES 业务主键缺失或无效。")
    if not (0 if allow_zero else 1) <= number <= _MAX_ID:
        raise _invalid("MES 业务主键超出有效范围。")
    return str(number)


def _text(value: object, field: str, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid(f"MES 字段 {field} 缺失或无效。")
    return value


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal) or len(str(value)) > 64:
        raise _invalid("业务数量缺失或格式无效。")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise _invalid("业务数量不是有效十进制数。") from exc
    exponent = result.as_tuple().exponent
    if (not result.is_finite() or result < 0 or (positive and result == 0)
            or not -16 <= result.adjusted() <= 17 or not isinstance(exponent, int) or exponent < -16):
        raise _invalid("业务数量为负数、非正需求或超出可核实范围。")
    return result


def _qstr(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _warnings(obj: JsonObject) -> list[JsonValue]:
    return cast(list[JsonValue], obj.setdefault("warnings", []))


def _note(obj: JsonObject, message: str) -> None:
    warnings = _warnings(obj)
    if message not in warnings:
        warnings.append(message)


async def _collect(client: KTGClient, path: str, params: Mapping[str, object], id_field: str,
                   *, order_by: str | None = None) -> list[dict[str, object]]:
    observed: list[dict[str, object]] = []
    ids: set[str] = set()
    expected: int | None = None
    for page in range(1, _MAX_PAGES + 1):
        query = {**params, "pageNum": page, "pageSize": _PAGE_SIZE}
        if order_by is not None:
            query.update({"orderByColumn": order_by, "isAsc": "asc", "reasonable": "false"})
        response = await client.get(path, query)
        total, rows = response.get("total"), response.get("rows")
        if (response.get("code") != 200 or type(total) is not int or total < 0 or not isinstance(rows, list)
                or len(rows) != max(0, min(_PAGE_SIZE, total - (page - 1) * _PAGE_SIZE))):
            raise _invalid("MES 分页记录与可信总数不一致。")
        if expected is None:
            expected = total
        elif total != expected:
            raise _invalid("分页读取期间总数变化，无法核实完整数量。")
        previous_id = int(_id(observed[-1][id_field])) if observed else 0
        for row in rows:
            if not isinstance(row, dict):
                raise _invalid("MES 返回无效记录。")
            row_id = _id(row.get(id_field))
            if row_id in ids or (order_by != "materialStockId" and int(row_id) <= previous_id):
                raise _invalid("MES 分页返回重复或非稳定记录。")
            ids.add(row_id)
            previous_id = int(row_id)
            observed.append(row)
        if len(observed) == expected:
            return observed
    raise KTGError("SOURCE_INCOMPLETE", "有限分页范围未覆盖全部记录，不能将部分数量当作总量。")


@dataclass(slots=True)
class _Workorder:
    row: JsonObject
    version: int | None
    quantity: Decimal


async def _lookup(client: KTGClient, code: str) -> _Workorder | None:
    rows = await _collect(client, _ORDER_PATH, {"workorderCode": code}, "workorderId",
                          order_by="workorderId")
    if not rows:
        return None
    if len(rows) != 1:
        raise _invalid("准确工单编码返回多条记录，无法唯一确定工单。")
    row = rows[0]
    returned = _text(row.get("workorderCode"), "workorderCode", 64)
    if returned.casefold() != code.casefold():
        raise _invalid("MES 工单不符合准确编码查询条件。")
    quantity = _decimal(row.get("quantity"))
    version = row.get("version")
    if version is not None and (type(version) is not int or version < 0):
        raise _invalid("MES 工单版本格式无效。")
    return _Workorder({
        "workorderId": _id(row.get("workorderId")), "workorderCode": returned,
        "workorderName": _text(row.get("workorderName"), "workorderName"),
        "status": _text(row.get("status"), "status", 64), "quantity": _qstr(quantity),
        "unitOfMeasure": _text(row.get("unitOfMeasure"), "unitOfMeasure", 64),
        "material_scope": "unavailable", "waitingAssembly": None, "assessment": "unavailable",
        "materials": [],
    }, version, quantity)


@dataclass(slots=True)
class _Requirement:
    item_id: str
    code: str
    name: str
    unit: str
    planned: Decimal

    def project(self, remaining: Decimal) -> JsonObject:
        return {"itemId": self.item_id, "itemCode": self.code, "itemName": self.name,
                "unitOfMeasure": self.unit, "planned_required": _qstr(self.planned),
                "remaining_required": _qstr(remaining), "stock_quantity": None,
                "shortage_quantity": None, "stock_state": "unavailable"}


def _requirements(rows: list[dict[str, object]], workorder_id: str, *, frozen: bool) -> list[_Requirement]:
    if not rows or len(rows) > _MAX_PAGES * _PAGE_SIZE:
        raise _invalid("工单 BOM 不存在或超出可核实的明细范围。")
    by_id: dict[str, _Requirement] = {}
    line_ids: set[str] = set()
    for row in rows:
        line = _id(row.get("line_id" if frozen else "lineId"))
        if line in line_ids or _id(row.get("workorder_id" if frozen else "workorderId")) != workorder_id:
            raise _invalid("工单 BOM 出现重复明细或跨工单记录。")
        line_ids.add(line)
        item_id = _id(row.get("item_id" if frozen else "itemId"))
        if row.get("item_or_product" if frozen else "itemOrProduct") != "ITEM":
            raise _invalid("工单 BOM 含非原材料项目，不能按当前用料口径核实。")
        code = _text(row.get("item_code" if frozen else "itemCode"), "itemCode", 64)
        name = _text(row.get("item_name" if frozen else "itemName"), "itemName")
        unit = _text(row.get("unit_of_measure" if frozen else "unitOfMeasure"), "unitOfMeasure", 64)
        if unit != unit.strip():
            raise _invalid("物料单位格式无效。")
        quantity = _decimal(row.get("quantity"), positive=True)
        current = by_id.get(item_id)
        if current is None:
            by_id[item_id] = _Requirement(item_id, code, name, unit, quantity)
        else:
            if (current.code, current.name, current.unit) != (code, name, unit):
                raise _invalid("相同物料具有不一致的 BOM 单位或身份。")
            current.planned += quantity
    if len(by_id) > _MAX_MATERIALS:
        raise KTGError("SOURCE_INCOMPLETE", "工单物料数超出本次完整核对范围。")
    return sorted(by_id.values(), key=lambda item: int(item.item_id))


def _verified_stock(material: JsonObject, total: Decimal, remaining: Decimal) -> None:
    material.update({"stock_quantity": _qstr(total), "shortage_quantity": _qstr(max(remaining - total, Decimal(0))),
                     "stock_state": "verified"})


def _breakdowns(material: JsonObject, warehouses: dict[str, tuple[str, str, Decimal]],
                batches: dict[tuple[str, str], Decimal], include_batches: bool, missing_batch: bool = False) -> None:
    if warehouses:
        if len(warehouses) > _MAX_WAREHOUSES:
            _note(material, "分仓明细超出显示范围，省略分仓，不影响已核实库存总量。")
        else:
            material["warehouses"] = [{"warehouseId": warehouse, "warehouseCode": code,
                                       "warehouseName": name, "quantity": _qstr(quantity)}
                                      for warehouse, (code, name, quantity) in sorted(warehouses.items())]
    if include_batches:
        if missing_batch or len(batches) > _MAX_BATCHES:
            _note(material, "批次字段缺失或明细超出显示范围，省略批次，不影响已核实库存总量。")
        else:
            material["batches"] = [{"warehouseCode": warehouse, "batchCode": batch, "quantity": _qstr(quantity)}
                                   for (warehouse, batch), quantity in sorted(batches.items())]


def _draft_stock(rows: list[dict[str, object]], req: _Requirement, output: JsonObject,
                 include_batches: bool) -> None:
    total = Decimal(0)
    count = 0
    warehouses: dict[str, tuple[str, str, Decimal]] = {}
    batches: dict[tuple[str, str], Decimal] = {}
    missing_batch = False
    for row in rows:
        if _id(row.get("itemId")) != req.item_id:
            raise _invalid("库存列表返回其他物料记录。")
        code = _text(row.get("warehouseCode"), "warehouseCode", 64)
        if code.strip().casefold() == _VIRTUAL_WAREHOUSE.casefold():
            _note(output, "已排除虚拟仓库存。")
            continue
        if "workorderId" not in row:
            raise _invalid("库存记录未明确工单归属，无法核实是否为未绑定原仓库存。")
        bound = row["workorderId"]
        if bound is not None and _id(bound, allow_zero=True) != "0":
            _note(output, "已排除工单绑定库存。")
            continue
        bound_code = row.get("workorderCode")
        if bound_code is not None and (not isinstance(bound_code, str) or bound_code.strip()):
            raise _invalid("库存工单编号与未绑定ID不一致，无法核实原仓归属。")
        unit = _text(row.get("unitOfMeasure"), "unitOfMeasure", 64)
        if unit != req.unit:
            _note(output, "已排除单位不一致的记录，不进行单位换算。")
            continue
        quantity = _decimal(row.get("quantityOnhand"))
        warehouse = _id(row.get("warehouseId"))
        name = _text(row.get("warehouseName"), "warehouseName")
        previous = warehouses.get(warehouse)
        if previous is not None and previous[:2] != (code, name):
            raise _invalid("同一仓库的代码或名称不一致，无法核实库存分组。")
        warehouses[warehouse] = (code, name, quantity + (previous[2] if previous else Decimal(0)))
        batch = row.get("batchCode")
        if isinstance(batch, str) and batch.strip() and len(batch) <= 255:
            batches[(code, batch)] = batches.get((code, batch), Decimal(0)) + quantity
        else:
            missing_batch = True
        total += quantity
        count += 1
    if count == 0:
        _note(output, "未查到符合物料、单位、仓库及未绑定工单条件的原仓账面记录，不能当作零库存。")
        return
    _verified_stock(output, total, req.planned)
    _breakdowns(output, warehouses, batches, include_batches, missing_batch)


def _assessment(order: JsonObject, prefix: str) -> None:
    materials = cast(list[JsonObject], order["materials"])
    if any(row["stock_state"] != "verified" for row in materials):
        order["assessment"] = "unavailable"
    elif any(Decimal(cast(str, row["shortage_quantity"])) > 0 for row in materials):
        order["assessment"] = prefix + "_shortage"
    else:
        order["assessment"] = prefix + "_sufficient"


def _explain_error(obj: JsonObject, exc: KTGError) -> None:
    if exc.code == "AUTH_EXPIRED":
        raise exc
    if exc.code == "PERMISSION_DENIED":
        _note(obj, "当前权限无法核实所需数据。")
    elif exc.code in {"KTG_INVALID_RESPONSE", "SOURCE_INCOMPLETE"}:
        _note(obj, str(exc))
    else:
        _note(obj, "相关 MES 数据未能读取或不可访问，不能作库存或缺口结论。")


async def _draft(client: KTGClient, order: _Workorder, include_batches: bool) -> list[_Requirement]:
    if order.quantity <= 0:
        raise _invalid("工单计划量非正，无法核实草稿备料需求。")
    workorder_id = cast(str, order.row["workorderId"])
    rows = await _collect(client, _BOM_PATH, {"workorderId": workorder_id}, "lineId", order_by="lineId")
    requirements = _requirements(rows, workorder_id, frozen=False)
    order.row["material_scope"] = "warehouse_reference"
    _note(order.row, "草稿仅按原仓账面库存比较，不代表已领料、预留或保证齐套。")
    materials = [req.project(req.planned) for req in requirements]
    order.row["materials"] = cast(list[JsonValue], materials)
    for req, result in zip(requirements, materials):
        try:
            stocks = await _collect(client, _STOCK_PATH, {"itemId": req.item_id}, "materialStockId",
                                    order_by="materialStockId")
            _draft_stock(stocks, req, result, include_batches)
        except KTGError as exc:
            _explain_error(result, exc)
    _assessment(order.row, "reference")
    return requirements


def _execution_details(response: dict[str, object], order: _Workorder
                       ) -> tuple[list[_Requirement], list[Decimal], int, str | None]:
    data = response.get("data")
    if response.get("code") != 200 or not isinstance(data, dict):
        raise _invalid("MES 未返回可验证的工单执行详情。")
    master, progress = data.get("order"), data.get("progress")
    if not isinstance(master, dict) or not isinstance(progress, dict):
        raise _invalid("工单无可验证的单件执行冻结数据。")
    workorder_id = cast(str, order.row["workorderId"])
    if (_id(master.get("workorderId")) != workorder_id or _id(progress.get("workorderId")) != workorder_id
            or master.get("workorderCode") != order.row["workorderCode"]
            or master.get("status") != order.row["status"] or progress.get("status") != order.row["status"]
            or order.version is None or type(master.get("version")) is not int
            or master["version"] != order.version):
        raise _invalid("工单列表和执行详情的身份、状态或版本不一致，需重新核实。")
    planned, allocated, waiting = progress.get("planned"), progress.get("allocated"), progress.get("waitingAssembly")
    assembled = progress.get("assembled")
    if (type(planned) is not int or planned <= 0 or type(allocated) is not int or allocated != planned
            or type(waiting) is not int or not 0 <= waiting <= allocated
            or type(assembled) is not int or assembled != allocated - waiting
            or type(master.get("plannedUnits")) is not int or master["plannedUnits"] != planned
            or order.quantity != planned or (order.row["status"] == "FINISHED" and waiting != 0)):
        raise _invalid("执行计划数、分配数或待装配数不一致，不能推算剩余需求。")
    snapshot = master.get("materialRequirementsSnapshot")
    if not isinstance(snapshot, str) or len(snapshot) > 500000:
        raise _invalid("执行冻结 BOM 缺失或超过可核实范围。")
    try:
        raw = json.loads(snapshot, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise _invalid("执行冻结 BOM 无效。") from exc
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise _invalid("执行冻结 BOM 无效。")
    requirements = _requirements(raw, workorder_id, frozen=True)
    remaining: list[Decimal] = []
    for req in requirements:
        # Match ExecutionMaterialRequirements: exact per-unit consumption to two
        # decimal places, never round an unverified order total into a BOM.
        per_unit = (req.planned / Decimal(planned)).quantize(Decimal("0.01"))
        if per_unit * planned != req.planned:
            raise _invalid("冻结 BOM 总需求不能按计划件数精确分摊，不能推算剩余用料。")
        remaining.append(per_unit * waiting)
    if waiting == 0:
        return requirements, remaining, waiting, None
    tasks = progress.get("tasks")
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        return requirements, remaining, waiting, None
    assembly = [_id(task.get("taskId")) for task in tasks
                if task.get("taskKind") == "NORMAL" and task.get("stepKind") == "ASSEMBLY"]
    return requirements, remaining, waiting, assembly[0] if len(assembly) == 1 else None


def _line_stock(rows: list[dict[str, object]], requirements: list[_Requirement],
                remaining: list[Decimal], output: list[JsonObject], include_batches: bool) -> None:
    by_id = {req.item_id: req for req in requirements}
    quantities = {req.item_id: Decimal(0) for req in requirements}
    batches: dict[str, dict[tuple[str, str], Decimal]] = {req.item_id: {} for req in requirements}
    for row in rows:
        item_id = _id(row.get("itemId"))
        if item_id not in by_id or row.get("unitOfMeasure") != by_id[item_id].unit:
            raise _invalid("线边库存返回冻结 BOM 外的物料或不一致单位。")
        _id(row.get("warehouseId"))
        batch = _text(row.get("sourceBatchCode"), "sourceBatchCode")
        quantity = _decimal(row.get("quantityOnhand"), positive=True)
        quantities[item_id] += quantity
        key = (_VIRTUAL_WAREHOUSE, batch)
        groups = batches[item_id]
        groups[key] = groups.get(key, Decimal(0)) + quantity
    for req, need, material in zip(requirements, remaining, output):
        stock = quantities[req.item_id]
        _verified_stock(material, stock, need)
        if stock == 0:
            _note(material, "已完整查询本装配任务的合法可耗批次，未发现该料可耗库存；仅此限定范围为0。")
        _breakdowns(material, {}, batches[req.item_id], include_batches)


async def _released(client: KTGClient, order: _Workorder, include_batches: bool) -> None:
    response = await client.get(_EXECUTION_PATH + cast(str, order.row["workorderId"]))
    requirements, remaining, waiting, task = _execution_details(response, order)
    order.row["material_scope"] = "line_side"
    order.row["waitingAssembly"] = waiting
    materials = [req.project(need) for req, need in zip(requirements, remaining)]
    order.row["materials"] = cast(list[JsonValue], materials)
    if waiting == 0:
        order.row["assessment"] = "no_remaining"
        for material in materials:
            material["shortage_quantity"] = "0"
        _note(order.row, "无剩余装配用料需求，未查线边库存；不据此判断质检、包装、余料退库或工单是否完结。")
        return
    if task is None:
        _note(order.row, "无法唯一核实当前可访问的正常装配任务，线边库存待核实。")
        return
    _note(order.row, "仅比较本工单已领入的合法可耗线边库存，不代表仓库全部余额。")
    try:
        stocks = await _collect(client, f"/mes/pro/execution/tasks/{task}/available-materials", {}, "materialStockId")
        _line_stock(stocks, requirements, remaining, materials, include_batches)
        _assessment(order.row, "line_side")
    except KTGError as exc:
        # The frozen requirements may still be valid when the stock read fails,
        # but the caller must recheck the order version before returning them.
        _explain_error(order.row, exc)


def _invalidate_material_facts(order: _Workorder) -> None:
    order.row.update({"material_scope": "unavailable", "waitingAssembly": None,
                      "assessment": "unavailable", "materials": []})
    order.row.pop("warnings", None)
    _note(order.row, "末尾一致性复核未通过，已撤销本次用料、库存和缺口结论。工单基本信息仅供定位。")


async def _recheck(client: KTGClient, code: str, order: _Workorder,
                   draft_requirements: list[_Requirement] | None) -> None:
    # BOM rows can be edited separately without a parent version increment.
    # Compare their parsed requirements, then finish with an order-header read.
    current_requirements = None
    if draft_requirements is not None:
        workorder_id = cast(str, order.row["workorderId"])
        rows = await _collect(client, _BOM_PATH, {"workorderId": workorder_id}, "lineId", order_by="lineId")
        current_requirements = _requirements(rows, workorder_id, frozen=False)
    latest = await _lookup(client, code)
    if (latest is None or latest.row["workorderId"] != order.row["workorderId"]
            or latest.row["workorderCode"] != order.row["workorderCode"]):
        raise _invalid("工单末尾读取无法确认同一业务对象，需重新查询。")
    if (order.version is None or latest.version is None or latest.version != order.version
            or latest.row["status"] != order.row["status"] or latest.quantity != order.quantity):
        # The latest header is still useful for locating this same order, but
        # no material fact obtained under its earlier state may be shown.
        for key in ("workorderName", "status", "quantity", "unitOfMeasure"):
            order.row[key] = latest.row[key]
        raise _invalid("读取期间工单版本、状态或计划量变化或版本缺失，需重新核实。")
    if draft_requirements is not None and current_requirements != draft_requirements:
        raise _invalid("读取期间草稿 BOM 需求发生变化，需重新核实。")


async def _execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        return ToolResult("KTG 查询客户端不可用。", status=ToolStatus.FAILED, error_code="KTG_CLIENT_MISSING")
    if not _INPUT_VALIDATOR.is_valid(dict(arguments)):
        return ToolResult("工单用料查询参数无效。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
    codes = [code.strip() for code in cast(list[str], arguments["workorder_codes"])]
    if len({code.casefold() for code in codes}) != len(codes):
        return ToolResult("工单编码不可重复。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
    include_batches = arguments.get("include_batches") is True
    orders: list[JsonValue] = []
    missing: list[JsonValue] = []
    warnings: list[JsonValue] = [
        "各工单独立比较；多张草稿共享的原仓账面库存不能相加或联合承诺，不代表预留、已领料或齐套。",
        "工单、BOM和库存为多次读取，不是跨接口一致事务快照；库存可能变化。",
    ]
    partial = False
    with localcontext() as decimal_context:
        decimal_context.prec = 70
        for code in codes:
            selected: _Workorder | None = None
            try:
                async with asyncio.timeout(9):
                    selected = await _lookup(client, code)
                    if selected is None:
                        missing.append(code)
                        partial = True
                        continue
                    orders.append(selected.row)
                    draft_requirements: list[_Requirement] | None = None
                    if selected.row["status"] == "PREPARE":
                        draft_requirements = await _draft(client, selected, include_batches)
                    elif selected.row["status"] in {"CONFIRMED", "FINISHED"}:
                        await _released(client, selected, include_batches)
                    else:
                        _note(selected.row, "该工单状态或流程不适用于本期备料核对，未推算用料。")
                    if selected.row["materials"]:
                        await _recheck(client, code, selected, draft_requirements)
            except TimeoutError:
                partial = True
                if selected is None:
                    warnings.append(f"工单 {code} 查询超时，待核实；不代表工单不存在。")
                else:
                    if selected.row["materials"]:
                        _invalidate_material_facts(selected)
                    _note(selected.row, "工单用料查询超出时间预算，部分数据待核实。")
            except KTGError as exc:
                if exc.code == "AUTH_EXPIRED":
                    return ToolResult("登录状态已过期，请重新登录后核对。", status=ToolStatus.FAILED,
                                      error_code="AUTH_EXPIRED")
                partial = True
                if selected is None:
                    warnings.append(f"工单 {code} 无法核实或不可访问；不代表工单不存在。")
                else:
                    if selected.row["materials"]:
                        _invalidate_material_facts(selected)
                    _explain_error(selected.row, exc)
            if selected is not None and selected.row["assessment"] == "unavailable":
                partial = True
    if missing:
        warnings.append("以下编码未在当前权限下的工单列表查询到匹配记录，仅说明查询未匹配。")
    result: JsonObject = {
        "kind": "workorder_materials",
        "filters": {"workorder_codes": cast(list[JsonValue], codes), "include_batches": include_batches},
        "orders": orders, "missing_codes": missing, "coverage": "partial" if partial else "complete",
        "queried_at": datetime.now(UTC).isoformat(), "warnings": warnings,
    }
    return ToolResult(
        ("工单用料核对仅限已核实事实；待核实处不能判断足够或缺料。" if partial else "工单用料已按上述范围核对。")
        + "草稿为原仓账面参考；已下达为本工单线边可耗库存，二者不可混用。"
        + json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        details=result,
    )
