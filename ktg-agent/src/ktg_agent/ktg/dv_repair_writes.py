"""Durable previews and trusted confirmation for repair drafts."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
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
from ktg_agent.ktg.dv_checkplan import _date, _positive_id, _text
from ktg_agent.ktg.dv_machinery import _schema, _client, _failure
from ktg_agent.ktg.dv_repair import (REPAIR, LINE, MACHINERY, SUBJECT, fetch_repair,
    fetch_line, line_page, repair_page, read_repair_lines)
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import (ExecutionMode, RetryPolicy, ToolChannel, ToolContext,
    ToolDefinition, ToolEffect, ToolResult, ToolStatus)

_ACTIONS = ("create", "update", "delete", "line_create", "line_update", "line_delete")
PREVIEW_NAMES = {action: f"ktg_preview_dv_repair_{action}" for action in _ACTIONS}
APPLY_NAMES = {action: f"ktg_dv_repair_{action}" for action in _ACTIONS}
_PERMS = {
    "create": ("mes:dv:repair:add", "mes:dv:repair:list", "mes:dv:machinery:query"),
    "update": ("mes:dv:repair:edit", "mes:dv:repair:query", "mes:dv:repair:list", "mes:dv:machinery:query"),
    "delete": ("mes:dv:repair:remove", "mes:dv:repair:query", "mes:dv:repairline:list"),
    "line_create": ("mes:dv:repairline:add", "mes:dv:repairline:list", "mes:dv:repair:query", "mes:dv:dvsubject:query"),
    "line_update": ("mes:dv:repairline:edit", "mes:dv:repairline:query", "mes:dv:repair:query", "mes:dv:dvsubject:query"),
    "line_delete": ("mes:dv:repairline:remove", "mes:dv:repairline:query", "mes:dv:repair:query"),
}
_BUSINESS_ERROR = {"KTG_REQUEST_FAILED", "AUTH_EXPIRED", "AUTH_REQUIRED", "AUTH_INVALID", "PERMISSION_DENIED"}


class DvRepairPreviewStore:
    """Durable, identity-bound, one-shot preview records and cached receipts."""

    def __init__(self, *, ttl_seconds: float = 600, path: str | Path = ":memory:") -> None:
        if ttl_seconds <= 0:
            raise ValueError("preview ttl must be positive")
        self.ttl_seconds, self.path = ttl_seconds, str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._memory_db = sqlite3.connect(":memory:", timeout=10, check_same_thread=False) if self.path == ":memory:" else None
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dv_repair_previews (
                preview_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, action TEXT NOT NULL,
                before_json TEXT, after_json TEXT, digest TEXT NOT NULL, expires_at REAL NOT NULL,
                state TEXT NOT NULL, result_json TEXT)""")

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
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def _digest(cls, action: str, before: JsonObject | None, after: JsonObject | None) -> str:
        return hashlib.sha256(cls._json({"action": action, "before": before, "after": after}).encode()).hexdigest()

    def create(self, user_key: str, action: str, before: JsonObject | None, after: JsonObject | None) -> str:
        preview_id = str(uuid4())
        with self._lock, self._connect() as db:
            now = time()
            db.execute("DELETE FROM dv_repair_previews WHERE state='pending' AND expires_at<=?", (now,))
            db.execute("INSERT INTO dv_repair_previews VALUES (?,?,?,?,?,?,?,'pending',NULL)",
                (preview_id, user_key, action, self._json(before) if before is not None else None,
                 self._json(after) if after is not None else None, self._digest(action, before, after), now + self.ttl_seconds))
        return preview_id

    def action_for(self, preview_id: str, user_key: str) -> str:
        record = self._read(preview_id)
        if record is None or record["user_key"] != user_key:
            raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在或不属于当前登录用户。")
        if record["state"] == "pending" and cast(float, record["expires_at"]) <= time():
            raise PreviewError("PREVIEW_EXPIRED", "预览已过期，请重新生成。")
        self._validate_digest(record)
        return cast(str, record["action"])

    def begin(self, preview_id: str, user_key: str, action: str) -> tuple[dict[str, object], ToolResult | None]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM dv_repair_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if row is None:
                raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在，请重新生成。")
            record = dict(row)
            if record["user_key"] != user_key:
                raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if record["action"] != action:
                raise PreviewError("PREVIEW_ACTION_MISMATCH", "预览操作类型不匹配。")
            self._validate_digest(record)
            if record["state"] == "completed":
                return record, self._decode_result(record["result_json"])
            if record["state"] == "executing":
                result = _result(action, preview_id, "unknown", "确认进程中断，MES写入结果未知；请先查询核对，勿重复提交。", error="WRITE_RESULT_UNKNOWN")
                db.execute("UPDATE dv_repair_previews SET state='completed',result_json=? WHERE preview_id=?",
                           (self._encode_result(result), preview_id))
                record["state"] = "completed"
                return record, result
            if record["expires_at"] <= time():
                raise PreviewError("PREVIEW_EXPIRED", "预览已过期，请重新生成。")
            db.execute("UPDATE dv_repair_previews SET state='executing' WHERE preview_id=? AND state='pending'", (preview_id,))
            record["state"] = "executing"
            record["before_json"] = json.loads(record["before_json"]) if record["before_json"] else None
            record["after_json"] = json.loads(record["after_json"]) if record["after_json"] else None
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE dv_repair_previews SET state='completed',result_json=? WHERE preview_id=?",
                       (self._encode_result(result), preview_id))

    @classmethod
    def _validate_digest(cls, record: Mapping[str, object]) -> None:
        before = json.loads(cast(str, record["before_json"])) if record["before_json"] else None
        after = json.loads(cast(str, record["after_json"])) if record["after_json"] else None
        if cls._digest(cast(str, record["action"]), before, after) != record["digest"]:
            raise PreviewError("PREVIEW_STALE", "预览内容校验失败，请重新生成。")

    def _read(self, preview_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM dv_repair_previews WHERE preview_id=?", (preview_id,)).fetchone()
            return None if row is None else dict(row)

    @staticmethod
    def _encode_result(result: ToolResult) -> str:
        return json.dumps({"content": result.content, "details": result.details, "status": result.status.value,
            "error_code": result.error_code, "operation_id": result.operation_id, "verification": result.verification},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _decode_result(raw: object) -> ToolResult | None:
        if not isinstance(raw, str):
            return None
        value = json.loads(raw)
        return ToolResult(content=value["content"], details=value["details"], status=ToolStatus(value["status"]),
            error_code=value["error_code"], operation_id=value["operation_id"], verification=value["verification"])


def build_dv_repair_write_tools(store: DvRepairPreviewStore) -> tuple[ToolDefinition, ...]:
    return tuple(_definition(store, action, hidden) for action in _ACTIONS for hidden in (False, True))


def dv_repair_write_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
    def factory(item: ToolDefinition) -> ToolFactory:
        return lambda: item
    return {f"dv_repair_writes:{item.name}": factory(item) for item in definitions}


def dv_repair_write_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
    return tuple(ToolManifest(name=item.name, version=item.version, module="dv", domain="repair",
        label=item.label, description=item.description,
        permission_policy=PermissionAll(tuple(PermissionLeaf(p) for p in item.required_permissions)),
        effect=item.effect, execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=item.expose_to_model,
        confirmation_policy="none" if item.expose_to_model else "trusted_confirmation",
        proposal_store_effect=item.expose_to_model, handler_ref=f"dv_repair_writes:{item.name}") for item in definitions)


def _definition(store: DvRepairPreviewStore, action: str, hidden: bool) -> ToolDefinition:
    name = APPLY_NAMES[action] if hidden else PREVIEW_NAMES[action]
    schema = _schema({"operation_id": {"type": "string", "minLength": 36, "maxLength": 100}}, ("operation_id",)) if hidden else _preview_schema(action)

    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await (_apply(store, action, context, arguments) if hidden else _preview(store, action, context, arguments))

    return ToolDefinition(name=name, version="1.0", label=("确认" if hidden else "预览") + _label(action),
        description=("仅网页可信确认通道可执行，模型不可见。" if hidden else "草稿维修单操作预览；实际写入需网页确认。"),
        input_schema=schema, output_schema={"type": "object", "properties": {key: {} for key in
            ("kind", "action", "preview_id", "before", "after", "warnings", "affected_counts", "outcome", "verified")},
            "additionalProperties": True}, required_permissions=_PERMS[action],
        effect=ToolEffect.WRITE if hidden else ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER, timeout_seconds=30, execute=execute, expose_to_model=not hidden)


def _preview_schema(action: str) -> JsonObject:
    positive: JsonValue = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
    def text(limit: int) -> JsonValue:
        return {"type": "string", "minLength": 1, "maxLength": limit}
    def optional_text(limit: int) -> JsonValue:
        return {"type": "string", "minLength": 0, "maxLength": limit}
    required: tuple[str, ...]
    if action == "create":
        props: dict[str, JsonValue] = {"repair_code": text(64), "repair_name": text(100),
            "machinery_id": positive, "require_date": {"type": "string", "format": "date"}, "remark": optional_text(250)}
        required = ("repair_code", "repair_name", "machinery_id", "require_date")
    elif action == "update":
        props = {"repair_id": positive, "repair_code": text(64), "repair_name": text(100),
            "machinery_id": positive, "require_date": {"type": "string", "format": "date"}, "remark": optional_text(250)}
        required = ("repair_id",)
    elif action == "delete": props, required = {"repair_id": positive}, ("repair_id",)
    elif action == "line_create":
        props = {"repair_id": positive, "subject_id": positive, "malfunction": text(500),
            "malfunction_url": optional_text(255), "repair_des": optional_text(500), "remark": optional_text(250)}
        required = ("repair_id", "subject_id", "malfunction")
    elif action == "line_update":
        props = {"repair_id": positive, "line_id": positive, "subject_id": positive,
            "malfunction": text(500), "malfunction_url": optional_text(255),
            "repair_des": optional_text(500), "remark": optional_text(250)}
        required = ("repair_id", "line_id")
    else: props, required = {"repair_id": positive, "line_id": positive}, ("repair_id", "line_id")
    return _schema(props, required)


def _label(action: str) -> str:
    return {"create": "新增维修单", "update": "修改维修单", "delete": "删除维修单",
        "line_create": "新增维修内容", "line_update": "修改维修内容", "line_delete": "删除维修内容"}[action]


def _validate_args(action: str, args: Mapping[str, object]) -> None:
    schema = _preview_schema(action)
    allowed = set(cast(dict[str, object], schema["properties"]))
    required = set(cast(list[str], schema["required"]))
    if set(args) - allowed or required - set(args):
        raise KTGError("VALIDATION_ERROR", f"{_label(action)}参数缺失或含越权字段。")
    if action in {"update", "line_update"} and not (set(args) - required):
        raise KTGError("VALIDATION_ERROR", "修改至少提供一个业务字段。")


async def _source(client: KTGClient, kind: str, source_id: object) -> JsonObject:
    sid = _positive_id(source_id, f"{kind}_id")
    raw = await client.get(f"{MACHINERY if kind == 'machinery' else SUBJECT}/{sid}")
    value = raw.get("data")
    if not isinstance(value, Mapping) or value.get("machineryId" if kind == "machinery" else "subjectId") != sid:
        raise KTGError("SOURCE_NOT_FOUND", "设备或项目不存在。", status=404)
    fields = (("machineryId", "machineryCode", "machineryName", "machineryBrand", "machinerySpec", "machineryTypeId")
              if kind == "machinery" else
              ("subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent", "subjectStandard"))
    source: JsonObject = {}
    for key in fields:
        item = value.get(key)
        if key.endswith("Id"):
            if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item <= 0):
                raise KTGError("KTG_INVALID_RESPONSE", f"源对象 {key} 格式无效。")
        elif item is not None and not isinstance(item, str):
            raise KTGError("KTG_INVALID_RESPONSE", f"源对象 {key} 格式无效。")
        source[key] = item
    if not source.get("machineryCode" if kind == "machinery" else "subjectCode"):
        raise KTGError("KTG_INVALID_RESPONSE", "源对象编码缺失。")
    return source


async def _code_matches(client: KTGClient, code: str, exclude: int | None = None) -> list[JsonObject]:
    rows, total = repair_page(await client.get(f"{REPAIR}/list", {"repairCode": code, "pageNum": 1, "pageSize": 100}))
    if total > 100: raise KTGError("AMBIGUOUS_MATCH", "同编号维修单超过100条，无法安全判断唯一性。")
    return [row for row in rows if row["repairCode"] == code and row["repairId"] != exclude]


async def _all_lines(client: KTGClient, repair_id: int) -> list[JsonObject]:
    result: list[JsonObject] = []
    for page in range(1, 12):
        rows, total = line_page(await client.get(f"{LINE}/list", {"repairId": repair_id, "pageNum": page, "pageSize": 100}))
        if any(row["repairId"] != repair_id for row in rows): raise KTGError("KTG_INVALID_RESPONSE", "维修内容归属不符。")
        result.extend(rows)
        if len(result) >= total: return result
        if not rows: raise KTGError("KTG_INVALID_RESPONSE", "维修内容分页提前结束。")
    raise KTGError("TOO_MANY_LINES", "维修内容超过1000条，无法安全预览删除。")


def _summary(rows: list[JsonObject]) -> JsonObject:
    ordered = sorted(rows, key=lambda row: cast(int, row["lineId"]))
    digest = hashlib.sha256(DvRepairPreviewStore._json(ordered).encode()).hexdigest()
    return {"count": len(ordered), "digest": digest, "samples": cast(list[JsonValue], ordered[:10])}


async def _make_preview(client: KTGClient, action: str, args: Mapping[str, object]) -> tuple[JsonObject | None, JsonObject | None, JsonObject]:
    _validate_args(action, args)
    affected: JsonObject = {}
    if action == "create":
        code = _text(args["repair_code"], "repair_code", 64)
        if await _code_matches(client, code): raise KTGError("CODE_EXISTS", "维修单编号已存在。")
        source = await _source(client, "machinery", args["machinery_id"])
        after: JsonObject = {"repairCode": code, "repairName": _text(args["repair_name"], "repair_name", 100),
            "requireDate": _date(args["require_date"], "require_date"), "status": "PREPARE", **source}
        if "remark" in args: after["remark"] = _text(args["remark"], "remark", 250, required=False)
        return None, after, affected
    repair_id = _positive_id(args["repair_id"], "repair_id")
    repair = await fetch_repair(client, repair_id)
    if repair["status"] != "PREPARE": raise KTGError("REPAIR_NOT_DRAFT", "只能维护草稿维修单。")
    if action == "update":
        after = dict(repair)
        for key, wire, limit in (("repair_code", "repairCode", 64), ("repair_name", "repairName", 100), ("remark", "remark", 250)):
            if key in args: after[wire] = _text(args[key], key, limit, required=key != "remark")
        if "require_date" in args: after["requireDate"] = _date(args["require_date"], "require_date")
        if "machinery_id" in args: after.update(await _source(client, "machinery", args["machinery_id"]))
        if after["repairCode"] != repair["repairCode"] and await _code_matches(client, cast(str, after["repairCode"]), repair_id):
            raise KTGError("CODE_EXISTS", "维修单编号已存在。")
        if after == repair: raise KTGError("NO_CHANGE", "修改内容与当前维修单相同。")
        return repair, after, affected
    if action == "delete":
        lines = await _all_lines(client, repair_id)
        affected = {"line_count": len(lines)}
        return {"repair": repair, "lines": _summary(lines)}, None, affected
    line: JsonObject | None = None
    if action != "line_create":
        line = await fetch_line(client, args["line_id"])
        if line["repairId"] != repair_id: raise KTGError("LINE_REPAIR_MISMATCH", "维修内容不属于指定维修单。")
    if action == "line_delete": return {"repair": repair, "line": line}, None, affected
    after = dict(line) if line else {"repairId": repair_id}
    if "subject_id" in args: after.update(await _source(client, "subject", args["subject_id"]))
    for key, wire, limit in (("malfunction", "malfunction", 500), ("malfunction_url", "malfunctionUrl", 255),
                             ("repair_des", "repairDes", 500), ("remark", "remark", 250)):
        if key in args: after[wire] = _text(args[key], key, limit, required=key == "malfunction")
    if line is not None and after == line: raise KTGError("NO_CHANGE", "维修内容没有变化。")
    return {"repair": repair, "line": line} if line else {"repair": repair}, after, affected


async def _preview(store: DvRepairPreviewStore, action: str, context: ToolContext, args: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None: return _failure("KTG查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        user_key = _identity_key(context.identity)
        before, after, affected = await _make_preview(client, action, args)
        preview_id = store.create(user_key, action, before, after)
        warnings = ["确认前会复核草稿状态与预览快照；MES并发修改仍可能导致写入失败。"]
        if action == "delete": warnings.append("删除主单会在同一事务删除所属全部维修内容，不可恢复。")
        details: JsonObject = {"kind": "dv_repair_preview", "action": action, "preview_id": preview_id,
            "before": before, "after": after, "warnings": cast(JsonValue, warnings),
            "affected_counts": affected, "expires_at": datetime.fromtimestamp(time() + store.ttl_seconds, UTC).isoformat(),
            "digest": DvRepairPreviewStore._digest(action, before, after)}
        text = f"{_label(action)}预览 [{preview_id}]。变更前：{before}；变更后：{after}；影响：{affected}。请在网页卡片确认。"
        return ToolResult(content=text[:4000], details=details, operation_id=preview_id)
    except (KTGError, PreviewError) as exc: return _failure(str(exc), exc.code)


async def _fresh(client: KTGClient, action: str, before: JsonObject | None, after: JsonObject | None) -> None:
    if action == "create":
        assert after is not None
        if await _code_matches(client, cast(str, after["repairCode"])): raise PreviewError("PREVIEW_STALE", "编号已被占用。")
        source = await _source(client, "machinery", after["machineryId"])
        if any(after.get(key) != value for key, value in source.items()): raise PreviewError("PREVIEW_STALE", "设备资料已变化。")
        return
    assert before is not None
    repair = cast(JsonObject, before["repair"]) if "repair" in before else before
    current = await fetch_repair(client, repair["repairId"])
    if current != repair or current["status"] != "PREPARE": raise PreviewError("PREVIEW_STALE", "维修单已变化或不再是草稿。")
    if action == "update" and after is not None:
        if after["repairCode"] != repair["repairCode"] and await _code_matches(client, cast(str, after["repairCode"]), cast(int, repair["repairId"])):
            raise PreviewError("PREVIEW_STALE", "编号已被占用。")
        if after["machineryId"] != repair["machineryId"]:
            source = await _source(client, "machinery", after["machineryId"])
            if any(after.get(key) != value for key, value in source.items()): raise PreviewError("PREVIEW_STALE", "设备资料已变化。")
    if action == "delete":
        lines = await _all_lines(client, cast(int, repair["repairId"]))
        if _summary(lines) != before["lines"]: raise PreviewError("PREVIEW_STALE", "所属维修内容已变化。")
    if action.startswith("line_"):
        line = before.get("line")
        if isinstance(line, Mapping):
            current_line = await fetch_line(client, line["lineId"])
            if current_line != line or current_line["repairId"] != repair["repairId"]:
                raise PreviewError("PREVIEW_STALE", "维修内容或归属已变化。")
        if action != "line_delete" and after is not None and (not isinstance(line, Mapping) or after["subjectId"] != line["subjectId"]):
            source = await _source(client, "subject", after["subjectId"])
            if any(after.get(key) != value for key, value in source.items()): raise PreviewError("PREVIEW_STALE", "项目资料已变化。")


def _wire(action: str, before: JsonObject | None, after: JsonObject | None) -> JsonObject:
    assert after is not None
    if action in {"create", "update"}:
        keys = ("repairCode", "repairName", "machineryId", "machineryCode", "machineryName", "machineryBrand",
                "machinerySpec", "machineryTypeId", "requireDate", "remark")
        prior = before if action == "update" else None
        payload = {key: after[key] for key in keys if key in after and (prior is None or after[key] != prior.get(key))}
        if action == "create": payload["status"] = "PREPARE"
        else: payload["repairId"] = after["repairId"]
        return payload
    keys = ("subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent",
            "subjectStandard", "malfunction", "malfunctionUrl", "repairDes", "remark")
    prior = cast(JsonObject | None, before.get("line")) if before else None
    payload = {key: after[key] for key in keys if key in after and (not isinstance(prior, Mapping) or after[key] != prior.get(key))}
    if action == "line_create": payload["repairId"] = after["repairId"]
    else: payload["lineId"] = after["lineId"]
    return payload


async def _execute(client: KTGClient, action: str, preview_id: str, before: JsonObject | None, after: JsonObject | None) -> ToolResult:
    sent = False
    acknowledged = False
    try:
        if action == "create":
            assert after is not None
            sent = True; await client.post(REPAIR, _wire(action, before, after)); acknowledged = True
            matches = await _code_matches(client, cast(str, after["repairCode"]))
            if len(matches) == 1 and _matches(matches[0], after, ("repairCode", "repairName", "machineryId",
                    "machineryCode", "machineryName", "machineryBrand", "machinerySpec", "machineryTypeId",
                    "requireDate", "status", "remark")):
                return _result(action, preview_id, "succeeded", "维修单已创建并唯一回读核验。", repair_id=cast(int, matches[0]["repairId"]))
        elif action == "update":
            assert after is not None
            sent = True; await client.put(REPAIR, _wire(action, before, after)); acknowledged = True
            row = await fetch_repair(client, after["repairId"])
            if _matches(row, after, ("repairCode", "repairName", "machineryId", "machineryCode", "machineryName", "machineryBrand", "machinerySpec", "machineryTypeId", "requireDate", "status", "remark")):
                return _result(action, preview_id, "succeeded", "维修单已修改并回读核验。", repair_id=cast(int, after["repairId"]))
        elif action == "delete":
            assert before is not None
            repair = cast(JsonObject, before["repair"])
            rid = cast(int, repair["repairId"])
            sent = True; await client.delete(f"{REPAIR}/{rid}"); acknowledged = True
            raw = await client.get(f"{REPAIR}/{rid}")
            remaining = await _all_lines(client, rid) if raw.get("data") is not None else await _line_rows_without_parent(client, rid)
            if raw.get("data") is None and not remaining:
                return _result(action, preview_id, "succeeded", "维修单及维修内容已删除并回读核验。", repair_id=rid)
        elif action == "line_create":
            assert before is not None and after is not None
            rid = cast(int, cast(JsonObject, before["repair"])["repairId"])
            previous = await _line_rows_without_parent(client, rid)
            sent = True; await client.post(LINE, _wire(action, before, after)); acknowledged = True
            current = await _line_rows_without_parent(client, rid)
            ids = {row["lineId"] for row in previous}
            matches = [row for row in current if row["lineId"] not in ids and _matches(row, after, ("repairId", "subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent", "subjectStandard", "malfunction", "malfunctionUrl", "repairDes", "remark"))]
            if len(matches) == 1: return _result(action, preview_id, "succeeded", "维修内容已新增并回读核验。", repair_id=rid, line_id=cast(int, matches[0]["lineId"]))
        elif action == "line_update":
            assert before is not None and after is not None
            sent = True; await client.put(LINE, _wire(action, before, after)); acknowledged = True
            row = await fetch_line(client, after["lineId"])
            if _matches(row, after, ("lineId", "repairId", "subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent", "subjectStandard", "malfunction", "malfunctionUrl", "repairDes", "remark")):
                return _result(action, preview_id, "succeeded", "维修内容已修改并回读核验。", repair_id=cast(int, after["repairId"]), line_id=cast(int, after["lineId"]))
        else:
            assert before is not None
            row = cast(JsonObject, before["line"])
            sent = True; await client.delete(f"{LINE}/{row['lineId']}"); acknowledged = True
            raw = await client.get(f"{LINE}/{row['lineId']}")
            if raw.get("data") is None: return _result(action, preview_id, "succeeded", "维修内容已删除并回读核验。", repair_id=cast(int, row["repairId"]), line_id=cast(int, row["lineId"]))
        return _result(action, preview_id, "unknown", "MES写入已发出，但回读未匹配；请查询核对，勿重复提交。", error="WRITE_RESULT_UNVERIFIED")
    except Exception as exc:
        if sent and (acknowledged or not isinstance(exc, KTGError) or (exc.code not in _BUSINESS_ERROR and exc.status not in {401, 403})):
            return _result(action, preview_id, "unknown", "写请求已发出，结果未知；请查询核对，勿重复提交。", error="WRITE_RESULT_UNKNOWN")
        raise


async def _line_rows_without_parent(client: KTGClient, repair_id: int) -> list[JsonObject]:
    result: list[JsonObject] = []
    for page in range(1, 12):
        rows, total = line_page(await client.get(f"{LINE}/list", {"repairId": repair_id, "pageNum": page, "pageSize": 100}))
        if any(row["repairId"] != repair_id for row in rows): raise KTGError("KTG_INVALID_RESPONSE", "维修内容归属不符。")
        result.extend(rows)
        if len(result) >= total: return result
        if not rows: raise KTGError("KTG_INVALID_RESPONSE", "维修内容分页提前结束。")
    raise KTGError("TOO_MANY_LINES", "维修内容超过1000条。")


def _matches(row: JsonObject, expected: JsonObject, fields: tuple[str, ...]) -> bool:
    return all(row.get(key) == expected.get(key) for key in fields)


async def _apply(store: DvRepairPreviewStore, action: str, context: ToolContext, args: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    preview_id = args.get("operation_id")
    if client is None: return _failure("KTG查询客户端未配置。", "KTG_CLIENT_MISSING")
    if context.channel is not ToolChannel.TRUSTED_CONFIRMATION:
        return _failure("维修写入只能通过网页可信确认。", "CONFIRMATION_REQUIRED")
    if not isinstance(preview_id, str) or context.operation_id != preview_id or context.confirmation_id is None or not 36 <= len(preview_id) <= 100:
        return _failure("确认上下文与预览不匹配。", "CONFIRMATION_MISMATCH")
    begun = False
    try:
        record, cached = store.begin(preview_id, _identity_key(context.identity), action)
        if cached is not None: return cached
        begun = True
        before, after = cast(JsonObject | None, record["before_json"]), cast(JsonObject | None, record["after_json"])
        await _fresh(client, action, before, after)
        result = await _execute(client, action, preview_id, before, after)
    except (PreviewError, KTGError) as exc:
        result = _result(action, preview_id, "failed", str(exc), error=exc.code)
    except Exception:
        result = _result(action, preview_id, "failed", "维修操作发生错误。", error="WRITE_FAILED")
    if begun: store.finish(preview_id, result)
    return result


def _result(action: str, preview_id: str, outcome: str, label: str, *, error: str | None = None,
            repair_id: int | None = None, line_id: int | None = None) -> ToolResult:
    details: JsonObject = {"kind": "dv_repair_result", "action": action, "preview_id": preview_id,
        "outcome": outcome, "verified": outcome == "succeeded", "label": label}
    if error: details["error_code"] = error
    if repair_id is not None: details["repair_id"] = repair_id
    if line_id is not None: details["line_id"] = line_id
    return ToolResult(content=label, details=details,
        status=ToolStatus.SUCCEEDED if outcome == "succeeded" else ToolStatus.UNKNOWN if outcome == "unknown" else ToolStatus.FAILED,
        error_code=None if outcome == "succeeded" else error or "WRITE_FAILED", operation_id=preview_id)


def _identity_key(identity: object | None) -> str:
    if not isinstance(identity, Mapping): raise PreviewError("AUTH_INVALID", "KTG未返回有效用户身份。")
    user_id = identity.get("userId")
    if isinstance(user_id, int) and not isinstance(user_id, bool): return f"id:{user_id}"
    username = identity.get("userName")
    if isinstance(username, str) and username.strip(): return f"name:{username.strip()}"
    raise PreviewError("AUTH_INVALID", "KTG未返回有效用户身份。")
