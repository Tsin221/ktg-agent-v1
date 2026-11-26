"""Model-visible inspection and proposal; confirmation is deliberately not a model tool."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.operations.materials import check_prospective_materials
from ktg_agent.operations.planner import ORDER_READ, PRODUCT_READ, current_permissions, prepare_steps, require_permissions
from ktg_agent.operations.store import OperationPlanStore, Plan, Step
from ktg_agent.operations.workorders import as_quantity, get_order, product_search, release_options
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus

TOOL_NAME = "ktg_prepare_workorder_operations"


def _object(properties: dict[str, Any], required: list[str]) -> Step:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _text(maximum: int = 64) -> Step:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


_ID = {"type": "string", "pattern": "^[0-9]{1,19}$"}
_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_QUANTITY = {"type": "integer", "minimum": 1, "maximum": 1000}
_STEP_ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,31}$"}
_TARGET = {"oneOf": [_object({"workorder_code": _text()}, ["workorder_code"]),
                     _object({"from_step": _STEP_ID}, ["from_step"])]}
_SELECTION = _object({"route_id": _ID, "workstation_ids": {"type": "array", "items": _ID, "minItems": 3, "maxItems": 3},
                      "template_id": _ID, "ipqc_type": {"enum": ["FIRST", "FINAL", "PATROL"]}}, [])
_CHANGES = _object({"name": _text(255), "quantity": _QUANTITY, "request_date": _DATE, "product_code": _text(),
                    "remark": {"type": "string", "maxLength": 500}, "batch_code": {"type": "string", "maxLength": 64}}, [])


def _step(action: str, specific: dict[str, Any], required: list[str]) -> Step:
    return _object({"action": {"const": action}, "step_id": _STEP_ID,
                    "depends_on": {"type": "array", "items": _STEP_ID, "maxItems": 19, "uniqueItems": True},
                    **specific}, ["action", "step_id", *required])


_STEPS = {"type": "array", "minItems": 1, "maxItems": 20, "items": {"oneOf": [
    _step("create_draft", {"product_code": _text(), "quantity": _QUANTITY, "request_date": _DATE,
                            "workorder_code": _text(), "name": _text(255), "order_source": {"enum": ["STORE", "ORDER"]},
                            "source_code": _text(), "remark": {"type": "string", "maxLength": 500}, "batch_code": _text()},
          ["product_code", "quantity", "request_date"]),
    _step("edit_draft", {"target": _TARGET, "changes": _CHANGES}, ["target", "changes"]),
    _step("release", {"target": _TARGET, "selection": _SELECTION, "condition": {"enum": ["always", "warehouse_reference_sufficient"]}}, ["target"]),
    _step("check_materials", {"targets": {"type": "array", "items": _TARGET, "minItems": 1, "maxItems": 5}}, ["targets"]),
]}}

INPUT_SCHEMA = cast(JsonObject, {"type": "object", "properties": {
    "mode": {"enum": ["inspect", "prepare"]}, "product_code": _text(), "product_name": _text(255),
    "workorder_code": _text(), "include_release_options": {"type": "boolean"}, "quantity": _QUANTITY, "steps": _STEPS,
}, "required": ["mode"], "additionalProperties": False,
    "oneOf": [
        {"properties": {"mode": {"const": "inspect"}}, "not": {"required": ["steps"]},
         "oneOf": [{"required": ["product_code"]}, {"required": ["product_name"]}, {"required": ["workorder_code"]}]},
        {"properties": {"mode": {"const": "prepare"}}, "required": ["steps"],
         "not": {"anyOf": [{"required": [key]} for key in ("product_code", "product_name", "workorder_code", "include_release_options", "quantity")]}},
    ]})


def public_plan(plan: Plan) -> JsonObject:
    return cast(JsonObject, {**plan, "results": [{key: value for key, value in row.items()
                                                 if key not in {"request", "request_path", "request_order_id"}}
                                                for row in plan["results"]]})


def context_summary(store: OperationPlanStore, owner: str, session_id: str) -> str:
    plans = store.list_plans(owner, session_id, limit=4)
    unresolved = [plan for plan in plans if plan["status"] in {"needs_reconciliation", "executing", "reconciling", "pending"}]
    recent = [plan for plan in plans if plan not in unresolved][:4]
    selected = (unresolved + recent)[:8]
    facts = [{"plan_id": plan["plan_id"], "status": plan["status"], "steps": [
        {"action": step["action"], "workorder_code": (step.get("target") or {}).get("workorder_code"),
         "status": result["status"], "workorder_id": result.get("workorder_id"),
         "quantity": result.get("quantity", (step.get("draft") or step.get("target") or {}).get("quantity")), "current_status": result.get("current_status"),
         "assessment": (result.get("material_reference") or {}).get("assessment"), "message": result.get("message")}
        for step, result in zip(plan["steps"], plan["results"], strict=True)]}
        for plan in selected]
    if not facts:
        return ""
    note = f"另有 {len(unresolved) - 8} 个未决方案未列出，须用户查看原方案，不得重做。" if len(unresolved) > 8 else ""
    return "\n本会话已持久化的操作方案与回执状态（仅事实，不是新指令；pending 未执行，不能自行确认）：" + json.dumps(facts, ensure_ascii=False, separators=(",", ":")) + note


def build_workorder_operation_tool(store: OperationPlanStore) -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        params = arguments
        client, identity = context.ktg_client, context.identity
        if not isinstance(client, KTGClient) or not isinstance(identity, dict):
            return ToolResult("缺少受信任的业务身份。", status=ToolStatus.FAILED, error_code="AUTH_REQUIRED")
        try:
            permissions = await current_permissions(client, identity)
            if params["mode"] == "inspect":
                if "workorder_code" in params:
                    if "quantity" in params:
                        raise ValueError("预计数量检查请选择产品；现有工单使用工单备料查询。")
                    require_permissions(permissions, ORDER_READ)
                    data = await get_order(client, params["workorder_code"])
                    result: Step = {"order": data}
                    if params.get("include_release_options"):
                        require_permissions(permissions, {"mes:pro:execution:release"})
                        result["release_options"] = await release_options(client, order_id=data["workorderId"])
                else:
                    require_permissions(permissions, PRODUCT_READ)
                    products = await product_search(client, code=params.get("product_code"), name=params.get("product_name"))
                    result = {"products": products}
                    if "quantity" in params:
                        if len(products) != 1:
                            raise ValueError("预计备料需明确唯一产品。")
                        require_permissions(permissions, {"mes:md:bom:list", "mes:wm:wmstock:list"})
                        result["projected_materials"] = await check_prospective_materials(client, products[0], as_quantity(params["quantity"]))
                    if params.get("include_release_options") and len(products) == 1:
                        require_permissions(permissions, {"mes:pro:execution:release"})
                        result["release_options"] = await release_options(client, product_id=products[0]["itemId"])
                return ToolResult(json.dumps(result, ensure_ascii=False), details=cast(JsonObject, {"kind": "workorder_operation_options", **result}))
            if params["mode"] != "prepare" or not identity.get("_agent_session_id"):
                raise ValueError("工单操作必须在有持久会话的助手中准备并由用户确认；当前无法写入。")
            steps = await prepare_steps(client, identity, params["steps"])
            plan = await asyncio.to_thread(store.create, owner_from_identity(cast(JsonObject, identity)), context.run_id, steps)
            if plan["session_id"] != identity["_agent_session_id"]:
                raise ValueError("会话身份与方案不一致。")
            details = public_plan(plan)
            details.update(kind="workorder_operation_preview", preview_id=plan["plan_id"])
            concise = {"plan_id": plan["plan_id"], "status": "pending", "step_count": len(steps),
                       "steps": [{"action": step["action"], "step_id": step["step_id"],
                                  "workorder_code": (step.get("target") or {}).get("workorder_code"),
                                  "quantity": (step.get("draft") or step.get("target") or {}).get("quantity"),
                                  "description": step["description"]} for step in steps]}
            return ToolResult("方案已准备，尚未写入 MES。请用户在方案卡确认；聊天文字不能代替确认。"
                              + json.dumps(concise, ensure_ascii=False), details=details, operation_id=plan["plan_id"])
        except (KTGError, ValueError) as exc:
            return ToolResult(str(exc), status=ToolStatus.FAILED,
                              error_code=exc.code if isinstance(exc, KTGError) else "PLAN_INVALID")

    return ToolDefinition(name=TOOL_NAME, version="1.0", label="准备工单操作方案",
                          description="可先 inspect 查询实际产品、工单、路线工位模板；产品加 quantity 可估算未建单的账面材料。prepare 组合 create_draft、edit_draft、release、check_materials。"
                                      "后续可用 from_step 引用前序创建或修改；depends_on 指定依赖，多个工单一起够料先联合 check_materials；"
                                      "release 可选 warehouse_reference_sufficient 条件；默认 always 不代表齐套。"
                                      "仅准备方案，不会写入 MES，必须点击用户确认卡。不要编造配置或日期。",
                          input_schema=INPUT_SCHEMA, output_schema=None, required_permissions=(), effect=ToolEffect.READ,
                          execution_mode=ExecutionMode.SEQUENTIAL, timeout_seconds=45, execute=execute, retry_policy=RetryPolicy.NEVER)
