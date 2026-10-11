"""Preview-bound create and edit tools for production workorder drafts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from typing import cast
from uuid import uuid4

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import (
    ExecutionMode,
    RetryPolicy,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolExecute,
    ToolResult,
    ToolStatus,
)


class PreviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _PendingPreview:
    preview_id: str
    user_key: str
    action: str
    request: JsonObject
    preview: JsonObject
    expires_at: float
    state: str = "pending"
    result: ToolResult | None = None


class PreviewStore:
    """Process-local one-time approvals bound to one authenticated KTG user."""

    def __init__(self, *, ttl_seconds: float = 600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("preview ttl must be positive")
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, _PendingPreview] = {}
        self._lock = Lock()

    def create(
        self,
        *,
        user_key: str,
        action: str,
        request: JsonObject,
        preview: JsonObject,
    ) -> str:
        preview_id = str(uuid4())
        with self._lock:
            self._purge_expired()
            self._items[preview_id] = _PendingPreview(
                preview_id=preview_id,
                user_key=user_key,
                action=action,
                request=deepcopy(request),
                preview=deepcopy(preview),
                expires_at=monotonic() + self._ttl_seconds,
            )
        return preview_id

    def begin(
        self, *, preview_id: str, user_key: str, action: str
    ) -> tuple[_PendingPreview, ToolResult | None]:
        with self._lock:
            record = self._items.get(preview_id)
            if record is None or record.expires_at <= monotonic():
                self._items.pop(preview_id, None)
                raise PreviewError("APPROVAL_EXPIRED", "预览已过期，请重新生成。")
            if record.user_key != user_key:
                raise PreviewError(
                    "APPROVAL_IDENTITY_MISMATCH", "该预览不属于当前登录用户。"
                )
            if record.action != action:
                raise PreviewError("APPROVAL_ACTION_MISMATCH", "预览操作类型不匹配。")
            if record.state == "completed":
                return record, record.result
            if record.state == "executing":
                raise PreviewError("APPROVAL_IN_PROGRESS", "该操作正在执行，请勿重复提交。")
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        with self._lock:
            record = self._items.get(preview_id)
            if record is not None:
                record.state = "completed"
                record.result = result

    def action_for(self, *, preview_id: str, user_key: str) -> str:
        with self._lock:
            record = self._items.get(preview_id)
            if record is None or record.expires_at <= monotonic():
                self._items.pop(preview_id, None)
                raise PreviewError("APPROVAL_EXPIRED", "预览已过期，请重新生成。")
            if record.user_key != user_key:
                raise PreviewError(
                    "APPROVAL_IDENTITY_MISMATCH", "该预览不属于当前登录用户。"
                )
            return record.action

    def _purge_expired(self) -> None:
        now = monotonic()
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            del self._items[key]


def build_workorder_tools(store: PreviewStore) -> tuple[ToolDefinition, ...]:
    create_properties = _draft_candidate_properties()
    edit_properties = _draft_candidate_properties()
    edit_properties["target_workorder_code"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
    }
    return (
        _preview_tool(
            name="ktg_preview_create_workorder_draft",
            label="预览创建工单草稿",
            description=(
                "为生产工单草稿生成可信预览。产品名称可能重名；工具会查询 KTG，"
                "无法唯一匹配时必须让用户选择。写入不会在本工具中发生。"
            ),
            properties=create_properties,
            required=("quantity", "request_date"),
            execute=lambda context, arguments: _preview_create(
                store, context, arguments
            ),
        ),
        _preview_tool(
            name="ktg_preview_edit_workorder_draft",
            label="预览编辑工单草稿",
            description=(
                "读取目标工单并预览允许的草稿字段变更。非草稿或版本冲突不能写入。"
            ),
            properties=edit_properties,
            required=("target_workorder_code",),
            execute=lambda context, arguments: _preview_edit(store, context, arguments),
        ),
        _write_tool(
            name="ktg_create_workorder_draft",
            label="创建工单草稿",
            action="create",
            store=store,
        ),
        _write_tool(
            name="ktg_edit_workorder_draft",
            label="编辑工单草稿",
            action="edit",
            store=store,
        ),
    )


def _draft_candidate_properties() -> JsonObject:
    return {
        "product_id": {"type": "integer", "minimum": 1},
        "product_code": {"type": "string", "minLength": 1, "maxLength": 64},
        "product_name": {"type": "string", "minLength": 1, "maxLength": 255},
        "workorder_code": {"type": "string", "minLength": 1, "maxLength": 64},
        "workorder_name": {"type": "string", "minLength": 1, "maxLength": 255},
        "workorder_type": {
            "type": "string",
            "enum": ["SELF", "OUTSOURCE", "PURCHASE"],
        },
        "order_source": {"type": "string", "minLength": 1, "maxLength": 64},
        "source_code": {"type": "string", "maxLength": 64},
        "batch_code": {"type": "string", "maxLength": 64},
        "quantity": {
            "anyOf": [
                {"type": "number", "exclusiveMinimum": 0},
                {"type": "string", "pattern": r"^\d+(?:\.\d+)?$"},
            ]
        },
        "request_date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
        },
        "client_id": {"type": "integer", "minimum": 0},
        "client_code": {"type": "string", "maxLength": 64},
        "client_name": {"type": "string", "maxLength": 255},
        "parent_id": {"type": "integer", "minimum": 0},
        "remark": {"type": "string", "maxLength": 500},
    }


def _preview_tool(
    *,
    name: str,
    label: str,
    description: str,
    properties: JsonObject,
    required: tuple[str, ...],
    execute: object,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1.0",
        description=description,
        label=label,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        output_schema=None,
        required_permissions=(),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=20,
        execute=cast(ToolExecute, execute),
    )


def _write_tool(
    *, name: str, label: str, action: str, store: PreviewStore
) -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _execute_approved(store, action, context, arguments)

    return ToolDefinition(
        name=name,
        version="1.0",
        description="仅执行服务器已绑定且由当前用户确认的一次性工单草稿预览。",
        label=label,
        input_schema={
            "type": "object",
            "properties": {"preview_id": {"type": "string", "minLength": 1}},
            "required": ["preview_id"],
            "additionalProperties": False,
        },
        output_schema=None,
        required_permissions=(
            "mes:pro:workorder:add" if action == "create" else "mes:pro:workorder:edit",
        ),
        effect=ToolEffect.WRITE,
        execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.RECONCILE_FIRST,
        timeout_seconds=40,
        execute=execute,
        expose_to_model=False,
    )


async def _preview_create(
    store: PreviewStore,
    context: ToolContext,
    arguments: Mapping[str, object],
) -> ToolResult:
    client = _client(context)
    if isinstance(client, ToolResult):
        return client
    try:
        product = await _resolve_product(client, arguments)
        quantity = _quantity(arguments.get("quantity"))
        request_date = _request_date(arguments.get("request_date"))
        _validate_note(arguments.get("remark"))
        workorder_code = _optional_text(arguments, "workorder_code")
        if workorder_code is None:
            workorder_code = await client.get_text(
                "/system/autocode/get/WORKORDER_CODE"
            )
        workorder_name = _optional_text(arguments, "workorder_name")
        if workorder_name is None:
            workorder_name = f"{product['itemName']} 工单"
        request = _create_request(
            arguments,
            product=product,
            quantity=quantity,
            request_date=request_date,
            workorder_code=workorder_code,
            workorder_name=workorder_name,
        )
    except _AmbiguousProduct as exc:
        return _failure(str(exc), exc.code, details=exc.details)
    except PreviewError as exc:
        return _failure(str(exc), exc.code)
    except KTGError as exc:
        return _failure(str(exc), exc.code)

    preview: JsonObject = {
        "kind": "workorder_draft_preview",
        "action": "create",
        "product": _product_view(product),
        "quantity": quantity,
        "unitOfMeasure": product.get("unitOfMeasure"),
        "requestDate": request_date,
        "workorderCode": workorder_code,
        "workorderName": workorder_name,
        "changes": [
            {"field": key, "before": None, "after": value}
            for key, value in request.items()
        ],
    }
    preview_id = store.create(
        user_key=_identity_key(context.identity),
        action="create",
        request=request,
        preview=preview,
    )
    preview["preview_id"] = preview_id
    preview["expires_in_seconds"] = 600
    return ToolResult(
        content="已生成工单草稿创建预览，必须由当前用户确认后才能写入。",
        details=preview,
        operation_id=preview_id,
    )


async def _preview_edit(
    store: PreviewStore,
    context: ToolContext,
    arguments: Mapping[str, object],
) -> ToolResult:
    client = _client(context)
    if isinstance(client, ToolResult):
        return client
    target_code = _optional_text(arguments, "target_workorder_code")
    if target_code is None:
        return _failure("请提供目标工单编号。", "VALIDATION_ERROR")
    try:
        current = await _find_workorder(client, target_code)
        if current.get("status") != "PREPARE":
            raise PreviewError("WORKORDER_NOT_DRAFT", "只有草稿状态工单可以编辑。")
        workorder_id = current.get("workorderId")
        if not isinstance(workorder_id, int) or workorder_id <= 0:
            raise PreviewError("KTG_INVALID_RESPONSE", "KTG 未返回有效工单 ID。")
        payload = await client.get(f"/mes/pro/workorder/{workorder_id}")
        raw_detail = payload.get("data")
        if not isinstance(raw_detail, Mapping):
            raise PreviewError("KTG_INVALID_RESPONSE", "KTG 未返回有效工单详情。")
        detail = cast(Mapping[object, object], raw_detail)
        if detail.get("status") != "PREPARE":
            raise PreviewError("WORKORDER_NOT_DRAFT", "只有草稿状态工单可以编辑。")
        request = _request_from_detail(detail)
        before = deepcopy(request)
        await _apply_edit_candidates(client, request, arguments)
        changes = _changes(before, request)
        if not changes:
            raise PreviewError("NO_CHANGES", "没有可应用的草稿字段变更。")
        product = await _product_for_request(client, request)
    except _AmbiguousProduct as exc:
        return _failure(str(exc), exc.code, details=exc.details)
    except PreviewError as exc:
        return _failure(str(exc), exc.code)
    except KTGError as exc:
        return _failure(str(exc), exc.code)

    preview: JsonObject = {
        "kind": "workorder_draft_preview",
        "action": "edit",
        "workorderId": request.get("workorderId"),
        "workorderCode": request.get("workorderCode"),
        "version": request.get("version"),
        "product": _product_view(product),
        "quantity": request.get("quantity"),
        "unitOfMeasure": product.get("unitOfMeasure"),
        "requestDate": request.get("requestDate"),
        "changes": changes,
    }
    preview_id = store.create(
        user_key=_identity_key(context.identity),
        action="edit",
        request=request,
        preview=preview,
    )
    preview["preview_id"] = preview_id
    preview["expires_in_seconds"] = 600
    return ToolResult(
        content="已生成工单草稿编辑预览，必须由当前用户确认后才能写入。",
        details=preview,
        operation_id=preview_id,
    )


async def _execute_approved(
    store: PreviewStore,
    action: str,
    context: ToolContext,
    arguments: Mapping[str, object],
) -> ToolResult:
    preview_id = arguments.get("preview_id")
    if not isinstance(preview_id, str) or not preview_id:
        return _failure("确认参数无效。", "VALIDATION_ERROR")
    if context.operation_id != preview_id:
        return _failure("确认上下文与预览不匹配。", "APPROVAL_CONTEXT_MISMATCH")
    try:
        record, cached = store.begin(
            preview_id=preview_id,
            user_key=_identity_key(context.identity),
            action=action,
        )
    except PreviewError as exc:
        return _failure(str(exc), exc.code)
    if cached is not None:
        return cached
    client = _client(context)
    if isinstance(client, ToolResult):
        store.finish(preview_id, client)
        return client
    try:
        if action == "edit":
            await _assert_current_edit_version(client, record.request)
        try:
            response = (
                await client.post("/mes/pro/workorder", record.request)
                if action == "create"
                else await client.put("/mes/pro/workorder", record.request)
            )
        except (KTGError, TimeoutError) as exc:
            if isinstance(exc, KTGError) and exc.code not in {
                "KTG_CONNECTION_ERROR",
                "KTG_HTTP_ERROR",
            }:
                raise
            result = await _reconcile_after_uncertain_write(
                client, action, record.request, preview_id
            )
        else:
            try:
                workorder_id = (
                    response.get("data")
                    if action == "create"
                    else record.request.get("workorderId")
                )
                if not isinstance(workorder_id, int) or workorder_id <= 0:
                    raise PreviewError(
                        "KTG_INVALID_RESPONSE", "KTG 未返回有效的工单 ID。"
                    )
                result = await _verified_result(
                    client,
                    action,
                    record.request,
                    workorder_id,
                    preview_id,
                    reconciled=False,
                )
            except (KTGError, PreviewError):
                result = _unknown_result(record.request, preview_id, reconciled=False)
    except PreviewError as exc:
        result = _failure(str(exc), exc.code, operation_id=preview_id)
    except KTGError as exc:
        result = _failure(str(exc), exc.code, operation_id=preview_id)
    store.finish(preview_id, result)
    return result


async def _reconcile_after_uncertain_write(
    client: KTGClient,
    action: str,
    request: Mapping[str, object],
    preview_id: str,
) -> ToolResult:
    workorder_id: object
    try:
        if action == "create":
            code = request.get("workorderCode")
            if not isinstance(code, str):
                raise PreviewError("WRITE_RESULT_UNKNOWN", "写入结果待核实。")
            found = await _find_workorder(client, code)
            workorder_id = found.get("workorderId")
        else:
            workorder_id = request.get("workorderId")
        if not isinstance(workorder_id, int) or workorder_id <= 0:
            raise PreviewError("WRITE_RESULT_UNKNOWN", "写入结果待核实。")
        return await _verified_result(
            client,
            action,
            request,
            workorder_id,
            preview_id,
            reconciled=True,
        )
    except (KTGError, PreviewError):
        return _unknown_result(request, preview_id, reconciled=True)


def _unknown_result(
    request: Mapping[str, object], preview_id: str, *, reconciled: bool
) -> ToolResult:
    return ToolResult(
        content="当前无法判定是否写入成功，请人工核对工单，系统不会自动重试。",
        details={
            "kind": "workorder_draft_result",
            "outcome": "unknown",
            "label": "待核实",
            "workorderCode": cast(JsonValue, request.get("workorderCode")),
            "preview_id": preview_id,
        },
        status=ToolStatus.UNKNOWN,
        error_code="WRITE_RESULT_UNKNOWN",
        operation_id=preview_id,
        verification={"reconciled_after_error": reconciled},
    )


async def _verified_result(
    client: KTGClient,
    action: str,
    request: Mapping[str, object],
    workorder_id: int,
    preview_id: str,
    *,
    reconciled: bool,
) -> ToolResult:
    payload = await client.get(f"/mes/pro/workorder/{workorder_id}")
    raw = payload.get("data")
    if not isinstance(raw, Mapping):
        raise PreviewError("WRITE_RESULT_UNKNOWN", "写后无法回读工单，结果待核实。")
    detail = cast(Mapping[object, object], raw)
    if not _matches_request(detail, request):
        raise PreviewError("WRITE_VERIFICATION_FAILED", "写后回读字段不一致，结果待核实。")
    bom_verified = action != "create"
    bom_count: int | None = None
    if action == "create":
        bom = await client.get(
            "/mes/pro/workorderbom/list",
            {"workorderId": workorder_id, "pageNum": 1, "pageSize": 100},
        )
        rows = bom.get("rows")
        if not isinstance(rows, list):
            raise PreviewError("BOM_VERIFICATION_FAILED", "工单已创建，但 BOM 回读失败。")
        bom_verified = True
        bom_count = len(rows)
    details: JsonObject = {
        "kind": "workorder_draft_result",
        "outcome": "succeeded",
        "label": "创建成功" if action == "create" else "修改成功",
        "action": action,
        "preview_id": preview_id,
        "workorderId": workorder_id,
        "workorderCode": cast(JsonValue, detail.get("workorderCode")),
        "status": cast(JsonValue, detail.get("status")),
        "version": cast(JsonValue, detail.get("version")),
        "productName": cast(JsonValue, detail.get("productName")),
        "quantity": cast(JsonValue, detail.get("quantity")),
        "unitOfMeasure": cast(JsonValue, detail.get("unitOfMeasure")),
        "requestDate": cast(JsonValue, detail.get("requestDate")),
        "bom_verified": bom_verified,
        "view_path": f"/mes/pro/workorder?workorderId={workorder_id}",
    }
    if bom_count is not None:
        details["bom_count"] = bom_count
    return ToolResult(
        content=f"工单草稿已{'创建' if action == 'create' else '修改'}并回读核验。",
        details=details,
        operation_id=preview_id,
        verification={
            "reconciled_after_error": reconciled,
            "fields_verified": True,
            "bom_verified": bom_verified,
        },
    )


async def _assert_current_edit_version(
    client: KTGClient, request: Mapping[str, object]
) -> None:
    workorder_id = request.get("workorderId")
    if not isinstance(workorder_id, int):
        raise PreviewError("VALIDATION_ERROR", "编辑预览缺少工单 ID。")
    payload = await client.get(f"/mes/pro/workorder/{workorder_id}")
    detail = payload.get("data")
    if not isinstance(detail, Mapping):
        raise PreviewError("KTG_INVALID_RESPONSE", "KTG 未返回有效工单详情。")
    if detail.get("status") != "PREPARE":
        raise PreviewError("WORKORDER_NOT_DRAFT", "工单已不再是草稿，未执行写入。")
    if detail.get("version") != request.get("version"):
        raise PreviewError("KTG_CONFLICT", "工单版本已变化，请重新预览。")


async def _resolve_product(
    client: KTGClient, arguments: Mapping[str, object]
) -> JsonObject:
    params: dict[str, object] = {"pageNum": 1, "pageSize": 20}
    product_id = arguments.get("product_id")
    product_code = _optional_text(arguments, "product_code")
    product_name = _optional_text(arguments, "product_name")
    if isinstance(product_id, int):
        params["itemId"] = product_id
    elif product_code is not None:
        params["itemCode"] = product_code
    elif product_name is not None:
        params["itemName"] = product_name
    else:
        raise PreviewError("PRODUCT_REQUIRED", "请提供产品编码、名称或 ID。")
    payload = await client.get("/mes/md/mditem/list", params)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise PreviewError("KTG_INVALID_RESPONSE", "KTG 产品查询结果无效。")
    candidates = [cast(Mapping[object, object], row) for row in rows if isinstance(row, Mapping)]
    if isinstance(product_id, int):
        candidates = [row for row in candidates if row.get("itemId") == product_id]
    elif product_code is not None:
        exact = [row for row in candidates if row.get("itemCode") == product_code]
        if exact:
            candidates = exact
    elif product_name is not None:
        exact = [row for row in candidates if row.get("itemName") == product_name]
        if exact:
            candidates = exact
    if not candidates:
        raise PreviewError("PRODUCT_NOT_FOUND", "未找到符合条件的产品。")
    if len(candidates) != 1:
        details: JsonObject = {
            "kind": "workorder_product_candidates",
            "candidates": cast(
                list[JsonValue],
                [_product_view(_json_mapping(candidate)) for candidate in candidates],
            ),
        }
        raise _AmbiguousProduct(details)
    product = candidates[0]
    item_id = product.get("itemId")
    if not isinstance(item_id, int) or item_id <= 0:
        raise PreviewError("KTG_INVALID_RESPONSE", "产品缺少有效 ID。")
    if product.get("enableFlag") == "N":
        raise PreviewError("PRODUCT_DISABLED", "所选产品已停用。")
    return cast(JsonObject, {str(key): cast(JsonValue, value) for key, value in product.items()})


class _AmbiguousProduct(PreviewError):
    def __init__(self, details: JsonObject) -> None:
        super().__init__("AMBIGUOUS_PRODUCT", "找到多个同名产品，请按编码选择。")
        self.details = details


async def _find_workorder(client: KTGClient, code: str) -> JsonObject:
    payload = await client.get(
        "/mes/pro/workorder/list",
        {"workorderCode": code, "pageNum": 1, "pageSize": 20},
    )
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise PreviewError("KTG_INVALID_RESPONSE", "KTG 工单查询结果无效。")
    exact = [row for row in rows if isinstance(row, Mapping) and row.get("workorderCode") == code]
    if not exact:
        raise PreviewError("WORKORDER_NOT_FOUND", "未找到目标工单。")
    if len(exact) != 1:
        raise PreviewError("AMBIGUOUS_WORKORDER", "工单编号未唯一匹配。")
    return cast(JsonObject, dict(exact[0]))


def _create_request(
    arguments: Mapping[str, object],
    *,
    product: Mapping[str, object],
    quantity: str,
    request_date: str,
    workorder_code: str,
    workorder_name: str,
) -> JsonObject:
    request: JsonObject = {
        "workorderCode": workorder_code,
        "workorderName": workorder_name,
        "workorderType": _optional_text(arguments, "workorder_type") or "SELF",
        "orderSource": _optional_text(arguments, "order_source") or "MANUAL",
        "productId": cast(int, product["itemId"]),
        "quantity": quantity,
        "requestDate": request_date,
        "parentId": cast(JsonValue, arguments.get("parent_id", 0)),
    }
    _copy_candidates(arguments, request)
    return request


def _request_from_detail(detail: Mapping[object, object]) -> JsonObject:
    required = {
        "workorderId": detail.get("workorderId"),
        "version": detail.get("version"),
        "workorderCode": detail.get("workorderCode"),
        "workorderName": detail.get("workorderName"),
        "workorderType": detail.get("workorderType") or "SELF",
        "orderSource": detail.get("orderSource") or "MANUAL",
        "productId": detail.get("productId"),
        "quantity": _quantity(detail.get("quantity")),
        "requestDate": _request_date(detail.get("requestDate")),
        "parentId": detail.get("parentId") or 0,
    }
    if not isinstance(required["workorderId"], int) or not isinstance(required["version"], int):
        raise PreviewError("KTG_INVALID_RESPONSE", "工单缺少 ID 或版本。")
    request = cast(JsonObject, required)
    for key in (
        "sourceCode",
        "batchCode",
        "clientId",
        "clientCode",
        "clientName",
        "remark",
    ):
        value = detail.get(key)
        if value is not None:
            request[key] = cast(JsonValue, value)
    return request


async def _apply_edit_candidates(
    client: KTGClient,
    request: JsonObject,
    arguments: Mapping[str, object],
) -> None:
    mapping = {
        "workorder_code": "workorderCode",
        "workorder_name": "workorderName",
        "workorder_type": "workorderType",
        "order_source": "orderSource",
        "source_code": "sourceCode",
        "batch_code": "batchCode",
        "client_id": "clientId",
        "client_code": "clientCode",
        "client_name": "clientName",
        "parent_id": "parentId",
        "remark": "remark",
    }
    for source, target in mapping.items():
        if source in arguments:
            request[target] = cast(JsonValue, arguments[source])
    if "quantity" in arguments:
        request["quantity"] = _quantity(arguments.get("quantity"))
    if "request_date" in arguments:
        request["requestDate"] = _request_date(arguments.get("request_date"))
    _validate_note(arguments.get("remark"))
    if any(key in arguments for key in ("product_id", "product_code", "product_name")):
        product = await _resolve_product(client, arguments)
        request["productId"] = product["itemId"]


async def _product_for_request(
    client: KTGClient, request: Mapping[str, object]
) -> JsonObject:
    product_id = request.get("productId")
    return await _resolve_product(client, {"product_id": product_id})


def _copy_candidates(source: Mapping[str, object], target: JsonObject) -> None:
    mapping = {
        "source_code": "sourceCode",
        "batch_code": "batchCode",
        "client_id": "clientId",
        "client_code": "clientCode",
        "client_name": "clientName",
        "remark": "remark",
    }
    for source_key, target_key in mapping.items():
        value = source.get(source_key)
        if value is not None:
            target[target_key] = cast(JsonValue, value)


def _changes(before: Mapping[str, object], after: Mapping[str, object]) -> list[JsonValue]:
    return [
        {
            "field": key,
            "before": cast(JsonValue, before.get(key)),
            "after": cast(JsonValue, value),
        }
        for key, value in after.items()
        if before.get(key) != value and key not in {"workorderId", "version"}
    ]


def _matches_request(detail: Mapping[object, object], request: Mapping[str, object]) -> bool:
    if detail.get("status") != "PREPARE":
        return False
    for key in (
        "workorderCode",
        "workorderName",
        "workorderType",
        "orderSource",
        "productId",
        "parentId",
    ):
        if detail.get(key) != request.get(key):
            return False
    try:
        if _quantity(detail.get("quantity")) != _quantity(request.get("quantity")):
            return False
        if _request_date(detail.get("requestDate")) != _request_date(request.get("requestDate")):
            return False
    except PreviewError:
        return False
    return True


def _product_view(product: Mapping[str, JsonValue]) -> JsonObject:
    return {
        "itemId": product.get("itemId"),
        "itemCode": product.get("itemCode"),
        "itemName": product.get("itemName"),
        "unitOfMeasure": product.get("unitOfMeasure"),
    }


def _json_mapping(value: Mapping[object, object]) -> JsonObject:
    return {str(key): cast(JsonValue, item) for key, item in value.items()}


def _quantity(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise PreviewError("INVALID_QUANTITY", "生产数量必须大于 0。")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PreviewError("INVALID_QUANTITY", "生产数量必须是有效数字。") from None
    if not number.is_finite() or number <= 0:
        raise PreviewError("INVALID_QUANTITY", "生产数量必须大于 0。")
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _request_date(value: object) -> str:
    if not isinstance(value, str):
        raise PreviewError("INVALID_DATE", "需求日期格式必须为 yyyy-MM-dd。")
    short = value[:10]
    try:
        parsed = date.fromisoformat(short)
    except ValueError:
        raise PreviewError("INVALID_DATE", "需求日期不是有效日期。") from None
    return parsed.isoformat()


def _validate_note(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 500:
        raise PreviewError("INVALID_REMARK", "备注格式无效或超过 500 个字符。")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
        raise PreviewError("INVALID_REMARK", "备注包含不允许的控制字符。")


def _optional_text(arguments: Mapping[str, object], key: str) -> str | None:
    value = arguments.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _identity_key(identity: object) -> str:
    if not isinstance(identity, Mapping):
        raise PreviewError("AUTH_REQUIRED", "无法确认当前用户身份。")
    user_id = identity.get("userId")
    if isinstance(user_id, int) and user_id >= 0:
        return f"id:{user_id}"
    username = identity.get("userName")
    if isinstance(username, str) and username.strip():
        return f"name:{username.strip()}"
    raise PreviewError("AUTH_REQUIRED", "无法确认当前用户身份。")


def _client(context: ToolContext) -> KTGClient | ToolResult:
    if isinstance(context.ktg_client, KTGClient):
        return context.ktg_client
    return _failure("KTG 客户端未配置。", "KTG_CLIENT_MISSING")


def _failure(
    message: str,
    code: str,
    *,
    details: JsonObject | None = None,
    operation_id: str | None = None,
) -> ToolResult:
    return ToolResult(
        content=message,
        details=details,
        status=ToolStatus.FAILED,
        error_code=code,
        operation_id=operation_id,
    )
