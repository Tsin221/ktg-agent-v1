"""Execute only a durable, owner-confirmed MES plan, with independently verified receipts."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import cast

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.operations.materials import check_materials, verify_target
from ktg_agent.operations.planner import ACTION_PERMISSIONS, PRODUCT_READ, STOCK_READ, current_permissions, require_permissions, material_guards
from ktg_agent.operations.store import OperationPlanStore, Plan, Step, final_status
from ktg_agent.operations.workorders import (
    as_id, as_quantity, draft_digest, edit_draft, get_order, get_product, release_options, release_request,
    selection_choices, select_release_configuration,
)

DRAFT_OPERATION = "/mes/pro/workorder/operations"
EXECUTION_OPERATION = "/mes/pro/execution/operations"


def _resolved_ids(plan: Plan) -> dict[str, str]:
    return {str(result["workorder_code"]): str(result["workorder_id"])
            for result in plan["results"] if result["status"] == "succeeded" and result.get("workorder_id") is not None}


def _receipt_data(response: Mapping[str, object]) -> Step:
    value = response.get("data")
    if response.get("code") != 200 or not isinstance(value, dict):
        raise ValueError("MES 尚未返回可核实回执。")
    return cast(Step, value)


def _bom_fingerprint(options: Step) -> str:
    value = options.get("bomFingerprint")
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("MES 未返回有效 BOM 核对标识。")
    return value


def _same_draft(actual: Step, expected: Step) -> bool:
    normalized = edit_draft({**actual, "status": "PREPARE"}, {"name": actual.get("workorderName")})
    # Java may populate optional defaults that were not submitted (e.g. parentId=0).
    return all(normalized.get(key) == value for key, value in expected.items() if key not in {"workorderId", "version"})


async def _draft_receipt(client: KTGClient, identity: Mapping[str, object], step: Step, result: Step) -> Step:
    request = result["request"]
    receipt = _receipt_data(await client.get(f"{DRAFT_OPERATION}/{result['operation_id']}"))
    if (receipt.get("operationId") != result["operation_id"] or receipt.get("action") != request["action"]
            or receipt.get("ownerId") != identity.get("userId") or receipt.get("requestDigest") != request["requestDigest"]
            or receipt.get("expectedVersion") != request.get("expectedVersion")):
        raise ValueError("草稿操作回执与原请求不一致。")
    if receipt.get("outcome") == "failed":
        return {"status": "failed", "message": "MES 回执确认操作未成功。", "error_code": receipt.get("errorCode")}
    if receipt.get("outcome") != "succeeded" or not receipt.get("committedAt"):
        raise ValueError("草稿操作回执无法确认完成。")
    order_id = str(as_id(receipt.get("workorderId")))
    code = step["target"]["workorder_code"]
    version = 0 if step["action"] == "create_draft" else step["target"]["expected_version"] + 1
    if (receipt.get("workorderCode") != code or as_id(receipt.get("workorderVersion"), allow_zero=True) != version
            or (step["action"] == "edit_draft" and order_id != str(request["draft"]["workorderId"]))):
        raise ValueError("草稿操作回执工单或版本不符。")
    actual = await get_order(client, code)
    actual_version = as_id(actual.get("version"), allow_zero=True)
    if str(as_id(actual.get("workorderId"))) != order_id or actual_version < version:
        raise ValueError("草稿回执与实际工单不一致。")
    if actual_version == version and (actual.get("status") != "PREPARE" or not _same_draft(actual, step["draft"])):
        raise ValueError("草稿操作回执尚未获实际字段验证。")
    return {"status": "succeeded", "message": "已确认草稿操作生效。" if actual_version == version else
            "MES 回执确认操作已生效，工单随后又发生变化。", "workorder_id": order_id,
            "workorder_code": code, "version": version, "current_status": actual.get("status"),
            "current_version": actual_version, "quantity": step["draft"]["quantity"], "committed_at": receipt["committedAt"]}


async def _release_receipt(client: KTGClient, step: Step, result: Step) -> Step:
    receipt = _receipt_data(await client.get(f"{EXECUTION_OPERATION}/{result['operation_id']}"))
    target = step["target"]
    order_id = result["request_order_id"]
    code, version, count = target["workorder_code"], target["expected_version"] + 1, target["quantity"]
    ref = receipt.get("resultRef")
    if (receipt.get("operationId") != result["operation_id"] or receipt.get("actionType") != "RELEASE"
            or str(as_id(receipt.get("workorderId"))) != order_id or not receipt.get("committedAt") or not isinstance(ref, dict)
            or ref.get("version") != version or ref.get("state") != "CONFIRMED"
            or ref.get("taskCount") != 3 or ref.get("snCount") != count or ref.get("allocated") != count):
        raise ValueError("下达回执与计划工单、版本或分配数量不一致。")
    execution = _receipt_data(await client.get(f"/mes/pro/execution/workorders/{order_id}"))
    order, summary = execution.get("order"), execution.get("summary")
    task_ids = ref.get("taskIds")
    tasks = execution.get("tasks")
    unit_rows = execution.get("units")
    if (not isinstance(order, dict) or str(as_id(order.get("workorderId"))) != order_id or order.get("workorderCode") != code
            or order.get("status") not in {"CONFIRMED", "FINISHED"} or as_id(order.get("version"), allow_zero=True) < version
            or str(as_id(order.get("routeId"))) != step["selection"]["route_id"]
            or order.get("plannedUnits") != count or not isinstance(summary, dict) or summary.get("allocated") != count
            or not isinstance(tasks, list) or not isinstance(task_ids, list) or len(task_ids) != 3
            or not set(map(str, task_ids)) <= {str(row.get("taskId")) for row in tasks if isinstance(row, dict)}
            or not isinstance(unit_rows, list) or len(unit_rows) != count):
        raise ValueError("下达回执尚未获实际执行工单、任务和 SN 验证。")
    return {"status": "succeeded", "message": "已核实下达并生成任务及单件号。", "workorder_id": order_id,
            "workorder_code": code, "version": version, "current_status": order["status"],
            "task_count": 3, "sn_count": count, "quantity": count, "committed_at": receipt["committedAt"]}


async def _verify_receipt(client: KTGClient, identity: Mapping[str, object], step: Step, result: Step) -> Step:
    return await (_release_receipt(client, step, result) if step["action"] == "release"
                  else _draft_receipt(client, identity, step, result))


async def _send(client: KTGClient, identity: Mapping[str, object], step: Step, result: Step) -> Step:
    try:
        response = await client.post_operation(result["request_path"], result["request"])
        code, error = response.get("code"), response.get("errorCode")
        if code in {400, 403, 404, 409, 422} and isinstance(error, str):
            return {"status": "failed", "message": str(response.get("msg") or "MES 拒绝该操作。")[:300], "error_code": error}
    except Exception:
        # Response loss is not proof of rejection. Only query the durable operation ID.
        pass
    try:
        return await _verify_receipt(client, identity, step, result)
    except Exception:
        return {"status": "unknown", "message": "操作结果待核实；不会重复发送原操作。"}


class OperationExecutor:
    def __init__(self, store: OperationPlanStore, enabled: Callable[[], bool] | None = None) -> None:
        self.store = store
        self.enabled = enabled or (lambda: True)

    @staticmethod
    async def _db[T](call: Callable[[], T]) -> T:
        """Finish the SQLite transaction before cancellation cleanup can read it."""
        saving = asyncio.create_task(asyncio.to_thread(call))
        try:
            return await asyncio.shield(saving)
        except asyncio.CancelledError:
            await saving
            raise

    async def _claim(self, owner: str, session_id: str, plan_id: str,
                     call: Callable[[], tuple[Plan, bool]]) -> tuple[Plan, bool]:
        claiming = asyncio.create_task(asyncio.to_thread(call))
        try:
            return await asyncio.shield(claiming)
        except asyncio.CancelledError:
            _, won = await claiming
            # Only the actual winner may interrupt a claimed plan.
            if won:
                await self._db(lambda: self.store.interrupt(owner, session_id, plan_id))
            raise

    async def confirm(self, client: KTGClient, identity: Mapping[str, object], owner: str,
                      session_id: str, plan_id: str, digest: str) -> Plan:
        plan, claimed = await self._claim(owner, session_id, plan_id,
            lambda: self.store.claim(owner, session_id, plan_id, digest))
        if not claimed:
            return plan
        try:
            return await self._execute_claimed(client, identity, owner, session_id, plan_id, plan)
        except BaseException:
            # A response/store interruption may happen after MES committed; recover the
            # durable sent intent without ever running a second executor for this plan.
            try:
                await self._db(lambda: self.store.interrupt(owner, session_id, plan_id))
            except Exception:
                pass  # If SQLite itself is down, startup recovery handles the durable row.
            raise

    async def _execute_claimed(self, client: KTGClient, identity: Mapping[str, object], owner: str,
                               session_id: str, plan_id: str, plan: Plan) -> Plan:
        results: list[Step] = plan["results"]
        by_step: dict[str, Step] = {}
        for step, item in zip(plan["steps"], results, strict=True):
            by_step[step["step_id"]] = item
            if any(by_step[dep]["status"] != "succeeded" for dep in step["depends_on"]):
                item.update(status="skipped", message="依赖步骤没有成功，本操作未发送。")
            else:
                sent_intent = False
                try:
                    if not self.enabled():
                        raise ValueError("工单操作功能已关闭，本步骤未执行。")
                    await self._prepare_request(client, identity, plan, step, item)
                    if item["status"] == "started":
                        if not self.enabled():
                            raise ValueError("工单操作功能已关闭，本步骤未发送。")
                        require_permissions(await current_permissions(client, identity), ACTION_PERMISSIONS[step["action"]])
                        # Save exact resolved request and stable operation ID BEFORE any write is sent.
                        await self._db(lambda: self.store.save_results(owner, session_id, plan_id, results, "executing"))
                        sent_intent = True
                        item.update(await _send(client, identity, step, item))
                except (ValueError, KTGError) as error:
                    item.update(status="unknown" if sent_intent else "failed",
                                message="操作结果待核实。" if sent_intent else str(error)[:300])
                except Exception:
                    item.update(status="unknown" if sent_intent else "failed",
                                message="步骤未能完成；请核对方案或原操作回执。")
            await self._db(lambda: self.store.save_results(owner, session_id, plan_id, results, "executing"))
        return await self._db(lambda: self.store.save_results(owner, session_id, plan_id, results, final_status(results)))

    async def _prepare_request(self, client: KTGClient, identity: Mapping[str, object], plan: Plan,
                               step: Step, item: Step) -> None:
        action = step["action"]
        permissions = await current_permissions(client, identity)
        require_permissions(permissions, ACTION_PERMISSIONS[action])
        resolved = _resolved_ids(plan)
        if action == "check_materials":
            assessment = await check_materials(client, step["targets"], resolved)
            allowed = assessment["assessment"] == "reference_sufficient"
            item.update(status="succeeded" if allowed else "skipped", material_reference=assessment,
                        message="当前原仓账面联合需求可覆盖。" if allowed else "账面需求不足或不可核实，不满足下达条件。")
            return
        target = step["target"]
        if action == "create_draft":
            product = await get_product(client, target["product_code"])
            if (product["itemId"] != target["product_id"] or product["itemName"] != target["product_name"]
                    or product["unitOfMeasure"] != target["product_unit"]):
                raise ValueError("产品配置已变化，须重新准备方案。")
            draft = step["draft"]
            request = {"operationId": item["operation_id"], "action": "CREATE_DRAFT", "draft": draft,
                       "requestDigest": draft_digest("CREATE_DRAFT", draft)}
            item.update(status="started", request_path=DRAFT_OPERATION, request=request)
            return
        actual = await verify_target(client, target, resolved)
        order_id = str(as_id(actual["workorderId"]))
        if action == "edit_draft":
            if not _same_draft(actual, step["before"]):
                raise ValueError("草稿已变化，须重新准备方案。")
            draft = {**step["draft"], "workorderId": as_id(order_id), "version": target["expected_version"]}
            if as_id(draft["productId"]) != as_id(actual["productId"]):
                require_permissions(permissions, PRODUCT_READ)
                after = step.get("after_product")
                if not isinstance(after, dict):
                    raise ValueError("新产品缺少已确认身份。")
                product = await get_product(client, after["code"])
                if (as_id(product["itemId"]) != as_id(draft["productId"]) or product["itemName"] != after["name"]
                        or product["unitOfMeasure"] != after["unit"]):
                    raise ValueError("新产品配置已改变，须重新准备方案。")
            request = {"operationId": item["operation_id"], "action": "EDIT_DRAFT", "draft": draft,
                       "expectedVersion": target["expected_version"], "requestDigest": draft_digest("EDIT_DRAFT", draft)}
            item.update(status="started", request_path=DRAFT_OPERATION, request=request)
            return
        if action != "release":
            raise ValueError("未知业务动作。")
        options = await release_options(client, order_id=order_id)
        selection = select_release_configuration(options, selection_choices(step["selection"]))
        if selection != step["selection"]:
            raise ValueError("已确认的下达配置发生变化，须重新准备。")
        fingerprint = _bom_fingerprint(options)
        if step.get("bom_fingerprint") is not None and step["bom_fingerprint"] != fingerprint:
            raise ValueError("已确认的 BOM 发生变化，须重新准备方案。")
        guards = material_guards(plan["steps"], step["depends_on"])
        group_covered = False
        if guards:
            require_permissions(permissions, STOCK_READ)
            results_by_id = {s["step_id"]: result for s, result in zip(plan["steps"], plan["results"], strict=True)}
            released = {result["workorder_code"]: result for s,result in zip(plan["steps"],plan["results"],strict=True)
                        if s["action"] == "release" and result["status"] == "succeeded"}
            for guard in guards:
                earlier = results_by_id[guard["step_id"]].get("material_reference")
                if not isinstance(earlier, dict) or not isinstance(earlier.get("order_requirements"), dict):
                    raise ValueError("联合备料原核对事实缺失，未下达。")
                assessment = await check_materials(client, guard["targets"], resolved, released=released,
                                                   expected_requirements=earlier["order_requirements"])
                item["material_reference"] = assessment
                if assessment["assessment"] != "reference_sufficient":
                    item.update(status="skipped", message="联合原仓账面条件已不满足或不可核实，未下达。")
                    return
                group_covered = group_covered or any(t == target for t in guard["targets"])
        if step["condition"] == "warehouse_reference_sufficient" and not group_covered:
            require_permissions(permissions, STOCK_READ)
            assessment = await check_materials(client, [target], resolved)
            item["material_reference"] = assessment
            if assessment["assessment"] != "reference_sufficient":
                item.update(status="skipped", message="原仓账面条件不足或不可核实，未下达。")
                return
        if guards or step["condition"] == "warehouse_reference_sufficient":
            latest = await release_options(client, order_id=order_id)
            if _bom_fingerprint(latest) != fingerprint or select_release_configuration(latest, selection_choices(selection)) != selection:
                raise ValueError("备料核对期间下达配置或 BOM 变化，未下达。")
        request = release_request(selection, item["operation_id"], target["expected_version"])
        request["expectedBomFingerprint"] = fingerprint
        item.update(status="started", request_path=f"/mes/pro/execution/workorders/{order_id}/releases",
                    request=request, request_order_id=order_id)

    async def reconcile(self, client: KTGClient, identity: Mapping[str, object], owner: str,
                        session_id: str, plan_id: str) -> Plan:
        plan, claimed = await self._claim(owner, session_id, plan_id,
            lambda: self.store.claim_reconciliation(owner, session_id, plan_id))
        if not claimed:
            return plan
        try:
            return await self._reconcile_claimed(client, identity, owner, session_id, plan_id, plan)
        except BaseException:
            try:
                await self._db(lambda: self.store.interrupt(owner, session_id, plan_id))
            except Exception:
                pass
            raise

    async def _reconcile_claimed(self, client: KTGClient, identity: Mapping[str, object], owner: str,
                                 session_id: str, plan_id: str, plan: Plan) -> Plan:
        results: list[Step] = plan["results"]
        for step, item in zip(plan["steps"], results, strict=True):
            if item["status"] != "unknown" or not item.get("request"):
                continue
            try:
                require_permissions(await current_permissions(client, identity), ACTION_PERMISSIONS[step["action"]])
                item.update(await _verify_receipt(client, identity, step, item))
            except Exception:
                item.update(message="尚未获得一致的原操作回执，仍待核实。")
        return await self._db(lambda: self.store.save_results(owner, session_id, plan_id, results, final_status(results)))
