"""Preview/confirmation tools for single-device ledger changes."""

from __future__ import annotations

from ktg_agent.identity_scope import identity_scope

from collections.abc import Iterator, Mapping
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
from ktg_agent.ktg.dv_subject import (FIELDS, TYPE_VALUES, ENABLE_VALUES, SUBJECT_PATH,
    fetch_subject, project_subject, subject_page, subject_value)
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolChannel, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)


PREVIEW_CREATE = "ktg_preview_dv_subject_create"
PREVIEW_UPDATE = "ktg_preview_dv_subject_update"
PREVIEW_DELETE = "ktg_preview_dv_subject_delete"
APPLY_CREATE = "ktg_dv_subject_create"
APPLY_UPDATE = "ktg_dv_subject_update"
APPLY_DELETE = "ktg_dv_subject_delete"

_SUBJECT_PATH = SUBJECT_PATH
_SUBJECT_LIST = f"{SUBJECT_PATH}/list"
_ACTIONS = {
    "create": (PREVIEW_CREATE, APPLY_CREATE, "mes:dv:dvsubject:add"),
    "update": (PREVIEW_UPDATE, APPLY_UPDATE, "mes:dv:dvsubject:edit"),
    "delete": (PREVIEW_DELETE, APPLY_DELETE, "mes:dv:dvsubject:remove"),
}
_MUTABLE = tuple(field for field, _limit in FIELDS.values())


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


class DvSubjectPreviewStore:
    """User-bound durable previews with one-shot write execution state."""

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
            db.execute("""CREATE TABLE IF NOT EXISTS dv_subject_previews (
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
        value = json.dumps({"action": action, "before": before, "after": after},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def create(self, *, user_key: str, action: str, before: JsonObject | None, after: JsonObject) -> str:
        preview_id = str(uuid4())
        digest = self._digest(action, before, after)
        with self._lock, self._connect() as db:
            now = time()
            db.execute("DELETE FROM dv_subject_previews WHERE expires_at<=? AND state='pending'", (now,))
            db.execute("""INSERT INTO dv_subject_previews
                (preview_id,user_key,action,before_json,after_json,digest,expires_at,state,result_json)
                VALUES (?,?,?,?,?,?,?,'pending',NULL)""",
                (preview_id, user_key, action, self._encode(before) if before is not None else None,
                 self._encode(after), digest, now + self._ttl_seconds))
        return preview_id

    def action_for(self, *, preview_id: str, user_key: str) -> str:
        record = self._read(preview_id)
        if record is None:
            raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在，请重新生成。")
        if record.user_key != user_key:
            raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
        if record.state != "completed" and record.expires_at <= time():
            raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
        return record.action

    def begin(self, *, preview_id: str, user_key: str, action: str) -> tuple[_Preview, ToolResult | None]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM dv_subject_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if row is None:
                raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在，请重新生成。")
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
                db.execute("DELETE FROM dv_subject_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
            db.execute("UPDATE dv_subject_previews SET state='executing' WHERE preview_id=? AND state='pending'",
                       (preview_id,))
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        encoded = self._encode({"content": result.content, "details": result.details,
                                "status": result.status.value, "error_code": result.error_code,
                                "operation_id": result.operation_id, "verification": result.verification})
        with self._lock, self._connect() as db:
            db.execute("UPDATE dv_subject_previews SET state='completed',result_json=? WHERE preview_id=?",
                       (encoded, preview_id))

    def _read(self, preview_id: str) -> _Preview | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM dv_subject_previews WHERE preview_id=?", (preview_id,)).fetchone()
            return None if row is None else self._record(row)

    def _record(self, row: sqlite3.Row) -> _Preview:
        before = cast(JsonObject | None, json.loads(row["before_json"])) if row["before_json"] else None
        after = cast(JsonObject, json.loads(row["after_json"]))
        if self._digest(row["action"], before, after) != row["digest"]:
            raise PreviewError("PREVIEW_STALE", "预览内容已变化，请重新生成。")
        result = None
        if row["result_json"]:
            value = json.loads(row["result_json"])
            result = ToolResult(content=value["content"], details=value["details"],
                                status=ToolStatus(value["status"]), error_code=value["error_code"],
                                operation_id=value["operation_id"], verification=value["verification"])
        return _Preview(row["preview_id"], row["user_key"], row["action"], before, after,
                        row["digest"], row["expires_at"], row["state"], result)


def build_dv_subject_write_tools(store: DvSubjectPreviewStore) -> tuple[ToolDefinition, ...]:
    return tuple(_definition(store, action, hidden=hidden)
                 for action in ("create", "update", "delete") for hidden in (False, True))


def dv_subject_write_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
    def create_factory(item: ToolDefinition) -> ToolFactory:
        return lambda: item
    return {f"dv_writes:{definition.name}": create_factory(definition) for definition in definitions}


def dv_subject_write_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
    permission: dict[str, PermissionPolicy] = {
        action: PermissionAll(tuple(PermissionLeaf(p) for p in _permissions(action)))
        for action in _ACTIONS
    }
    return tuple(ToolManifest(
        name=item.name, version=item.version, module="dv", domain="subject",
        label=item.label, description=item.description,
        permission_policy=permission[_action_for_name(item.name)], effect=item.effect,
        execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=item.expose_to_model,
        confirmation_policy="none" if item.expose_to_model else "trusted_confirmation",
        proposal_store_effect=item.name.startswith("ktg_preview_"),
        handler_ref=f"dv_writes:{item.name}") for item in definitions)


def _definition(store: DvSubjectPreviewStore, action: str, *, hidden: bool) -> ToolDefinition:
    preview_name, apply_name, permission = _ACTIONS[action]
    name = apply_name if hidden else preview_name
    if hidden:
        schema = {"type": "object", "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 100}},
                  "required": ["operation_id"], "additionalProperties": False}
        description = f"受信确认通道专用：执行已确认的项目{_action_label(action)}预览，模型不可见。"
        label = f"确认{_action_label(action)}项目"
    else:
        schema = _preview_schema(action)
        description = _PREVIEW_DESCRIPTIONS[action]
        label = f"预览{_action_label(action)}项目"

    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        if hidden:
            return await _apply(store, action, context, arguments)
        return await _preview(store, action, context, arguments)

    required = _permissions(action)
    return ToolDefinition(
        name=name, version="1.0", label=label, description=description,
        input_schema=cast(JsonObject, schema),
        output_schema={"type": "object", "properties": {"kind": {}, "action": {}, "preview_id": {},
                             "before": {}, "after": {}, "warnings": {}, "outcome": {}, "verified": {}},
                       "additionalProperties": True},
        required_permissions=tuple(dict.fromkeys(required)),
        effect=ToolEffect.WRITE if hidden else ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER, timeout_seconds=30, execute=execute,
        expose_to_model=not hidden,
    )


def _action_for_name(name: str) -> str:
    for action, (preview, apply, _permission) in _ACTIONS.items():
        if name in {preview, apply}:
            return action
    raise ValueError("unknown subject tool")


def _permissions(action: str) -> tuple[str, ...]:
    extra = ("mes:dv:dvsubject:list",) if action == "create" else (
        "mes:dv:dvsubject:query", "mes:dv:dvsubject:list") if action == "update" else ("mes:dv:dvsubject:query",)
    return (_ACTIONS[action][2], *extra)


_PREVIEW_DESCRIPTIONS = {
    "create": "生成点检保养项目台账新增预览，网页确认后才写入。必须提供业务编码、类型和内容；名称可选，编码由用户提供。",
    "update": "生成点检保养项目台账修改预览，网页确认后才写入。按项目系统 ID 定位，只传要修改的字段。"
              "已有计划保留关联快照，确认时会复核原值和编码冲突。",
    "delete": "生成单条点检保养项目台账删除预览，网页确认后才写入。已有计划仍保留项目快照，删除源项目可能留下引用。",
}


def _preview_schema(action: str) -> dict[str, object]:
    properties: dict[str, JsonValue] = {}
    if action != "create":
        properties["subject_id"] = {"type": "integer", "minimum": 1,
                                    "description": "项目台账记录的系统 ID，取自项目查询的 subjectId。"}
    if action != "delete":
        field_descriptions = {
            "subject_code": "项目台账的唯一业务编码。",
            "subject_name": "项目名称，可传空字符串。",
            "subject_content": "实际点检或保养的内容，传非空文本。",
            "subject_standard": "项目合格标准，可传空字符串。",
            "remark": "项目备注，可传空字符串。",
        }
        for key, (_wire, limit) in FIELDS.items():
            field_schema: JsonObject = {"type": "string", "maxLength": limit}
            if key in field_descriptions:
                field_schema["description"] = field_descriptions[key]
            properties[key] = field_schema
        properties["subject_type"] = {"type": "string", "enum": cast(JsonValue, sorted(TYPE_VALUES)),
                                      "description": "CHECK 为点检，MAINTEN 为保养。"}
        enable_flag: JsonObject = {"type": "string", "enum": cast(JsonValue, sorted(ENABLE_VALUES)),
                                   "description": "Y 为启用，N 为停用。省略时"
                                                  + ("默认启用。" if action == "create" else "保留原状态。")}
        if action == "create":
            enable_flag["default"] = "Y"
        properties["enable_flag"] = enable_flag
    required = ["subject_code", "subject_type", "subject_content"] if action == "create" else ["subject_id"]
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


async def _preview(store: DvSubjectPreviewStore, action: str, context: ToolContext,
                   arguments: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        user_key = _identity_key(context.identity)
        allowed = set(cast(dict[str, JsonValue], _preview_schema(action)["properties"]))
        if set(arguments) - allowed:
            raise KTGError("VALIDATION_ERROR", "包含未支持的项目字段。")
        if action == "create":
            before, after = None, await _create_payload(client, arguments)
        elif action == "update":
            before, after = await _update_payload(client, arguments)
        else:
            before = await fetch_subject(client, arguments.get("subject_id"))
            after = {key: before[key] for key in ("subjectId", "subjectCode", "subjectName")}
        preview_id = store.create(user_key=user_key, action=action, before=before, after=after)
        warnings = (["已有计划保存快照，修改源项目不会同步历史明细；删除源项目可能留下引用，请确认关联影响。"]
                    if action == "delete" else ["已有计划快照不会自动同步此次源项目变更。"] if action == "update" else [])
        details: JsonObject = {"kind": "dv_subject_preview", "action": action,
                               "preview_id": preview_id, "before": before, "after": after,
                               "warnings": cast(list[JsonValue], warnings)}
        return ToolResult(content=f"项目{_action_label(action)}预览已生成，请用户检查并在卡片中确认。",
                          details=details, operation_id=preview_id)
    except (KTGError, PreviewError) as exc:
        code = exc.code if isinstance(exc, (KTGError, PreviewError)) else "VALIDATION_ERROR"
        return _failure(str(exc), code)


async def _create_payload(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    after: JsonObject = {}
    for key, (field, _limit) in FIELDS.items():
        required = key in {"subject_code", "subject_type", "subject_content"}
        value = arguments.get(key, "Y" if key == "enable_flag" else "")
        after[field] = subject_value(key, value, required=required)
    if await _code_exists(client, cast(str, after["subjectCode"])):
        raise KTGError("SUBJECT_CODE_EXISTS", "项目编码已存在，请明确提供唯一编码。")
    return after


async def _update_payload(client: KTGClient, arguments: Mapping[str, object]) -> tuple[JsonObject, JsonObject]:
    before = await fetch_subject(client, arguments.get("subject_id"))
    after = dict(before)
    changed = []
    for key, (field, _limit) in FIELDS.items():
        if key in arguments:
            after[field] = subject_value(key, arguments[key], required=key in {"subject_code", "subject_type", "subject_content"})
            if after[field] != before[field]:
                changed.append(field)
    if not changed:
        raise KTGError("NO_CHANGE", "没有提供发生变化的项目字段。")
    if await _code_exists(client, cast(str, after["subjectCode"]), cast(int, before["subjectId"])):
        raise KTGError("SUBJECT_CODE_EXISTS", "编码与其他项目冲突（含历史重复码），请用户决定，不会自动换码。")
    after["changedFields"] = cast(JsonValue, changed)
    return before, after


async def _code_exists(client: KTGClient, code: str, exclude_id: int | None = None) -> bool:
    rows, total = await _find_by_code(client, code)
    return total > len(rows) or any(row["subjectId"] != exclude_id for row in rows)


async def _apply(store: DvSubjectPreviewStore, action: str, context: ToolContext,
                 arguments: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    if context.channel is not ToolChannel.TRUSTED_CONFIRMATION:
        return _failure("点检保养项目写入只能通过用户确认执行。", "CONFIRMATION_REQUIRED")
    preview_id = arguments.get("operation_id")
    if not isinstance(preview_id, str) or not preview_id.strip() or len(preview_id) > 100:
        return _failure("operation_id 无效。", "VALIDATION_ERROR")
    if context.operation_id != preview_id or context.confirmation_id is None:
        return _failure("确认上下文与预览不匹配。", "CONFIRMATION_MISMATCH")
    begun = False
    try:
        user_key = _identity_key(context.identity)
        record, cached = store.begin(preview_id=preview_id, user_key=user_key, action=action)
        if cached is not None:
            return cached
        begun = True
        if record.before is not None:
            current = await fetch_subject(client, record.before["subjectId"])
            if not _snapshot_matches(current, record.before):
                raise PreviewError("PREVIEW_STALE", "项目信息已变化，请重新生成预览。")
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
    code = cast(str, after["subjectCode"])
    if await _code_exists(client, code):
        return _result("create", "failed", "编码在确认前已被占用，请重新预览。", preview_id, error_code="SUBJECT_CODE_EXISTS")
    await client.post(_SUBJECT_PATH, {key: after[key] for key in _MUTABLE})
    try:
        rows, total = await _find_by_code(client, code)
        matching = [r for r in rows if _created_matches(r, after)]
        if total == 1 and len(matching) == 1:
            return _result("create", "succeeded", "项目已新增并回读核验。", preview_id, verified=True, created=matching[0])
    except KTGError:
        pass
    return _result("create", "unknown", "MES已接受新增，但回读无法唯一核实；请勿重复提交。", preview_id, error_code="WRITE_RESULT_UNVERIFIED")


async def _apply_update(client: KTGClient, preview_id: str, before: JsonObject | None, after: JsonObject) -> ToolResult:
    if before is None:
        raise PreviewError("PREVIEW_STALE", "预览缺少项目快照。")
    sid = cast(int, before["subjectId"])
    if await _code_exists(client, cast(str, after["subjectCode"]), sid):
        return _result("update", "failed", "编码与其他项目冲突，请重新预览。", preview_id, error_code="SUBJECT_CODE_EXISTS")
    changed = cast(list[str], after["changedFields"])
    body: dict[str, object] = {"subjectId": sid, "subjectCode": after["subjectCode"]}
    body.update({field: after[field] for field in changed})
    await client.put(_SUBJECT_PATH, body)
    try:
        updated = await fetch_subject(client, sid)
        if all(updated.get(f) == after.get(f) for f in changed):
            return _result("update", "succeeded", "项目已修改并回读核验。", preview_id, verified=True, updated=updated)
    except KTGError:
        pass
    return _result("update", "unknown", "MES已接受修改，但回读未核实，请先核对项目。", preview_id, error_code="WRITE_RESULT_UNVERIFIED")


async def _apply_delete(client: KTGClient, preview_id: str, before: JsonObject | None) -> ToolResult:
    if before is None:
        raise PreviewError("PREVIEW_STALE", "预览缺少项目快照。")
    sid = cast(int, before["subjectId"])
    await client.delete(f"{_SUBJECT_PATH}/{sid}")
    try:
        payload = await client.get(f"{_SUBJECT_PATH}/{sid}")
        if payload.get("data") is None:
            return _result("delete", "succeeded", "项目已删除并回读核验；未清理既有计划快照。", preview_id,
                verified=True, deleted={key: before[key] for key in ("subjectId", "subjectCode", "subjectName")})
    except KTGError:
        pass
    return _result("delete", "unknown", "MES已接受删除，但回读未核实；请勿重复提交。", preview_id, error_code="WRITE_RESULT_UNVERIFIED")


async def _find_by_code(client: KTGClient, code: str) -> tuple[list[JsonObject], int]:
    payload = await client.get(_SUBJECT_LIST, {"subjectCode": code, "pageNum": 1, "pageSize": 100})
    return subject_page(payload)


def _created_matches(row: JsonObject, expected: JsonObject) -> bool:
    return all((row.get(field) or "") == (expected.get(field) or "") for field in _MUTABLE)


def _snapshot_matches(current: JsonObject, before: JsonObject) -> bool:
    return all(current.get(key) == before.get(key) for key in _MUTABLE + ("subjectId", "subjectCode"))


def _result(action: str, outcome: str, label: str, preview_id: str, *,
            error_code: str | None = None, verified: bool | None = None,
            created: JsonObject | None = None, updated: JsonObject | None = None,
            deleted: JsonObject | None = None) -> ToolResult:
    details: JsonObject = {"kind": f"dv_subject_{action}_result", "action": action,
                           "outcome": outcome, "label": label, "preview_id": preview_id}
    if verified is not None:
        details["verified"] = verified
    if created is not None:
        details["created"] = created
    if updated is not None:
        details["updated"] = updated
    if deleted is not None:
        details["deleted"] = deleted
    return ToolResult(content=label, details=details,
                      status=ToolStatus.SUCCEEDED if outcome == "succeeded" else
                      ToolStatus.UNKNOWN if outcome == "unknown" else ToolStatus.FAILED,
                      error_code=None if outcome == "succeeded" else error_code or "WRITE_FAILED",
                      operation_id=preview_id)


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
