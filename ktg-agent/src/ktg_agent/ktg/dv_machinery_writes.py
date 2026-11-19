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
from ktg_agent.ktg.dv_machinery import (
    _project_machinery, _project_workshop, load_machinery_types,
)
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolChannel, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)


PREVIEW_CREATE = "ktg_preview_dv_machinery_create"
PREVIEW_UPDATE = "ktg_preview_dv_machinery_update"
PREVIEW_DELETE = "ktg_preview_dv_machinery_delete"
APPLY_CREATE = "ktg_dv_machinery_create"
APPLY_UPDATE = "ktg_dv_machinery_update"
APPLY_DELETE = "ktg_dv_machinery_delete"

_MACHINERY_PATH = "/mes/dv/machinery"
_MACHINERY_LIST = f"{_MACHINERY_PATH}/list"
_WORKSHOP_LIST = "/mes/md/workshop/listAll"
_ACTIONS = {
    "create": (PREVIEW_CREATE, APPLY_CREATE, "mes:dv:machinery:add"),
    "update": (PREVIEW_UPDATE, APPLY_UPDATE, "mes:dv:machinery:edit"),
    "delete": (PREVIEW_DELETE, APPLY_DELETE, "mes:dv:machinery:remove"),
}
_MUTABLE = ("machineryName", "machineryBrand", "machinerySpec", "machineryTypeId",
            "machineryTypeCode", "machineryTypeName", "workshopId", "workshopCode",
            "workshopName", "status", "remark")
_TEXT_LIMITS = {"machinery_code": 64, "machinery_name": 255, "machinery_brand": 255,
                "machinery_spec": 255, "remark": 500}
_STATUS = {"STOP", "WORKING", "REPAIR"}


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


class DvMachineryPreviewStore:
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
            db.execute("""CREATE TABLE IF NOT EXISTS dv_machinery_previews (
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
            db.execute("DELETE FROM dv_machinery_previews WHERE expires_at<=? AND state='pending'", (now,))
            db.execute("""INSERT INTO dv_machinery_previews
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
            row = db.execute("SELECT * FROM dv_machinery_previews WHERE preview_id=?", (preview_id,)).fetchone()
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
                db.execute("DELETE FROM dv_machinery_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
            db.execute("UPDATE dv_machinery_previews SET state='executing' WHERE preview_id=? AND state='pending'",
                       (preview_id,))
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        encoded = self._encode({"content": result.content, "details": result.details,
                                "status": result.status.value, "error_code": result.error_code,
                                "operation_id": result.operation_id, "verification": result.verification})
        with self._lock, self._connect() as db:
            db.execute("UPDATE dv_machinery_previews SET state='completed',result_json=? WHERE preview_id=?",
                       (encoded, preview_id))

    def _read(self, preview_id: str) -> _Preview | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM dv_machinery_previews WHERE preview_id=?", (preview_id,)).fetchone()
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


def build_dv_machinery_write_tools(store: DvMachineryPreviewStore) -> tuple[ToolDefinition, ...]:
    return tuple(_definition(store, action, hidden=hidden)
                 for action in ("create", "update", "delete") for hidden in (False, True))


def dv_machinery_write_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
    def create_factory(item: ToolDefinition) -> ToolFactory:
        return lambda: item
    return {f"dv_writes:{definition.name}": create_factory(definition) for definition in definitions}


def dv_machinery_write_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
    permission: dict[str, PermissionPolicy] = {
        "create": PermissionAll((PermissionLeaf("mes:dv:machinery:add"),
                                 PermissionLeaf("mes:dv:machinery:list"),
                                 PermissionLeaf("mes:dv:machinerytype:list"),
                                 PermissionLeaf("mes:md:workshop:list"))),
        "update": PermissionAll((PermissionLeaf("mes:dv:machinery:edit"),
                                 PermissionLeaf("mes:dv:machinery:query"),
                                 PermissionLeaf("mes:dv:machinery:list"),
                                 PermissionLeaf("mes:dv:machinerytype:list"),
                                 PermissionLeaf("mes:md:workshop:list"))),
        "delete": PermissionAll((PermissionLeaf("mes:dv:machinery:remove"),
                                 PermissionLeaf("mes:dv:machinery:query"))),
    }
    return tuple(ToolManifest(
        name=item.name, version=item.version, module="dv", domain="machinery",
        label=item.label, description=item.description,
        permission_policy=permission[_action_for_name(item.name)], effect=item.effect,
        execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=item.expose_to_model,
        confirmation_policy="none" if item.expose_to_model else "trusted_confirmation",
        proposal_store_effect=item.name.startswith("ktg_preview_"),
        handler_ref=f"dv_writes:{item.name}") for item in definitions)


def _definition(store: DvMachineryPreviewStore, action: str, *, hidden: bool) -> ToolDefinition:
    preview_name, apply_name, permission = _ACTIONS[action]
    name = apply_name if hidden else preview_name
    if hidden:
        schema = {"type": "object", "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 100}},
                  "required": ["operation_id"], "additionalProperties": False}
        description = f"受信确认通道专用：执行已确认的设备{_action_label(action)}预览，模型不可见。"
        label = f"确认{_action_label(action)}设备"
    else:
        schema = _preview_schema(action)
        description = _PREVIEW_DESCRIPTIONS[action]
        label = f"预览{_action_label(action)}设备"

    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        if hidden:
            return await _apply(store, action, context, arguments)
        return await _preview(store, action, context, arguments)

    required = [permission]
    if action in {"create", "update"}:
        required.extend(("mes:dv:machinery:list", "mes:dv:machinerytype:list", "mes:md:workshop:list"))
    elif action == "delete":
        required.append("mes:dv:machinery:query")
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
    raise ValueError("unknown machinery tool")


_PREVIEW_DESCRIPTIONS = {
    "create": "生成新增设备台账记录的预览，由用户在网页确认后创建并由 MES 生成条码。"
              "提供设备编码和名称，从设备分类树选启用末级分类，从启用车间候选选车间，传系统 ID。",
    "update": "生成单台设备台账的修改预览，由用户在网页确认后保存。传设备系统 ID 及发生变化的字段；"
              "设备编码固定。更换分类时选启用末级分类，更换车间时选启用车间。",
    "delete": "生成单台设备台账记录的删除预览，传设备系统 ID。由用户在网页确认后只删除这台设备；"
              "请在确认前人工核对维修和点检关联。",
}


def _preview_schema(action: str) -> dict[str, object]:
    if action == "create":
        properties: dict[str, dict[str, JsonValue]] = {
            "machinery_code": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"\S",
                               "description": "调用方提供的完整设备业务编码，需唯一。"},
            "machinery_name": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"\S",
                               "description": "设备台账名称。"},
            "machinery_type_id": {"type": "integer", "minimum": 1,
                                  "description": "设备分类树中启用的末级分类 machineryTypeId。"},
            "workshop_id": {"type": "integer", "minimum": 1,
                            "description": "启用车间候选返回的 workshopId。"},
            "machinery_brand": {"type": "string", "maxLength": 255, "pattern": r"\S",
                                "description": "设备品牌，未提供时省略。"},
            "machinery_spec": {"type": "string", "maxLength": 255, "pattern": r"\S",
                               "description": "设备规格，未提供时省略。"},
            "status": {"type": "string", "enum": cast(JsonValue, sorted(_STATUS)), "default": "STOP"},
            "remark": {"type": "string", "maxLength": 500},
        }
        required = ["machinery_code", "machinery_name", "machinery_type_id", "workshop_id"]
    elif action == "update":
        properties = {"machinery_id": {"type": "integer", "minimum": 1,
                                      "description": "目标设备的系统 ID，取自设备台账的 machineryId。"}}
        properties.update({key: {"type": "string", "maxLength": limit}
                           for key, limit in _TEXT_LIMITS.items() if key != "machinery_code"})
        properties["machinery_name"]["minLength"] = 1
        properties["machinery_name"]["pattern"] = r"\S"
        properties["machinery_name"]["description"] = "新设备名称，省略保留原值。"
        for key in ("machinery_brand", "machinery_spec", "remark"):
            properties[key]["description"] = "省略保留原值；可传空字符串。"
        properties["machinery_type_id"] = {"type": "integer", "minimum": 1,
                                           "description": "设备分类树中启用的末级分类 machineryTypeId，省略保留原值。"}
        properties["workshop_id"] = {"type": "integer", "minimum": 1,
                                     "description": "启用车间候选返回的 workshopId，省略保留原值。"}
        properties["status"] = {"type": "string", "enum": cast(JsonValue, sorted(_STATUS))}
        required = ["machinery_id"]
    else:
        properties = {"machinery_id": {"type": "integer", "minimum": 1,
                                      "description": "待删除设备的系统 ID，取自设备台账的 machineryId。"}}
        required = ["machinery_id"]
    if action in {"create", "update"}:
        default = "省略时默认为 STOP。" if action == "create" else "省略保留原状态。"
        properties["status"]["description"] = "STOP 停机，WORKING 生产中，REPAIR 维修中。" + default
    return {"type": "object", "properties": cast(JsonValue, properties),
            "required": cast(JsonValue, required),
            "additionalProperties": False}


async def _preview(store: DvMachineryPreviewStore, action: str, context: ToolContext,
                   arguments: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        user_key = _identity_key(context.identity)
        allowed = set(cast(dict[str, JsonValue], _preview_schema(action)["properties"]))
        if set(arguments) - allowed:
            raise KTGError("VALIDATION_ERROR", "包含未支持的设备字段。")
        if action == "create":
            before, after = None, await _create_payload(client, arguments)
        elif action == "update":
            before, after = await _update_payload(client, arguments)
        else:
            before = await _fetch_device(client, arguments.get("machinery_id"))
            after = {key: before[key] for key in ("machineryId", "machineryCode", "machineryName")}
        preview_id = store.create(user_key=user_key, action=action, before=before, after=after)
        warnings = (["MES 新增设备时会生成条码。"] if action == "create" else
                    ["设备删除后维修/点检关联如何处理未得到源码验证；请在确认前人工核对。"]
                    if action == "delete" else [])
        details: JsonObject = {"kind": "dv_machinery_preview", "action": action,
                               "preview_id": preview_id, "before": before, "after": after,
                               "warnings": cast(list[JsonValue], warnings)}
        return ToolResult(content=f"设备{_action_label(action)}预览已生成，请用户检查并在卡片中确认。",
                          details=details, operation_id=preview_id)
    except (KTGError, PreviewError) as exc:
        code = exc.code if isinstance(exc, (KTGError, PreviewError)) else "VALIDATION_ERROR"
        return _failure(str(exc), code)


async def _create_payload(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    code = _required_text(arguments, "machinery_code", 64)
    name = _required_text(arguments, "machinery_name", 255)
    type_id = _positive_id(arguments.get("machinery_type_id"), "machinery_type_id")
    workshop_id = _positive_id(arguments.get("workshop_id"), "workshop_id")
    brand = _optional_text(arguments, "machinery_brand", 255)
    spec = _optional_text(arguments, "machinery_spec", 255)
    remark = _optional_text(arguments, "remark", 500, allow_empty=True)
    status = _status(arguments.get("status", "STOP"))
    if await _code_exists(client, code):
        raise KTGError("MACHINERY_CODE_EXISTS", "设备编码已存在。")
    type_row = await _resolve_type(client, type_id)
    workshop_row = await _resolve_workshop(client, workshop_id)
    after: JsonObject = {
        "machineryCode": code, "machineryName": name,
        "machineryTypeId": type_id, "machineryTypeCode": type_row["machineryTypeCode"],
        "machineryTypeName": type_row["machineryTypeName"],
        "workshopId": workshop_id, "workshopCode": workshop_row["workshopCode"],
        "workshopName": workshop_row["workshopName"], "status": status,
        "machineryBrand": brand or "", "machinerySpec": spec or "", "remark": remark or "",
    }
    return after


async def _update_payload(client: KTGClient, arguments: Mapping[str, object]) -> tuple[JsonObject, JsonObject]:
    machinery_id = _positive_id(arguments.get("machinery_id"), "machinery_id")
    before = await _fetch_device(client, machinery_id)
    after = dict(before)
    changed: set[str] = set()
    for key, field, limit, required in (
        ("machinery_name", "machineryName", 255, True),
        ("machinery_brand", "machineryBrand", 255, False),
        ("machinery_spec", "machinerySpec", 255, False),
        ("remark", "remark", 500, False),
    ):
        if key in arguments:
            value = _text_value(arguments[key], key, limit, allow_empty=not required)
            after[field] = value
            changed.add(field)
    if "status" in arguments:
        after["status"] = _status(arguments["status"])
        changed.add("status")
    if "machinery_type_id" in arguments:
        type_id = _positive_id(arguments["machinery_type_id"], "machinery_type_id")
        node = await _resolve_type(client, type_id)
        after.update({"machineryTypeId": type_id, "machineryTypeCode": node["machineryTypeCode"],
                      "machineryTypeName": node["machineryTypeName"]})
        changed.update(("machineryTypeId", "machineryTypeCode", "machineryTypeName"))
    if "workshop_id" in arguments:
        workshop_id = _positive_id(arguments["workshop_id"], "workshop_id")
        workshop = await _resolve_workshop(client, workshop_id)
        after.update({"workshopId": workshop_id, "workshopCode": workshop["workshopCode"],
                      "workshopName": workshop["workshopName"]})
        changed.update(("workshopId", "workshopCode", "workshopName"))
    if not changed or all(after.get(key) == before.get(key) for key in changed):
        raise KTGError("NO_CHANGE", "没有提供发生变化的设备字段。")
    after["changedFields"] = cast(JsonValue, sorted(changed))
    return before, after


async def _fetch_device(client: KTGClient, machinery_id: object) -> JsonObject:
    device_id = _positive_id(machinery_id, "machinery_id")
    payload = await client.get(f"{_MACHINERY_PATH}/{device_id}")
    data = payload.get("data")
    if data is None:
        raise KTGError("MACHINERY_NOT_FOUND", f"未找到 ID 为 {device_id} 的设备。")
    row = _project_machinery(data)
    if row["machineryId"] != device_id:
        raise KTGError("KTG_INVALID_RESPONSE", "设备详情 ID 与请求不一致。")
    return row


async def _resolve_type(client: KTGClient, type_id: int) -> JsonObject:
    nodes = await load_machinery_types(client)
    matches = [node for node in nodes if node["machineryTypeId"] == type_id]
    if not matches:
        raise KTGError("TYPE_NOT_FOUND", f"未找到设备分类 ID {type_id}。")
    node = matches[0]
    if node["enableFlag"] != "Y":
        raise KTGError("TYPE_DISABLED", "设备分类已停用，不能用于设备台账。")
    if any(child["parentTypeId"] == type_id for child in nodes):
        raise KTGError("TYPE_NOT_LEAF", "设备分类不是叶子节点，请选择启用的末级分类。")
    return node


async def _resolve_workshop(client: KTGClient, workshop_id: int) -> JsonObject:
    payload = await client.get(_WORKSHOP_LIST)
    data = payload.get("data")
    if not isinstance(data, list):
        raise KTGError("KTG_INVALID_RESPONSE", "车间候选列表格式无效。")
    matches = []
    for item in data:
        if not isinstance(item, Mapping):
            raise KTGError("KTG_INVALID_RESPONSE", "车间候选不是对象。")
        row = _project_workshop(item)
        if row["workshopId"] == workshop_id and row["enableFlag"] == "Y":
            matches.append(row)
    if len(matches) != 1:
        raise KTGError("WORKSHOP_NOT_FOUND", f"未找到唯一启用车间 ID {workshop_id}。")
    return matches[0]


async def _code_exists(client: KTGClient, code: str, exclude_id: int | None = None) -> bool:
    payload = await client.get(_MACHINERY_LIST, {"machineryCode": code, "pageNum": 1, "pageSize": 100})
    rows = payload.get("rows")
    total = payload.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool):
        raise KTGError("KTG_INVALID_RESPONSE", "设备编码检查结果格式无效。")
    if total > len(rows):
        return True
    for item in rows:
        row = _project_machinery(item)
        if cast(str, row["machineryCode"]).casefold() == code.casefold() and row["machineryId"] != exclude_id:
            return True
    return False


async def _apply(store: DvMachineryPreviewStore, action: str, context: ToolContext,
                 arguments: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
    if context.channel is not ToolChannel.TRUSTED_CONFIRMATION:
        return _failure("设备台账写入只能通过用户确认执行。", "CONFIRMATION_REQUIRED")
    preview_id = arguments.get("operation_id")
    if not isinstance(preview_id, str) or not preview_id.strip() or len(preview_id) > 100:
        return _failure("operation_id 无效。", "VALIDATION_ERROR")
    if context.operation_id != preview_id or context.confirmation_id is None:
        return _failure("确认上下文与预览不匹配。", "CONFIRMATION_MISMATCH")
    user_key = _identity_key(context.identity)
    try:
        record, cached = store.begin(preview_id=preview_id, user_key=user_key, action=action)
        if cached is not None:
            return cached
        if record.before is not None:
            current = await _fetch_device(client, record.before["machineryId"])
            if not _snapshot_matches(current, record.before):
                raise PreviewError("PREVIEW_STALE", "设备信息已变化，请重新生成预览。")
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
        store.finish(preview_id, result)
        return result
    except KTGError as exc:
        outcome = "unknown" if exc.code in {"KTG_CONNECTION_ERROR", "KTG_HTTP_ERROR"} else "failed"
        result = _result(action, outcome, str(exc), preview_id, error_code=exc.code)
        store.finish(preview_id, result)
        return result


async def _apply_create(client: KTGClient, preview_id: str, after: JsonObject) -> ToolResult:
    code = cast(str, after["machineryCode"])
    if await _code_exists(client, code):
        return _result("create", "failed", "设备编码在确认前已被占用，请重新预览。",
                       preview_id, error_code="MACHINERY_CODE_EXISTS")
    body = {key: after[key] for key in (
        "machineryCode", "machineryName", "machineryBrand", "machinerySpec",
        "machineryTypeId", "machineryTypeCode", "machineryTypeName",
        "workshopId", "workshopCode", "workshopName", "status", "remark",
    )}
    await client.post(_MACHINERY_PATH, body)
    rows, total = await _find_by_code(client, code)
    matching = [row for row in rows if _created_matches(row, after)]
    if total != 1 or len(matching) != 1:
        return _result("create", "unknown", "MES 已接受新增请求，但回读无法唯一核实；请勿重复提交。",
                       preview_id, error_code="WRITE_RESULT_UNVERIFIED")
    return _result("create", "succeeded", "设备已新增并完成回读核验。", preview_id,
                   verified=True, created=matching[0])


async def _apply_update(client: KTGClient, preview_id: str, before: JsonObject | None,
                        after: JsonObject) -> ToolResult:
    if before is None:
        return _result("update", "failed", "预览缺少设备快照，请重新生成。", preview_id,
                       error_code="PREVIEW_STALE")
    machinery_id = cast(int, before["machineryId"])
    changed = cast(list[str], after["changedFields"])
    body: dict[str, object] = {"machineryId": machinery_id}
    for field in changed:
        body[field] = after[field]
    await client.put(_MACHINERY_PATH, body)
    updated = await _fetch_device(client, machinery_id)
    if any(updated.get(field) != after.get(field) for field in changed):
        return _result("update", "unknown", "MES 已接受修改请求，但回读内容不一致；请先核对设备。",
                       preview_id, error_code="WRITE_RESULT_UNVERIFIED")
    return _result("update", "succeeded", "设备已修改并完成回读核验。", preview_id,
                   verified=True, updated=updated)


async def _apply_delete(client: KTGClient, preview_id: str, before: JsonObject | None) -> ToolResult:
    if before is None:
        return _result("delete", "failed", "预览缺少设备快照，请重新生成。", preview_id,
                       error_code="PREVIEW_STALE")
    machinery_id = cast(int, before["machineryId"])
    await client.delete(f"{_MACHINERY_PATH}/{machinery_id}")
    payload = await client.get(f"{_MACHINERY_PATH}/{machinery_id}")
    if payload.get("data") is not None:
        return _result("delete", "unknown", "MES 已接受删除请求，但设备仍可读取；请先核对，不要重试。",
                       preview_id, error_code="WRITE_RESULT_UNVERIFIED")
    return _result("delete", "succeeded", "设备已删除并完成回读核验。", preview_id,
                   verified=True, deleted={"machineryId": machinery_id,
                                            "machineryCode": before["machineryCode"],
                                            "machineryName": before["machineryName"]})


async def _find_by_code(client: KTGClient, code: str) -> tuple[list[JsonObject], int]:
    payload = await client.get(_MACHINERY_LIST, {"machineryCode": code, "pageNum": 1, "pageSize": 100})
    rows, total = payload.get("rows"), payload.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool):
        raise KTGError("KTG_INVALID_RESPONSE", "设备回读结果格式无效。")
    return [_project_machinery(item) for item in rows], total


def _created_matches(row: JsonObject, expected: JsonObject) -> bool:
    return all(row.get(field) == expected.get(field) for field in (
        "machineryCode", "machineryName", "machineryTypeId", "workshopId", "status"))


def _snapshot_matches(current: JsonObject, before: JsonObject) -> bool:
    return all(current.get(key) == before.get(key) for key in _MUTABLE + ("machineryId", "machineryCode"))


def _result(action: str, outcome: str, label: str, preview_id: str, *,
            error_code: str | None = None, verified: bool | None = None,
            created: JsonObject | None = None, updated: JsonObject | None = None,
            deleted: JsonObject | None = None) -> ToolResult:
    details: JsonObject = {"kind": f"dv_machinery_{action}_result", "action": action,
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


def _positive_id(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KTGError("VALIDATION_ERROR", f"{name} 必须是正整数。")
    return value


def _required_text(arguments: Mapping[str, object], key: str, maximum: int) -> str:
    value = _text_value(arguments.get(key), key, maximum)
    if not value:
        raise KTGError("VALIDATION_ERROR", f"{key} 不能为空。")
    return value


def _optional_text(arguments: Mapping[str, object], key: str, maximum: int,
                   *, allow_empty: bool = False) -> str | None:
    if key not in arguments:
        return None
    return _text_value(arguments[key], key, maximum, allow_empty=allow_empty)


def _text_value(value: object, name: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise KTGError("VALIDATION_ERROR", f"{name} 长度不能超过 {maximum}。")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise KTGError("VALIDATION_ERROR", f"{name} 不能为空。")
    return cleaned


def _status(value: object) -> str:
    if not isinstance(value, str) or value not in _STATUS:
        raise KTGError("VALIDATION_ERROR", "status 只允许 STOP、WORKING、REPAIR。")
    return value


def _action_label(action: str) -> str:
    return {"create": "新增", "update": "修改", "delete": "删除"}[action]


def _client(context: ToolContext) -> KTGClient | None:
    return context.ktg_client if isinstance(context.ktg_client, KTGClient) else None


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code)
