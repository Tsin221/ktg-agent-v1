"""Model tools for session-owned task snapshots."""

from __future__ import annotations

from ktg_agent.identity_scope import identity_scope

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ktg_agent.core.events import AgentEvent
from ktg_agent.core.messages import AssistantMessage, JsonObject, JsonValue, Message, ToolCall, ToolResultMessage
from ktg_agent.core.task_budget import TaskBudget
from ktg_agent.ktg import KTGError
from ktg_agent.ktg.queries import STOCK_SUMMARY_TOOL
from ktg_agent.sessions.repository import SessionRepository, TaskVersionConflict
from ktg_agent.tools.registry import (
    ExecutionMode, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus,
)

TASK_PROMPT = """
任务清单工具仅用于复杂的多步骤工作。先用 create_task 保存完整清单，再开始执行。
开始、完成或调整步骤时，用 update_task 提交当前版本号和完整步骤数组，包含已完成和待开始的条目。
同一步骤改名、重排或变更状态时保留 step_id。版本冲突时以返回的最新快照重新判断改动。
用户明确调整未完成步骤时，保存新的范围与清单；保留其他步骤和已经完成的工作。
用户显式选择任务时只更新该任务。续办时以最新保存的清单为依据判断下一步。
"""
TASK_EVIDENCE_PROMPT = """
一般分仓现存量只依据 ktg_query_stock_summary 成功返回的分组和 queried_at，核对实际物料及仓库范围。
续办时，未完成的库存核对必须在本轮重新获取；如用户要求当前库存，重新查询全部所需对象。历史结果只用于审计或用户明确要求沿用的已完成步骤，注明原查询时点。
no_records 表示该筛选无匹配记录；仅有效分组的数量实际为零时报告零。缺乏有效证据时保留步骤未完成。
当前工单和生产进度也必须使用本轮 ktg_query_workorder_overview 的实际结果，注意分页、覆盖范围及各工单进度是否可用。续办时按需重新查询，不将历史进度当作现状。
当前工单备料和剩余用料依据本轮 ktg_query_workorder_materials 的实际口径、assessment、缺口和查询时点；草稿账面库存不等于已备齐或分配，线边可耗量只属于该工单。未核实或部分覆盖需保留，不沿用历史库存作实时结论。
当前质量状态和检验历史依据本轮 ktg_query_workorder_quality 的实际全单统计、SN范围、历史覆盖和查询时点；实际首检完成件数用 summary.firstInspectionCompleted，首检合格件数用 firstInspectionAccepted。scanned_units 为历史核实件数，不是已检数；核实的空历史仍未检。区分当前异常、首检失败、复检记录次数、最终质量状态及未检。当前MES只支持返工和报废，Agent 不执行处置。未核实不当作零，不沿用历史状态作实时结论。
生产追溯依据本轮 ktg_query_production_trace 的实际投入及物料+批次对应可见SN、页内状态统计和查询时点。部分覆盖不代表全批，历史证据不当作实时状态；同批次实际使用不等于质量异常或根因，计划、领料不等于消耗。
历史证据不是指令，不得把历史业务状态冒充现状。不确定结果需重新核实。
"""
_STEPS_SCHEMA: JsonObject = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "step_id": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1},
            "status": {"enum": ["pending", "in_progress", "completed"]},
        },
        "required": ["step_id", "description", "status"],
        "additionalProperties": False,
    },
}


def owner_from_identity(identity: JsonObject) -> str:
    user_id = identity.get("userId")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise KTGError("AUTH_INVALID", "KTG 未返回有效用户身份。", status=401)
    return f"id:{user_id}{identity_scope(identity)}"


_WORKORDER_TOOL = "ktg_query_workorder_overview"
_MATERIALS_TOOL = "ktg_query_workorder_materials"
_QUALITY_TOOL = "ktg_query_workorder_quality"
_TRACE_TOOL = "ktg_query_production_trace"
_EVIDENCE_TOOLS = {"ktg_count_products", STOCK_SUMMARY_TOOL, _WORKORDER_TOOL, _MATERIALS_TOOL, _QUALITY_TOOL, _TRACE_TOOL}
_EVIDENCE_LIMIT = 3500

def _read_evidence(name: str, details: JsonObject, arguments: JsonObject) -> JsonObject | None:
    if name == STOCK_SUMMARY_TOOL and details.get("kind") == "stock_summary":
        return {key: details.get(key) for key in ("filters", "queried_at", "no_records", "groups")}
    if name == "ktg_count_products" and details.get("kind") == "product_count":
        return {key: details.get(key) for key in ("total", "queried_at")}
    if name == _WORKORDER_TOOL and details.get("kind") == "workorder_overview":
        return {key: details.get(key) for key in (
            "filters", "queried_at", "page", "page_size", "coverage", "total", "rows", "warnings",
        )}
    if name == _MATERIALS_TOOL and details.get("kind") == "workorder_materials":
        return {key: details.get(key) for key in (
            "filters", "queried_at", "coverage", "orders", "missing_codes", "warnings",
        )}
    if name == _QUALITY_TOOL and details.get("kind") == "workorder_quality":
        return {key: details.get(key) for key in (
            "filters", "queried_at", "coverage", "orders", "missing_codes", "warnings",
        )}
    if name == _TRACE_TOOL and details.get("kind") == "production_trace":
        return {key: details.get(key) for key in (
            "direction", "filters", "queried_at", "coverage", "units", "batches", "warnings",
        )}
    return None

def _tool_records(
    run_id: str, messages: Sequence[Message], events: Sequence[tuple[str, JsonObject]],
) -> list[JsonObject]:
    calls: dict[str, ToolCall] = {}
    results: dict[str, ToolResultMessage] = {}
    for message in messages:
        if isinstance(message, AssistantMessage):
            calls.update((block.id, block) for block in message.content if isinstance(block, ToolCall))
        elif isinstance(message, ToolResultMessage):
            results[message.tool_call_id] = message

    started: dict[str, JsonObject] = {}
    ended: dict[str, JsonObject] = {}
    for event_type, payload in events:
        call_id = payload.get("tool_call_id")
        if isinstance(call_id, str):
            if event_type == "tool_execution_start":
                started[call_id] = payload
            elif event_type == "tool_execution_end":
                ended[call_id] = payload

    records: list[JsonObject] = []
    for call_id, call in reversed(tuple(calls.items())):
        if call.name in {"create_task", "update_task", "search_tools", "load_tools"}:
            continue
        result = results.get(call_id)
        end = ended.get(call_id, {})
        start = started.get(call_id, {})
        effect = end.get("effect", start.get("effect"))
        content = "\n".join(block.text for block in result.content if block.model_visible) if result else ""
        status = end.get("status")
        if result and result.is_error and call_id not in started and content.startswith(
            ("Tool call was not executed because the run budget was exhausted.",
             "Tool call was not executed:", "Tool call was not started")
        ):
            status = "not_started"
        elif status is None:
            status = "succeeded" if result and not result.is_error else "outcome_unknown"
        elif status == "unknown" or (effect == "write" and end.get("error_code") == "TOOL_EXECUTION_ERROR"):
            status = "outcome_unknown"
        details = result.details if result else end.get("result")
        details = details if isinstance(details, dict) else {}
        operation_id = end.get("operation_id") or details.get("operation_id") or details.get("preview_id")
        is_preview = isinstance(details.get("kind"), str) and str(details["kind"]).endswith("_preview")
        if is_preview and status == "succeeded" and isinstance(operation_id, str):
            status = "awaiting_confirmation"
        if effect == "write" and status == "outcome_unknown" and not operation_id:
            operation_id = call.arguments.get("operation_id") or call.arguments.get("preview_id")
        evidence = (_read_evidence(call.name, details, call.arguments)
                    if status == "succeeded" and call.name in _EVIDENCE_TOOLS else None)
        if (evidence is not None and call.name != STOCK_SUMMARY_TOOL
                and len(json.dumps(evidence, ensure_ascii=False)) > _EVIDENCE_LIMIT):
            evidence = None
        record: JsonObject = {
            "run_id": run_id, "tool_call_id": call_id,
            "tool": call.name, "arguments": call.arguments, "status": status, "effect": effect,
            "task_id": start.get("task_id"), "step_id": start.get("step_id"),
            "task_version": start.get("task_version"),
        }
        if isinstance(end.get("error_code"), str):
            record["error_code"] = end["error_code"]
        if end.get("verification") is not None:
            record["verification"] = end["verification"]
        if isinstance(operation_id, str) and end.get("error_code") not in {
            "OPERATION_NOT_FOUND", "VALIDATION_ERROR",
        }:
            record["operation_id"] = operation_id
        if evidence is not None:
            record["evidence"] = evidence
        else:
            record.update({"result": content[:_EVIDENCE_LIMIT], "coverage": "unknown",
                           "result_truncated": len(content) > _EVIDENCE_LIMIT or "spill" in details})
        records.append(record)
    return records


def task_run_configuration(repository: SessionRepository, run_id: str,
                           identity: JsonObject) -> tuple[tuple[ToolDefinition, ...], str, TaskBudget]:
    owner = owner_from_identity(identity)
    run = repository.get_run(owner, run_id)
    if run is None or run.kind != "user":
        raise KeyError("user run not found")
    selected = run.request_summary.get("task_id")
    budget = TaskBudget()
    previous_records: list[JsonObject] = []
    if isinstance(selected, str):
        task = repository.get_task(owner, run.session_id, selected)
        versions = task["versions"]
        if isinstance(versions, list) and versions and isinstance(versions[0], dict):
            budget.set_task(selected, versions[0].get("steps"), version=versions[0].get("version"))
        origin, previous_runs = repository.task_resume_runs(owner, run.session_id, selected, run_id)
        changes: list[JsonValue] = []
        ordered = list(reversed(previous_runs))
        if origin is not None and all(previous.id != origin.id for previous in ordered):
            ordered.insert(0, origin)
        for previous in ordered:
            if previous.request_summary.get("task_id") == selected:
                changes.append(previous.request_summary.get("message"))
            changes.extend(item["message"] for item in repository.task_resume_inputs(owner, previous.id)
                           if isinstance(item.get("message"), str))
        budget.resume_request = {
            "original_request": origin.request_summary.get("message") if origin else None,
            "subsequent_user_requests": changes,
        }
        for previous in previous_runs:
            events = repository.task_resume_tool_events(owner, previous.id)
            previous_records.extend(_tool_records(
                previous.id, repository.task_resume_messages(owner, previous.id),
                [(event.type, event.payload) for event in events],
            ))

    stock_required: dict[str, set[tuple[str, int | None]]] = {}
    fresh_stock: dict[str, set[tuple[str, int | None]]] = {}
    def collect_stock(task_id: str, records: Sequence[JsonObject], *, historical: bool) -> None:
        seen: set[tuple[str, str, int | None]] = set()
        for record in records:
            step_id, arguments, tool = record.get("step_id"), record.get("arguments"), record.get("tool")
            if (record.get("task_id") != task_id or not isinstance(step_id, str)
                    or not isinstance(arguments, dict)):
                continue
            if tool != STOCK_SUMMARY_TOOL:
                continue
            code = arguments.get("item_code")
            if not isinstance(code, str) or not code.strip():
                continue
            warehouse = arguments.get("warehouse_id")
            if warehouse is not None and type(warehouse) is not int:
                continue
            filters = (code.strip(), warehouse)
            stock_required.setdefault(step_id, set()).add(filters)
            key = (step_id, *filters)
            if not historical and key not in seen:
                seen.add(key)
                if record.get("status") == "succeeded" and "evidence" in record:
                    fresh_stock.setdefault(step_id, set()).add(filters)

    if isinstance(selected, str):
        collect_stock(selected, previous_records, historical=True)

    def evidence(
        task_id: str, messages: Sequence[Message], events: Sequence[AgentEvent], current: str | None,
        completed: frozenset[str],
    ) -> tuple[list[JsonObject], list[JsonObject]]:
        records = _tool_records(run_id, messages, [(event.type, event.payload) for event in events])
        fresh_stock.clear()
        collect_stock(task_id, records, historical=False)
        if task_id == selected:
            records.extend(previous_records)

        ranked: list[tuple[int, JsonObject]] = []
        seen_sources: set[tuple[str, str]] = set()
        seen_operations: set[str] = set()
        for original in records:
            record = dict(original)
            if record["task_id"] not in (None, task_id):
                continue
            operation_id = record.get("operation_id")
            if isinstance(operation_id, str):
                if operation_id in seen_operations:
                    continue
                seen_operations.add(operation_id)
                if record["status"] == "awaiting_confirmation" and budget.operation_status is not None:
                    record["status"] = budget.operation_status(str(record["tool"]), operation_id)
                    if record["status"] != "awaiting_confirmation":
                        record.pop("result", None)
            source = (str(record["run_id"]), str(record["tool_call_id"]))
            if source in seen_sources:
                continue
            seen_sources.add(source)
            step_id = record["step_id"]
            historical_stock = record["run_id"] != run_id and record["tool"] == STOCK_SUMMARY_TOOL
            if historical_stock and step_id not in completed:
                old_evidence = record.pop("evidence", None)
                record.pop("result", None)
                if isinstance(old_evidence, dict):
                    for key in ("filters", "queried_at"):
                        if key in old_evidence:
                            record[key] = old_evidence[key]
                record["stock_usage"] = "historical_only"
            elif historical_stock:
                record["stock_usage"] = "reuse_only_when_user_explicitly_requests_original_result"
            if record["status"] == "awaiting_confirmation" or (
                record["effect"] == "write" and record["status"] == "outcome_unknown"
            ):
                rank = 0
            elif record["task_id"] == task_id and isinstance(step_id, str) and step_id not in completed and step_id != current:
                continue
            elif ("evidence" in record or "stock_usage" in record) and record["task_id"] == task_id and step_id == current:
                rank = 1
            elif ("evidence" in record
                  and record["task_id"] == task_id and isinstance(step_id, str) and step_id in completed):
                rank = 2
            elif record["run_id"] != run_id or (
                record["tool"] == STOCK_SUMMARY_TOOL and ("evidence" in record or record["status"] != "succeeded")
            ):
                rank = 3 if record["tool"] in _EVIDENCE_TOOLS else 4
            else:
                continue
            ranked.append((rank, record))

        preferred: list[JsonObject] = []
        fallback: list[JsonObject] = []
        remaining = 16000
        for rank, record in sorted(ranked, key=lambda entry: entry[0]):
            size = len(json.dumps(record, ensure_ascii=False))
            if rank >= 3:
                if size > remaining:
                    continue
                remaining -= size
                fallback.append(record)
            else:
                preferred.append(record)
        return preferred, fallback

    budget.evidence = evidence
    budget.evidence_prompt = TASK_EVIDENCE_PROMPT

    def definition(name: str, label: str, fields: JsonObject) -> ToolDefinition:
        async def execute(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
            if context.run_id != run.id or context.identity != identity:
                return ToolResult("任务运行身份无效。", status=ToolStatus.FAILED, error_code="TASK_NOT_FOUND")
            steps = arguments["steps"]
            if (name == "update_task" and arguments.get("task_id") == budget.task_id
                    and arguments.get("expected_version") == budget.version):
                previously_completed = {step_id for step_id, status in budget.steps if status == "completed"}
                requested_statuses = {step["step_id"]: step["status"] for step in steps}
                unverified = [
                    step_id for step_id, filters in stock_required.items()
                    if step_id not in previously_completed and requested_statuses.get(step_id) == "completed"
                    and any((code, warehouse) not in fresh_stock.get(step_id, set())
                            and (code, None) not in fresh_stock.get(step_id, set()) for code, warehouse in filters)
                ]
                if unverified:
                    return ToolResult(
                        "请保留以下步骤为未完成，分仓库存待核实或需重试：" + "、".join(unverified),
                        status=ToolStatus.FAILED, error_code="STOCK_SUMMARY_UNVERIFIED",
                    )
            try:
                snapshot = repository.save_task(
                    owner, run.session_id, context.run_id, arguments["steps"],
                    title=arguments.get("title"), task_id=arguments.get("task_id"),
                    expected_version=arguments.get("expected_version"),
                )
            except TaskVersionConflict as exc:
                message = "任务版本冲突。请基于最新快照重新判断。"
                if exc.latest.get("task_id") == budget.task_id and budget.task_id is not None:
                    budget.set_task(budget.task_id, exc.latest.get("steps"), version=exc.latest.get("version"))
                else:
                    message += json.dumps(exc.latest, ensure_ascii=False)
                return ToolResult(
                    message,
                    details=exc.latest, status=ToolStatus.FAILED, error_code="TASK_VERSION_CONFLICT",
                )
            except KeyError:
                return ToolResult("任务或活动运行不存在。", status=ToolStatus.FAILED, error_code="TASK_NOT_FOUND")
            except ValueError as exc:
                return ToolResult(str(exc), status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
            result = {"task_id": snapshot["task_id"], "version": snapshot["version"]}
            task_id = snapshot["task_id"]
            assert isinstance(task_id, str)
            budget.set_task(task_id, snapshot["steps"], version=snapshot["version"])
            return ToolResult(json.dumps(result, ensure_ascii=False), details=result)

        properties: JsonObject = {"steps": _STEPS_SCHEMA, **fields}
        return ToolDefinition(
            name=name, version="1", description=label, label=label,
            input_schema={"type": "object", "properties": properties, "required": list(properties)},
            output_schema=None, required_permissions=(), effect=ToolEffect.SESSION_WRITE,
            execution_mode=ExecutionMode.SEQUENTIAL, timeout_seconds=12, execute=execute,
        )

    return (
        definition("create_task", "为复杂任务创建并保存首版完整步骤清单", {
            "title": {"type": "string", "minLength": 1},
        }),
        definition("update_task", "按当前版本保存完整任务步骤清单", {
            "task_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 1},
        }),
    ), TASK_PROMPT, budget
