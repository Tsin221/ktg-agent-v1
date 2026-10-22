"""Five read-only production queries, with reusable validated MES reads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionLeaf
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)


_SPECS = (
    ("ktg_pro_workorder_get", "工单详情", "按 ID 读取一张生产工单。", "workorder", "query", "workorder_id"),
    ("ktg_pro_task_list", "生产任务列表", "按工单 ID 分页读取生产任务，按任务 ID 升序。", "protask", "list", None),
    ("ktg_pro_task_get", "生产任务详情", "按 ID 读取一项生产任务。", "protask", "query", "task_id"),
    ("ktg_pro_feedback_list", "报工列表", "按工单或任务 ID 分页读取报工，按记录 ID 升序；带时区的时间范围包含起点、不包含终点。", "feedback", "list", None),
    ("ktg_pro_feedback_get", "报工详情", "按 ID 读取一条报工。", "feedback", "query", "record_id"),
)
_PATHS = {"workorder": "workorder", "protask": "protask", "feedback": "feedback"}
_IDS = {"workorder": "workorderId", "protask": "taskId", "feedback": "recordId"}
_FIELDS = {
    "workorder": ("workorderId", "workorderCode", "workorderName", "workorderType", "productCode", "productName", "unitOfMeasure", "quantity", "quantityProduced", "quantityChanged", "quantityScheduled", "requestDate", "status", "version", "updateTime"),
    "protask": ("taskId", "taskCode", "taskName", "workorderId", "workorderCode", "processId", "processCode", "processName", "unitOfMeasure", "quantity", "quantityProduced", "quantityChanged", "quantityQuanlify", "quantityUnquanlify", "startTime", "endTime", "status", "updateTime"),
    "feedback": ("recordId", "feedbackType", "workorderId", "workorderCode", "taskId", "taskCode", "processId", "processCode", "processName", "unitOfMeasure", "quantity", "quantityFeedback", "quantityQualified", "quantityUnquanlified", "feedbackTime", "status", "updateTime"),
}
_QUANTITIES = {"quantity", "quantityProduced", "quantityChanged", "quantityScheduled", "quantityQuanlify", "quantityUnquanlify", "quantityFeedback", "quantityQualified", "quantityUnquanlified"}


def _positive() -> JsonObject:
    return {"type": "integer", "minimum": 1}


def _schema(domain: str, list_mode: bool, id_argument: str | None) -> JsonObject:
    properties: JsonObject = {}
    required: list[str] = []
    if id_argument:
        properties[id_argument] = _positive()
        required.append(id_argument)
    else:
        properties["workorder_id"] = _positive()
        if domain == "feedback":
            properties["task_id"] = _positive()
            properties["status"] = {"type": "string", "minLength": 1, "maxLength": 64}
            properties["from_time"] = {"type": "string", "format": "date-time"}
            properties["to_time"] = {"type": "string", "format": "date-time"}
        else:
            required.append("workorder_id")
        properties["page"] = {"type": "integer", "minimum": 1, "default": 1}
        properties["page_size"] = {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}
    return {"type": "object", "properties": properties, "required": cast(list[JsonValue], required), "additionalProperties": False}


def _output_schema(list_mode: bool) -> JsonObject:
    fields = ("kind", "rows", "total", "page", "page_size", "queried_at", "truncated") if list_mode else ("kind", "id", "data", "queried_at")
    return {"type": "object", "properties": {key: {} for key in fields}, "required": list(fields), "additionalProperties": False}


def _definition(spec: tuple[str, str, str, str, str, str | None]) -> ToolDefinition:
    name, label, description, domain, action, id_argument = spec
    list_mode = action == "list"
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        if not isinstance(context.ktg_client, KTGClient):
            return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
        try:
            if list_mode:
                details = await read_production_list(context.ktg_client, domain, arguments)
            else:
                assert id_argument is not None
                details = await read_production_detail(context.ktg_client, domain, arguments[id_argument])
        except KTGError as exc:
            return _failure(str(exc), exc.code)
        if list_mode:
            rows = cast(list[JsonObject], details["rows"])
            summary = f"{label}：本页 {len(rows)} 条，总数 {details['total']}，第 {details['page']} 页；还有后续页：{details['truncated']}。"
            summary += f" 本页事实：{rows[:5]}"
        else:
            summary = f"{label}：{details['data']}"
        return ToolResult(content=summary[:4000], details=details)
    return ToolDefinition(
        name=name, version="1.0", label=label, description=description,
        input_schema=_schema(domain, list_mode, id_argument),
        output_schema=_output_schema(list_mode),
        required_permissions=(f"mes:pro:{domain}:{action}",),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=execute,
    )


def production_query_factories() -> dict[str, ToolFactory]:
    """Static white-list fragment for MES-12; factories capture no user state."""
    def factory_for(spec: tuple[str, str, str, str, str, str | None]) -> ToolFactory:
        def factory() -> ToolDefinition:
            return _definition(spec)
        return factory
    return {f"production:{spec[0]}": factory_for(spec) for spec in _SPECS}


def production_query_manifests() -> tuple[ToolManifest, ...]:
    return tuple(ToolManifest(
        name=name, version="1.0", module="pro", domain=domain,
        label=label, description=description,
        permission_policy=PermissionLeaf(f"mes:pro:{domain}:{action}"),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20,
        expose_to_model=True, confirmation_policy="none", proposal_store_effect=False,
        handler_ref=f"production:{name}",
    ) for name, label, description, domain, action, _ in _SPECS)


def build_production_query_tools() -> tuple[ToolDefinition, ...]:
    return tuple(_definition(spec) for spec in _SPECS)


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code)


def _id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KTGError("VALIDATION_ERROR", "ID 必须是正整数。")
    return value


def _project(domain: str, value: object, expected_id: int | None = None) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "MES 数据不是对象。")
    actual = value.get(_IDS[domain])
    if not isinstance(actual, int) or isinstance(actual, bool) or actual <= 0 or (expected_id is not None and actual != expected_id):
        raise KTGError("KTG_INVALID_RESPONSE", "MES 返回的业务 ID 无效。")
    result: JsonObject = {}
    for key in _FIELDS[domain]:
        raw = value.get(key)
        if key in _QUANTITIES and raw is not None:
            try:
                number = Decimal(str(raw))
            except InvalidOperation as exc:
                raise KTGError("KTG_INVALID_RESPONSE", "MES 数量字段无效。") from exc
            if not number.is_finite() or isinstance(raw, bool):
                raise KTGError("KTG_INVALID_RESPONSE", "MES 数量字段无效。")
            result[key] = format(number, "f")
        elif raw is None or isinstance(raw, str | int | float | bool):
            result[key] = cast(JsonValue, raw)
        else:
            raise KTGError("KTG_INVALID_RESPONSE", "MES 字段格式无效。")
    return result


async def read_production_detail(client: KTGClient, domain: str, business_id: object) -> JsonObject:
    """Shared retrieval for this tool family and the MES-08 progress view."""
    if domain not in _PATHS:
        raise ValueError("unsupported production domain")
    valid_id = _id(business_id)
    payload = await client.get(f"/mes/pro/{_PATHS[domain]}/{valid_id}")
    data = payload.get("data")
    if data is None:
        raise KTGError("KTG_NOT_FOUND", "MES 未找到该记录。", status=404)
    projected = _project(domain, data, valid_id)
    return {"kind": domain, "id": valid_id, "data": projected, "queried_at": datetime.now(UTC).isoformat()}


async def read_production_list(client: KTGClient, domain: str, arguments: Mapping[str, object]) -> JsonObject:
    """Read one server page; total always comes from the server, never this page."""
    if domain not in {"protask", "feedback"}:
        raise ValueError("unsupported production list")
    if any(key not in ({"workorder_id", "page", "page_size"} if domain == "protask" else {"workorder_id", "task_id", "status", "from_time", "to_time", "page", "page_size"}) for key in arguments):
        raise KTGError("VALIDATION_ERROR", "包含未支持的筛选字段。")
    if domain == "protask" and "workorder_id" not in arguments:
        raise KTGError("VALIDATION_ERROR", "需提供工单 ID。")
    if domain == "feedback" and not any(key in arguments for key in ("workorder_id", "task_id")):
        raise KTGError("VALIDATION_ERROR", "需提供工单 ID 或任务 ID。")
    page, size = arguments.get("page", 1), arguments.get("page_size", 10)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1 or not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 20:
        raise KTGError("VALIDATION_ERROR", "分页参数无效。")
    params: dict[str, object] = {"pageNum": page, "pageSize": size}
    bounds: dict[str, datetime] = {}
    for source, target in (("from_time", "fromTime"), ("to_time", "toTime")):
        if source in arguments:
            value = arguments[source]
            try:
                if not isinstance(value, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
                ):
                    raise ValueError("offset timestamp required")
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise KTGError("VALIDATION_ERROR", "时间必须是带时区的 ISO8601 日期时间。") from exc
            bounds[source] = parsed
            params[target] = parsed.isoformat()
    if "from_time" in bounds and "to_time" in bounds and bounds["from_time"] >= bounds["to_time"]:
        raise KTGError("VALIDATION_ERROR", "时间范围终点必须晚于起点。")
    for source, target in (("workorder_id", "workorderId"), ("task_id", "taskId")):
        if source in arguments:
            params[target] = _id(arguments[source])
    status = arguments.get("status")
    if status is not None:
        if domain != "feedback" or not isinstance(status, str) or not 1 <= len(status) <= 64 or not status.strip():
            raise KTGError("VALIDATION_ERROR", "状态筛选无效。")
        params["status"] = status.strip()
    payload = await client.get(f"/mes/pro/{_PATHS[domain]}/list", params)
    raw_rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(raw_rows, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0 or len(raw_rows) > size:
        raise KTGError("KTG_INVALID_RESPONSE", "MES 分页结果格式无效。")
    rows = [_project(domain, row) for row in raw_rows]
    if total < (page - 1) * size + len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "MES 分页总数小于已返回记录数。")
    return {"kind": domain, "rows": cast(list[JsonValue], rows), "total": total, "page": page, "page_size": size,
            "queried_at": datetime.now(UTC).isoformat(), "truncated": page * size < total}
