"""Joint material condition for confirmed workorder plans (reference only, not a reservation)."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from datetime import UTC, datetime
import json
from typing import cast

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.workorder_materials import _collect, _draft_stock, _requirements, _Requirement, _qstr, _decimal
from ktg_agent.operations.store import Step
from ktg_agent.operations.workorders import as_id, as_quantity, get_order


async def verify_target(client: KTGClient, target: Step, resolved_ids: Mapping[str, str]) -> Step:
    code = target["workorder_code"]
    actual = await get_order(client, code)
    expected_id = target["workorder_id"] or resolved_ids.get(code)
    if expected_id is None or str(as_id(actual.get("workorderId"))) != str(as_id(expected_id)):
        raise ValueError("工单身份与已确认方案不一致。")
    if (actual.get("status") != "PREPARE" or as_id(actual.get("version"), allow_zero=True) != target["expected_version"]
            or as_quantity(actual.get("quantity")) != target["quantity"]
            or str(as_id(actual.get("productId"))) != target["product_id"]
            or actual.get("workorderName") != target["workorder_name"]):
        raise ValueError("工单草稿、版本或业务参数已变化，须重新准备方案。")
    return actual


async def _order_requirements(client: KTGClient, target: Step,
                              resolved_ids: Mapping[str, str], released: Mapping[str, Step]) -> list[_Requirement]:
    code = target["workorder_code"]
    if code in released:
        receipt = released[code]
        order_id = str(as_id(receipt["workorder_id"]))
        response = await client.get(f"/mes/pro/execution/workorders/{order_id}")
        data = response.get("data")
        order = data.get("order") if isinstance(data, dict) else None
        if (not isinstance(order, dict) or str(as_id(order.get("workorderId"))) != order_id
                or order.get("workorderCode") != code or order.get("status") not in {"CONFIRMED", "FINISHED"}
                or as_id(order.get("version"), allow_zero=True) < target["expected_version"] + 1
                or receipt.get("version") != target["expected_version"] + 1 or order.get("plannedUnits") != target["quantity"]):
            raise ValueError("联合检查中已下达工单的原执行快照无法核实。")
        snapshot = order.get("materialRequirementsSnapshot")
        if not isinstance(snapshot, str):
            raise ValueError("已下达工单冻结 BOM 缺失。")
        rows = json.loads(snapshot, parse_float=Decimal)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("已下达工单冻结 BOM 格式无效。")
        return _requirements(rows, order_id, frozen=True)
    order = await verify_target(client, target, resolved_ids)
    order_id = str(as_id(order["workorderId"]))
    rows = await _collect(client, "/mes/pro/workorderbom/list", {"workorderId": order_id},
                          "lineId", order_by="lineId")
    return _requirements(rows, order_id, frozen=False)


async def check_materials(client: KTGClient, targets: list[Step], resolved_ids: Mapping[str, str], *,
                          released: Mapping[str, Step] | None = None, expected_requirements: Step | None = None) -> Step:
    """Check aggregate demand against one stock reading per material, never fictional zero stock."""
    released = released or {}
    aggregate: dict[str, _Requirement] = {}
    before: dict[str, list[_Requirement]] = {}
    for target in targets:
        code = target["workorder_code"]
        if code in before:
            raise ValueError("联合备料工单不可重复。")
        requirements = await _order_requirements(client, target, resolved_ids, released)
        before[code] = requirements
        for req in requirements:
            previous = aggregate.get(req.item_id)
            if previous is not None and (previous.code, previous.name, previous.unit) != (req.code, req.name, req.unit):
                raise ValueError("联合 BOM 中相同物料的身份或单位不一致。")
            aggregate[req.item_id] = _Requirement(req.item_id, req.code, req.name, req.unit,
                                                  req.planned + (previous.planned if previous else Decimal(0)))
    if not aggregate or len(aggregate) > 50:
        raise ValueError("联合用料需求为空或超出完整核对范围。")
    order_requirements = {code: [{"item_id": req.item_id, "code": req.code, "name": req.name, "unit": req.unit,
                                  "quantity": _qstr(req.planned)} for req in requirements]
                          for code, requirements in before.items()}
    if expected_requirements is not None and order_requirements != expected_requirements:
        raise ValueError("联合检查中的工单实际 BOM 已变化，须重新准备原方案。")
    materials: list[Step] = []
    for req in sorted(aggregate.values(), key=lambda item: int(item.item_id)):
        stock = await _collect(client, "/mes/wm/wmstock/list", {"itemId": req.item_id},
                               "materialStockId", order_by="materialStockId")
        output = req.project(req.planned)
        _draft_stock(stock, req, output, False)
        materials.append(output)
    for target in targets:
        latest = await _order_requirements(client, target, resolved_ids, released)
        if latest != before[target["workorder_code"]]:
            raise ValueError("备料检查期间 BOM 发生变化，不能据此下达。")
    verified = all(item["stock_state"] == "verified" for item in materials)
    sufficient = verified and all(Decimal(cast(str, item["shortage_quantity"])) == 0 for item in materials)
    return {"scope": "warehouse_reference", "assessment": "reference_sufficient" if sufficient else
            "reference_shortage" if verified else "unavailable", "workorder_codes": list(before),
            "checked_at": datetime.now(UTC).isoformat(), "materials": materials, "order_requirements": order_requirements,
            "notice": "联合比较未绑定原仓账面库存，不代表预留、领料或保证齐套；库存仍可能变化。"}


async def check_prospective_materials(client: KTGClient, product: Step, quantity: int) -> Step:
    """Project the current product BOM for a proposed quantity, without creating a draft."""
    product_id = str(as_id(product["itemId"]))

    async def requirement_rows() -> list[_Requirement]:
        rows = await _collect(client, "/mes/md/bom/list", {"itemId": product_id}, "bomId", order_by="bomId")
        projected = []
        for row in rows:
            if str(as_id(row.get("itemId"))) != product_id:
                raise ValueError("产品 BOM 返回了其他产品记录。")
            projected.append({"lineId": row.get("bomId"), "workorderId": product_id,
                              "itemId": row.get("bomItemId"), "itemCode": row.get("bomItemCode"),
                              "itemName": row.get("bomItemName"), "unitOfMeasure": row.get("unitOfMeasure"),
                              "itemOrProduct": row.get("itemOrProduct"),
                              "quantity": _qstr(_decimal(row.get("quantity"), positive=True) * Decimal(quantity))})
        return _requirements(projected, product_id, frozen=False)

    requirements = await requirement_rows()
    materials: list[Step] = []
    for requirement in requirements:
        stock = await _collect(client, "/mes/wm/wmstock/list", {"itemId": requirement.item_id},
                               "materialStockId", order_by="materialStockId")
        output = requirement.project(requirement.planned)
        _draft_stock(stock, requirement, output, False)
        materials.append(output)
    if requirements != await requirement_rows():
        raise ValueError("读取期间产品 BOM 变化，不能给出预计备料判断。")
    verified = all(item["stock_state"] == "verified" for item in materials)
    sufficient = verified and all(Decimal(cast(str, item["shortage_quantity"])) == 0 for item in materials)
    return {"scope": "prospective_product_bom_reference", "product_code": product["itemCode"], "quantity": quantity,
            "assessment": "reference_sufficient" if sufficient else "reference_shortage" if verified else "unavailable",
            "materials": materials, "checked_at": datetime.now(UTC).isoformat(),
            "notice": "按当前产品 BOM 估算尚未创建的工单。不是已创建的草稿 BOM、预留或领料；实际草稿生成后需重新核对。"}
