"""Prepare validated workorder plans without calling any MES mutation endpoints."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import uuid4

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.operations.store import Step
from ktg_agent.operations.workorders import (
    as_id, as_text, as_quantity, build_draft, edit_draft, get_order, get_product,
    release_options, select_release_configuration,
)

ORDER_READ = {"mes:pro:workorder:list", "mes:pro:workorder:query"}
PRODUCT_READ = {"mes:md:mditem:list"}
STOCK_READ = {"mes:pro:workorderbom:list", "mes:wm:wmstock:list", "mes:pro:execution:query"}
ACTION_PERMISSIONS = {
    "create_draft": ORDER_READ | PRODUCT_READ | {"mes:pro:workorder:add"},
    "edit_draft": ORDER_READ | {"mes:pro:workorder:edit"},
    "release": ORDER_READ | {"mes:pro:execution:release", "mes:pro:execution:query"},
    "check_materials": ORDER_READ | STOCK_READ,
}


async def current_permissions(client: KTGClient, identity: Mapping[str, object]) -> frozenset[str]:
    payload = await client.get("/getInfo")
    user, permissions = payload.get("user"), payload.get("permissions")
    if (not isinstance(user, dict) or type(identity.get("userId")) is not int
            or user.get("userId") != identity.get("userId") or not isinstance(permissions, list)
            or any(not isinstance(permission, str) for permission in permissions)):
        raise KTGError("PERMISSION_UNAVAILABLE", "无法核实当前账号业务权限。", status=403)
    return frozenset(permissions)


def require_permissions(permissions: frozenset[str], required: set[str]) -> None:
    if "*:*:*" not in permissions and not required <= permissions:
        raise KTGError("PERMISSION_DENIED", "当前账号不具备该业务操作或回读权限。", status=403)


def _target_state(state: Step) -> Step:
    return {"workorder_code": state["draft"]["workorderCode"], "workorder_id": state["id"],
            "expected_version": state["version"], "quantity": as_quantity(state["draft"]["quantity"]),
            "product_id": str(as_id(state["draft"]["productId"])), "product_code": state["product_code"],
            "product_name": state["product_name"], "product_unit": state.get("product_unit"),
            "workorder_name": state["draft"]["workorderName"]}


def _generated_code(product_code: str) -> str:
    # Do not call the MES auto-code GET: that endpoint actually advances a sequence.
    clean = re.sub(r"[^A-Za-z0-9-]", "-", product_code)[:24].strip("-") or "PRODUCT"
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    return f"WO-{clean}-{stamp}-{uuid4().hex[:10].upper()}"


def material_guards(steps: list[Step], dependencies: list[str]) -> list[Step]:
    """Find all explicit material prerequisites, even through intermediate steps."""
    by_id = {step["step_id"]: step for step in steps}
    seen: set[str] = set()
    queued = list(dependencies)
    while queued:
        key = queued.pop()
        if key in seen:
            continue
        seen.add(key)
        queued.extend(by_id[key]["depends_on"])
    return [step for step in steps if step["step_id"] in seen and step["action"] == "check_materials"]


async def prepare_steps(client: KTGClient, identity: Mapping[str, object], requests: list[Step]) -> list[Step]:
    if not 1 <= len(requests) <= 20:
        raise ValueError("方案须包含 1–20 项操作。")
    permissions = await current_permissions(client, identity)
    result: list[Step] = []
    states: dict[str, Step] = {}
    origins: dict[str, str] = {}
    seen: set[str] = set()

    async def state_for(target: object) -> Step:
        if not isinstance(target, dict) or set(target) not in ({"workorder_code"}, {"from_step"}):
            raise ValueError("须提供准确工单编号，或引用本方案此前创建/修改步骤。")
        if "from_step" in target:
            ref = target["from_step"]
            if not isinstance(ref, str) or ref not in origins:
                raise ValueError("引用的前序工单步骤不存在。")
            code = origins[ref]
        else:
            code = as_text(target.get("workorder_code"), "工单编号", 64)
        if code not in states:
            order = await get_order(client, code)
            if order.get("status") != "PREPARE":
                raise ValueError(f"工单 {code} 不是草稿，不能修改或下达。")
            # Convert to a full writable DTO, keeping all existing non-null optional fields.
            draft = edit_draft(order, {"name": order.get("workorderName")})
            states[code] = {"draft": draft, "id": str(as_id(order.get("workorderId"))),
                            "version": as_id(order.get("version"), allow_zero=True),
                            "product_code": as_text(order.get("productCode"), "产品编码", 64),
                            "product_name": as_text(order.get("productName"), "产品名称", 255),
                            "product_unit": order.get("unitOfMeasure"),
                            "original_product_id": str(as_id(order.get("productId"))), "last": None,
                            "released": False, "bom_rebuild": False}
        state = states[code]
        if state["released"]:
            raise ValueError(f"方案中工单 {code} 已安排下达，不允许再次修改或下达。")
        return state

    for raw in requests:
        step_id = as_text(raw.get("step_id"), "步骤编号", 32)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", step_id) or step_id in seen:
            raise ValueError("步骤编号须唯一，且仅含字母、数字、下划线或连字符。")
        action = raw.get("action")
        if not isinstance(action, str) or action not in ACTION_PERMISSIONS:
            raise ValueError("当前不支持该业务动作。")
        require_permissions(permissions, ACTION_PERMISSIONS[action])
        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) or dep not in seen for dep in deps):
            raise ValueError("依赖只允许引用此前步骤。")
        dependencies = list(dict.fromkeys(deps))
        step: Step = {"step_id": step_id, "action": action, "depends_on": dependencies}
        if action == "create_draft":
            product = await get_product(client, as_text(raw.get("product_code"), "产品编码", 64))
            code = as_text(raw.get("workorder_code", _generated_code(product["itemCode"])), "工单编号", 64)
            if code in states:
                raise ValueError("同一方案不能重复创建相同工单编号。")
            draft = build_draft(raw, product, code=code)
            state = {"draft": draft, "id": None, "version": 0, "product_code": product["itemCode"],
                     "product_name": product["itemName"], "product_unit": product["unitOfMeasure"], "original_product_id": None, "last": step_id,
                     "released": False, "bom_rebuild": True}
            states[code] = state
            origins[step_id] = code
            step.update(draft=draft, target=_target_state(state), description="创建草稿并生成用料计划")
        elif action == "check_materials":
            targets = raw.get("targets")
            if not isinstance(targets, list) or not 1 <= len(targets) <= 5:
                raise ValueError("联合账面备料检查须指定1–5张工单。")
            resolved: list[Step] = []
            for item in targets:
                state = await state_for(item)
                target = _target_state(state)
                if any(old["workorder_code"] == target["workorder_code"] for old in resolved):
                    raise ValueError("联合备料检查中工单不能重复。")
                resolved.append(target)
                if state["last"] and state["last"] not in dependencies:
                    dependencies.append(state["last"])
            step.update(targets=resolved, description="联合核对原仓账面需求，不预留或领料")
        else:
            state = await state_for(raw.get("target"))
            code = state["draft"]["workorderCode"]
            before = _target_state(state)
            if state["last"] and state["last"] not in dependencies:
                dependencies.append(state["last"])
            step["target"] = before
            if action == "edit_draft":
                changes = raw.get("changes")
                if not isinstance(changes, dict):
                    raise ValueError("必须提供要修改的草稿字段。")
                product = None
                if "product_code" in changes:
                    require_permissions(permissions, PRODUCT_READ)
                    product = await get_product(client, as_text(changes["product_code"], "产品编码", 64))
                draft = edit_draft({**state["draft"], "status": "PREPARE"}, changes, product=product)
                rebuild = draft["quantity"] != state["draft"]["quantity"] or draft["productId"] != state["draft"]["productId"]
                step.update(draft=draft, before=state["draft"], bom_rebuild=rebuild, description="修改草稿工单")
                state["bom_rebuild"] = state["bom_rebuild"] or rebuild
                state["draft"] = draft
                state["version"] += 1
                state["last"] = step_id
                if product:
                    state.update(product_code=product["itemCode"], product_name=product["itemName"], product_unit=product["unitOfMeasure"])
                    step["after_product"] = {"code": product["itemCode"], "name": product["itemName"], "unit": product["unitOfMeasure"]}
                origins[step_id] = code
            else:
                for guard in material_guards(result, dependencies):
                    for checked in guard["targets"]:
                        current = _target_state(states[checked["workorder_code"]])
                        if current != checked:
                            raise ValueError("联合备料检查后又计划修改其中工单，须重新安排备料检查。")
                if state["id"] is not None:
                    # Existing order scope is checked by MES irrespective of the future product choice.
                    options = await release_options(client, order_id=state["id"])
                if state["id"] is None or before["product_id"] != state["original_product_id"]:
                    options = await release_options(client, product_id=before["product_id"])
                selection = raw.get("selection", {})
                if not isinstance(selection, dict):
                    raise ValueError("下达配置格式无效。")
                condition = raw.get("condition", "always")
                if condition not in {"always", "warehouse_reference_sufficient"}:
                    raise ValueError("不支持该下达执行条件。")
                if condition == "warehouse_reference_sufficient":
                    require_permissions(permissions, STOCK_READ)
                step.update(selection=select_release_configuration(options, selection), condition=condition,
                            description="下达工单，生成三道任务及计划数量对应的单件 SN")
                if state["id"] is not None and not state["bom_rebuild"]:
                    fingerprint = options.get("bomFingerprint")
                    if not isinstance(fingerprint, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
                        raise ValueError("MES 未返回可核对的现有草稿 BOM 标识。")
                    step["bom_fingerprint"] = fingerprint
                state["released"] = True
        result.append(step)
        seen.add(step_id)
    if all(step["action"] == "check_materials" for step in result):
        raise ValueError("纯查询请使用已有用料查询能力，不需要操作方案。")
    return result
