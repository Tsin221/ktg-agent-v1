"""Model tools for session-owned task snapshots."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

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
仅当用户明确替换未完成步骤所需的物料或仓库范围时，先更新清单，删除被替换的步骤，为新范围保存新 step_id，再执行汇总；保留其他步骤和已经完成的工作。
用户显式选择任务时只更新该任务。续办时以最新保存的清单为依据判断下一步。
对已经尝试过的业务写入，先通过现有查询能力核对实际结果，再决定是否重试。
"""

TASK_EVIDENCE_PROMPT = """
库存证据的 rows 按 columns 顺序给出每条记录，shared 为各行共有的字段，省略的空列按 null 理解。
分仓现存量必须直接使用 ktg_query_stock_summary 成功返回的分组和 queried_at；核对 filters 覆盖所需物料和仓库。不要以明细或其历史汇总代算。
续办时，尚未核完的物料必须在本次运行重查分仓汇总。用户要求当前库存时，重查涉及的各物料；仅在用户明确要求沿用已完成结果时，保留已有可信汇总的原查询时点。若已完成步骤仅有旧明细且最终仍需汇报汇总，应补查可信汇总，并标明实际查询时点。历史助手答复中的数量和总计也不可作为库存依据；每个汇报数量都必须有符合上述条件的分仓汇总工具记录。
历史明细页仅供审计。若需要当前逐条明细，请在本次运行从首页重查，或说明目前只核实了哪些页。同次运行核对明细时按同一 filters 与 page_size 补查缺页；page_verified 仅说明某个请求的页覆盖情况。发现重复或覆盖不全时说明明细尚未核齐，不同运行的明细页不可拼接。
no_records 表示该筛选下无匹配记录，不能等同于数量为零。只在有效分组的数量实际为 0 时报告 0。无可靠汇总时保留库存核对步骤未完成，答复标为待核实或需重试。
awaiting_confirmation 表示仍待网页确认，outcome_unknown 或 confirmation_status_unknown 表示需查询回执或业务状态。
not_started 表示调用未启动。
旧记录的步骤归属可能未知。仅在业务编号和用户当前要求匹配时复用，必要时按权限补查。
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
    return f"id:{user_id}"


_STOCK_TOOLS = {"ktg_query_stock", STOCK_SUMMARY_TOOL}
_EVIDENCE_TOOLS = {"ktg_pro_workorder_get", "ktg_list_workorders", *_STOCK_TOOLS}
_EVIDENCE_LIMIT = 3500
_STOCK_SUMMARY_FIELDS = (
    "materialStockId", "itemCode", "warehouseId", "warehouseCode", "warehouseName",
    "quantityOnhand", "unitOfMeasure",
)
_STOCK_DETAIL_FIELDS = ("itemName", "specification", "locationName", "areaName", "batchCode")
_PREVIEWS = {
    "ktg_preview_create_workorder_draft", "ktg_preview_edit_workorder_draft",
    "ktg_preview_item_receipt", "ktg_preview_production_issue",
}


def _stock_evidence(details: JsonObject, arguments: JsonObject, rows: list[JsonObject]) -> JsonObject:
    item_code, warehouse_id = arguments.get("item_code"), arguments.get("warehouse_id")
    item_code = item_code.strip() if isinstance(item_code, str) else None
    total, page, size = details.get("total"), details.get("page"), details.get("page_size")
    verified = False
    page_count: int | None = None
    covered_rows: JsonValue = None
    if (type(total) is int and total >= 0 and type(page) is int and page >= 1
            and type(size) is int and size >= 1 and item_code and not details.get("truncated")
            and all(row.get("itemCode") in (None, item_code)
                    and (warehouse_id is None or row.get("warehouseId") == warehouse_id) for row in rows)):
        offset = (page - 1) * size
        page_count = (total + size - 1) // size
        verified = len(rows) == max(0, min(size, total - offset))
        if verified and rows:
            covered_rows = [offset + 1, offset + len(rows)]

    complete = verified and page == 1 and len(rows) == total
    evidence: JsonObject = {
        "filters": {"item_code": item_code, "warehouse_id": warehouse_id},
        "total": total, "page": page, "page_size": size, "page_count": page_count,
        "page_verified": verified, "covered_rows": covered_rows,
        "coverage": "complete" if complete else "partial",
        "no_records": complete and not rows, "queried_at": details.get("queried_at"),
    }
    fields = (*_STOCK_SUMMARY_FIELDS, *_STOCK_DETAIL_FIELDS)
    for columns in (fields, _STOCK_SUMMARY_FIELDS):
        shared = {key: rows[0][key] for key in columns if rows and rows[0].get(key) is not None
                  and all(row.get(key) == rows[0][key] for row in rows)}
        varying = [key for key in columns if key not in shared and any(row.get(key) is not None for row in rows)]
        evidence.update({"shared": shared, "columns": list(varying),
                         "rows": [[row.get(key) for key in varying] for row in rows],
                         "details_omitted": columns == _STOCK_SUMMARY_FIELDS})
        if len(json.dumps(evidence, ensure_ascii=False)) <= _EVIDENCE_LIMIT:
            break
    return evidence


def _read_evidence(name: str, details: JsonObject, arguments: JsonObject) -> JsonObject | None:
    rows = details.get("rows")
    if name == "ktg_query_stock" and isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
        # Spilling shortens the text only. The original structured rows can still be verified.
        return _stock_evidence(details, arguments, cast(list[JsonObject], rows))
    if name == "ktg_query_stock":
        return None
    if name == STOCK_SUMMARY_TOOL:
        if details.get("kind") != "stock_summary":
            return None
        return {key: details.get(key) for key in ("filters", "queried_at", "no_records", "groups")}
    if "spill" in details:
        return None
    if name == "ktg_pro_workorder_get":
        data = details.get("data")
        if not isinstance(data, dict):
            return None
        return {"data": data,
                "queried_at": details.get("queried_at"), "coverage": "complete"}
    rows = details.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    fields = (
        "workorderId", "workorderCode", "workorderName", "workorderType", "productCode",
        "productName", "status", "quantity", "quantityProduced", "unitOfMeasure", "requestDate", "version",
    )
    total, page = details.get("total"), details.get("page")
    complete = type(total) is int and total == len(rows) and page == 1 and not details.get("truncated")
    return {
        "rows": [{key: row.get(key) for key in fields} for row in rows if isinstance(row, dict)],
        "total": total, "page": page, "page_size": details.get("page_size"),
        "queried_at": details.get("queried_at"), "coverage": "complete" if complete else "partial",
    }


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
        is_preview = call.name in _PREVIEWS and status == "succeeded"
        is_pending_receipt = call.name == "ktg_wm_operation_result" and end.get("error_code") == "OPERATION_PENDING"
        if (is_preview or is_pending_receipt) and isinstance(operation_id, str):
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

    # A legacy detail call may also represent an explicit row-level request.
    # Migrate its scope only if the original request asked for stock totals.
    original_request = budget.resume_request.get("original_request") if budget.resume_request else None
    legacy_summary = isinstance(original_request, str) and any(
        term in original_request for term in ("分仓库存", "分仓现存量", "库存汇总")
    ) and not any(term in original_request for term in ("批次", "库位", "逐条", "逐行", "分页", "库存明细"))
    stock_required: dict[str, set[tuple[str, int | None]]] = {}
    fresh_stock: dict[str, set[tuple[str, int | None]]] = {}
    original_completed = {step_id for step_id, status in budget.steps if status == "completed"}

    def collect_stock(task_id: str, records: Sequence[JsonObject], *, historical: bool) -> None:
        seen: set[tuple[str, str, int | None]] = set()
        for record in records:
            step_id, arguments, tool = record.get("step_id"), record.get("arguments"), record.get("tool")
            if (record.get("task_id") != task_id or not isinstance(step_id, str)
                    or not isinstance(arguments, dict)):
                continue
            if tool != STOCK_SUMMARY_TOOL and not (historical and legacy_summary and tool == "ktg_query_stock"
                                                  and step_id not in original_completed):
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
            historical_stock = record["run_id"] != run_id and record["tool"] in _STOCK_TOOLS
            if historical_stock and (record["tool"] != STOCK_SUMMARY_TOOL or step_id not in completed):
                old_evidence = record.pop("evidence", None)
                record.pop("result", None)
                if isinstance(old_evidence, dict):
                    for key in ("filters", "queried_at", "page", "page_count", "page_size", "coverage"):
                        if key in old_evidence:
                            record[key] = old_evidence[key]
                record["stock_usage"] = (
                    "new_summary_required_for_reporting"
                    if legacy_summary and step_id in original_completed
                    else "historical_only"
                )
            elif historical_stock:
                record["stock_usage"] = "reuse_only_when_user_explicitly_requests_original_result"
            if record["status"] == "awaiting_confirmation" or (
                record["effect"] == "write" and record["status"] == "outcome_unknown"
            ) or (isinstance(operation_id, str) and (
                record["tool"] in _PREVIEWS or record["tool"] == "ktg_wm_operation_result"
            )):
                rank = 0
            elif record["task_id"] == task_id and isinstance(step_id, str) and step_id not in completed and step_id != current:
                continue
            elif ("evidence" in record or "stock_usage" in record) and record["task_id"] == task_id and step_id == current:
                rank = 1
            elif (("evidence" in record or record.get("stock_usage") == "new_summary_required_for_reporting")
                  and record["task_id"] == task_id and isinstance(step_id, str) and step_id in completed):
                rank = 2
            elif record["run_id"] != run_id or (
                record["tool"] in _STOCK_TOOLS and ("evidence" in record or record["status"] != "succeeded")
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
