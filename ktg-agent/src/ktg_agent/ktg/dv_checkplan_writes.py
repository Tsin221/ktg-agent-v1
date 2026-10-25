"""Preview and trusted-confirmation writes for point-inspection plans."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, UTC
import hashlib
import json
import hashlib
from pathlib import Path
import sqlite3
from threading import Lock
from time import time
from typing import cast
from uuid import uuid4

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_checkplan import (BASELINE_CYCLES, MACHINERY_LINK_PATH, MACHINERY_PATH,
    PLAN_LIST, PLAN_PATH, PLAN_STATUSES, PLAN_TYPES, SUBJECT_LINK_PATH, SUBJECT_PATH,
    _date, _link_page, _positive_id, _text, fetch_plan, plan_page, read_plan_options)
from ktg_agent.ktg.dv_machinery import _bounded_integer, _schema, _output_schema, _client, _failure
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import (ExecutionMode, RetryPolicy, ToolChannel, ToolContext,
    ToolDefinition, ToolEffect, ToolResult, ToolStatus)

_ACTIONS = ("create", "update", "delete", "machinery_add", "machinery_remove", "subject_add", "subject_remove")
PREVIEW_NAMES = {action: f"ktg_preview_dv_checkplan_{action}" for action in _ACTIONS}
APPLY_NAMES = {action: f"ktg_dv_checkplan_{action}" for action in _ACTIONS}
_BASE = ("mes:dv:checkplan:query",)
_PERMS = {
    "create": ("mes:dv:checkplan:add", "mes:dv:checkplan:list"),
    "update": ("mes:dv:checkplan:edit", "mes:dv:checkplan:query", "mes:dv:checkplan:list", "mes:dv:checkmachinery:list", "mes:dv:checksubject:list"),
    "delete": ("mes:dv:checkplan:remove", "mes:dv:checkplan:query", "mes:dv:checkmachinery:list", "mes:dv:checksubject:list"),
    "machinery_add": ("mes:dv:checkplan:query", "mes:dv:checkplan:list", "mes:dv:checkmachinery:add", "mes:dv:checkmachinery:list", "mes:dv:machinery:list", "mes:dv:machinery:query"),
    "machinery_remove": ("mes:dv:checkplan:query", "mes:dv:checkmachinery:list", "mes:dv:checkmachinery:query", "mes:dv:checkmachinery:remove"),
    "subject_add": ("mes:dv:checkplan:query", "mes:dv:checksubject:add", "mes:dv:checksubject:list", "mes:dv:dvsubject:list", "mes:dv:dvsubject:query"),
    "subject_remove": ("mes:dv:checkplan:query", "mes:dv:checksubject:list", "mes:dv:checksubject:query", "mes:dv:checksubject:remove"),
}
_BUSINESS_ERROR = {"KTG_REQUEST_FAILED", "AUTH_EXPIRED", "AUTH_REQUIRED", "AUTH_INVALID", "PERMISSION_DENIED"}


class DvCheckPlanPreviewStore:
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
            db.execute("""CREATE TABLE IF NOT EXISTS dv_checkplan_previews (
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
            db.execute("DELETE FROM dv_checkplan_previews WHERE state='pending' AND expires_at<=?", (now,))
            db.execute("INSERT INTO dv_checkplan_previews VALUES (?,?,?,?,?,?,?,'pending',NULL)",
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
            row = db.execute("SELECT * FROM dv_checkplan_previews WHERE preview_id=?", (preview_id,)).fetchone()
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
                db.execute("UPDATE dv_checkplan_previews SET state='completed',result_json=? WHERE preview_id=?",
                           (self._encode_result(result), preview_id))
                record["state"] = "completed"
                return record, result
            if record["expires_at"] <= time():
                raise PreviewError("PREVIEW_EXPIRED", "预览已过期，请重新生成。")
            db.execute("UPDATE dv_checkplan_previews SET state='executing' WHERE preview_id=? AND state='pending'", (preview_id,))
            record["state"] = "executing"
            record["before_json"] = json.loads(record["before_json"]) if record["before_json"] else None
            record["after_json"] = json.loads(record["after_json"]) if record["after_json"] else None
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE dv_checkplan_previews SET state='completed',result_json=? WHERE preview_id=?",
                       (self._encode_result(result), preview_id))

    @classmethod
    def _validate_digest(cls, record: Mapping[str, object]) -> None:
        before = json.loads(cast(str, record["before_json"])) if record["before_json"] else None
        after = json.loads(cast(str, record["after_json"])) if record["after_json"] else None
        if cls._digest(cast(str, record["action"]), before, after) != record["digest"]:
            raise PreviewError("PREVIEW_STALE", "预览内容校验失败，请重新生成。")

    def _read(self, preview_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM dv_checkplan_previews WHERE preview_id=?", (preview_id,)).fetchone()
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


def build_dv_checkplan_write_tools(store: DvCheckPlanPreviewStore) -> tuple[ToolDefinition, ...]:
    return tuple(_definition(store, action, hidden) for action in _ACTIONS for hidden in (False, True))


def dv_checkplan_write_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
    def factory(item: ToolDefinition) -> ToolFactory:
        return lambda: item
    return {f"dv_writes:{item.name}": factory(item) for item in definitions}


def dv_checkplan_write_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
    return tuple(ToolManifest(name=item.name, version=item.version, module="dv", domain="checkplan",
        label=item.label, description=item.description,
        permission_policy=PermissionAll(tuple(PermissionLeaf(p) for p in _PERMS[_action_for_name(item.name)])),
        effect=item.effect, execution_mode=item.execution_mode, retry_policy=item.retry_policy,
        timeout_seconds=item.timeout_seconds, expose_to_model=item.expose_to_model,
        confirmation_policy="none" if item.expose_to_model else "trusted_confirmation",
        proposal_store_effect=item.name.startswith("ktg_preview_"), handler_ref=f"dv_writes:{item.name}")
        for item in definitions)


def _action_for_name(name: str) -> str:
    for action in _ACTIONS:
        if name in {PREVIEW_NAMES[action], APPLY_NAMES[action]}:
            return action
    raise ValueError("unknown plan write tool")


def _definition(store: DvCheckPlanPreviewStore, action: str, hidden: bool) -> ToolDefinition:
    name = APPLY_NAMES[action] if hidden else PREVIEW_NAMES[action]
    schema: JsonObject
    if hidden:
        schema = _schema({"operation_id": {"type": "string", "minLength": 36, "maxLength": 100}}, ("operation_id",))
        label, desc = f"确认{_label(action)}计划", f"仅可信确认通道可用：执行已确认的计划{_label(action)}预览。模型不可见。"
    else:
        schema = cast(JsonObject, _preview_schema(action))
        label, desc = f"预览{_label(action)}计划", _PREVIEW_DESCRIPTION[action]

    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        return await (_apply(store, action, context, arguments) if hidden else _preview(store, action, context, arguments))

    return ToolDefinition(name=name, version="1.0", label=label, description=desc,
        input_schema=schema,
        output_schema={"type": "object", "properties": {key: {} for key in
            ("kind", "action", "preview_id", "before", "after", "warnings", "affected_counts", "outcome", "verified")},
            "additionalProperties": True},
        required_permissions=_PERMS[action], effect=ToolEffect.WRITE if hidden else ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL, retry_policy=RetryPolicy.NEVER,
        timeout_seconds=30, execute=execute, expose_to_model=not hidden)


_PREVIEW_DESCRIPTION = {
    "create": "创建手工编号的计划草稿预览。必须给编号、名称、类型、周期和周期次数；不调用自动编号。",
    "update": "编辑PREPARE计划，或以互斥transition=activate/deactivate切换状态；启用需设备和项目明细均非空。",
    "delete": "单条删除PREPARE计划，预览显示级联删除设备及项目关联的影响；不可恢复。",
    "machinery_add": "给PREPARE计划关联单台设备；检查跨所有计划（含草稿）的同类型设备冲突。",
    "machinery_remove": "按明确record_id从PREPARE计划移除一条设备快照。",
    "subject_add": "给PREPARE计划关联一个已启用且计划类型匹配的项目；MES从源项目保存快照。",
    "subject_remove": "按明确record_id从PREPARE计划移除一条项目快照。",
}


def _preview_schema(action: str) -> dict[str, object]:
    positive = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
    if action == "create":
        props: dict[str, object] = {"plan_code": {"type": "string", "minLength": 1, "maxLength": 64},
            "plan_name": {"type": "string", "minLength": 1, "maxLength": 100},
            "plan_type": {"type": "string", "enum": list(PLAN_TYPES)},
            "cycle_type": {"type": "string", "minLength": 1, "maxLength": 64},
            "cycle_count": {"type": "integer", "minimum": 1, "maximum": 99999999},
            "start_date": {"type": "string", "format": "date"}, "end_date": {"type": "string", "format": "date"},
            "remark": {"type": "string", "maxLength": 250}}
        required: tuple[str, ...] = ("plan_code", "plan_name", "plan_type", "cycle_type", "cycle_count")
    elif action == "update":
        props = {"plan_id": positive, "transition": {"type": "string", "enum": ["edit", "activate", "deactivate"]}}
        business = {"plan_code": {"type": "string", "minLength": 1, "maxLength": 64},
            "plan_name": {"type": "string", "minLength": 1, "maxLength": 100},
            "start_date": {"type": "string", "format": "date"}, "end_date": {"type": "string", "format": "date"},
            "cycle_type": {"type": "string", "minLength": 1, "maxLength": 64},
            "cycle_count": {"type": "integer", "minimum": 1, "maximum": 99999999}, "remark": {"type": "string", "maxLength": 250}}
        base = {"plan_id": positive}
        edit_props = {**base, "transition": {"const": "edit"}, **business}
        branches: list[dict[str, object]] = [{"type": "object", "properties": edit_props,
            "required": ["plan_id", "transition"], "minProperties": 3, "additionalProperties": False}]
        for transition in ("activate", "deactivate"):
            branches.append({"type": "object", "properties": {"plan_id": positive,
                "transition": {"const": transition}}, "required": ["plan_id", "transition"], "additionalProperties": False})
        return {"type": "object", "properties": {**base, "transition": props["transition"], **business},
                "oneOf": branches, "additionalProperties": False}
    elif action in {"delete"}:
        props, required = {"plan_id": positive}, ("plan_id",)
    elif action in {"machinery_add", "subject_add"}:
        props, required = {"plan_id": positive, "machinery_id" if action == "machinery_add" else "subject_id": positive}, ("plan_id", "machinery_id" if action == "machinery_add" else "subject_id")
    else:
        props, required = {"plan_id": positive, "record_id": positive}, ("plan_id", "record_id")
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


async def _preview(store: DvCheckPlanPreviewStore, action: str, context: ToolContext,
                   args: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    if client is None:
        return _failure("KTG查询客户端未配置。", "KTG_CLIENT_MISSING")
    try:
        user_key = _identity_key(context.identity)
        before, after, affected = await _make_preview(client, action, args)
        preview_id = store.create(user_key, action, before, after)
        details: JsonObject = {"kind": "dv_checkplan_preview", "action": action, "preview_id": preview_id,
            "before": before, "after": after, "warnings": cast(JsonValue, _warnings(action)),
            "affected_counts": affected, "expires_at": datetime.fromtimestamp(time() + store.ttl_seconds, UTC).isoformat(),
            "digest": DvCheckPlanPreviewStore._digest(action, before, after)}
        return ToolResult(content=_preview_content(action, preview_id, before, after, affected), details=details, operation_id=preview_id)
    except (KTGError, PreviewError) as exc:
        return _failure(str(exc), exc.code)


async def _make_preview(client: KTGClient, action: str, args: Mapping[str, object]) -> tuple[JsonObject | None, JsonObject | None, JsonObject]:
    allowed = set(cast(dict[str, object], _preview_schema(action).get("properties", {})))
    if action == "update":
        allowed |= {"plan_id", "transition"}
    if set(args) - allowed:
        raise KTGError("VALIDATION_ERROR", "包含未支持的计划字段。")
    affected: JsonObject = {}
    if action == "create":
        after = await _create_plan_payload(client, args)
        return None, after, affected
    plan_id = _positive_id(args.get("plan_id"), "plan_id")
    plan = await fetch_plan(client, plan_id)
    if action == "update":
        before = plan
        transition = args.get("transition")
        if transition == "edit":
            fields = set(args) - {"plan_id", "transition"}
            if not fields:
                raise KTGError("VALIDATION_ERROR", "edit至少提供一个业务字段。")
            if plan["status"] != "PREPARE":
                raise KTGError("PLAN_NOT_DRAFT", "仅PREPARE草稿允许编辑计划字段。")
            after = await _edit_plan_payload(client, plan, args)
        elif transition == "activate":
            if set(args) != {"plan_id", "transition"} or plan["status"] != "PREPARE":
                raise KTGError("INVALID_TRANSITION", "activate仅允许PREPARE计划且不能附加业务字段。")
            mach, _ = await _plan_links(client, plan_id, "machinery")
            subs, _ = await _plan_links(client, plan_id, "subject")
            if not mach or not subs:
                raise KTGError("PLAN_LINKS_REQUIRED", "启用计划前必须至少关联一台设备和一个项目。")
            after = dict(plan); after["status"] = "FINISHED"; after["transition"] = "activate"
            affected = {"machinery_count": len(mach), "subject_count": len(subs)}
            before = {"plan": plan, "machinery": _link_summary(mach), "subject": _link_summary(subs)}
        elif transition == "deactivate":
            if set(args) != {"plan_id", "transition"} or plan["status"] != "FINISHED":
                raise KTGError("INVALID_TRANSITION", "deactivate仅允许FINISHED计划且不能附加业务字段。")
            after = dict(plan); after["status"] = "PREPARE"; after["transition"] = "deactivate"
        else:
            raise KTGError("VALIDATION_ERROR", "transition只允许edit、activate或deactivate。")
        return before, after, affected
    if action == "delete":
        if set(args) != {"plan_id"} or plan["status"] != "PREPARE":
            raise KTGError("PLAN_NOT_DRAFT", "只能删除单条PREPARE草稿计划。")
        machinery, _ = await _plan_links(client, plan_id, "machinery")
        subjects, _ = await _plan_links(client, plan_id, "subject")
        affected = {"machinery_count": len(machinery), "subject_count": len(subjects)}
        before = {"plan": plan, "machinery": _link_summary(machinery), "subject": _link_summary(subjects), "affected_counts": affected}
        return before, None, affected
    if plan["status"] != "PREPARE":
        raise KTGError("PLAN_NOT_DRAFT", "仅PREPARE计划允许修改关联。")
    if action == "machinery_add":
        mid = _positive_id(args.get("machinery_id"), "machinery_id")
        source = await _machinery(client, mid)
        links, _ = await _plan_links(client, plan_id, "machinery")
        if any(link["machineryId"] == mid for link in links):
            raise KTGError("LINK_EXISTS", "该设备已关联到此计划。")
        conflicts = await _machinery_conflicts(client, mid, cast(str, plan["planType"]), plan_id)
        if conflicts:
            raise KTGError("MACHINERY_PLAN_CONFLICT", f"设备已关联同类型计划：{', '.join(str(x) for x in conflicts[:10])}。")
        return {"plan": plan, "machinery": source, "links": _link_summary(links)}, {key: source[key] for key in
            ("machineryId", "machineryCode", "machineryName", "machineryBrand", "machinerySpec")}, affected
    if action == "subject_add":
        sid = _positive_id(args.get("subject_id"), "subject_id")
        source = await _subject(client, sid)
        if source.get("enableFlag") != "Y" or source.get("subjectType") != plan["planType"]:
            raise KTGError("SUBJECT_NOT_ELIGIBLE", "项目必须启用且类型与计划类型匹配。")
        links, _ = await _plan_links(client, plan_id, "subject")
        if any(link["subjectId"] == sid for link in links):
            raise KTGError("LINK_EXISTS", "该项目已关联到此计划。")
        return {"plan": plan, "subject": source, "links": _link_summary(links)}, {key: source.get(key) for key in
            ("subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent", "subjectStandard", "enableFlag")}, affected
    record_id = _positive_id(args.get("record_id"), "record_id")
    link_kind = "machinery" if action == "machinery_remove" else "subject"
    record = await _link_detail(client, link_kind, record_id)
    if record["planId"] != plan_id:
        raise KTGError("LINK_PLAN_MISMATCH", "record_id不属于指定计划。")
    links, _ = await _plan_links(client, plan_id, link_kind)
    return {"plan": plan, "record": record, "links": _link_summary(links)}, {"planId": plan_id, "recordId": record_id}, affected


async def _create_plan_payload(client: KTGClient, args: Mapping[str, object]) -> JsonObject:
    code = _text(args.get("plan_code"), "plan_code", 64)
    name = _text(args.get("plan_name"), "plan_name", 100)
    plan_type_value = args.get("plan_type")
    if not isinstance(plan_type_value, str) or plan_type_value not in PLAN_TYPES:
        raise KTGError("VALIDATION_ERROR", "plan_type只允许CHECK或MAINTEN。")
    plan_type = plan_type_value
    cycle_type = _text(args.get("cycle_type"), "cycle_type", 64)
    options = await read_plan_options(client)
    cycles = [row["value"] for row in cast(list[JsonObject], options["cycle_types"])]
    if cycle_type not in cycles:
        raise KTGError("CYCLE_TYPE_INVALID", "cycle_type不是当前MES周期字典值。")
    count = _bounded_integer(args.get("cycle_count"), "cycle_count", 1, 99999999)
    after: JsonObject = {"planCode": code, "planName": name, "planType": plan_type,
        "cycleType": cycle_type, "cycleCount": count, "status": "PREPARE",
        "remark": _text(args.get("remark", ""), "remark", 250, required=False)}
    for key, wire in (("start_date", "startDate"), ("end_date", "endDate")):
        if key in args: after[wire] = _date(args[key], key)
    _check_dates(after.get("startDate"), after.get("endDate"))
    if await _plan_code_matches(client, code):
        raise KTGError("PLAN_CODE_EXISTS", "计划编号在CHECK/MAINTEN共享编号空间已存在。")
    return after


async def _edit_plan_payload(client: KTGClient, plan: JsonObject, args: Mapping[str, object]) -> JsonObject:
    after = dict(plan)
    mapping = {"plan_code": ("planCode", 64), "plan_name": ("planName", 100), "remark": ("remark", 250)}
    for key, (wire, maximum) in mapping.items():
        if key in args: after[wire] = _text(args[key], key, maximum, required=key != "remark")
    if "cycle_type" in args:
        value = _text(args["cycle_type"], "cycle_type", 64)
        options = await read_plan_options(client)
        if value not in [row["value"] for row in cast(list[JsonObject], options["cycle_types"])]:
            raise KTGError("CYCLE_TYPE_INVALID", "cycle_type不是当前MES周期字典值。")
        after["cycleType"] = value
    if "cycle_count" in args: after["cycleCount"] = _bounded_integer(args["cycle_count"], "cycle_count", 1, 99999999)
    for key, wire in (("start_date", "startDate"), ("end_date", "endDate")):
        if key in args: after[wire] = _date(args[key], key)
    _check_dates(after.get("startDate"), after.get("endDate"))
    if all(after.get(key) == plan.get(key) for key in after if key in plan):
        raise KTGError("NO_CHANGE", "未检测到计划字段变化。")
    if "plan_code" in args and await _plan_code_matches(client, cast(str, after["planCode"]), cast(int, plan["planId"])):
        raise KTGError("PLAN_CODE_EXISTS", "计划编号在CHECK/MAINTEN共享编号空间冲突。")
    return after


def _check_dates(start: object, end: object) -> None:
    if start is not None and end is not None and cast(str, end) < cast(str, start):
        raise KTGError("VALIDATION_ERROR", "计划结束日期不能早于开始日期。")


async def _plan_code_matches(client: KTGClient, code: str, exclude_id: int | None = None) -> bool:
    rows, total = plan_page(await client.get(PLAN_LIST, {"planCode": code, "pageNum": 1, "pageSize": 100}))
    if total > 100:
        raise KTGError("PLAN_CODE_SCAN_INCOMPLETE", "编号查重结果超过安全扫描上限，不能确认编号唯一。")
    if total != len(rows) or len({row["planId"] for row in rows}) != len(rows):
        raise KTGError("KTG_INVALID_RESPONSE", "编号查重分页不完整或包含重复ID。")
    if any(row["planCode"] != code for row in rows):
        raise KTGError("KTG_INVALID_RESPONSE", "编号查询返回了不匹配的计划，已拒绝确认。")
    return any(row["planCode"] == code and row["planId"] != exclude_id for row in rows)


async def _plan_links(client: KTGClient, plan_id: int, kind: str) -> tuple[list[JsonObject], int]:
    path = MACHINERY_LINK_PATH if kind == "machinery" else SUBJECT_LINK_PATH
    first = await client.get(f"{path}/list", {"planId": plan_id, "pageNum": 1, "pageSize": 100})
    rows, total = _link_page(first, kind)
    if total > 1000: raise KTGError("LINK_SCAN_LIMIT", "关联记录超过安全扫描上限，拒绝写入。")
    if len(rows) != min(100, total): raise KTGError("KTG_INVALID_RESPONSE", "MES首屏关联内容不完整。")
    if any(row["planId"] != plan_id for row in rows): raise KTGError("KTG_INVALID_RESPONSE", "MES返回关联归属不一致。")
    page = 2
    record_ids = {row["recordId"] for row in rows}
    if len(record_ids) != len(rows): raise KTGError("KTG_INVALID_RESPONSE", "MES首屏含重复关联记录。")
    while len(rows) < total:
        payload = await client.get(f"{path}/list", {"planId": plan_id, "pageNum": page, "pageSize": 100})
        batch, batch_total = _link_page(payload, kind)
        if batch_total != total: raise KTGError("KTG_INVALID_RESPONSE", "MES关联分页总数发生变化。")
        if not batch or any(row["planId"] != plan_id for row in batch): raise KTGError("KTG_INVALID_RESPONSE", "MES关联分页不完整或归属不一致。")
        if len(batch) != min(100, total - len(rows)) or any(row["recordId"] in record_ids for row in batch):
            raise KTGError("KTG_INVALID_RESPONSE", "MES关联分页有缺项或重复记录。")
        record_ids.update(cast(int, row["recordId"]) for row in batch)
        rows.extend(batch); page += 1
    if len(rows) != total: raise KTGError("KTG_INVALID_RESPONSE", "MES关联总数与分页内容不一致。")
    return rows, total


async def _link_detail(client: KTGClient, kind: str, record_id: int) -> JsonObject:
    path = MACHINERY_LINK_PATH if kind == "machinery" else SUBJECT_LINK_PATH
    payload = await client.get(f"{path}/{record_id}")
    if payload.get("data") is None: raise KTGError("LINK_NOT_FOUND", "未找到该关联记录。", status=404)
    rows, _ = _link_page({"rows": [payload["data"]], "total": 1}, kind)
    if rows[0]["recordId"] != record_id: raise KTGError("KTG_INVALID_RESPONSE", "关联记录ID不一致。")
    return rows[0]


async def _machinery(client: KTGClient, mid: int) -> JsonObject:
    payload = await client.get(f"{MACHINERY_PATH}/{mid}")
    if payload.get("data") is None: raise KTGError("MACHINERY_NOT_FOUND", "未找到该设备。", status=404)
    from ktg_agent.ktg.dv_machinery import _project_machinery
    row = _project_machinery(payload["data"])
    if row["machineryId"] != mid: raise KTGError("KTG_INVALID_RESPONSE", "设备详情ID不一致。")
    return row


async def _subject(client: KTGClient, sid: int) -> JsonObject:
    payload = await client.get(f"{SUBJECT_PATH}/{sid}")
    if payload.get("data") is None: raise KTGError("SUBJECT_NOT_FOUND", "未找到该项目。", status=404)
    from ktg_agent.ktg.dv_subject import project_subject
    row = project_subject(payload["data"])
    if row["subjectId"] != sid: raise KTGError("KTG_INVALID_RESPONSE", "项目详情ID不一致。")
    return row


async def _machinery_conflicts(client: KTGClient, machinery_id: int, plan_type: str, plan_id: int) -> list[int]:
    path = f"{MACHINERY_LINK_PATH}/list"
    payload = await client.get(path, {"machineryId": machinery_id, "pageNum": 1, "pageSize": 100})
    total = payload.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or total > 1000:
        raise KTGError("MACHINERY_CONFLICT_SCAN_INCOMPLETE", "设备关联超过安全扫描上限，无法证明无冲突。")
    records: list[JsonObject] = []
    for page in range(1, (total + 99) // 100 + 1):
        item = payload if page == 1 else await client.get(path, {"machineryId": machinery_id, "pageNum": page, "pageSize": 100})
        rows, page_total = _link_page(item, "machinery")
        if page_total != total or len(rows) != min(100, total - len(records)):
            raise KTGError("KTG_INVALID_RESPONSE", "设备关联分页总数变化或内容不完整。")
        for row in rows:
            if row["machineryId"] != machinery_id:
                raise KTGError("KTG_INVALID_RESPONSE", "设备关联源数据不匹配。")
            records.append({"recordId": row["recordId"], "planId": row["planId"]})
    record_ids = [row["recordId"] for row in records]
    if len(records) != total or len(set(record_ids)) != len(record_ids):
        raise KTGError("KTG_INVALID_RESPONSE", "设备关联分页总数不一致或包含重复记录。")
    conflicts = []
    for record in records:
        linked_plan_id = cast(int, record["planId"])
        if linked_plan_id == plan_id: continue
        other = await fetch_plan(client, linked_plan_id)
        if other["planType"] == plan_type: conflicts.append(linked_plan_id)
    return conflicts


async def _apply(store: DvCheckPlanPreviewStore, action: str, context: ToolContext,
                 args: Mapping[str, object]) -> ToolResult:
    client = _client(context)
    preview_id = args.get("operation_id")
    if client is None: return _failure("KTG查询客户端未配置。", "KTG_CLIENT_MISSING")
    if context.channel is not ToolChannel.TRUSTED_CONFIRMATION:
        return _failure("计划写入只能通过网页可信确认通道执行。", "CONFIRMATION_REQUIRED")
    if (not isinstance(preview_id, str) or context.operation_id != preview_id or context.confirmation_id is None
            or not 36 <= len(preview_id) <= 100):
        return _failure("确认上下文与预览不匹配。", "CONFIRMATION_MISMATCH")
    begun = False
    try:
        record, cached = store.begin(preview_id, _identity_key(context.identity), action)
        if cached is not None: return cached
        begun = True
        before = cast(JsonObject | None, record["before_json"])
        after = cast(JsonObject | None, record["after_json"])
        _check_fresh(await _snapshot(client, action, before, after), before)
        result = await _execute_action(client, action, preview_id, before, after)
        store.finish(preview_id, result)
        return result
    except PreviewError as exc:
        result = _result(action, preview_id, "failed", str(exc), error=exc.code)
    except KTGError as exc:
        outcome = "unknown" if exc.code == "WRITE_RESULT_UNKNOWN" else "failed"
        result = _result(action, preview_id, outcome, str(exc), error=exc.code)
    except Exception:
        result = _result(action, preview_id, "failed", "计划操作发生错误。", error="WRITE_FAILED")
    if begun: store.finish(preview_id, result)
    return result


async def _snapshot(client: KTGClient, action: str, before: JsonObject | None, after: JsonObject | None) -> object:
    if action == "create":
        if after is None: raise PreviewError("PREVIEW_STALE", "预览缺少新计划数据。")
        options = await read_plan_options(client)
        cycles = [row["value"] for row in cast(list[JsonObject], options["cycle_types"])]
        if after.get("cycleType") not in cycles:
            raise PreviewError("PREVIEW_STALE", "周期字典已变化，请重新读取选项并生成预览。")
        if await _plan_code_matches(client, cast(str, after["planCode"])): raise PreviewError("PREVIEW_STALE", "编号已被占用，请重新预览。")
        return None
    if before is None: raise PreviewError("PREVIEW_STALE", "预览缺少当前数据快照。")
    parent_plan = before.get("plan")
    plan_id_value = parent_plan.get("planId") if isinstance(parent_plan, Mapping) else before.get("planId")
    plan_id = _positive_id(plan_id_value, "plan_id")
    plan = await fetch_plan(client, plan_id)
    if action in {"update", "delete"}:
        if action == "delete":
            mach, _ = await _plan_links(client, plan_id, "machinery"); subs, _ = await _plan_links(client, plan_id, "subject")
            return {"plan": plan, "machinery": _link_summary(mach), "subject": _link_summary(subs),
                    "affected_counts": {"machinery_count": len(mach), "subject_count": len(subs)}}
        if "plan" in before:
            mach, _ = await _plan_links(client, plan_id, "machinery")
            subs, _ = await _plan_links(client, plan_id, "subject")
            return {"plan": plan, "machinery": _link_summary(mach), "subject": _link_summary(subs)}
        return plan
    if action == "machinery_add":
        source = await _machinery(client, cast(int, cast(JsonObject, before["machinery"])["machineryId"]))
        links, _ = await _plan_links(client, plan_id, "machinery")
        if await _machinery_conflicts(client, cast(int, source["machineryId"]), cast(str, plan["planType"]), plan_id):
            raise PreviewError("PREVIEW_STALE", "设备已在其他同类型计划关联，请重新核对。")
        return {"plan": plan, "machinery": source, "links": _link_summary(links)}
    if action == "subject_add":
        source = await _subject(client, cast(int, cast(JsonObject, before["subject"])["subjectId"]))
        links, _ = await _plan_links(client, plan_id, "subject")
        return {"plan": plan, "subject": source, "links": _link_summary(links)}
    kind = "machinery" if action == "machinery_remove" else "subject"
    rid = cast(int, cast(JsonObject, before["record"])["recordId"])
    record = await _link_detail(client, kind, rid)
    links, _ = await _plan_links(client, plan_id, kind)
    return {"plan": plan, "record": record, "links": _link_summary(links)}


def _link_summary(rows: list[JsonObject]) -> JsonObject:
    # Digest covers the entire complete, <=1000-row snapshot; UI receives only examples.
    canonical = sorted(rows, key=lambda row: cast(int, row["recordId"]))
    digest = hashlib.sha256(DvCheckPlanPreviewStore._json(canonical).encode("utf-8")).hexdigest()
    return {"count": len(canonical), "digest": digest,
            "samples": cast(list[JsonValue], canonical[:10])}


def _check_fresh(current: object, before: JsonObject | None) -> None:
    if current is None and before is None: return
    if DvCheckPlanPreviewStore._json(current) != DvCheckPlanPreviewStore._json(before):
        raise PreviewError("PREVIEW_STALE", "计划、来源或关联已变化，请重新读取并生成预览。")


async def _execute_action(client: KTGClient, action: str, preview_id: str,
                          before: JsonObject | None, after: JsonObject | None) -> ToolResult:
    write_sent = False
    write_acknowledged = False
    try:
        if action == "create":
            assert after is not None
            write_sent = True
            await client.post(PLAN_PATH, dict(after))
            write_acknowledged = True
            rows, total = plan_page(await client.get(PLAN_LIST, {"planCode": after["planCode"], "pageNum": 1, "pageSize": 100}))
            match = [row for row in rows if row["planCode"] == after["planCode"] and _same_plan_values(row, after)]
            if total == 1 and len(match) == 1:
                return _result(action, preview_id, "succeeded", "计划已创建并按编号唯一回读核验。", verified=True, plan_id=cast(int, match[0]["planId"]), plan=match[0])
            return _result(action, preview_id, "unknown", "请求已发出但未能唯一回读核验；请先按编号查询，勿重复提交。", error="WRITE_RESULT_UNVERIFIED")
        if action == "update":
            assert before is not None and after is not None
            plan_id = cast(int, _plan_from_before(before)["planId"])
            body: dict[str, object] = {"planId": plan_id}
            if after.get("transition") in {"activate", "deactivate"}:
                body["status"] = after["status"]
            else:
                for key in ("planCode", "planName", "startDate", "endDate", "cycleType", "cycleCount", "remark"):
                    if after.get(key) != before.get(key): body[key] = after.get(key)
            write_sent = True
            await client.put(PLAN_PATH, body)
            write_acknowledged = True
            updated = await fetch_plan(client, plan_id)
            expected = {key: value for key, value in after.items() if key in updated}
            if all(updated.get(key) == value for key, value in expected.items()):
                return _result(action, preview_id, "succeeded", "计划已更新并回读核验。", verified=True, plan_id=plan_id, plan=updated)
            return _result(action, preview_id, "unknown", "计划更新已发出但回读未匹配；请先核对计划，勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=plan_id)
        if action == "delete":
            assert before is not None
            plan_id = cast(int, cast(JsonObject, before["plan"])["planId"])
            write_sent = True
            await client.delete(f"{PLAN_PATH}/{plan_id}")
            write_acknowledged = True
            head_missing = False
            try:
                payload = await client.get(f"{PLAN_PATH}/{plan_id}")
                head_missing = payload.get("data") is None
            except KTGError as exc:
                if exc.status == 404 or exc.code == "PLAN_NOT_FOUND":
                    head_missing = True
                else:
                    raise
            if head_missing:
                mach, _ = await _plan_links(client, plan_id, "machinery")
                subjects, _ = await _plan_links(client, plan_id, "subject")
                if not mach and not subjects:
                    return _result(action, preview_id, "succeeded", "计划及其关联已删除并回读核验。", verified=True, plan_id=plan_id)
                return _result(action, preview_id, "unknown", "计划已删除但关联仍存在，回读未能核验级联结果；勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=plan_id)
            return _result(action, preview_id, "unknown", "删除已发出但计划仍可读取；请核对计划和关联，勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=plan_id)
        if action == "machinery_add":
            assert before is not None and after is not None
            plan_id = cast(int, cast(JsonObject, before["plan"])["planId"])
            machinery_body: dict[str, object] = {"planId": plan_id, "machineryId": after["machineryId"],
                "machineryCode": after["machineryCode"], "machineryName": after["machineryName"],
                "machineryBrand": after["machineryBrand"], "machinerySpec": after["machinerySpec"]}
            write_sent = True
            await client.post(MACHINERY_LINK_PATH, machinery_body)
            write_acknowledged = True
            rows, _ = await _plan_links(client, plan_id, "machinery")
            matches = [row for row in rows if row["machineryId"] == after["machineryId"]]
            if len(matches) == 1 and _link_matches_preview(matches[0], after, "machinery"):
                return _result(action, preview_id, "succeeded", "设备已关联并回读核验。", verified=True, plan_id=plan_id, record_id=cast(int, matches[0]["recordId"]), link=matches[0])
            return _result(action, preview_id, "unknown", "设备关联已发出但回读未能唯一核实；勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=plan_id)
        if action == "subject_add":
            assert before is not None and after is not None
            plan_id = cast(int, cast(JsonObject, before["plan"])["planId"])
            write_sent = True
            await client.post(SUBJECT_LINK_PATH, {"planId": plan_id, "subjectId": after["subjectId"]})
            write_acknowledged = True
            rows, _ = await _plan_links(client, plan_id, "subject")
            matches = [row for row in rows if row["subjectId"] == after["subjectId"]]
            if len(matches) == 1 and _link_matches_preview(matches[0], after, "subject"):
                return _result(action, preview_id, "succeeded", "项目已关联并按MES保存快照回读核验。", verified=True, plan_id=plan_id, record_id=cast(int, matches[0]["recordId"]), link=matches[0])
            return _result(action, preview_id, "unknown", "项目关联已发出但回读未能唯一核实；勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=plan_id)
        assert before is not None
        record = cast(JsonObject, before["record"])
        kind = "machinery" if action == "machinery_remove" else "subject"
        path = MACHINERY_LINK_PATH if kind == "machinery" else SUBJECT_LINK_PATH
        write_sent = True
        await client.delete(f"{path}/{record['recordId']}")
        write_acknowledged = True
        try:
            remaining = await _link_detail(client, kind, cast(int, record["recordId"]))
            if remaining: return _result(action, preview_id, "unknown", "移除已发出但记录仍可读取；请核对关联，勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=cast(int, record["planId"]))
        except KTGError as exc:
            if exc.status == 404 or exc.code == "LINK_NOT_FOUND":
                return _result(action, preview_id, "succeeded", "关联已移除并回读核验。", verified=True, plan_id=cast(int, record["planId"]), record_id=cast(int, record["recordId"]))
            raise
        return _result(action, preview_id, "unknown", "移除已发出但回读未确认；勿重试。", error="WRITE_RESULT_UNVERIFIED", plan_id=cast(int, record["planId"]))
    except KTGError as exc:
        if write_acknowledged:
            return _result(action, preview_id, "unknown", "MES写请求已接受，但回读核验未能完成；请先查询当前计划及关联，勿重试。", error="WRITE_RESULT_UNVERIFIED")
        if write_sent and exc.code not in _BUSINESS_ERROR and exc.status not in {401, 403}:
            return _result(action, preview_id, "unknown", "写入请求已发出但未收到可确认响应；请先查询核对，勿重试。", error="WRITE_RESULT_UNKNOWN")
        if exc.code in _BUSINESS_ERROR or exc.status in {401, 403}:
            raise
        raise
    except Exception:
        if write_sent:
            return _result(action, preview_id, "unknown", "写请求之后的结果无法核验；请先查询核对，勿重试。", error="WRITE_RESULT_UNKNOWN")
        raise


def _link_matches_preview(link: JsonObject, preview: JsonObject, kind: str) -> bool:
    fields = (("machineryId", "machineryCode", "machineryName", "machineryBrand", "machinerySpec")
              if kind == "machinery" else
              ("subjectId", "subjectCode", "subjectName", "subjectType", "subjectContent", "subjectStandard"))
    return all(link.get(key) == preview.get(key) for key in fields)


def _same_plan_values(row: JsonObject, expected: JsonObject) -> bool:
    return all(row.get(key) == expected.get(key) for key in ("planCode", "planName", "planType", "cycleType", "cycleCount", "startDate", "endDate", "status", "remark"))


def _plan_from_before(before: JsonObject) -> JsonObject:
    plan = before.get("plan")
    return plan if isinstance(plan, Mapping) else before


def _result(action: str, preview_id: str, outcome: str, label: str, *, error: str | None = None,
            verified: bool = False, plan_id: int | None = None, record_id: int | None = None,
            plan: JsonObject | None = None, link: JsonObject | None = None) -> ToolResult:
    details: JsonObject = {"kind": "dv_checkplan_result", "action": action, "preview_id": preview_id,
                           "outcome": outcome, "verified": verified, "label": label}
    if plan_id is not None: details["plan_id"] = plan_id
    if record_id is not None: details["record_id"] = record_id
    if error is not None: details["error_code"] = error
    if plan is not None: details["plan"] = plan
    if link is not None: details["link"] = link
    content = label
    if plan_id is not None: content += f" 计划ID[{plan_id}]。"
    if record_id is not None: content += f" 关联recordId[{record_id}]。"
    if verified: content += " MES回读已核验。"
    elif outcome == "unknown": content += " 结果未知，请先查询核对，勿重复提交。"
    return ToolResult(content=content[:4000], details=details, status=ToolStatus.SUCCEEDED if outcome == "succeeded" else
        ToolStatus.UNKNOWN if outcome == "unknown" else ToolStatus.FAILED,
        error_code=None if outcome == "succeeded" else error or "WRITE_FAILED", operation_id=preview_id)


def _identity_key(identity: object | None) -> str:
    if not isinstance(identity, Mapping): raise PreviewError("AUTH_INVALID", "KTG未返回有效用户身份。")
    user_id = identity.get("userId")
    if isinstance(user_id, int) and not isinstance(user_id, bool): return f"id:{user_id}"
    username = identity.get("userName")
    if isinstance(username, str) and username.strip(): return f"name:{username.strip()}"
    raise PreviewError("AUTH_INVALID", "KTG未返回有效用户身份。")


def _warnings(action: str) -> list[str]:
    common = ["MES状态守卫与Agent预读之间仍存在并发窗口；不能据此承诺数据库级唯一或exactly-once。"]
    extra = {"create": ["编号由用户手工提供；不调用自动编号。并发编号竞态仍由MES处理。"],
        "update": ["仅支持PREPARE字段编辑及PREPARE/FINISHED有限状态切换。FINISHED值在页面表示启用。"],
        "delete": ["确认后会删除单条草稿计划及关联，操作不可恢复。"],
        "machinery_add": ["设备快照取当前设备台账；MES按设备ID和计划类型跨计划查重。"],
        "machinery_remove": ["仅移除本计划的一条历史设备关联，不删除设备台账。"],
        "subject_add": ["POST仅传planId/subjectId；MES从项目源保存快照。"],
        "subject_remove": ["仅移除本计划的一条历史项目关联，不删除源项目。"],}
    return common + extra[action]


def _label(action: str) -> str:
    return {"create": "新增", "update": "修改/状态切换", "delete": "删除", "machinery_add": "关联设备",
        "machinery_remove": "移除设备", "subject_add": "关联项目", "subject_remove": "移除项目"}[action]


def _preview_content(action: str, preview_id: str, before: JsonObject | None,
                     after: JsonObject | None, affected: JsonObject) -> str:
    if before and isinstance(before.get("plan"), Mapping):
        plan = cast(JsonObject, before["plan"])
    elif before and "planId" in before:
        plan = before
    elif after and "planCode" in after:
        plan = after
    else:
        plan = {}
    target = (f"计划ID[{plan.get('planId')}] 编号{plan.get('planCode')} 名称{plan.get('planName')} "
              if plan else f"计划编号{(after or {}).get('planCode')} 名称{(after or {}).get('planName')} ")
    changes = ""
    if action == "update" and before and after:
        if after.get("transition"):
            changes = f"转换 {before.get('status')}→{after.get('status')}（{after.get('transition')}）。"
        else:
            changes = "拟修改：" + "；".join(f"{key}={after.get(key)}" for key in
                ("planCode", "planName", "startDate", "endDate", "cycleType", "cycleCount", "remark")
                if after.get(key) != before.get(key)) + "。"
    elif action == "create" and after:
        changes = f"计划类型{after.get('planType')}；周期{after.get('cycleType')}×{after.get('cycleCount')}；状态PREPARE草稿。"
    elif action == "delete":
        changes = f"将级联删除设备关联{affected.get('machinery_count', 0)}条、项目关联{affected.get('subject_count', 0)}条。"
    elif action.endswith("_add") and after:
        source_id = after.get("machineryId") if action == "machinery_add" else after.get("subjectId")
        source_code = after.get("machineryCode") if action == "machinery_add" else after.get("subjectCode")
        source_name = after.get("machineryName") if action == "machinery_add" else after.get("subjectName")
        changes = f"拟关联ID[{source_id}] 编号{source_code} 名称{source_name}。"
    elif action.endswith("_remove") and before:
        record = cast(JsonObject, before["record"])
        source_id = record.get("machineryId") if action == "machinery_remove" else record.get("subjectId")
        changes = f"拟移除recordId[{record.get('recordId')}]、源ID[{source_id}]。"
    return f"计划{_label(action)}预览：{target}{changes} operation_id={preview_id}。此预览尚未写入MES；请用户在网页确认卡片操作。"[:4000]
