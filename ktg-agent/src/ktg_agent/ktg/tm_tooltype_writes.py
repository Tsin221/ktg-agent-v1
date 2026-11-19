"""Durable previews and trusted confirmation for tool type CRUD."""
from __future__ import annotations

from ktg_agent.identity_scope import identity_scope

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from time import time
from typing import cast
from uuid import uuid4

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.tm_tooltype import (
    FILTER_FIELDS, MAINTENANCE_TYPES, MAX_PERIOD, TOOLTYPE_LIST, TOOLTYPE_PATH,
    fetch_tooltype, project_tooltype, tooltype_page,
)
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolChannel, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)


PREVIEW_CREATE = "ktg_preview_tm_tooltype_create"
PREVIEW_UPDATE = "ktg_preview_tm_tooltype_update"
PREVIEW_DELETE = "ktg_preview_tm_tooltype_delete"
APPLY_CREATE = "ktg_tm_tooltype_create"
APPLY_UPDATE = "ktg_tm_tooltype_update"
APPLY_DELETE = "ktg_tm_tooltype_delete"
MUTABLE = ("toolTypeCode", "toolTypeName", "codeFlag", "maintenType", "maintenPeriod", "remark")
_WIRE_TO_ARG = {"toolTypeCode": "tool_type_code", "toolTypeName": "tool_type_name",
                 "codeFlag": "code_flag", "maintenType": "mainten_type",
                 "maintenPeriod": "mainten_period", "remark": "remark"}
_ARG_TO_WIRE = {argument: wire for wire, argument in _WIRE_TO_ARG.items()}
_ARG_LIMITS = {"tool_type_code": 64, "tool_type_name": 100, "remark": 250}
_ACTIONS = {
    "create": (PREVIEW_CREATE, APPLY_CREATE, "mes:tm:tooltype:add"),
    "update": (PREVIEW_UPDATE, APPLY_UPDATE, "mes:tm:tooltype:edit"),
    "delete": (PREVIEW_DELETE, APPLY_DELETE, "mes:tm:tooltype:remove"),
}


@dataclass(slots=True)
class _Preview:
    preview_id: str
    user_key: str
    action: str
    before: JsonObject | None
    after: JsonObject
    digest: str
    expires_at: float
    state: str
    result: ToolResult | None


class TmToolTypePreviewStore:
    """User-bound previews and one-shot results kept in a dedicated SQLite database."""

    def __init__(self, *, ttl_seconds: float = 600, path: str | Path = ":memory:") -> None:
        if ttl_seconds <= 0:
            raise ValueError("preview ttl must be positive")
        self._ttl_seconds, self.path = ttl_seconds, str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._memory_db = sqlite3.connect(":memory:", timeout=10, check_same_thread=False) if self.path == ":memory:" else None
        if self._memory_db is not None:
            self._memory_db.row_factory = sqlite3.Row
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tm_tooltype_previews (
                preview_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, action TEXT NOT NULL,
                before_json TEXT, after_json TEXT NOT NULL, digest TEXT NOT NULL,
                expires_at REAL NOT NULL, state TEXT NOT NULL, result_json TEXT
            )""")

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
    def _digest(action: str, before: JsonObject | None, after: JsonObject) -> str:
        encoded = json.dumps({"action": action, "before": before, "after": after}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def create(self, *, user_key: str, action: str, before: JsonObject | None, after: JsonObject) -> str:
        preview_id, digest = str(uuid4()), self._digest(action, before, after)
        with self._lock, self._connect() as db:
            now = time()
            db.execute("DELETE FROM tm_tooltype_previews WHERE expires_at<=? AND state='pending'", (now,))
            db.execute("""INSERT INTO tm_tooltype_previews
                (preview_id,user_key,action,before_json,after_json,digest,expires_at,state,result_json)
                VALUES (?,?,?,?,?,?,?,'pending',NULL)""",
                (preview_id, user_key, action, self._encode(before) if before is not None else None,
                 self._encode(after), digest, now + self._ttl_seconds))
        return preview_id

    def action_for(self, *, preview_id: str, user_key: str) -> str:
        record = self._read(preview_id)
        if record is None:
            raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在或已过期，请重新生成。")
        if record.user_key != user_key:
            raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
        if record.state != "completed" and record.expires_at <= time():
            raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
        return record.action

    def begin(self, *, preview_id: str, user_key: str, action: str) -> tuple[_Preview, ToolResult | None]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tm_tooltype_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if row is None:
                raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在或已过期，请重新生成。")
            record = self._record(row)
            if record.user_key != user_key:
                raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if record.action != action:
                raise PreviewError("PREVIEW_ACTION_MISMATCH", "预览操作类型不匹配。")
            if record.state == "completed":
                return record, record.result
            if record.state == "executing":
                raise PreviewError("PREVIEW_IN_PROGRESS", "该操作正在执行，请勿重复提交。")
            if record.expires_at <= time():
                db.execute("DELETE FROM tm_tooltype_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
            db.execute("UPDATE tm_tooltype_previews SET state='executing' WHERE preview_id=? AND state='pending'", (preview_id,))
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        encoded = self._encode({"content": result.content, "details": result.details,
            "status": result.status.value, "error_code": result.error_code,
            "operation_id": result.operation_id, "verification": result.verification})
        with self._lock, self._connect() as db:
            db.execute("UPDATE tm_tooltype_previews SET state='completed',result_json=? WHERE preview_id=?", (encoded, preview_id))

    def _read(self, preview_id: str) -> _Preview | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tm_tooltype_previews WHERE preview_id=?", (preview_id,)).fetchone()
            return None if row is None else self._record(row)

    def _record(self, row: sqlite3.Row) -> _Preview:
        before = cast(JsonObject | None, json.loads(row["before_json"])) if row["before_json"] else None
        after = cast(JsonObject, json.loads(row["after_json"]))
        if self._digest(row["action"], before, after) != row["digest"]:
            raise PreviewError("PREVIEW_STALE", "预览内容已变化，请重新生成。")
        result = None
        if row["result_json"]:
            value = json.loads(row["result_json"])
            result = ToolResult(content=value["content"], details=value["details"], status=ToolStatus(value["status"]),
                error_code=value["error_code"], operation_id=value["operation_id"], verification=value["verification"])
        return _Preview(row["preview_id"], row["user_key"], row["action"], before, after,
                        row["digest"], row["expires_at"], row["state"], result)


def build_tm_tooltype_write_tools(store: TmToolTypePreviewStore) -> tuple[ToolDefinition, ...]:
    return tuple(_definition(store, action, hidden)
                 for action in ("create", "update", "delete") for hidden in (False, True))


def tm_tooltype_write_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
    def create_factory(item: ToolDefinition) -> ToolFactory:
        return lambda: item
    return {f"tm_tooltype_writes:{item.name}": create_factory(item) for item in definitions}


def tm_tooltype_write_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
    permissions = {action: PermissionAll(tuple(PermissionLeaf(permission) for permission in _permissions(action)))
                   for action in _ACTIONS}
    return tuple(ToolManifest(name=item.name, version=item.version, module="tm", domain="tooltype",
        label=item.label, description=item.description, permission_policy=permissions[_action_for_name(item.name)],
        effect=item.effect, execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=item.expose_to_model,
        confirmation_policy="none" if item.expose_to_model else "trusted_confirmation",
        proposal_store_effect=item.name.startswith("ktg_preview_"), handler_ref=f"tm_tooltype_writes:{item.name}")
        for item in definitions)


def _definition(store: TmToolTypePreviewStore, action: str, hidden: bool) -> ToolDefinition:
    preview_name, apply_name, _ = _ACTIONS[action]
    name = apply_name if hidden else preview_name
    schema = _apply_schema() if hidden else _preview_schema(action)

    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _apply(store, action, context, arguments) if hidden else await _preview(store, action, context, arguments)

    return ToolDefinition(name=name, version="1.0", label=f"{'确认' if hidden else '预览'}工装夹具类型{_action_label(action)}",
        description=(f"受信确认通道专用：执行已确认的工装夹具类型{_action_label(action)}预览，模型不可见。" if hidden else
                     _PREVIEW_DESCRIPTIONS[action]), input_schema=cast(JsonObject, schema),
        output_schema={"type": "object", "properties": {key: {} for key in
            ("kind", "action", "preview_id", "before", "after", "warnings", "outcome", "verified")},
            "additionalProperties": True}, required_permissions=_permissions(action),
        effect=ToolEffect.WRITE if hidden else ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER, timeout_seconds=30, execute=execute, expose_to_model=not hidden)


def _preview_schema(action: str) -> dict[str, object]:
    properties: dict[str, JsonValue] = {}
    if action != "create":
        properties["tool_type_id"] = {
            "type": "integer", "minimum": 1,
            "description": "目标类型的系统 ID，取自类型查询结果的 toolTypeId。",
        }
    if action != "delete":
        properties.update({
            "tool_type_code": {"type": "string", "minLength": 1, "maxLength": 64,
                               "description": "工装夹具类型的唯一业务编码。"},
            "tool_type_name": {"type": "string", "minLength": 1, "maxLength": 100,
                               "description": "工装夹具类型的唯一名称。"},
            "code_flag": {"type": "string", "enum": ["Y", "N"],
                          "description": "Y 表示一件一码，须有维护方式和周期。N 表示非独立编码，维护字段将置空。"},
            "mainten_type": {"type": "string", "enum": ["REGULAR", "USAGE"],
                             "description": "类型维护方式。REGULAR 为按天定期维护，USAGE 为按使用次数维护。"},
            "mainten_period": {"type": "integer", "minimum": 1, "maximum": MAX_PERIOD,
                               "description": "类型维护周期。REGULAR 单位为天，USAGE 单位为次。"},
            "remark": {"type": "string", "maxLength": 250,
                       "description": "类型备注，传空字符串可清空。"},
        })
    required = ["tool_type_code", "tool_type_name", "code_flag"] if action == "create" else ["tool_type_id"]
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _apply_schema() -> dict[str, object]:
    return {"type": "object", "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["operation_id"], "additionalProperties": False}


async def _preview(store: TmToolTypePreviewStore, action: str, context: ToolContext,
                   arguments: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        user_key = _identity_key(context.identity)
        if action == "create":
            before, after = None, await _create_payload(client, arguments)
        elif action == "update":
            before, after = await _update_payload(client, arguments)
        else:
            if set(arguments) != {"tool_type_id"}:
                raise KTGError("VALIDATION_ERROR", "删除只允许提供 tool_type_id。")
            before = await fetch_tooltype(client, arguments.get("tool_type_id"))
            await _ensure_unreferenced(client, cast(int, before["toolTypeId"]))
            after = {key: before[key] for key in ("toolTypeId", "toolTypeCode", "toolTypeName")}
        preview_id = store.create(user_key=user_key, action=action, before=before, after=after)
        details: JsonObject = {"kind": "tm_tooltype_preview", "action": action, "preview_id": preview_id,
                               "before": before, "after": after, "warnings": []}
        return ToolResult(content=_preview_content(action, preview_id, before, after),
                          details=details, operation_id=preview_id)
    except (KTGError, PreviewError) as exc:
        return _failure(str(exc), exc.code)


async def _create_payload(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    after = _base_payload(arguments, partial=False)
    await _ensure_unique(client, after)
    return after


async def _update_payload(client: KTGClient, arguments: Mapping[str, object]) -> tuple[JsonObject, JsonObject]:
    before = await fetch_tooltype(client, arguments.get("tool_type_id"))
    after = dict(before)
    changed: list[str] = []
    permitted = set(_ARG_LIMITS) | {"code_flag", "mainten_type", "mainten_period"}
    if set(arguments) - permitted - {"tool_type_id"}:
        raise KTGError("VALIDATION_ERROR", "包含未支持的工装夹具类型字段。")
    for wire, arg in _WIRE_TO_ARG.items():
        if arg in arguments:
            after[wire] = _normalize_update_field(arg, arguments[arg])
    if after["codeFlag"] == "N":
        after["maintenType"] = None
        after["maintenPeriod"] = None
    elif after["codeFlag"] == "Y":
        if after["maintenType"] not in MAINTENANCE_TYPES:
            raise KTGError("VALIDATION_ERROR", "编码管理为Y时必须提供合法维护类型。")
        period = after["maintenPeriod"]
        if not isinstance(period, int) or isinstance(period, bool) or not 1 <= period <= MAX_PERIOD:
            raise KTGError("VALIDATION_ERROR", "编码管理为Y时维护周期必须是1至2147483647的整数。")
    else:
        raise KTGError("VALIDATION_ERROR", "工装夹具类型当前编码管理值异常，请先修复页面记录。")
    changed.extend(field for field in MUTABLE if after[field] != before[field])
    if not changed:
        raise KTGError("NO_CHANGE", "没有提供发生变化的工装夹具类型字段。")
    await _ensure_unique(client, after, exclude_id=cast(int, before["toolTypeId"]))
    after["changedFields"] = cast(JsonValue, changed)
    return before, after


def _preview_content(action: str, preview_id: str, before: JsonObject | None, after: JsonObject) -> str:
    if action == "delete":
        target = f"[{after['toolTypeId']}] {after['toolTypeCode']} {after['toolTypeName'] or ''}"
        return f"删除工装夹具类型预览（{preview_id}）：目标 {target}。确认时会重新检查工装台账引用。"
    code = after.get("toolTypeCode", "")
    name = after.get("toolTypeName", "")
    if action == "create":
        return f"新增工装夹具类型预览（{preview_id}）：编码 {code}，名称 {name}；{_maintenance_summary(after)}。"
    fields = cast(list[str], after.get("changedFields", []))
    changes = "；".join(f"{field}: {before.get(field) if before else None} → {after.get(field)}" for field in fields)
    return f"修改工装夹具类型预览（{preview_id}）：[{after['toolTypeId']}] {code} {name}；变更 {changes}。"


def _maintenance_summary(record: Mapping[str, object]) -> str:
    if record.get("codeFlag") == "N":
        return "编码管理N，维护类型与周期均为无"
    type_value = record.get("maintenType")
    type_label = {"REGULAR": "定期维护", "USAGE": "按使用次数维护"}.get(cast(str, type_value), "未设置")
    unit = {"REGULAR": "天", "USAGE": "次"}.get(cast(str, type_value), "")
    period = record.get("maintenPeriod")
    return f"编码管理Y，{type_label}，周期{period}{unit}"


def _normalize_update_field(arg: str, value: object) -> JsonValue:
    if arg in _ARG_LIMITS:
        limit = _ARG_LIMITS[arg]
        required = arg in {"tool_type_code", "tool_type_name"}
        if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
            raise KTGError("VALIDATION_ERROR", f"{arg} 必须是长度不超过 {limit} 的{'非空' if required else ''}文本。")
        return value.strip()
    if arg == "code_flag":
        if not isinstance(value, str) or value not in {"Y", "N"}:
            raise KTGError("VALIDATION_ERROR", "code_flag 必须是 Y 或 N。")
        return value
    if arg == "mainten_type":
        if value is not None and (not isinstance(value, str) or value not in MAINTENANCE_TYPES):
            raise KTGError("VALIDATION_ERROR", "mainten_type 只允许 REGULAR 或 USAGE。")
        return value
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_PERIOD):
        raise KTGError("VALIDATION_ERROR", "mainten_period 必须是1至2147483647之间的整数。")
    return cast(JsonValue, value)


def _base_payload(arguments: Mapping[str, object], *, partial: bool) -> JsonObject:
    allowed = set(_ARG_LIMITS) | {"code_flag", "mainten_type", "mainten_period"}
    if set(arguments) - allowed - ({"tool_type_id"} if partial else set()):
        raise KTGError("VALIDATION_ERROR", "包含未支持的工装夹具类型字段。")
    return _normalize_payload(arguments, partial=partial)


def _normalize_payload(arguments: Mapping[str, object], *, partial: bool) -> JsonObject:
    after: JsonObject = {}
    for arg in ("tool_type_code", "tool_type_name", "code_flag", "mainten_type", "mainten_period", "remark"):
        wire = _ARG_TO_WIRE[arg]
        if partial and arg not in arguments:
            value: object = None
        elif arg not in arguments:
            value = "" if arg == "remark" else None
        else:
            value = arguments[arg]
        if arg in _ARG_LIMITS:
            limit = _ARG_LIMITS[arg]
            required = arg in {"tool_type_code", "tool_type_name"}
            if value is None and partial:
                after[wire] = None
                continue
            if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
                raise KTGError("VALIDATION_ERROR", f"{arg} 必须是长度不超过 {limit} 的{'非空' if required else ''}文本。")
            after[wire] = value.strip()
        elif arg == "code_flag":
            if not isinstance(value, str) or value not in {"Y", "N"}:
                raise KTGError("VALIDATION_ERROR", "code_flag 必须是 Y 或 N。")
            after[wire] = value
        elif arg == "mainten_type":
            if value is not None and (not isinstance(value, str) or value not in MAINTENANCE_TYPES):
                raise KTGError("VALIDATION_ERROR", "mainten_type 只允许 REGULAR 或 USAGE。")
            after[wire] = value
        else:
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_PERIOD):
                raise KTGError("VALIDATION_ERROR", "mainten_period 必须是1至2147483647之间的整数。")
            after[wire] = value
    if after["codeFlag"] == "N":
        after["maintenType"] = None
        after["maintenPeriod"] = None
    elif after["codeFlag"] == "Y":
        if after["maintenType"] not in MAINTENANCE_TYPES:
            raise KTGError("VALIDATION_ERROR", "编码管理为Y时必须提供合法维护类型。")
        if not isinstance(after["maintenPeriod"], int) or isinstance(after["maintenPeriod"], bool) or not 1 <= after["maintenPeriod"] <= MAX_PERIOD:
            raise KTGError("VALIDATION_ERROR", "编码管理为Y时维护周期必须是1至2147483647的整数。")
    return after


async def _ensure_unique(client: KTGClient, after: Mapping[str, object], exclude_id: int | None = None) -> None:
    for wire in ("toolTypeCode", "toolTypeName"):
        value = after.get(wire)
        if not isinstance(value, str) or not value:
            raise KTGError("VALIDATION_ERROR", "类型编码和名称不能为空。")
        if await _has_duplicate(client, wire, value, exclude_id):
            raise KTGError("TOOLTYPE_DUPLICATE", f"工装夹具类型{'编码' if wire == 'toolTypeCode' else '名称'}已存在，请使用唯一值。")


async def _has_duplicate(client: KTGClient, wire: str, value: str, exclude_id: int | None) -> bool:
    params: dict[str, object] = {wire: value, "pageNum": 1, "pageSize": 100}
    payload = await client.get(TOOLTYPE_LIST, params)
    rows, total = tooltype_page(payload)
    pages = min((total + 99) // 100, 1000)
    for page in range(1, pages + 1):
        if page > 1:
            rows, _ = tooltype_page(await client.get(TOOLTYPE_LIST, {**params, "pageNum": page}))
        for row in rows:
            same = (cast(str, row[wire]).casefold() == value.casefold()) if wire == "toolTypeCode" else row[wire] == value
            if same and row["toolTypeId"] != exclude_id:
                return True
    return total > pages * 100


async def _ensure_unreferenced(client: KTGClient, tool_type_id: int) -> None:
    payload = await client.get("/mes/tm/tool/list", {"toolTypeId": tool_type_id, "pageNum": 1, "pageSize": 1})
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "工装引用查询返回格式无效，已拒绝删除。")
    if rows or total:
        raise KTGError("TOOLTYPE_IN_USE", "该类型已关联工装台账，不能通过助手删除。")


async def _apply(store: TmToolTypePreviewStore, action: str, context: ToolContext,
                 arguments: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    if context.channel is not ToolChannel.TRUSTED_CONFIRMATION:
        return _failure("工装夹具类型写入只能通过网页用户确认执行。", "CONFIRMATION_REQUIRED")
    preview_id = arguments.get("operation_id")
    if not isinstance(preview_id, str) or not preview_id.strip() or len(preview_id) > 100:
        return _failure("operation_id 无效。", "VALIDATION_ERROR")
    if context.operation_id != preview_id or context.confirmation_id is None:
        return _failure("确认上下文与预览不匹配。", "CONFIRMATION_MISMATCH")
    begun = False
    try:
        record, cached = store.begin(preview_id=preview_id, user_key=_identity_key(context.identity), action=action)
        if cached is not None:
            return cached
        begun = True
        if record.before is not None:
            current = await fetch_tooltype(client, record.before["toolTypeId"])
            if not _snapshot_matches(current, record.before):
                raise PreviewError("PREVIEW_STALE", "工装夹具类型已变化，请重新生成预览。")
        if action == "delete":
            if record.before is None:
                raise PreviewError("PREVIEW_STALE", "预览缺少工装夹具类型快照。")
            await _ensure_unreferenced(client, cast(int, record.before["toolTypeId"]))
        if action == "create":
            result = await _apply_create(client, preview_id, record.after)
        elif action == "update":
            result = await _apply_update(client, preview_id, record.before, record.after)
        else:
            result = await _apply_delete(client, preview_id, record.before)
        store.finish(preview_id, result)
        return result
    except PreviewError as exc:
        result = _result(action, "failed", str(exc), preview_id, error_code=exc.code)
        if begun:
            store.finish(preview_id, result)
        return result
    except KTGError as exc:
        outcome = "unknown" if exc.code in {"KTG_CONNECTION_ERROR", "KTG_HTTP_ERROR"} else "failed"
        result = _result(action, outcome, str(exc), preview_id, error_code=exc.code)
        if begun:
            store.finish(preview_id, result)
        return result


async def _apply_create(client: KTGClient, preview_id: str, after: JsonObject) -> ToolResult:
    await _ensure_unique(client, after)
    try:
        await client.post(TOOLTYPE_PATH, {field: after[field] for field in MUTABLE})
    except KTGError as exc:
        if exc.code == "KTG_INVALID_RESPONSE":
            return _result("create", "unknown", "新增请求已发送，但MES响应格式无效；请勿重复提交。",
                           preview_id, error_code="WRITE_RESULT_UNVERIFIED")
        raise
    try:
        rows, total = tooltype_page(await client.get(TOOLTYPE_LIST, {"toolTypeCode": after["toolTypeCode"], "pageNum": 1, "pageSize": 100}))
        matches = [row for row in rows if _same_payload(row, after) and cast(str, row["toolTypeCode"]).casefold() == cast(str, after["toolTypeCode"]).casefold()]
        if total == 1 and len(matches) == 1:
            return _result("create", "succeeded", "工装夹具类型已新增并回读核验。", preview_id, verified=True, created=matches[0])
    except KTGError:
        pass
    return _result("create", "unknown", "MES已接受新增，但无法唯一回读核实；请勿重复提交。", preview_id, error_code="WRITE_RESULT_UNVERIFIED")


async def _apply_update(client: KTGClient, preview_id: str, before: JsonObject | None, after: JsonObject) -> ToolResult:
    if before is None:
        raise PreviewError("PREVIEW_STALE", "预览缺少工装夹具类型快照。")
    await _ensure_unique(client, after, cast(int, before["toolTypeId"]))
    body = {field: after[field] for field in MUTABLE}
    body["toolTypeId"] = before["toolTypeId"]
    try:
        await client.put(TOOLTYPE_PATH, body)
    except KTGError as exc:
        if exc.code == "KTG_INVALID_RESPONSE":
            return _result("update", "unknown", "修改请求已发送，但MES响应格式无效；请勿重复提交。",
                           preview_id, error_code="WRITE_RESULT_UNVERIFIED")
        raise
    try:
        updated = await fetch_tooltype(client, before["toolTypeId"])
        if _same_payload(updated, after):
            return _result("update", "succeeded", "工装夹具类型已修改并回读核验。", preview_id, verified=True, updated=updated)
    except KTGError:
        pass
    return _result("update", "unknown", "MES已接受修改，但回读未核实，请先核对记录。", preview_id, error_code="WRITE_RESULT_UNVERIFIED")


async def _apply_delete(client: KTGClient, preview_id: str, before: JsonObject | None) -> ToolResult:
    if before is None:
        raise PreviewError("PREVIEW_STALE", "预览缺少工装夹具类型快照。")
    try:
        await client.delete(f"{TOOLTYPE_PATH}/{before['toolTypeId']}")
    except KTGError as exc:
        if exc.code == "KTG_INVALID_RESPONSE":
            return _result("delete", "unknown", "删除请求已发送，但MES响应格式无效；请勿重复提交。",
                           preview_id, error_code="WRITE_RESULT_UNVERIFIED")
        raise
    try:
        rows, total = tooltype_page(await client.get(TOOLTYPE_LIST, {
            "toolTypeCode": before["toolTypeCode"], "pageNum": 1, "pageSize": 100,
        }))
        if total == 0 and not rows:
            deleted = {key: before[key] for key in ("toolTypeId", "toolTypeCode", "toolTypeName")}
            return _result("delete", "succeeded", "工装夹具类型已删除并回读核验。", preview_id, verified=True, deleted=deleted)
    except KTGError:
        pass
    return _result("delete", "unknown", "MES已接受删除，但回读未核实；请勿重复提交。", preview_id, error_code="WRITE_RESULT_UNVERIFIED")


def _same_payload(actual: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    return all(actual.get(field) == expected.get(field) for field in MUTABLE)


def _snapshot_matches(current: Mapping[str, object], before: Mapping[str, object]) -> bool:
    return current.get("toolTypeId") == before.get("toolTypeId") and _same_payload(current, before)


def _result(action: str, outcome: str, label: str, preview_id: str, *, error_code: str | None = None,
            verified: bool | None = None, created: JsonObject | None = None,
            updated: JsonObject | None = None, deleted: JsonObject | None = None) -> ToolResult:
    details: JsonObject = {"kind": f"tm_tooltype_{action}_result", "action": action,
                           "outcome": outcome, "label": label, "preview_id": preview_id}
    if verified is not None:
        details["verified"] = verified
    for name, value in (("created", created), ("updated", updated), ("deleted", deleted)):
        if value is not None:
            details[name] = value
    status = ToolStatus.SUCCEEDED if outcome == "succeeded" else ToolStatus.UNKNOWN if outcome == "unknown" else ToolStatus.FAILED
    return ToolResult(content=label, details=details, status=status,
                      error_code=None if status is ToolStatus.SUCCEEDED else error_code or "WRITE_FAILED",
                      operation_id=preview_id)


def _permissions(action: str) -> tuple[str, ...]:
    extras = {"create": ("mes:tm:tooltype:list",),
              "update": ("mes:tm:tooltype:query", "mes:tm:tooltype:list"),
              "delete": ("mes:tm:tooltype:query", "mes:tm:tooltype:list", "mes:tm:tool:list")}
    return (_ACTIONS[action][2], *extras[action])


def _action_for_name(name: str) -> str:
    for action, (preview, apply, _) in _ACTIONS.items():
        if name in {preview, apply}:
            return action
    raise ValueError("unknown tooltype action")


_PREVIEW_DESCRIPTIONS = {
    "create": "生成单条工装夹具类型新增预览，须经网页确认后才写入。须传唯一类型编码、唯一名称和编码管理值。"
              "code_flag=Y 时必须传维护方式和周期，N 时省略维护字段。编码由用户提供。",
    "update": "生成单条工装夹具类型修改预览，须经网页确认后才写入。按 tool_type_id 定位，只传需要改变的类型字段。"
              "省略字段沿用现有值；合并后 code_flag=Y 时须有合法维护方式和周期，N 时自动清空维护字段。",
    "delete": "生成单条工装夹具类型删除预览，须经网页确认后才写入。按 tool_type_id 定位；"
              "预览和确认时均检查关联工装台账，有关联时拒绝删除。",
}


def _identity_key(identity: object | None) -> str:
    if not isinstance(identity, Mapping):
        raise PreviewError("AUTH_INVALID", "KTG 未返回有效用户身份。")
    user_id = identity.get("userId")
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        return f"id:{user_id}{identity_scope(identity)}"
    username = identity.get("userName")
    if isinstance(username, str) and username.strip():
        return f"name:{username.strip()}{identity_scope(identity)}"
    raise PreviewError("AUTH_INVALID", "KTG 未返回有效用户身份。")


def _action_label(action: str) -> str:
    return {"create": "新增", "update": "修改", "delete": "删除"}[action]


def _client(context: ToolContext) -> KTGClient | None:
    return context.ktg_client if isinstance(context.ktg_client, KTGClient) else None


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code)
