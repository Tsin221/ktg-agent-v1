"""Preview/confirmation write tools for equipment classification.

Writes are only reachable through the trusted confirmation channel: the model
sees a preview tool, the browser confirms, and a hidden write tool executes.
There is no delete tool and no parent-change tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from time import time
from threading import Lock
from typing import Iterator, cast
from uuid import uuid4

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_machinerytype import load_machinery_types, path_of
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import (
    ExecutionMode,
    RetryPolicy,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolStatus,
)


PREVIEW_CREATE = "ktg_preview_dv_machinerytype_create"
PREVIEW_UPDATE = "ktg_preview_dv_machinerytype_update"
APPLY_CREATE = "ktg_dv_machinerytype_create"
APPLY_UPDATE = "ktg_dv_machinerytype_update"

_CREATE_PATH = "/mes/dv/machinerytype"

_CREATE_DESCRIPTION = (
    "为“新增设备分类”生成预览，不会写入数据库。必须先通过 tree 找到已存在的父分类，"
    "不支持新建根分类；名称必填（1-100 字），同级重名会被拒绝；编码由 MES 在最终确认时自动生成，"
    "预览中不会给出具体编码。生成后必须由用户在网页点击确认，模型不得声称已经新增。"
)
_UPDATE_DESCRIPTION = (
    "为“修改设备分类”生成预览，不会写入数据库。可修改名称、是否启用、备注，至少提供一项；"
    "父分类不可修改。改名会校验同级重名。生成后必须由用户在网页点击确认才会保存。"
)
_APPLY_CREATE_DESCRIPTION = "受信确认通道专用：执行已确认的新增设备分类预览；模型不可见。"
_APPLY_UPDATE_DESCRIPTION = "受信确认通道专用：执行已确认的修改设备分类预览；模型不可见。"


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


class DvMachineryTypePreviewStore:
    """SQLite-backed previews bound to a user; never auto-retries a write."""

    def __init__(self, *, ttl_seconds: float = 600, path: str | Path = ":memory:") -> None:
        if ttl_seconds <= 0:
            raise ValueError("preview ttl must be positive")
        self._ttl_seconds = ttl_seconds
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._memory_db = (
            sqlite3.connect(":memory:", timeout=10, check_same_thread=False)
            if self.path == ":memory:" else None
        )
        if self._memory_db is not None:
            self._memory_db.row_factory = sqlite3.Row
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dv_machinerytype_previews (
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
        payload = {"action": action, "before": before, "after": after}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _result_json(result: ToolResult | None) -> str | None:
        if result is None:
            return None
        return DvMachineryTypePreviewStore._encode({
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

    def _record(self, row: sqlite3.Row) -> _Preview:
        before = cast(JsonObject | None, json.loads(row["before_json"])) if row["before_json"] else None
        after = cast(JsonObject, json.loads(row["after_json"]))
        if self._digest(row["action"], before, after) != row["digest"]:
            raise PreviewError("PREVIEW_STALE", "预览内容已变化，请重新生成。")
        return _Preview(
            preview_id=row["preview_id"], user_key=row["user_key"], action=row["action"],
            before=before, after=after, digest=row["digest"],
            expires_at=row["expires_at"], state=row["state"],
            result=self._result(row["result_json"]),
        )

    def create(self, *, user_key: str, action: str, before: JsonObject | None,
               after: JsonObject) -> str:
        preview_id = str(uuid4())
        digest = self._digest(action, before, after)
        with self._lock, self._connect() as db:
            now = time()
            db.execute(
                "DELETE FROM dv_machinerytype_previews WHERE expires_at<=? AND state='pending'",
                (now,),
            )
            db.execute(
                """INSERT INTO dv_machinerytype_previews (
                    preview_id, user_key, action, before_json, after_json,
                    digest, expires_at, state, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL)""",
                (preview_id, user_key, action,
                 self._encode(before) if before is not None else None,
                 self._encode(after), digest, now + self._ttl_seconds),
            )
        return preview_id

    def action_for(self, *, preview_id: str, user_key: str) -> str:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM dv_machinerytype_previews WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if row is None:
                raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在，请重新生成。")
            if row["user_key"] != user_key:
                raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if row["state"] != "completed" and row["expires_at"] <= time():
                db.execute("DELETE FROM dv_machinerytype_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
            return cast(str, row["action"])

    def begin(self, *, preview_id: str, user_key: str, action: str
              ) -> tuple[_Preview, ToolResult | None]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM dv_machinerytype_previews WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if row is None:
                raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在，请重新生成。")
            record = self._record(row)
            if record.user_key != user_key:
                raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if record.action != action:
                raise PreviewError("PREVIEW_ACTION_MISMATCH", "预览操作类型不匹配。")
            if record.state == "completed":
                if record.result is None:
                    raise PreviewError("PREVIEW_STALE", "预览状态异常，请重新生成。")
                return record, record.result
            if record.state == "executing":
                raise PreviewError("PREVIEW_IN_PROGRESS", "该操作正在执行，请勿重复提交。")
            if record.expires_at <= time():
                db.execute("DELETE FROM dv_machinerytype_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
            db.execute(
                "UPDATE dv_machinerytype_previews SET state='executing' WHERE preview_id=?",
                (preview_id,),
            )
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE dv_machinerytype_previews SET state='completed', result_json=? WHERE preview_id=?",
                (self._result_json(result), preview_id),
            )


def build_dv_machinerytype_write_tools(
    store: DvMachineryTypePreviewStore,
) -> tuple[ToolDefinition, ...]:
    return (
        _preview_create_definition(store),
        _apply_create_definition(store),
        _preview_update_definition(store),
        _apply_update_definition(store),
    )


def dv_machinerytype_write_factories(
    definitions: tuple[ToolDefinition, ...],
) -> dict[str, ToolFactory]:
    def factory(definition: ToolDefinition) -> ToolFactory:
        def resolve() -> ToolDefinition:
            return definition

        return resolve

    return {f"dv_writes:{definition.name}": factory(definition) for definition in definitions}


def dv_machinerytype_write_manifests(
    definitions: tuple[ToolDefinition, ...],
) -> tuple[ToolManifest, ...]:
    policies = {
        PREVIEW_CREATE: PermissionAll((
            PermissionLeaf("mes:dv:machinerytype:add"),
            PermissionLeaf("mes:dv:machinerytype:list"),
        )),
        APPLY_CREATE: PermissionAll((
            PermissionLeaf("mes:dv:machinerytype:add"),
            PermissionLeaf("mes:dv:machinerytype:list"),
        )),
        PREVIEW_UPDATE: PermissionAll((
            PermissionLeaf("mes:dv:machinerytype:edit"),
            PermissionLeaf("mes:dv:machinerytype:query"),
            PermissionLeaf("mes:dv:machinerytype:list"),
        )),
        APPLY_UPDATE: PermissionAll((
            PermissionLeaf("mes:dv:machinerytype:edit"),
            PermissionLeaf("mes:dv:machinerytype:query"),
            PermissionLeaf("mes:dv:machinerytype:list"),
        )),
    }
    return tuple(
        ToolManifest(
            name=definition.name, version=definition.version, module="dv",
            domain="machinery_type", label=definition.label,
            description=definition.description, permission_policy=policies[definition.name],
            effect=definition.effect, execution_mode=definition.execution_mode,
            retry_policy=definition.retry_policy, timeout_seconds=definition.timeout_seconds,
            expose_to_model=definition.expose_to_model,
            confirmation_policy=("none" if definition.expose_to_model else "trusted_confirmation"),
            proposal_store_effect=definition.name.startswith("ktg_preview_"),
            handler_ref=f"dv_writes:{definition.name}",
        )
        for definition in definitions
    )


def _preview_create_definition(store: DvMachineryTypePreviewStore) -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _preview_create(store, context, arguments)

    return ToolDefinition(
        name=PREVIEW_CREATE, version="1.0", label="预览新增设备分类",
        description=_CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "parent_type_id": {
                    "type": "integer", "minimum": 1,
                    "description": "父分类 ID，必须已存在；先用 tree 查到，不支持新建根分类",
                },
                "machinery_type_name": {
                    "type": "string", "minLength": 1, "maxLength": 100,
                    "description": "新分类名称，不能与同父分类下已有名称重复",
                },
                "enable_flag": {
                    "type": "string", "enum": ["Y", "N"], "default": "Y",
                    "description": "是否启用，默认 Y",
                },
                "remark": {"type": "string", "maxLength": 250, "description": "备注，可选"},
            },
            "required": ["parent_type_id", "machinery_type_name"],
            "additionalProperties": False,
        },
        output_schema=_output_schema(("kind", "action", "preview_id", "before", "after", "warnings")),
        required_permissions=("mes:dv:machinerytype:add", "mes:dv:machinerytype:list"),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER, timeout_seconds=20, execute=execute,
    )


def _preview_update_definition(store: DvMachineryTypePreviewStore) -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _preview_update(store, context, arguments)

    return ToolDefinition(
        name=PREVIEW_UPDATE, version="1.0", label="预览修改设备分类",
        description=_UPDATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "machinery_type_id": {
                    "type": "integer", "minimum": 1, "description": "要修改的分类 ID",
                },
                "machinery_type_name": {
                    "type": "string", "minLength": 1, "maxLength": 100,
                    "description": "新名称；不传=不改",
                },
                "enable_flag": {
                    "type": "string", "enum": ["Y", "N"], "description": "新启用状态；不传=不改",
                },
                "remark": {"type": "string", "maxLength": 250, "description": "新备注；不传=不改"},
            },
            "required": ["machinery_type_id"],
            "anyOf": [
                {"required": ["machinery_type_name"]},
                {"required": ["enable_flag"]},
                {"required": ["remark"]},
            ],
            "additionalProperties": False,
        },
        output_schema=_output_schema(("kind", "action", "preview_id", "before", "after", "warnings")),
        required_permissions=("mes:dv:machinerytype:edit", "mes:dv:machinerytype:query",
                              "mes:dv:machinerytype:list"),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER, timeout_seconds=20, execute=execute,
    )


def _apply_create_definition(store: DvMachineryTypePreviewStore) -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _apply_create(store, context, arguments)

    return ToolDefinition(
        name=APPLY_CREATE, version="1.0", label="执行新增设备分类",
        description=_APPLY_CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["operation_id"], "additionalProperties": False,
        },
        output_schema=_output_schema(("kind", "outcome", "preview_id", "created", "verified", "label")),
        required_permissions=("mes:dv:machinerytype:add", "mes:dv:machinerytype:list"),
        effect=ToolEffect.WRITE, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.RECONCILE_FIRST, timeout_seconds=20,
        execute=execute, expose_to_model=False,
    )


def _apply_update_definition(store: DvMachineryTypePreviewStore) -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await _apply_update(store, context, arguments)

    return ToolDefinition(
        name=APPLY_UPDATE, version="1.0", label="执行修改设备分类",
        description=_APPLY_UPDATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["operation_id"], "additionalProperties": False,
        },
        output_schema=_output_schema(("kind", "outcome", "preview_id", "updated", "verified", "label")),
        required_permissions=("mes:dv:machinerytype:edit", "mes:dv:machinerytype:query",
                              "mes:dv:machinerytype:list"),
        effect=ToolEffect.WRITE, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.RECONCILE_FIRST, timeout_seconds=20,
        execute=execute, expose_to_model=False,
    )


async def _preview_create(
    store: DvMachineryTypePreviewStore, context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    if set(arguments) - {"parent_type_id", "machinery_type_name", "enable_flag", "remark"}:
        return _failure("包含未支持的字段。", "VALIDATION_ERROR")
    client = _client(context)
    if client is None:
        return _failure("KTG 客户端未配置。", "KTG_CLIENT_MISSING")
    parent_id = _positive_id(arguments.get("parent_type_id"), "parent_type_id")
    if isinstance(parent_id, ToolResult):
        return parent_id
    name = _required_text(arguments.get("machinery_type_name"), "machinery_type_name", 100)
    if isinstance(name, ToolResult):
        return name
    enable_flag = _enable_flag(arguments.get("enable_flag", "Y"))
    if isinstance(enable_flag, ToolResult):
        return enable_flag
    remark = _optional_text(arguments.get("remark", ""), "remark", 250)
    if isinstance(remark, ToolResult):
        return remark
    try:
        nodes = await load_machinery_types(client)
    except KTGError as exc:
        return _failure(str(exc), exc.code)
    by_id = {cast(int, node["machineryTypeId"]): node for node in nodes}
    parent = by_id.get(parent_id)
    if parent is None:
        return _failure(f"父分类 ID {parent_id} 不存在，请先用 tree 查找。", "PARENT_NOT_FOUND")
    duplicate = _duplicate_siblings(nodes, parent_id, name)
    if duplicate:
        return _failure(
            "同级下已存在名称为「" + name + "」的设备分类：" + _names_text(duplicate),
            "DUPLICATE_NAME",
        )
    parent_path, _ = path_of(parent, by_id)
    after: JsonObject = {
        "parentTypeId": parent_id,
        "parentPath": parent_path,
        "machineryTypeName": name,
        "enableFlag": enable_flag,
        "remark": remark,
    }
    preview_id = store.create(
        user_key=_user_key(context), action="create", before=None, after=after
    )
    details: JsonObject = {
        "kind": "dv_machinerytype_preview", "action": "create",
        "preview_id": preview_id, "before": None, "after": after,
        "warnings": ["编码将在确认时由系统生成", "确认前不会写入"],
    }
    return ToolResult(
        content=(
            f"已生成新增预览：在「{parent_path}」下新增「{name}」，"
            f"{'启用' if enable_flag == 'Y' else '停用'}；"
            "编码将在确认时由系统生成，请在网页确认后才会真正写入。"
        ),
        details=details, operation_id=preview_id,
    )


async def _preview_update(
    store: DvMachineryTypePreviewStore, context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    if set(arguments) - {"machinery_type_id", "machinery_type_name", "enable_flag", "remark"}:
        return _failure("包含未支持的字段。", "VALIDATION_ERROR")
    if not any(key in arguments for key in ("machinery_type_name", "enable_flag", "remark")):
        return _failure("至少需要提供名称、启用状态或备注中的一项。", "VALIDATION_ERROR")
    client = _client(context)
    if client is None:
        return _failure("KTG 客户端未配置。", "KTG_CLIENT_MISSING")
    type_id = _positive_id(arguments.get("machinery_type_id"), "machinery_type_id")
    if isinstance(type_id, ToolResult):
        return type_id
    try:
        nodes = await load_machinery_types(client)
    except KTGError as exc:
        return _failure(str(exc), exc.code)
    by_id = {cast(int, node["machineryTypeId"]): node for node in nodes}
    current = by_id.get(type_id)
    if current is None:
        return _failure(f"未找到 ID 为 {type_id} 的设备分类。", "TYPE_NOT_FOUND")
    current_path, _ = path_of(current, by_id)
    before: JsonObject = {
        "machineryTypeId": current["machineryTypeId"],
        "machineryTypeCode": current["machineryTypeCode"],
        "machineryTypeName": current["machineryTypeName"],
        "parentTypeId": current["parentTypeId"],
        "enableFlag": current["enableFlag"],
        "remark": current["remark"],
        "path": current_path,
    }
    after = dict(before)
    if "machinery_type_name" in arguments:
        name = _required_text(arguments.get("machinery_type_name"), "machinery_type_name", 100)
        if isinstance(name, ToolResult):
            return name
        after["machineryTypeName"] = name
    if "enable_flag" in arguments:
        enable_flag = _enable_flag(arguments.get("enable_flag"))
        if isinstance(enable_flag, ToolResult):
            return enable_flag
        after["enableFlag"] = enable_flag
    if "remark" in arguments:
        remark = _optional_text(arguments.get("remark"), "remark", 250)
        if isinstance(remark, ToolResult):
            return remark
        after["remark"] = remark
    if _same_changes(before, after):
        return _failure("名称、启用状态和备注都没有变化，无需修改。", "NO_CHANGE")
    if after["machineryTypeName"] != before["machineryTypeName"]:
        duplicate = _duplicate_siblings(
            nodes, cast(int, current["parentTypeId"]), str(after["machineryTypeName"]),
            exclude_id=type_id,
        )
        if duplicate:
            return _failure(
                "同级下已存在名称为「" + str(after["machineryTypeName"]) + "」的设备分类："
                + _names_text(duplicate),
                "DUPLICATE_NAME",
            )
        parent = by_id.get(cast(int, current["parentTypeId"]))
        parent_path = path_of(parent, by_id)[0] if parent is not None else ""
        after["path"] = f"{parent_path}/{after['machineryTypeName']}" if parent_path else str(after["machineryTypeName"])
    preview_id = store.create(
        user_key=_user_key(context), action="update", before=before, after=after
    )
    details: JsonObject = {
        "kind": "dv_machinerytype_preview", "action": "update",
        "preview_id": preview_id, "before": before, "after": after,
        "warnings": ["父分类不可修改", "停用只是标志位，当前不会阻止设备选用", "删除功能已停用"],
    }
    return ToolResult(
        content=(
            f"已生成修改预览：「{before['machineryTypeName']}」→「{after['machineryTypeName']}」，"
            f"启用状态 {before['enableFlag']} → {after['enableFlag']}；"
            "请在网页确认后才会保存。"
        ),
        details=details, operation_id=preview_id,
    )


async def _apply_create(
    store: DvMachineryTypePreviewStore, context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    operation_id = _operation_id(arguments, context)
    if isinstance(operation_id, ToolResult):
        return operation_id
    try:
        record, cached = store.begin(
            preview_id=operation_id, user_key=_user_key(context), action="create"
        )
    except PreviewError as exc:
        return _failure(str(exc), exc.code)
    if cached is not None:
        return cached
    client = _client(context)
    if client is None:
        return _finish(store, operation_id, _failure("KTG 客户端未配置。", "KTG_CLIENT_MISSING"))
    after = record.after
    parent_id = cast(int, after["parentTypeId"])
    name = str(after["machineryTypeName"])
    try:
        nodes = await load_machinery_types(client)
    except KTGError as exc:
        return _finish(store, operation_id, _failure(str(exc), exc.code))
    parent = next((node for node in nodes if node["machineryTypeId"] == parent_id), None)
    if parent is None:
        return _finish(store, operation_id, _failure(
            f"父分类 ID {parent_id} 已不存在，请重新预览。", "PARENT_NOT_FOUND"))
    if _duplicate_siblings(nodes, parent_id, name):
        return _finish(store, operation_id, _failure(
            "同级下已出现同名分类，预览已失效，请重新预览。", "PREVIEW_STALE"))
    try:
        await client.post(_CREATE_PATH, {
            "parentTypeId": parent_id,
            "machineryTypeName": name,
            "enableFlag": after["enableFlag"],
            "remark": after["remark"],
        })
    except KTGError as exc:
        return _finish(store, operation_id, _write_error(exc, "create"))
    try:
        nodes = await load_machinery_types(client)
    except KTGError:
        return _finish(store, operation_id, _unknown(
            "新增请求已发出，但回读失败，可能已经写入；请按名称核对，不要重复提交。",
            {"kind": "dv_machinerytype_create_result", "outcome": "unknown",
             "preview_id": operation_id},
        ))
    created = _find_created(nodes, parent_id, name)
    if created is None:
        return _finish(store, operation_id, _unknown(
            "新增请求已发出，但未能在分类列表中确认，可能已经写入；请按名称核对，不要重复提交。",
            {"kind": "dv_machinerytype_create_result", "outcome": "unknown",
             "preview_id": operation_id},
        ))
    by_id = {cast(int, node["machineryTypeId"]): node for node in nodes}
    path, _ = path_of(created, by_id)
    verified = (
        created["machineryTypeName"] == name
        and created["parentTypeId"] == parent_id
        and created["enableFlag"] == after["enableFlag"]
        and created["remark"] == after["remark"]
    )
    details: JsonObject = {
        "kind": "dv_machinerytype_create_result", "outcome": "succeeded" if verified else "failed",
        "preview_id": operation_id,
        "created": {
            "machineryTypeId": created["machineryTypeId"],
            "machineryTypeCode": created["machineryTypeCode"],
            "machineryTypeName": created["machineryTypeName"],
            "parentTypeId": created["parentTypeId"],
            "path": path,
        },
        "verified": verified,
        "label": "设备分类已新增并完成回读核验。" if verified else "写入后回读字段不一致，请人工核对。",
    }
    return _finish(store, operation_id, ToolResult(
        content=details["label"] if isinstance(details["label"], str) else "处理完成。",
        details=details,
        status=ToolStatus.SUCCEEDED if verified else ToolStatus.FAILED,
        error_code=None if verified else "WRITE_VERIFICATION_FAILED",
    ))


async def _apply_update(
    store: DvMachineryTypePreviewStore, context: ToolContext, arguments: Mapping[str, object]
) -> ToolResult:
    operation_id = _operation_id(arguments, context)
    if isinstance(operation_id, ToolResult):
        return operation_id
    try:
        record, cached = store.begin(
            preview_id=operation_id, user_key=_user_key(context), action="update"
        )
    except PreviewError as exc:
        return _failure(str(exc), exc.code)
    if cached is not None:
        return cached
    client = _client(context)
    if client is None:
        return _finish(store, operation_id, _failure("KTG 客户端未配置。", "KTG_CLIENT_MISSING"))
    before = record.before or {}
    after = record.after
    type_id = cast(int, after["machineryTypeId"])
    try:
        nodes = await load_machinery_types(client)
    except KTGError as exc:
        return _finish(store, operation_id, _failure(str(exc), exc.code))
    current = next((node for node in nodes if node["machineryTypeId"] == type_id), None)
    if current is None:
        return _finish(store, operation_id, _failure(
            f"ID 为 {type_id} 的设备分类已不存在，请重新预览。", "TYPE_NOT_FOUND"))
    if any(current.get(key) != before.get(key) for key in (
            "machineryTypeName", "enableFlag", "remark", "parentTypeId")):
        return _finish(store, operation_id, _failure(
            "分类已在预览后发生变化，请重新预览。", "PREVIEW_STALE"))
    body: dict[str, object] = {"machineryTypeId": type_id}
    for key in ("machineryTypeName", "enableFlag", "remark"):
        if after.get(key) != before.get(key):
            body[key] = after.get(key)
    if len(body) == 1:
        return _finish(store, operation_id, _failure(
            "名称、启用状态和备注都没有变化，无需修改。", "NO_CHANGE"))
    try:
        await client.put(_CREATE_PATH, body)
    except KTGError as exc:
        return _finish(store, operation_id, _write_error(exc, "update"))
    try:
        nodes = await load_machinery_types(client)
    except KTGError:
        return _finish(store, operation_id, _unknown(
            "修改请求已发出，但回读失败，可能已经保存；请核对后再操作。",
            {"kind": "dv_machinerytype_update_result", "outcome": "unknown",
             "preview_id": operation_id},
        ))
    updated = next((node for node in nodes if node["machineryTypeId"] == type_id), None)
    if updated is None:
        return _finish(store, operation_id, _unknown(
            "修改请求已发出，但未能在分类列表中确认；请核对后再操作。",
            {"kind": "dv_machinerytype_update_result", "outcome": "unknown",
             "preview_id": operation_id},
        ))
    verified = all(updated.get(key) == after.get(key)
                   for key in ("machineryTypeName", "enableFlag", "remark"))
    by_id = {cast(int, node["machineryTypeId"]): node for node in nodes}
    path, _ = path_of(updated, by_id)
    details: JsonObject = {
        "kind": "dv_machinerytype_update_result", "outcome": "succeeded" if verified else "failed",
        "preview_id": operation_id,
        "updated": {
            "machineryTypeId": updated["machineryTypeId"],
            "machineryTypeCode": updated["machineryTypeCode"],
            "machineryTypeName": updated["machineryTypeName"],
            "enableFlag": updated["enableFlag"],
            "remark": updated["remark"],
            "path": path,
        },
        "verified": verified,
        "label": "设备分类已保存并完成回读核验。" if verified else "写入后回读字段不一致，请人工核对。",
    }
    return _finish(store, operation_id, ToolResult(
        content="设备分类已保存并完成回读核验。" if verified else "写入后回读字段不一致，请人工核对。",
        details=details,
        status=ToolStatus.SUCCEEDED if verified else ToolStatus.FAILED,
        error_code=None if verified else "WRITE_VERIFICATION_FAILED",
    ))


def _find_created(nodes: list[JsonObject], parent_id: int, name: str) -> JsonObject | None:
    matches = [
        node for node in nodes
        if node["parentTypeId"] == parent_id and node["machineryTypeName"] == name
    ]
    if not matches:
        return None
    return max(matches, key=lambda node: cast(int, node["machineryTypeId"]))


def _duplicate_siblings(
    nodes: list[JsonObject], parent_id: int, name: str, *, exclude_id: int | None = None
) -> list[JsonObject]:
    target = name.strip()
    return [
        node for node in nodes
        if node["parentTypeId"] == parent_id
        and node["machineryTypeId"] != exclude_id
        and str(node["machineryTypeName"]).strip() == target
    ]


def _names_text(nodes: list[JsonObject]) -> str:
    return "、".join(
        f"[{node['machineryTypeId']}] {node['machineryTypeName']}" for node in nodes[:10]
    )


def _same_changes(before: JsonObject, after: JsonObject) -> bool:
    return all(before.get(key) == after.get(key) for key in (
        "machineryTypeName", "enableFlag", "remark", "parentTypeId"))


def _write_error(exc: KTGError, action: str) -> ToolResult:
    message = str(exc)
    if "同级" in message or "已存在" in message:
        return _failure(message, "DUPLICATE_NAME")
    if exc.code in {"KTG_CONNECTION_ERROR", "KTG_HTTP_ERROR", "KTG_INVALID_RESPONSE"}:
        return _unknown(
            f"{'新增' if action == 'create' else '修改'}请求结果未知，可能已经写入；请核对后再操作。",
            {"kind": f"dv_machinerytype_{action}_result", "outcome": "unknown"},
        )
    return _failure(message, exc.code)


def _unknown(message: str, details: JsonObject) -> ToolResult:
    return ToolResult(content=message, details=details, status=ToolStatus.UNKNOWN,
                      error_code="WRITE_UNKNOWN")


def _finish(
    store: DvMachineryTypePreviewStore, operation_id: str, result: ToolResult
) -> ToolResult:
    store.finish(operation_id, result)
    return result


def _operation_id(arguments: Mapping[str, object], context: ToolContext) -> str | ToolResult:
    if set(arguments) != {"operation_id"}:
        return _failure("只接受 operation_id 参数。", "VALIDATION_ERROR")
    operation_id = arguments.get("operation_id")
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 100:
        return _failure("operation_id 无效。", "VALIDATION_ERROR")
    if context.operation_id != operation_id:
        return _failure("确认上下文与参数不一致。", "CONTEXT_MISMATCH")
    return operation_id


def _user_key(context: ToolContext) -> str:
    identity = context.identity
    if not isinstance(identity, Mapping):
        raise ValueError("KTG 未返回有效用户身份。")
    user_id = identity.get("userId")
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        return f"id:{user_id}"
    username = identity.get("userName")
    if isinstance(username, str) and username.strip():
        return f"name:{username.strip()}"
    raise ValueError("KTG 未返回有效用户身份。")


def _client(context: ToolContext) -> KTGClient | None:
    return context.ktg_client if isinstance(context.ktg_client, KTGClient) else None


def _positive_id(value: object, label: str) -> int | ToolResult:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return _failure(f"{label} 必须是正整数。", "VALIDATION_ERROR")
    return value


def _required_text(value: object, label: str, max_length: int) -> str | ToolResult:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        return _failure(f"{label} 长度必须为 1-{max_length}。", "VALIDATION_ERROR")
    return value.strip()


def _optional_text(value: object, label: str, max_length: int) -> str | ToolResult:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        return _failure(f"{label} 长度不能超过 {max_length}。", "VALIDATION_ERROR")
    return value


def _enable_flag(value: object) -> str | ToolResult:
    if value is None:
        return "Y"
    if value not in {"Y", "N"}:
        return _failure("enable_flag 只能是 Y 或 N。", "VALIDATION_ERROR")
    return cast(str, value)


def _output_schema(fields: tuple[str, ...]) -> JsonObject:
    return {
        "type": "object",
        "properties": {key: {} for key in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code)
