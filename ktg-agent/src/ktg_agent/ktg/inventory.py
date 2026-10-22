"""Server-owned inventory previews and durable operation bindings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, cast
from uuid import UUID, uuid4

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools import (
    ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect,
    ToolResult, ToolStatus,
)

RECEIPT = "ITEM_RECEIPT_EXECUTE"
ISSUE = "PRODUCTION_ISSUE_EXECUTE"
PREVIEW_TTL_SECONDS = 15 * 60
_ACTIONS = {
    RECEIPT: ("itemrecpt", "recptId", "recptCode", "additions", "mes:wm:itemrecpt:edit"),
    ISSUE: ("issueheader", "issueId", "issueCode", "movements", "mes:wm:issueheader:edit"),
}


def _user_key(identity: object) -> str:
    if not isinstance(identity, Mapping) or not isinstance(identity.get("userId"), int):
        raise ValueError("KTG 未返回有效用户 ID。")
    return f"id:{identity['userId']}"


def _digest(proposal: Mapping[str, object]) -> str:
    encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class InventoryStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS inventory_previews (
                operation_id TEXT PRIMARY KEY, user_key TEXT NOT NULL,
                action TEXT NOT NULL, document_id INTEGER NOT NULL,
                digest TEXT NOT NULL, preview_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'prepared', result_json TEXT,
                created_at REAL NOT NULL, expires_at REAL NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(inventory_previews)")}
            # Legacy previews have no known creation time; expire them safely.
            if "created_at" not in columns:
                db.execute("ALTER TABLE inventory_previews ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
            if "expires_at" not in columns:
                db.execute("ALTER TABLE inventory_previews ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, user_key: str, action: str, proposal: JsonObject) -> str:
        operation_id = str(uuid4())
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO inventory_previews "
                "(operation_id,user_key,action,document_id,digest,preview_json,state,result_json,created_at,expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, ?)",
                (operation_id, user_key, action, proposal["documentId"],
                 _digest(proposal), json.dumps(proposal, ensure_ascii=False),
                 now, now + PREVIEW_TTL_SECONDS),
            )
        return operation_id

    def get(self, operation_id: str, user_key: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM inventory_previews WHERE operation_id=? AND user_key=?",
                (operation_id, user_key),
            ).fetchone()
        return dict(row) if row else None

    def claim(self, operation_id: str, user_key: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE inventory_previews SET state='submitted' "
                "WHERE operation_id=? AND user_key=? AND state='prepared'",
                (operation_id, user_key),
            )
            return cursor.rowcount == 1

    def finish(self, operation_id: str, result: JsonObject) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE inventory_previews SET state='finished', result_json=? WHERE operation_id=?",
                (json.dumps(result, ensure_ascii=False), operation_id),
            )


def build_inventory_tools(store: InventoryStore, enabled: frozenset[str]) -> tuple[ToolDefinition, ...]:
    tools: list[ToolDefinition] = []
    for action, (_, _, _, _, permission) in _ACTIONS.items():
        if action not in enabled:
            continue

        async def preview(context: ToolContext, arguments: Mapping[str, object], selected: str = action) -> ToolResult:
            try:
                proposal = await _read_proposal(context, selected, arguments)
                operation_id = store.create(_user_key(context.identity), selected, proposal)
                return ToolResult(
                    content=f"已准备单据 {proposal['documentCode']} 的执行预览，请在网页卡片确认。",
                    details={"kind": "inventory_preview", "action_type": selected,
                             "operation_id": operation_id, **proposal},
                    operation_id=operation_id,
                )
            except (ValueError, KTGError) as exc:
                return _failure(str(exc), getattr(exc, "code", "VALIDATION_ERROR"))

        tools.append(ToolDefinition(
            name="ktg_preview_item_receipt" if action == RECEIPT else "ktg_preview_production_issue",
            version="1.0", label="预览物料入库" if action == RECEIPT else "预览生产领料",
            description="只读预览已有单据。仅传单据 ID 或准确单据编号，不接受库存数和执行参数。",
            input_schema={"type": "object", "properties": {
                "document_id": {"type": "integer", "minimum": 1},
                "document_code": {"type": "string", "minLength": 1, "maxLength": 100},
            }, "oneOf": [{"required": ["document_id"]}, {"required": ["document_code"]}],
                "additionalProperties": False},
            output_schema=None, required_permissions=(
                permission, permission.rsplit(":", 1)[0] + ":list",
                permission.rsplit(":", 1)[0] + ":query", "mes:wm:wmstock:list",
            ), effect=ToolEffect.READ,
            execution_mode=ExecutionMode.SEQUENTIAL, retry_policy=RetryPolicy.NEVER,
            timeout_seconds=25, execute=preview,
        ))

        async def execute(context: ToolContext, arguments: Mapping[str, object], selected: str = action) -> ToolResult:
            operation_id = arguments.get("operation_id")
            if not isinstance(operation_id, str) or context.operation_id != operation_id:
                return _failure("操作 ID 不匹配。", "CONTEXT_MISMATCH")
            result = await confirm_inventory(store, context, operation_id, selected)
            status = ToolStatus.SUCCEEDED if result.get("outcome") == "succeeded" else ToolStatus.UNKNOWN if result.get("outcome") == "unknown" else ToolStatus.FAILED
            return ToolResult(content=cast(str, result["label"]), details=result, status=status,
                              error_code=None if status is ToolStatus.SUCCEEDED else cast(str, result.get("error_code", "RESULT_UNKNOWN")),
                              operation_id=operation_id)

        tools.append(ToolDefinition(
            name="ktg_execute_item_receipt" if action == RECEIPT else "ktg_execute_production_issue",
            version="1.0", label="确认物料入库" if action == RECEIPT else "确认生产领料",
            description="仅执行当前用户已确认并持久绑定的库存单据预览。",
            input_schema={"type": "object", "properties": {"operation_id": {"type": "string"}},
                          "required": ["operation_id"], "additionalProperties": False},
            output_schema=None, required_permissions=(
                permission, permission.rsplit(":", 1)[0] + ":query", "mes:wm:wmstock:list",
            ), effect=ToolEffect.WRITE,
            execution_mode=ExecutionMode.SEQUENTIAL, retry_policy=RetryPolicy.RECONCILE_FIRST,
            timeout_seconds=45, execute=execute, expose_to_model=False,
        ))
    return tuple(tools)


def build_inventory_result_tool(store: InventoryStore) -> ToolDefinition:
    """Build the read-only inventory result tool for explicit catalog wiring."""
    async def read_result(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        operation_id = arguments.get("operation_id")
        if not isinstance(operation_id, str):
            return _failure("操作 ID 无效。", "VALIDATION_ERROR")
        try:
            result = await inventory_result(store, context, operation_id)
        except ValueError:
            return _failure("操作 ID 无效。", "VALIDATION_ERROR")
        if result.get("kind") == "inventory_preview":
            return ToolResult(
                content="库存操作尚未提交，等待用户确认。",
                details={"kind": "inventory_result", "operation_id": operation_id,
                         "outcome": "prepared"},
                status=ToolStatus.UNKNOWN, error_code="OPERATION_PENDING",
                operation_id=operation_id,
            )
        outcome = result.get("outcome")
        status = ToolStatus.SUCCEEDED if outcome == "succeeded" else ToolStatus.UNKNOWN if outcome == "unknown" else ToolStatus.FAILED
        return ToolResult(
            content=cast(str, result["label"]), details=result, status=status,
            error_code=None if status is ToolStatus.SUCCEEDED else cast(str, result.get("error_code", "RESULT_UNKNOWN")),
            operation_id=operation_id,
        )

    return ToolDefinition(
        name="ktg_wm_operation_result", version="1.0", label="查询库存操作回执",
        description="查询当前用户有权读取的库存入库或领料操作回执。",
        input_schema={"type": "object", "properties": {
            "operation_id": {"type": "string", "minLength": 36, "maxLength": 36},
        }, "required": ["operation_id"], "additionalProperties": False},
        output_schema=None, required_permissions=("mes:wm:wmstock:list",),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=25,
        execute=read_result,
    )


async def _read_proposal(context: ToolContext, action: str, arguments: Mapping[str, object]) -> JsonObject:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        raise ValueError("KTG 客户端不可用。")
    resource, id_key, code_key, lines_key, _ = _ACTIONS[action]
    document_id = arguments.get("document_id")
    if document_id is None:
        code = arguments.get("document_code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("请提供准确单据编号或 ID。")
        payload = await client.get(f"/mes/wm/{resource}/list", {code_key: code.strip(), "pageNum": 1, "pageSize": 100})
        rows = payload.get("rows")
        if not isinstance(rows, list) or payload.get("total") != 1:
            raise ValueError("单据编号未唯一匹配，请提供准确编号或 ID。")
        row = rows[0]
        if not isinstance(row, dict) or row.get(code_key) != code.strip():
            raise ValueError("单据编号未精确匹配，请提供 ID。")
        document_id = row.get(id_key)
    if not isinstance(document_id, int) or document_id <= 0:
        raise ValueError("单据 ID 无效。")
    # The result endpoint has separate read permissions. Check them before a
    # user can receive a confirmable preview.
    await client.get(f"/mes/wm/inventory-operations/documents/{action}/{document_id}")
    payload = await client.get(f"/mes/wm/{resource}/{document_id}/execution-preview")
    proposal = payload.get("data")
    if not isinstance(proposal, dict) or proposal.get("documentId") != document_id:
        raise KTGError("KTG_INVALID_RESPONSE", "单据预览无效。")
    if proposal.get("documentStatus") != "APPROVED" or not isinstance(proposal.get("documentVersion"), int):
        raise KTGError("KTG_INVALID_RESPONSE", "单据状态或版本无效。")
    if not isinstance(proposal.get(lines_key), list) or not proposal[lines_key]:
        raise KTGError("KTG_INVALID_RESPONSE", "单据没有可执行明细。")
    return cast(JsonObject, proposal)


def _failure(label: str, code: str) -> ToolResult:
    return ToolResult(content=label, status=ToolStatus.FAILED, error_code=code)


async def confirm_inventory(store: InventoryStore, context: ToolContext, operation_id: str, action: str) -> JsonObject:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        raise ValueError("KTG 客户端不可用。")
    row = store.get(str(UUID(operation_id)), _user_key(context.identity))
    if row is None or row["action"] != action:
        return _result(operation_id, "failed", "预览不存在或不属于当前用户。", "PREVIEW_NOT_FOUND")
    if row["state"] != "prepared":
        return await inventory_result(store, context, operation_id)
    try:
        await client.get(
            f"/mes/wm/inventory-operations/documents/{action}/{row['document_id']}"
        )
    except KTGError:
        return _result(operation_id, "failed", "无权读取此库存单据或操作不存在。", "PREVIEW_NOT_FOUND")
    if cast(float, row["expires_at"]) <= time.time():
        result = _bound_result(row, _result(operation_id, "failed", "预览已过期，请重新预览。", "PREVIEW_EXPIRED"))
        store.finish(operation_id, result)
        return result
    document_id = cast(int, row["document_id"])
    try:
        current = await _read_proposal(context, action, {"document_id": document_id})
    except (ValueError, KTGError) as exc:
        result = _bound_result(row, _result(operation_id, "failed", f"单据已变化或无法读取，请重新预览：{exc}", "PREVIEW_STALE"))
        store.finish(operation_id, result)
        return result
    if _digest(current) != row["digest"]:
        result = _bound_result(row, _result(operation_id, "failed", "单据明细、状态或库存位置已变化，请重新预览。", "PREVIEW_STALE"))
        store.finish(operation_id, result)
        return result
    if not store.claim(operation_id, _user_key(context.identity)):
        return await inventory_result(store, context, operation_id)
    command = {"actionType": action, "documentId": document_id, "operationId": operation_id,
               "expectedStatus": current["documentStatus"], "expectedVersion": current["documentVersion"]}
    try:
        receipt = await client.execute_inventory_operation(command)
    except KTGError:
        receipt = {"operationStatus": "unknown"}
    result = _receipt_result(operation_id, receipt, row)
    if result["outcome"] != "unknown":
        store.finish(operation_id, result)
    return result


async def inventory_result(store: InventoryStore, context: ToolContext, operation_id: str) -> JsonObject:
    client = context.ktg_client
    if not isinstance(client, KTGClient):
        raise ValueError("KTG 客户端不可用。")
    row = store.get(str(UUID(operation_id)), _user_key(context.identity))
    if row is None:
        return _result(operation_id, "failed", "操作不存在或不属于当前用户。", "OPERATION_NOT_FOUND")
    # Local proposal/result data is sensitive. Recheck current MES read rights
    # for its bound document before returning any of it, including cached data.
    action = cast(str, row["action"])
    if action not in _ACTIONS:
        return _result(operation_id, "failed", "操作不存在或不属于当前用户。", "OPERATION_NOT_FOUND")
    try:
        await client.get(
            f"/mes/wm/inventory-operations/documents/{action}/{row['document_id']}"
        )
    except KTGError:
        return _result(operation_id, "failed", "无权读取此库存单据或操作不存在。", "OPERATION_NOT_FOUND")
    if row["state"] == "prepared":
        if cast(float, row["expires_at"]) <= time.time():
            expired = _bound_result(row, _result(operation_id, "failed", "预览已过期，请重新预览。", "PREVIEW_EXPIRED"))
            store.finish(operation_id, expired)
            return expired
        return {"kind": "inventory_preview", "action_type": row["action"],
                "operation_id": operation_id, **json.loads(cast(str, row["preview_json"]))}
    if row["state"] == "finished" and row["result_json"]:
        stored = json.loads(cast(str, row["result_json"]))
        if stored.get("error_code") in {"PREVIEW_STALE", "PREVIEW_EXPIRED"}:
            return cast(JsonObject, stored)
    try:
        receipt = await client.inventory_operation_result(operation_id)
    except KTGError:
        return _bound_result(row, _result(operation_id, "unknown", "结果待核实，请稍后查询。", "RESULT_UNKNOWN"))
    result = _receipt_result(operation_id, receipt, row)
    if result["outcome"] != "unknown":
        store.finish(operation_id, result)
    return result


def _receipt_result(operation_id: str, receipt: Mapping[str, object], row: Mapping[str, object]) -> JsonObject:
    state = receipt.get("operationStatus")
    if receipt.get("operationId") != operation_id or receipt.get("actionType") != row["action"] or receipt.get("documentId") != row["document_id"]:
        state = "unknown"
    outcome = "succeeded" if state == "succeeded" else "failed" if state in {"failed", "rejected"} else "unknown"
    message = receipt.get("message")
    label = "库存操作已完成并核实。" if outcome == "succeeded" else (str(message) if isinstance(message, str) and message else "库存操作未完成，请重新预览。") if outcome == "failed" else "库存结果待核实，请查询回执。"
    error_code = receipt.get("errorCode")
    result = _result(operation_id, outcome, label, str(error_code) if outcome == "failed" and isinstance(error_code, str) else "EXECUTION_FAILED" if outcome == "failed" else "RESULT_UNKNOWN" if outcome == "unknown" else None)
    _bound_result(row, result)
    result["receipt"] = cast(JsonObject, dict(receipt)) if outcome != "unknown" else {}
    return result


def _bound_result(row: Mapping[str, object], result: JsonObject) -> JsonObject:
    result.update({"action_type": cast(JsonValue, row["action"]),
                   "document_id": cast(JsonValue, row["document_id"]),
                   "document_code": json.loads(cast(str, row["preview_json"]))["documentCode"]})
    return result


def _result(operation_id: str, outcome: str, label: str, code: str | None = None) -> JsonObject:
    result: JsonObject = {"kind": "inventory_result", "operation_id": operation_id,
                          "outcome": outcome, "label": label}
    if code:
        result["error_code"] = code
    return result
