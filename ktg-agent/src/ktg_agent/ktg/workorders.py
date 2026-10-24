"""Preview-bound create and edit tools for production workorder drafts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import sqlite3
from threading import Lock
from time import time
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
    request_digest: str
    expected_version: int | None
    expires_at: float
    state: str = "pending"
    result: ToolResult | None = None


class PreviewStore:
    """SQLite-backed approvals bound to a user and stable MES operation ID."""

    def __init__(self, *, ttl_seconds: float = 600, path: str | Path = ":memory:") -> None:
        if ttl_seconds <= 0:
            raise ValueError("preview ttl must be positive")
        self._ttl_seconds = ttl_seconds
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._memory_db = (sqlite3.connect(":memory:", timeout=10, check_same_thread=False)
                           if self.path == ":memory:" else None)
        if self._memory_db is not None:
            self._memory_db.row_factory = sqlite3.Row
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workorder_previews (
                preview_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, action TEXT NOT NULL,
                request_json TEXT NOT NULL, preview_json TEXT NOT NULL,
                request_digest TEXT NOT NULL, contract_version TEXT NOT NULL,
                workorder_id INTEGER, expected_version INTEGER,
                expires_at REAL NOT NULL, state TEXT NOT NULL, result_json TEXT
            )""")
            # An interrupted confirmation is safe to resume because every attempt
            # reuses preview_id as MES operationId and first reads its receipt.
            db.execute("UPDATE workorder_previews SET state='pending' WHERE state='executing'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = self._memory_db or sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            if self._memory_db is None:
                db.close()

    @staticmethod
    def _encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _result_json(result: ToolResult | None) -> str | None:
        if result is None:
            return None
        return PreviewStore._encode({
            "content": result.content, "details": result.details,
            "status": result.status.value, "error_code": result.error_code,
            "operation_id": result.operation_id, "verification": result.verification,
        })

    @staticmethod
    def _result(value: str | None) -> ToolResult | None:
        if value is None:
            return None
        row = json.loads(value)
        return ToolResult(
            content=row["content"], details=row["details"],
            status=ToolStatus(row["status"]), error_code=row["error_code"],
            operation_id=row["operation_id"], verification=row["verification"],
        )

    def _record(self, row: sqlite3.Row) -> _PendingPreview:
        if row["contract_version"] != "contract-v1":
            raise PreviewError("APPROVAL_CONTRACT_UNSUPPORTED", "提案契约已变化，请重新预览。")
        request = cast(JsonObject, json.loads(row["request_json"]))
        action = row["action"]
        action_code = "CREATE_DRAFT" if action == "create" else "EDIT_DRAFT"
        expected_version = request.get("version") if action == "edit" else None
        if (KTGClient.workorder_operation_digest(action_code, request) != row["request_digest"]
                or request.get("workorderId") != row["workorder_id"]
                or expected_version != row["expected_version"]):
            raise PreviewError("APPROVAL_CHANGED", "提案内容已变化，请重新预览。")
        return _PendingPreview(
            preview_id=row["preview_id"], user_key=row["user_key"], action=action,
            request=request, preview=cast(JsonObject, json.loads(row["preview_json"])),
            request_digest=row["request_digest"], expected_version=row["expected_version"],
            expires_at=row["expires_at"], state=row["state"],
            result=self._result(row["result_json"]),
        )

    def create(self, *, user_key: str, action: str, request: JsonObject,
               preview: JsonObject) -> str:
        preview_id = str(uuid4())
        action_code = "CREATE_DRAFT" if action == "create" else "EDIT_DRAFT"
        digest = KTGClient.workorder_operation_digest(action_code, request)
        workorder_id = request.get("workorderId")
        expected_version = request.get("version") if action == "edit" else None
        with self._lock, self._connect() as db:
            now = time()
            # Expired approvals may be pruned, but completed operation receipts
            # remain addressable so callers can recover the durable outcome.
            db.execute(
                "DELETE FROM workorder_previews WHERE expires_at<=? AND state='pending'",
                (now,),
            )
            db.execute(
                """INSERT INTO workorder_previews (
                    preview_id, user_key, action, request_json, preview_json,
                    request_digest, contract_version, workorder_id, expected_version,
                    expires_at, state, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'contract-v1', ?, ?, ?, 'pending', NULL)""",
                (preview_id, user_key, action, self._encode(request), self._encode(preview),
                 digest, workorder_id, expected_version, now + self._ttl_seconds),
            )
        return preview_id

    def begin(self, *, preview_id: str, user_key: str, action: str
              ) -> tuple[_PendingPreview, ToolResult | None]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM workorder_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if row is None:
                raise PreviewError("APPROVAL_EXPIRED", "预览已过期，请重新生成。")
            record = self._record(row)
            if record.user_key != user_key:
                raise PreviewError("APPROVAL_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if record.action != action:
                raise PreviewError("APPROVAL_ACTION_MISMATCH", "预览操作类型不匹配。")
            if record.state == "completed" and record.result is not None and record.result.status is not ToolStatus.UNKNOWN:
                return record, record.result
            if record.expires_at <= time():
                db.execute("DELETE FROM workorder_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("APPROVAL_EXPIRED", "预览已过期，请重新生成。")
            if record.state == "executing":
                raise PreviewError("APPROVAL_IN_PROGRESS", "该操作正在执行，请勿重复提交。")
            db.execute("UPDATE workorder_previews SET state='executing' WHERE preview_id=?", (preview_id,))
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        state = "completed" if result.status is not ToolStatus.UNKNOWN else "pending"
        with self._lock, self._connect() as db:
            db.execute("UPDATE workorder_previews SET state=?, result_json=? WHERE preview_id=?",
                       (state, self._result_json(result), preview_id))

    def action_for(self, *, preview_id: str, user_key: str) -> str:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM workorder_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if row is None:
                raise PreviewError("APPROVAL_EXPIRED", "预览已过期，请重新生成。")
            if row["user_key"] != user_key:
                raise PreviewError("APPROVAL_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if row["state"] != "completed" and row["expires_at"] <= time():
                db.execute("DELETE FROM workorder_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("APPROVAL_EXPIRED", "预览已过期，请重新生成。")
            return cast(str, row["action"])


def build_workorder_tools(store: PreviewStore) -> tuple[ToolDefinition, ...]:
    create_properties = _draft_candidate_properties()
    create_properties["code_mode"] = {
        "type": "string", "enum": ["auto", "manual"],
        "description": "编号模式。auto=按MES规则自动生成；manual=使用用户明确给出的编号，此时必须同时提供workorder_code。新增必填；编辑工具不使用此参数。",
    }
    create_properties["order_source"] = {
        "type": "string", "enum": ["ORDER", "STORE"],
        "description": "来源类型，新增必填：ORDER=客户订单，STORE=库存备货。只接受这两个值；编辑时可省略以保留原来源，明确提供时按给定值修改。",
    }
    create_properties["workorder_type"] = {
        **cast(dict[str, JsonValue], create_properties["workorder_type"]),
        "enum": ["SELF", "OUTSOURCE", "PURCHASE"],
        "description": "工单类型：SELF=自制，OUTSOURCE=委外，PURCHASE=外购。新增省略时采用网页默认SELF并在预览显示；编辑省略时保留原值。",
    }
    create_schema: JsonObject = {
        "type": "object", "properties": create_properties,
        "required": ["code_mode", "workorder_name", "order_source", "quantity", "request_date"],
        "anyOf": [{"required": ["product_id"]}, {"required": ["product_code"]}, {"required": ["product_name"]}],
        "allOf": [{"if": {"properties": {"code_mode": {"const": "manual"}}},
                   "then": {"required": ["workorder_code"]}}],
        "additionalProperties": False,
    }
    edit_properties = _draft_candidate_properties()
    edit_properties["target_workorder_code"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "description": "要编辑的现有工单编号，最大64字符，必须准确指定；工具先读取并校验目标为可编辑草稿。",
    }
    return (
        _preview_tool(
            name="ktg_preview_create_workorder_draft",
            label="添加生产工单预览",
            description=(
                "添加生产工单：仅收集用户明确提供的网页字段并生成草稿预览，不在此工具写入；"
                "实际保存须由用户在可信网页确认。新增必填工单名称、来源、数量、需求日期、编号模式及至少一种产品标识。"
                "product_id仅可来自已知查询结果，product_code/product_name可直接用用户给出的值；"
                "工具内部查询及分页并唯一解析，多个候选返回有限候选要求用户选择，无需先调用产品查询。"
                "ORDER=客户订单，STORE=库存备货；订单号及客户字段在ORDER可选。数量0.01至99999999、有效最多两位小数；日期用用户明确给出的yyyy-MM-dd，不得猜测。"
                "编号模式auto自动生成、manual手工提供编号；类型可选SELF自制、OUTSOURCE委外、PURCHASE外购，新增省略采用网页默认SELF并显示在预览。"
                "响应返回完整预览或有限候选；编辑工具仅改明确提供字段，省略字段保留原值。"
            ),
            properties=create_properties,
            required=("code_mode", "workorder_name", "order_source", "quantity", "request_date"),
            input_schema=create_schema,
            execute=lambda context, arguments: _preview_create(
                store, context, arguments
            ),
        ),
        _preview_tool(
            name="ktg_preview_edit_workorder_draft",
            label="预览编辑工单草稿",
            description=(
                "预览编辑生产工单草稿：用于修改已存在的草稿字段；必须用target_workorder_code指定目标。仅修改明确提供的字段，省略字段保留原值；不接受新增编号模式。"
                "读取目标并返回变更前后预览。非草稿、无权限或版本冲突时拒绝；此工具不写入，只有用户在可信网页确认后才执行保存。"
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
        "product_id": {"type": "integer", "minimum": 1,
                        "description": "产品ID，只能使用可信查询已返回的ID，不得猜测；可与编码/名称同时提供以交叉核对且必须指向同一产品。新增至少提供一种产品标识；编辑省略表示不换产品。"},
        "product_code": {"type": "string", "minLength": 1, "maxLength": 64,
                         "description": "产品编号/物料编码，1至64字符。可直接使用用户给出的准确值，工具内部查询解析；可与ID/名称同时给出且须一致。新增至少一种标识；编辑省略表示不换产品。"},
        "product_name": {"type": "string", "minLength": 1, "maxLength": 255,
                         "description": "产品名称，1至255字符。可直接使用用户提供名称，工具内部查询；重名返回有限候选要求按编码选择。可与ID/编码同时提供且须一致。新增至少一种标识；编辑省略不换产品。产品名称/规格/单位由查询结果生成。"},
        "workorder_code": {"type": "string", "minLength": 1, "maxLength": 64,
                           "description": "工单编号，1至64字符。新增仅manual模式必填；auto模式由MES生成。编辑时明确提供才修改，省略保留原编号。"},
        "workorder_name": {"type": "string", "minLength": 1, "maxLength": 255,
                            "description": "工单名称，1至255字符；新增必填且来自用户明确输入，不得从产品名推造。编辑可选，省略保留原名称。"},
        "workorder_type": {
            "type": "string",
            "enum": ["SELF", "OUTSOURCE", "PURCHASE"],
            "description": "工单类型：SELF=自制，OUTSOURCE=委外，PURCHASE=外购。新增省略采用网页默认SELF；编辑省略保留原类型。",
        },
        "order_source": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "来源类型。新增必填：ORDER=客户订单、STORE=库存备货；编辑可选，省略保留原来源，提供才修改。"},
        "source_code": {"type": "string", "maxLength": 64,
                         "description": "来源单号，最多64字符；新增ORDER时可选，STORE新增时忽略并清空；编辑省略保留现值、明确提供才修改。ORDER可留空。"},
        "batch_code": {"type": "string", "maxLength": 64,
                        "description": "批次号，最多64字符，可选；新增提供时写入，编辑省略保留原值，提供才修改。"},
        "quantity": {
            "anyOf": [
                {"type": "number", "minimum": 0.01, "maximum": 99999999},
                {"type": "string", "pattern": r"^\d+(?:\.\d+)?$"},
            ],
            "description": "生产数量，新增必填、编辑可选。范围0.01至99999999，有效最多两位小数；拒绝超精度，不四舍五入。编辑省略保留原数量。",
        },
        "request_date": {
            "type": "string",
            "description": "需求日期，格式yyyy-MM-dd且必须是用户明确给出的有效日期，不猜今天或填具体示例日期。新增必填，编辑可选且省略保留原值。",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
        },
        "client_id": {"type": "integer", "minimum": 0,
                       "description": "客户ID，非负整数，可选，只使用用户或可信查询确认的值。新增STORE时忽略并清空、ORDER下可选；编辑省略保留原值，提供才修改。"},
        "client_code": {"type": "string", "maxLength": 64,
                         "description": "客户编码，最多64字符，可选。新增STORE时忽略并清空、ORDER下可选；编辑省略保留原值，提供才修改。"},
        "client_name": {"type": "string", "maxLength": 255,
                         "description": "客户名称，最多255字符，可选。新增STORE时忽略并清空、ORDER下可选；编辑省略保留原值，提供才修改。"},
        "parent_id": {"type": "integer", "minimum": 0,
                       "description": "父工单ID，非负整数，0和非0均允许，可选；新增省略默认0，编辑省略保留原值，提供才修改。"},
        "remark": {"type": "string", "maxLength": 500,
                    "description": "备注，最多500字符，可选；新增省略为空，编辑省略保留原备注，提供才修改。"},
    }


def _preview_tool(
    *,
    name: str,
    label: str,
    description: str,
    properties: JsonObject,
    required: tuple[str, ...],
    execute: object,
    input_schema: JsonObject | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1.0",
        description=description,
        label=label,
        input_schema=input_schema or {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        output_schema=None,
        # Catalog authorization covers every supported input path (ID or search).
        required_permissions=(
            "mes:md:mditem:list", "mes:md:mditem:query",
            *(("mes:pro:workorder:add",) if name == "ktg_preview_create_workorder_draft"
              else ("mes:pro:workorder:edit", "mes:pro:workorder:list", "mes:pro:workorder:query")),
        ),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER,
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
        description="执行当前用户在可信网页明确确认的工单草稿预览。preview_id是服务器生成并绑定当前用户的一次性不透明标识，只能原样引用工具返回值，不得猜造或复用他人标识；没有网页确认不得执行。",
        label=label,
        input_schema={
            "type": "object",
            "properties": {"preview_id": {"type": "string", "minLength": 1,
                                          "description": "服务器生成并绑定当前登录用户的一次性预览ID；只能从本轮可信预览结果原样引用，不能由模型编造。调用代表网页用户已确认，模型不得自行调用。"}},
            "required": ["preview_id"],
            "additionalProperties": False,
        },
        output_schema=None,
        required_permissions=(
            "mes:pro:workorder:add" if action == "create" else "mes:pro:workorder:edit",
            "mes:pro:workorder:list", "mes:pro:workorder:query",
            "mes:md:bom:list", "mes:pro:workorderbom:list",
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
        code_mode = arguments.get("code_mode")
        if code_mode not in {"auto", "manual"}:
            raise PreviewError("INVALID_CODE_MODE", "请选择工单编号自动生成或手工填写。")
        workorder_code = _optional_text(arguments, "workorder_code")
        if code_mode == "manual" and workorder_code is None:
            raise PreviewError("WORKORDER_CODE_REQUIRED", "手工编号模式必须提供工单编号。")
        if code_mode == "manual" and (workorder_code is None or len(workorder_code) > 64):
            raise PreviewError("INVALID_WORKORDER_CODE", "工单编号不能为空且不能超过 64 个字符。")
        workorder_name = _optional_text(arguments, "workorder_name")
        if workorder_name is None:
            raise PreviewError("WORKORDER_NAME_REQUIRED", "请提供工单名称（最多 255 个字符）。")
        if len(workorder_name) > 255:
            raise PreviewError("INVALID_WORKORDER_NAME", "工单名称不能超过 255 个字符。")
        order_source = _optional_text(arguments, "order_source")
        if order_source not in {"ORDER", "STORE"}:
            raise PreviewError("INVALID_ORDER_SOURCE", "来源类型请选择客户订单（ORDER）或库存备货（STORE）。")
        workorder_type = _optional_text(arguments, "workorder_type") or "SELF"
        if workorder_type not in {"SELF", "OUTSOURCE", "PURCHASE"}:
            raise PreviewError("INVALID_WORKORDER_TYPE", "工单类型请选择 SELF、OUTSOURCE 或 PURCHASE。")
        quantity = _quantity(arguments.get("quantity"))
        request_date = _request_date(arguments.get("request_date"))
        _validate_note(arguments.get("remark"))
        product = await _resolve_product(client, arguments)
        if code_mode == "auto":
            workorder_code = await client.get_text("/system/autocode/get/WORKORDER_CODE")
        assert workorder_code is not None
        if order_source == "STORE":
            arguments = {**arguments, "source_code": "", "client_id": None,
                         "client_code": None, "client_name": None}
        arguments = {**arguments, "workorder_type": workorder_type}
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
        "code_mode": cast(str, code_mode),
        "product": _product_view(product),
        "quantity": quantity,
        "unitOfMeasure": product.get("unitOfMeasure"),
        "requestDate": request_date,
        "workorderCode": workorder_code,
        "workorderName": workorder_name,
        "orderSource": order_source,
        "sourceCode": request.get("sourceCode"),
        "workorderType": workorder_type,
        "remark": request.get("remark"),
        "clientCode": request.get("clientCode"),
        "clientName": request.get("clientName"),
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
        content=(
            "工单草稿预览已准备（尚未写入；需在网页确认）。"
            f"编号：{workorder_code}（{'自动生成' if code_mode == 'auto' else '手工填写'}）；"
            f"名称：{workorder_name}；来源：{order_source}"
            f"{('，来源单号：' + str(request.get('sourceCode'))) if request.get('sourceCode') else ''}；"
            f"类型：{workorder_type}；产品：{product.get('itemCode')} / {product.get('itemName')}；"
            f"规格：{product.get('specification')}；单位：{product.get('unitOfMeasure')}；"
            f"数量：{quantity}；需求日期：{request_date}；"
            f"备注：{request.get('remark') or '无'}。网页确认后只保存草稿。"
        ),
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
        verify_bom = action == "create" or any(
            isinstance(change, Mapping)
            and change.get("field") in {"productId", "quantity"}
            for change in cast(list[object], record.preview.get("changes", []))
        )
        action_code = "CREATE_DRAFT" if action == "create" else "EDIT_DRAFT"
        operation = {
            "operationId": preview_id,
            "action": action_code,
            "requestDigest": record.request_digest,
            "expectedVersion": record.expected_version,
            "draft": record.request,
        }
        receipt = await client.execute_workorder_operation(operation)
        outcome = receipt.get("outcome")
        if outcome == "failed":
            code = receipt.get("errorCode")
            result = _failure(
                "工单操作未执行。", code if isinstance(code, str) else "WORKORDER_OPERATION_FAILED",
                details={"kind": "workorder_draft_result", "outcome": "failed",
                         "label": "写入失败", "preview_id": preview_id},
                operation_id=preview_id,
            )
        elif outcome != "succeeded":
            result = _unknown_result(record.request, preview_id, reconciled=True)
        else:
            workorder_id = receipt.get("workorderId")
            if not isinstance(workorder_id, int) or workorder_id <= 0:
                result = _unknown_result(record.request, preview_id, reconciled=True)
            else:
                result = await _verified_result(
                    client, action, record.request, workorder_id, preview_id,
                    reconciled=False, verify_bom=verify_bom,
                )
    except PreviewError as exc:
        result = _failure(str(exc), exc.code, operation_id=preview_id)
    except KTGError as exc:
        result = _failure(str(exc), exc.code, operation_id=preview_id)
    store.finish(preview_id, result)
    return result


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
            "bom_verified": False,
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
    verify_bom: bool = True,
) -> ToolResult:
    payload = await client.get(f"/mes/pro/workorder/{workorder_id}")
    raw = payload.get("data")
    if not isinstance(raw, Mapping):
        raise PreviewError("WRITE_RESULT_UNKNOWN", "写后无法回读工单，结果待核实。")
    detail = cast(Mapping[object, object], raw)
    if not _matches_request(detail, request):
        raise PreviewError("WRITE_VERIFICATION_FAILED", "写后回读字段不一致，结果待核实。")
    expected_version = 0 if action == "create" else cast(int, request.get("version")) + 1
    if detail.get("version") != expected_version:
        raise PreviewError("WRITE_VERIFICATION_FAILED", "写后回读版本不一致，结果待核实。")
    bom_verified = False
    bom_count: int | None = None
    if verify_bom:
        product_id = request.get("productId")
        if not isinstance(product_id, int):
            raise PreviewError("BOM_VERIFICATION_FAILED", "产品 ID 无效，无法核验 BOM。")
        templates = await _list_all_rows(
            client, "/mes/md/bom/list", {"itemId": product_id}
        )
        rows = await _list_all_rows(
            client, "/mes/pro/workorderbom/list", {"workorderId": workorder_id}
        )
        planned_quantity = Decimal(_quantity(request.get("quantity")))
        expected: list[tuple[int, Decimal]] = []
        actual: list[tuple[int, Decimal]] = []
        try:
            for template in templates:
                item_id = template.get("bomItemId")
                if not isinstance(item_id, int):
                    raise ValueError("BOM item ID missing")
                quantity = Decimal(_quantity(template.get("quantity"))) * planned_quantity
                expected.append((item_id, quantity))
            for row in rows:
                item_id = row.get("itemId")
                if not isinstance(item_id, int):
                    raise ValueError("workorder BOM item ID missing")
                actual.append((item_id, Decimal(_quantity(row.get("quantity")))))
        except (PreviewError, ValueError) as exc:
            raise PreviewError("BOM_VERIFICATION_FAILED", "BOM 回读数据无效，结果待核实。") from exc
        expected.sort()
        actual.sort()
        # pro_workorder_bom.quantity is stored with two decimal places in MySQL.
        rounding_tolerance = Decimal("0.0050001")
        if len(expected) != len(actual) or any(
            expected_id != actual_id or abs(expected_quantity - actual_quantity) > rounding_tolerance
            for (expected_id, expected_quantity), (actual_id, actual_quantity)
            in zip(expected, actual)
        ):
            raise PreviewError("BOM_VERIFICATION_FAILED", "工单 BOM 与产品模板不一致，结果待核实。")
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
    product_id = arguments.get("product_id")
    product_code = _optional_text(arguments, "product_code")
    product_name = _optional_text(arguments, "product_name")
    if isinstance(product_id, int) and not isinstance(product_id, bool):
        payload = await client.get(f"/mes/md/mditem/{product_id}")
        raw = payload.get("data")
        candidates = [raw] if isinstance(raw, Mapping) and raw.get("itemId") == product_id else []
    elif product_code is not None or product_name is not None:
        params: dict[str, object] = (
            {"itemCode": product_code}
            if product_code is not None
            else {"itemName": product_name}
        )
        candidates = await _list_all_rows(client, "/mes/md/mditem/list", params)
    else:
        raise PreviewError("PRODUCT_REQUIRED", "请提供产品编码、名称或 ID。")
    if product_code is not None and not isinstance(product_id, int):
        candidates = [row for row in candidates if row.get("itemCode") == product_code]
    elif product_name is not None and not isinstance(product_id, int):
        exact = [row for row in candidates if row.get("itemName") == product_name]
        if exact:
            candidates = exact
    if not candidates:
        raise PreviewError("PRODUCT_NOT_FOUND", "未找到符合条件的产品。")
    if len(candidates) != 1:
        candidate_views = [
            _product_view(_json_mapping(candidate)) for candidate in candidates
        ]
        candidate_limit = 20
        visible_candidates = candidate_views[:candidate_limit]
        details: JsonObject = {
            "kind": "workorder_product_candidates",
            "candidates": cast(list[JsonValue], visible_candidates),
            "total": len(candidate_views),
            "truncated": len(candidate_views) > candidate_limit,
        }
        candidate_summary = "；".join(
            f"{row.get('itemCode', '无编码')} / {row.get('itemName', '无名称')}"
            for row in visible_candidates
        )
        suffix = "；候选已截断，请提供更精确的产品编码" if len(candidate_views) > candidate_limit else ""
        raise _AmbiguousProduct(details, candidate_summary, len(candidate_views), suffix)
    product = candidates[0]
    if (
        (product_code is not None and product.get("itemCode") != product_code)
        or (
            product_name is not None
            and (isinstance(product_id, int) or product_code is not None)
            and product.get("itemName") != product_name
        )
    ):
        raise PreviewError("PRODUCT_MISMATCH", "产品 ID 与编码或名称不一致。")
    item_id = product.get("itemId")
    if not isinstance(item_id, int) or item_id <= 0:
        raise PreviewError("KTG_INVALID_RESPONSE", "产品缺少有效 ID。")
    if product.get("enableFlag") == "N":
        raise PreviewError("PRODUCT_DISABLED", "所选产品已停用。")
    return cast(JsonObject, {str(key): cast(JsonValue, value) for key, value in product.items()})


async def _list_all_rows(
    client: KTGClient, path: str, filters: Mapping[str, object]
) -> list[Mapping[object, object]]:
    rows: list[Mapping[object, object]] = []
    page_size = 20
    for page in range(1, 26):
        payload = await client.get(
            path, {**filters, "pageNum": page, "pageSize": page_size}
        )
        page_rows = payload.get("rows")
        total = payload.get("total")
        if (
            not isinstance(page_rows, list)
            or not isinstance(total, int)
            or total < 0
            or any(not isinstance(row, Mapping) for row in page_rows)
        ):
            raise PreviewError("KTG_INVALID_RESPONSE", "KTG 列表查询结果无效。")
        if total > 500:
            raise PreviewError("KTG_RESULT_TOO_LARGE", "匹配结果过多，请提供更精确的编码。")
        rows.extend(cast(list[Mapping[object, object]], page_rows))
        if len(rows) >= total:
            if len(rows) != total:
                raise PreviewError("KTG_INVALID_RESPONSE", "KTG 分页结果数量不一致。")
            return rows
        if not page_rows:
            raise PreviewError("KTG_INVALID_RESPONSE", "KTG 分页结果不完整。")
    raise PreviewError("KTG_RESULT_TOO_LARGE", "匹配结果过多，请提供更精确的编码。")


class _AmbiguousProduct(PreviewError):
    def __init__(self, details: JsonObject, summary: str, total: int, suffix: str) -> None:
        super().__init__(
            "AMBIGUOUS_PRODUCT",
            f"找到 {total} 个匹配产品，无法确定唯一产品。候选：{summary}{suffix}。请按产品编码选择。",
        )
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
        "workorderType": _optional_text(arguments, "workorder_type"),
        "orderSource": _optional_text(arguments, "order_source"),
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
    for key in ("sourceCode", "batchCode", "clientId", "clientCode", "clientName", "remark"):
        if key in request and detail.get(key) != request[key]:
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
        "specification": product.get("specification"),
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
    exponent = number.normalize().as_tuple().exponent
    if (not number.is_finite() or number < Decimal("0.01")
            or number > Decimal("99999999") or (isinstance(exponent, int) and exponent < -2)):
        raise PreviewError("INVALID_QUANTITY", "生产数量必须在 0.01 到 99999999 之间，最多两位小数。")
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
