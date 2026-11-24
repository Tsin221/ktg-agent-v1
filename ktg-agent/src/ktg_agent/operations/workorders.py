"""D1 workorder action contracts, resolving identities against the existing MES APIs."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.workorder_materials import _collect
from ktg_agent.operations.store import Step

MAX_QUANTITY = 1000
DRAFT_FIELDS = (
    "workorderCode", "workorderName", "workorderType", "orderSource", "sourceCode", "productId",
    "batchCode", "quantity", "clientId", "clientCode", "clientName", "requestDate", "parentId", "remark",
)
INTEGER_FIELDS = {"workorderId", "version", "productId", "clientId", "parentId"}
_CREATE_FIELDS = {"name": "workorderName", "order_source": "orderSource", "source_code": "sourceCode",
                  "batch_code": "batchCode", "remark": "remark", "client_id": "clientId",
                  "client_code": "clientCode", "client_name": "clientName"}


def as_id(value: object, *, allow_zero: bool = False) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
    else:
        raise ValueError("MES 标识格式无效，须使用已核实的配置或业务对象。")
    if not (0 if allow_zero else 1) <= number <= 2**63 - 1:
        raise ValueError("MES 标识超出有效范围。")
    return number


def as_quantity(value: object) -> int:
    if type(value) is bool or not isinstance(value, (int, float, str, Decimal)) or len(str(value)) > 40:
        raise ValueError("生产数量必须是有效正整数。")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("生产数量必须是有效正整数。") from exc
    if not decimal.is_finite() or decimal != decimal.to_integral_value() or not 1 <= decimal <= MAX_QUANTITY:
        raise ValueError(f"生产数量须为 1–{MAX_QUANTITY} 的整数。")
    return int(decimal)


def as_date(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("请明确需求日期（YYYY-MM-DD）。")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("需求日期无效。") from exc


def as_text(value: object, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{label}格式无效。")
    return value


def draft_digest(action: str, draft: Mapping[str, object]) -> str:
    """Same contract-v1 digest as ProWorkorderOperationService.digest."""
    canonical: dict[str, object] = {}
    for key, value in draft.items():
        if value is None:
            continue
        if key == "quantity":
            text = format(Decimal(str(value)), "f")
            canonical[key] = text.rstrip("0").rstrip(".") if "." in text else text
        elif key in INTEGER_FIELDS:
            canonical[key] = as_id(value, allow_zero=key in {"version", "parentId", "clientId"})
        elif isinstance(value, str):
            canonical[key] = value
        else:
            raise ValueError("草稿字段格式无效。")
    wire = json.dumps({"action": action, "draft": canonical}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(wire.encode()).hexdigest()


def build_draft(action: Mapping[str, object], product: Mapping[str, object], *, code: str) -> Step:
    draft: Step = {
        "workorderCode": as_text(code, "工单编号", 64),
        "workorderName": as_text(action.get("name", f"{product['itemName']}生产工单"), "工单名称", 255),
        "workorderType": "SELF", "orderSource": "STORE",
        "productId": as_id(product.get("itemId")), "quantity": as_quantity(action.get("quantity")),
        "requestDate": as_date(action.get("request_date")),
    }
    for key, field in _CREATE_FIELDS.items():
        if key not in action:
            continue
        value = action[key]
        if field == "clientId":
            draft[field] = as_id(value)
        else:
            maximum = 500 if field == "remark" else 255 if field in {"workorderName", "clientName"} else 64
            draft[field] = as_text(value, field, maximum, empty=field not in {"workorderName", "orderSource"})
    if draft["orderSource"] not in {"STORE", "ORDER"}:
        raise ValueError("工单来源须为库存备货或客户订单。")
    if draft["orderSource"] == "ORDER" and not draft.get("sourceCode"):
        raise ValueError("客户订单来源必须明确来源单据。")
    return draft


def edit_draft(original: Mapping[str, object], changes: Mapping[str, object], *,
               product: Mapping[str, object] | None = None) -> Step:
    if original.get("status") != "PREPARE":
        raise ValueError("仅允许修改草稿工单。")
    if not changes or set(changes) - {"name", "quantity", "request_date", "product_code", "remark", "batch_code"}:
        raise ValueError("修改字段无效；仅支持草稿名称、数量、需求日期、产品、备注和批次。")
    result: Step = {field: value for field in DRAFT_FIELDS
                    if (value := original.get(field)) is not None}
    for field in INTEGER_FIELDS & result.keys():
        result[field] = as_id(result[field], allow_zero=field in {"parentId", "clientId"})
    result["quantity"] = as_quantity(result.get("quantity"))
    original_date = result.get("requestDate")
    if isinstance(original_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", original_date):
        original_date = original_date[:10]
    result["requestDate"] = as_date(original_date)
    for field, limit in (("workorderCode", 64), ("workorderName", 255), ("workorderType", 64), ("orderSource", 64)):
        as_text(result.get(field), field, limit)
    if "quantity" in changes:
        result["quantity"] = as_quantity(changes["quantity"])
    if "request_date" in changes:
        result["requestDate"] = as_date(changes["request_date"])
    if "product_code" in changes:
        if product is None or product.get("itemCode") != changes["product_code"]:
            raise ValueError("修改的产品尚未核实。")
        result["productId"] = as_id(product.get("itemId"))
    for key, field, limit in (("name", "workorderName", 255), ("remark", "remark", 500), ("batch_code", "batchCode", 64)):
        if key in changes:
            result[field] = as_text(changes[key], field, limit, empty=(key != "name"))
    return result


def _rows(value: object, label: str) -> list[Step]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"MES未返回有效的{label}。")
    return cast(list[Step], value)


def _select(rows: list[Step], field: str, choice: object, label: str) -> Step:
    if choice is None:
        if len(rows) != 1:
            raise ValueError(f"{label}不唯一或不可用，请先查询可选配置并明确选择。")
        return rows[0]
    target = as_id(choice)
    matches = [row for row in rows if as_id(row.get(field)) == target]
    if len(matches) != 1:
        raise ValueError(f"选定{label}不在MES当前可用配置中。")
    return matches[0]


def select_release_configuration(options: Mapping[str, object], choices: Mapping[str, object]) -> Step:
    """Resolve explicit selections, or use a genuinely unique valid choice; never guess."""
    route = _select(_rows(options.get("routes"), "路线"), "routeId", choices.get("route_id"), "路线")
    steps = _rows(route.get("steps"), "工序")
    if [row.get("executionKind") for row in steps] != ["ASSEMBLY", "INSPECTION", "PACKING"]:
        raise ValueError("路线不符合装配、检验、包装三道工序。")
    requested = choices.get("workstation_ids")
    if requested is not None and (not isinstance(requested, list) or len(requested) != 3):
        raise ValueError("必须按三道工序提供工位。")
    selected: list[Step] = []
    for index, step in enumerate(steps):
        station = _select(_rows(step.get("workstations"), "工位"), "workstationId",
                          requested[index] if isinstance(requested, list) else None, "工位")
        entry: Step = {"route_process_id": str(as_id(step.get("routeProcessId"))),
                       "process_name": as_text(step.get("processName"), "工序名称", 255),
                       "workstation_id": str(as_id(station.get("workstationId"))),
                       "workstation_name": as_text(station.get("workstationName"), "工位名称", 255)}
        if index == 1:
            template = _select(_rows(step.get("templates"), "检验模板"), "templateId",
                               choices.get("template_id"), "检验模板")
            kinds = template.get("qcTypes")
            if not isinstance(kinds, list):
                raise ValueError("检验类型不可核实。")
            valid = [kind for kind in kinds if kind in ("FIRST", "FINAL", "PATROL")]
            choice = choices.get("ipqc_type")
            if choice is None and len(valid) == 1:
                choice = valid[0]
            if choice not in valid:
                raise ValueError("检验类型不唯一或不可用，请明确选择。")
            entry.update(template_id=str(as_id(template.get("templateId"))),
                         template_name=as_text(template.get("templateName"), "检验模板名称", 255), ipqc_type=choice)
        selected.append(entry)
    return {"route_id": str(as_id(route.get("routeId"))),
            "route_name": as_text(route.get("routeName"), "路线名称", 255), "steps": selected}


def selection_choices(selection: Step) -> Step:
    return {"route_id": selection["route_id"],
            "workstation_ids": [step["workstation_id"] for step in selection["steps"]],
            "template_id": selection["steps"][1]["template_id"],
            "ipqc_type": selection["steps"][1]["ipqc_type"]}


def release_request(selection: Step, operation_id: str, version: int) -> Step:
    steps: list[Step] = []
    for step in selection["steps"]:
        row = {"routeProcessId": as_id(step["route_process_id"]), "workstationId": as_id(step["workstation_id"])}
        if "template_id" in step:
            row.update(templateId=as_id(step["template_id"]), ipqcType=step["ipqc_type"])
        steps.append(row)
    return {"operationId": operation_id, "expectedVersion": version,
            "routeId": as_id(selection["route_id"]), "steps": steps}


async def product_search(client: KTGClient, *, code: str | None = None, name: str | None = None) -> list[Step]:
    if (code is None) == (name is None):
        raise ValueError("请提供产品编码或名称。")
    params: dict[str, object] = {"itemOrProduct": "PRODUCT", "enableFlag": "Y",
                                **({"itemCode": code} if code is not None else {"itemName": name})}
    rows = await _collect(client, "/mes/md/mditem/list", params, "itemId", order_by="itemId")
    found = []
    for row in rows:
        if row.get("itemOrProduct") != "PRODUCT" or row.get("enableFlag") != "Y":
            raise ValueError("MES 未返回有效的生产产品。")
        if code is not None and row.get("itemCode") != code:
            continue
        if name is not None and name.casefold() not in str(row.get("itemName", "")).casefold():
            continue
        found.append({"itemId": str(as_id(row.get("itemId"))), "itemCode": as_text(row.get("itemCode"), "产品编码", 64),
                      "itemName": as_text(row.get("itemName"), "产品名称", 255), "unitOfMeasure": row.get("unitOfMeasure")})
    return found


async def get_product(client: KTGClient, code: str) -> Step:
    rows = await product_search(client, code=code)
    if len(rows) != 1:
        raise ValueError("未能唯一确定启用的生产产品，请查询产品编码。")
    return rows[0]


async def get_order(client: KTGClient, code: str) -> Step:
    rows = await _collect(client, "/mes/pro/workorder/list", {"workorderCode": code}, "workorderId", order_by="workorderId")
    matches = [row for row in rows if row.get("workorderCode") == code]
    if len(matches) != 1:
        raise ValueError("未能唯一核实工单编码。")
    order_id = as_id(matches[0].get("workorderId"))
    response = await client.get(f"/mes/pro/workorder/{order_id}")
    data = response.get("data")
    if not isinstance(data, dict) or as_id(data.get("workorderId")) != order_id or data.get("workorderCode") != code:
        raise KTGError("KTG_INVALID_RESPONSE", "工单详情与定位记录不一致。")
    return cast(Step, data)


async def release_options(client: KTGClient, *, product_id: object = None, order_id: object = None) -> Step:
    if (product_id is None) == (order_id is None):
        raise ValueError("下达配置查询须指定唯一对象。")
    scope = f"products/{as_id(product_id)}" if product_id is not None else f"workorders/{as_id(order_id)}"
    response = await client.get(f"/mes/pro/execution/{scope}/release-options")
    data = response.get("data")
    if not isinstance(data, dict):
        raise KTGError("KTG_INVALID_RESPONSE", "MES 未返回下达配置。")
    return cast(Step, data)
