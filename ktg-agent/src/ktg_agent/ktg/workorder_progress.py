"""Disabled-by-default experimental workorder observation view."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.production_queries import read_production_detail, read_production_list
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)

NAME = "ktg_pro_workorder_progress"
_LABEL = "查询工单生产进度（试验）"
_DESCRIPTION = (
    "查看单张工单的进度试验视图，汇总计划量、已记录产量、算术差与比例、生产任务和近期报工。"
    "数据来自多次读取，业务进度和工序完成规则未确认；默认关闭。"
)
ORACLE_VERSION = "mes-progress-oracle-v1"
PERMISSIONS = (
    "mes:pro:workorder:query", "mes:pro:protask:list", "mes:pro:feedback:list",
)
PAGE_SIZE = 20
MAX_ROWS_PER_SOURCE = 200
MAX_REQUESTS = 23  # 1 + 10 task pages + 10 feedback pages + 1 reread, with one spare
TIME_BUDGET_SECONDS = 12


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fail(code: str, message: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code,
                      details={"available": False, "reason": code})


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise KTGError("KTG_INVALID_RESPONSE", "工单数量无效。") from exc
    if not number.is_finite():
        raise KTGError("KTG_INVALID_RESPONSE", "工单数量无效。")
    return number


def _number(value: Decimal) -> str:
    return format(value, "f")


def _metrics(workorder: Mapping[str, object]) -> JsonObject:
    unit = workorder.get("unitOfMeasure")
    plan = _decimal(workorder.get("quantity"))
    produced = _decimal(workorder.get("quantityProduced"))
    reason = (
        "MISSING_PLAN" if plan is None else
        "MISSING_PRODUCED" if produced is None else
        "MISSING_UNIT" if not isinstance(unit, str) or not unit.strip() else
        "NEGATIVE_QUANTITY" if plan < 0 or produced < 0 else None
    )
    remaining: str | None = None
    ratio: str | None = None
    ratio_reason = reason
    if reason is None:
        assert plan is not None and produced is not None
        remaining = _number(plan - produced)
        if plan == 0:
            ratio_reason = "ZERO_DENOMINATOR"
        else:
            with localcontext() as ctx:
                ctx.prec = 50
                ratio = _number((produced * 100 / plan).quantize(
                    Decimal("0.000000000000000001"), rounding=ROUND_HALF_UP))
    return {
        "workorder_produced_fact": cast(JsonValue, workorder.get("quantityProduced")),
        "remaining_raw": remaining, "remaining_reason": reason,
        "ratio_percent": ratio, "ratio_reason": ratio_reason,
        "unit": unit if isinstance(unit, str) else None,
        "basis": "workorder.quantity and workorder.quantityProduced; arithmetic only, business meaning unconfirmed",
    }


async def read_workorder_progress(client: KTGClient, workorder_id: int,
                                  recent_feedback_limit: int = 5) -> JsonObject:
    if not isinstance(workorder_id, int) or isinstance(workorder_id, bool) or workorder_id <= 0:
        raise KTGError("VALIDATION_ERROR", "工单 ID 必须为正整数。")
    if (not isinstance(recent_feedback_limit, int) or isinstance(recent_feedback_limit, bool)
            or not 0 <= recent_feedback_limit <= 20):
        raise KTGError("VALIDATION_ERROR", "最近报工条数须在 0 至 20 之间。")

    observations: list[JsonObject] = []
    request_count = 0

    async def detail() -> JsonObject:
        nonlocal request_count
        if request_count >= MAX_REQUESTS:
            raise KTGError("SOURCE_INCOMPLETE", "MES 请求预算已耗尽。")
        request_count += 1
        result = await read_production_detail(client, "workorder", workorder_id)
        observations.append({"source": "workorder", "queried_at": result["queried_at"], "page": None})
        return result

    async def pages(domain: str) -> list[JsonObject]:
        nonlocal request_count
        rows: list[JsonObject] = []
        total: int | None = None
        seen: set[int] = set()
        last_id = 0
        page = 1
        id_field = "taskId" if domain == "protask" else "recordId"
        while True:
            if request_count >= MAX_REQUESTS:
                raise KTGError("SOURCE_INCOMPLETE", "MES 请求预算已耗尽。")
            request_count += 1
            result = await read_production_list(client, domain, {
                "workorder_id": workorder_id, "page": page, "page_size": PAGE_SIZE,
            })
            observations.append({"source": domain, "queried_at": result["queried_at"], "page": page})
            current_total = cast(int, result["total"])
            if total is None:
                total = current_total
            if current_total != total or total > MAX_ROWS_PER_SOURCE:
                raise KTGError("SOURCE_INCOMPLETE", "MES 分页总数变化或超出行数预算。")
            batch = cast(list[JsonObject], result["rows"])
            for row in batch:
                row_id = row.get(id_field)
                if (not isinstance(row_id, int) or isinstance(row_id, bool)
                        or row_id <= last_id or row_id in seen
                        or row.get("workorderId") != workorder_id):
                    raise KTGError("SOURCE_INCOMPLETE", "MES 分页无稳定升序、存在重复或跨工单记录。")
                seen.add(row_id)
                last_id = row_id
            rows.extend(batch)
            if len(rows) == total:
                return rows
            if not batch or len(rows) > total or not result["truncated"]:
                raise KTGError("SOURCE_INCOMPLETE", "MES 分页缺失记录。")
            page += 1

    try:
        async with asyncio.timeout(TIME_BUDGET_SECONDS):
            before = await detail()
            tasks = await pages("protask")
            feedback = await pages("feedback")
            after = await detail()
    except TimeoutError as exc:
        raise KTGError("SOURCE_INCOMPLETE", "MES 读取超出时间预算。") from exc
    except KTGError as exc:
        if exc.code in {"PERMISSION_DENIED", "AUTH_EXPIRED", "VALIDATION_ERROR", "KTG_NOT_FOUND"}:
            raise
        raise KTGError("SOURCE_INCOMPLETE", f"MES 来源未完整读取：{exc.code}。") from exc

    first = cast(JsonObject, before["data"])
    last = cast(JsonObject, after["data"])
    if first != last:
        raise KTGError("INCONSISTENT_OBSERVATION", "读取期间工单字段变化。")
    task_ids = {cast(int, row["taskId"]) for row in tasks}
    for row in feedback:
        task_id = row.get("taskId")
        if task_id is not None and task_id not in task_ids:
            raise KTGError("SOURCE_INCOMPLETE", "报工关联任务不在完整任务集合中。")
    times_available = all(isinstance(row.get("feedbackTime"), str)
                          and cast(str, row["feedbackTime"]).strip() for row in feedback)
    recent = (sorted(feedback, key=lambda row: (
        str(row["feedbackTime"]), cast(int, row["recordId"])), reverse=True
    )[:recent_feedback_limit] if times_available else [])
    unit = first.get("unitOfMeasure")
    processes: list[JsonObject] = [
        {"task_id": row["taskId"], "process_id": row.get("processId"),
         "unit": row.get("unitOfMeasure"), "metric": None,
         "reason": "UNIT_MISMATCH" if row.get("unitOfMeasure") not in (None, unit)
                   else "BUSINESS_RULE_UNCONFIRMED", "query_tool": "ktg_pro_task_get"}
        for row in tasks
    ]
    return {
        "kind": "workorder_progress", "workorder": first,
        "source_observations": cast(list[JsonValue], observations),
        "metrics": _metrics(first), "processes": cast(list[JsonValue], processes),
        "recent_feedback": cast(list[JsonValue], recent),
        "completeness": {
            "available": True, "tasks_total": len(tasks), "feedback_total": len(feedback),
            "recent_feedback_truncated": len(recent) < len(feedback),
            "recent_feedback_available": times_available,
            "recent_feedback_reason": None if times_available else "MISSING_FEEDBACK_TIME",
            "mes_requests": request_count, "snapshot_consistent": False,
            "observation_grade": "MULTI_REQUEST_NON_ATOMIC",
            "business_progress_available": False,
            "business_progress_reason": "BUSINESS_RULE_UNCONFIRMED",
            "note": "Separate HTTP reads; matching workorder reread cannot prove task or feedback snapshot consistency.",
        },
        "trace": {"workorder_id": workorder_id,
                  "task_ids": cast(list[JsonValue], sorted(task_ids)),
                  "recent_feedback_ids": [row["recordId"] for row in recent],
                  "query_tools": ["ktg_pro_workorder_get", "ktg_pro_task_get", "ktg_pro_feedback_get", "ktg_pro_feedback_list"]},
        "oracle_version": ORACLE_VERSION, "queried_at": _now(),
    }


def build_workorder_progress_tool() -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        if not isinstance(context.ktg_client, KTGClient):
            return _fail("KTG_CLIENT_MISSING", "KTG 查询客户端未配置。")
        if set(arguments) - {"workorder_id", "recent_feedback_limit"}:
            return _fail("VALIDATION_ERROR", "包含未支持的参数。")
        try:
            details = await read_workorder_progress(
                context.ktg_client, cast(int, arguments.get("workorder_id")),
                cast(int, arguments.get("recent_feedback_limit", 5)),
            )
        except KTGError as exc:
            return _fail(exc.code, str(exc))
        metrics = cast(JsonObject, details["metrics"])
        completeness = cast(JsonObject, details["completeness"])
        summary = (f"工单 {arguments['workorder_id']}：MES 已记录产量字段 {metrics['workorder_produced_fact']} "
                   f"{metrics['unit']}；计划量字段 {cast(JsonObject, details['workorder'])['quantity']}；"
                   f"算术差 {metrics['remaining_raw']}，算术比 {metrics['ratio_percent']}%。"
                   f"业务进度与工序完成规则未确认；近期报工摘要"
                   f"{'可用' if completeness['recent_feedback_available'] else '不可用'}，"
                   f"返回 {len(cast(list[JsonValue], details['recent_feedback']))} 条，"
                   f"总数 {completeness['feedback_total']}。非同一数据库快照。")
        return ToolResult(content=summary, details=details)
    return ToolDefinition(
        name=NAME, version="1.0", label=_LABEL,
        description=_DESCRIPTION,
        input_schema={"type": "object", "properties": {
            "workorder_id": {"type": "integer", "minimum": 1,
                             "description": "生产工单的系统 ID，取自工单列表结果中的 workorderId。"},
            "recent_feedback_limit": {"type": "integer", "minimum": 0, "maximum": 20, "default": 5,
                                      "description": "返回的近期报工条数上限。传 0 省略近期条目，仍统计全部报工记录。"},
        }, "required": ["workorder_id"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {key: {} for key in (
            "kind", "workorder", "source_observations", "metrics", "processes",
            "recent_feedback", "completeness", "queried_at", "trace", "oracle_version")},
            "required": ["kind", "workorder", "source_observations", "metrics", "processes",
                         "recent_feedback", "completeness", "queried_at", "trace", "oracle_version"],
            "additionalProperties": False},
        required_permissions=PERMISSIONS, effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=15, execute=execute,
    )


def workorder_progress_factories() -> dict[str, ToolFactory]:
    return {"production:ktg_pro_workorder_progress": build_workorder_progress_tool}


def workorder_progress_manifests() -> tuple[ToolManifest, ...]:
    return (ToolManifest(
        name=NAME, version="1.0", module="pro", domain="workorder",
        label=_LABEL,
        description=_DESCRIPTION,
        permission_policy=PermissionAll(tuple(PermissionLeaf(p) for p in PERMISSIONS)),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=15,
        expose_to_model=True, confirmation_policy="none", proposal_store_effect=False,
        handler_ref="production:ktg_pro_workorder_progress",
    ),)
