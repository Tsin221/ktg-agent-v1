"""Authenticated projections from the native durable session store."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import cast

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, RetryPromptPart, UserPromptPart

from ktg_agent.core.messages import JsonObject, JsonValue, _json_object, _json_value
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.sessions.repository import MessageRecord, Page, SessionRecord, SessionRepository
from ktg_agent.tools.mes_catalog import MES_PAGE_MODULES
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.operations.executor import OperationExecutor
from ktg_agent.operations.store import OperationPlanStore, Plan
from ktg_agent.operations.tool import public_plan

_PRIVATE_FIELD = re.compile(r"authorization|api[_-]?key|token|password|credential|secret|headers?|cookies?|traceback|stack[_-]?trace|exception[_-]?stack", re.I)
_PRIVATE_TEXT = re.compile(r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}\b|\b(?:api[_ -]?key|password|credential|(?:access|refresh)[_-]?token)\s*[:=]\s*\S+|Traceback \(most recent call last\):", re.I)


def _public_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items() if not _PRIVATE_FIELD.search(key)}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        if _PRIVATE_TEXT.search(value):
            return "[redacted]"
        # Some tools encode their structured result as a string. Apply the
        # same field filtering as JSON objects before exposing it to the UI.
        if value.lstrip().startswith(('{', '[')):
            try:
                decoded = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(decoded, (dict, list)):
                    clean = _public_value(_json_value(decoded, 'tool result'))
                    return json.dumps(clean, ensure_ascii=False, allow_nan=False)
    return value


def _safe_public(value: object) -> JsonValue:
    try:
        return _public_value(_json_value(value, "public message"))
    except ValueError:
        return None


def _text_block(text: str) -> JsonObject:
    return {"type": "text", "text": _safe_public(text), "model_visible": True}


def project_message(item: MessageRecord) -> list[JsonObject]:
    """Project native parts at the HTTP boundary; no separate stored text copy."""
    base: JsonObject = {"run_id": item.run_id, "created_at": item.created_at}
    result: list[JsonObject] = []
    message = item.message
    if isinstance(message, ModelResponse):
        content: list[JsonValue] = []
        for part in message.parts:
            if isinstance(part, TextPart):
                content.append(_text_block(part.content))
            elif isinstance(part, ToolCallPart):
                try:
                    args = _safe_public(part.args_as_dict())
                except (ValueError, AssertionError):
                    args = {}
                content.append({"type": "tool_call", "id": part.tool_call_id, "name": part.tool_name, "arguments": args})
        if content:
            result.append({**base, "message_id": item.id, "role": "assistant", "content": content})
    elif isinstance(message, ModelRequest):
        for index, request_part in enumerate(message.parts):
            entry_id = f"{item.id}:{index}"
            if isinstance(request_part, UserPromptPart):
                text = request_part.content if isinstance(request_part.content, str) else " ".join(p for p in request_part.content if isinstance(p, str))
                text = text.split("\n\n当前 MES 页面上下文（仅供定位业务对象", 1)[0]
                result.append({**base, "message_id": entry_id, "role": "user", "content": [_text_block(text)]})
            elif isinstance(request_part, ToolReturnPart):
                metadata = request_part.metadata if isinstance(request_part.metadata, dict) else {}
                details = _safe_public(metadata.get("details"))
                body = request_part.content if isinstance(request_part.content, str) else json.dumps(_safe_public(request_part.content), ensure_ascii=False)
                row: JsonObject = {**base, "message_id": entry_id, "role": "tool", "tool_call_id": request_part.tool_call_id,
                    "content": [_text_block(body)], "is_error": metadata.get("status") in ("failed", "unknown") or request_part.outcome != "success"}
                if isinstance(details, dict):
                    row["details"] = details
                result.append(row)
            elif isinstance(request_part, RetryPromptPart) and request_part.tool_name:
                result.append({**base, "message_id": entry_id, "role": "tool", "tool_call_id": request_part.tool_call_id,
                    "content": [_text_block("工具请求未完成。")], "is_error": True})
    return result


class SessionService:
    def __init__(self, repository: SessionRepository, client_factory: Callable[[str], KTGClient], *,
                 operation_store: OperationPlanStore | None = None,
                 operation_enabled: Callable[[], bool] | None = None) -> None:
        self._repository = repository
        self._client_factory = client_factory
        self._operation_store = operation_store
        self._operation_enabled = operation_enabled or (lambda: False)
        self._operation_executor = OperationExecutor(operation_store, self._operation_enabled) if operation_store else None

    async def _owner(self, authorization: str) -> str:
        return owner_from_identity(await self._client_factory(authorization).get_current_user())

    async def operation_plans(self, authorization: str, session_id: str, plan_id: str | None = None) -> JsonObject:
        owner = await self._owner(authorization)
        store = self._operation_store
        if store is None:
            raise KeyError("operation store not found")
        def read() -> JsonObject:
            if self._repository.get_session(owner, session_id) is None and not store.is_historical_session(owner, session_id):
                raise KeyError("operation session not found")
            def project(plan: Plan) -> JsonObject:
                run = self._repository.get_run(owner, plan["run_id"])
                result = public_plan(plan)
                result["source_run_status"] = run.status if run else "historical"
                result["confirmable"] = bool(run and run.status == "completed" and plan["status"] == "pending" and self._operation_enabled())
                return result
            if plan_id is not None:
                return project(store.get(owner, session_id, plan_id))
            return {"items": [project(plan) for plan in store.list_plans(owner, session_id)]}
        return await asyncio.to_thread(read)

    async def historical_operations(self, authorization: str) -> JsonObject:
        owner = await self._owner(authorization)
        store = self._operation_store
        if store is None:
            return {"items": []}
        plans = await asyncio.to_thread(store.list_historical_plans, owner)
        return {"items": [{**public_plan(plan), "source_run_status": "historical", "confirmable": False} for plan in plans]}

    async def operation_action(self, authorization: str, session_id: str, plan_id: str,
                               action: str, payload: JsonObject) -> JsonObject:
        client = self._client_factory(authorization)
        identity = await client.get_current_user()
        owner = owner_from_identity(identity)
        store, executor = self._operation_store, self._operation_executor
        if store is None or executor is None:
            raise KeyError("operation session not found")
        session = await asyncio.to_thread(self._repository.get_session, owner, session_id)
        historical = session is None and await asyncio.to_thread(store.is_historical_session, owner, session_id)
        if session is None and not historical:
            raise KeyError("operation session not found")
        if historical and action != "reconcile":
            raise ValueError("历史业务记录仅可查询及核实。")
        if action in {"confirm", "cancel"}:
            digest = payload.get("digest")
            if set(payload) != {"digest"} or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("仅接受原操作方案摘要，不接受客户端修改方案参数。")
            if action == "cancel":
                return public_plan(await asyncio.to_thread(store.cancel, owner, session_id, plan_id, digest))
            if not self._operation_enabled():
                raise KTGError("OPERATIONS_DISABLED", "工单操作功能当前已关闭。", status=403)
            return public_plan(await executor.confirm(client, identity, owner, session_id, plan_id, digest))
        if action == "reconcile" and not payload:
            return public_plan(await executor.reconcile(client, identity, owner, session_id, plan_id))
        raise ValueError("操作方案请求无效。")

    @staticmethod
    def _session(record: SessionRecord) -> JsonObject:
        return {"session_id": record.id, "title": record.title, "created_at": record.created_at, "updated_at": record.updated_at}

    @staticmethod
    def _page(page: Page[SessionRecord]) -> JsonObject:
        return {"items": [SessionService._session(item) for item in page.items], "next_cursor": page.next_cursor}

    async def create(self, authorization: str, title: str | None) -> JsonObject:
        owner = await self._owner(authorization)
        return self._session(await asyncio.to_thread(self._repository.create_session, owner, title=title))

    async def rename(self, authorization: str, session_id: str, title: str) -> JsonObject:
        owner = await self._owner(authorization)
        return self._session(await asyncio.to_thread(self._repository.rename_session, owner, session_id, title))

    async def list(self, authorization: str, *, cursor: int | None, limit: int) -> JsonObject:
        owner = await self._owner(authorization)
        return self._page(await asyncio.to_thread(self._repository.list_sessions, owner, after=cursor, limit=limit))

    async def get(self, authorization: str, session_id: str) -> JsonObject:
        owner = await self._owner(authorization)
        record = await asyncio.to_thread(self._repository.get_session, owner, session_id)
        if record is None:
            raise KeyError("session not found")
        active = await asyncio.to_thread(self._repository.get_active_run, owner, session_id)
        return {**self._session(record), "active_run_id": active.id if active else None}

    async def stats(self, authorization: str, session_id: str) -> JsonObject:
        owner = await self._owner(authorization)
        result = await asyncio.to_thread(self._repository.session_stats, owner, session_id)
        active = result.pop("active_run_id", None)
        result["context"] = {"configured": True, "estimate_kind": None, "estimated_tokens": None,
                             "active_run_id": active}
        return result

    async def messages(self, authorization: str, session_id: str, *, cursor: int | None, limit: int) -> JsonObject:
        owner = await self._owner(authorization)
        if await asyncio.to_thread(self._repository.get_session, owner, session_id) is None:
            raise KeyError("session not found")
        page = await asyncio.to_thread(self._repository.list_messages, owner, session_id, after=cursor, limit=limit)
        return {"items": [entry for item in page.items for entry in project_message(item)], "next_cursor": page.next_cursor}

    async def tasks(self, authorization: str, session_id: str, task_id: str | None = None) -> JsonObject:
        owner = await self._owner(authorization)
        if task_id is None:
            return {"items": await asyncio.to_thread(self._repository.list_tasks, owner, session_id)}
        return await asyncio.to_thread(self._repository.get_task, owner, session_id, task_id)

    @staticmethod
    def _page_context(value: object) -> JsonObject | None:
        if value is None:
            return None
        context = _json_object(value, "page_context")
        if set(context) - {"route_name", "module", "selected_entity", "filters", "captured_at", "response_style"}:
            raise ValueError("page_context 包含不允许的字段。")
        if "response_style" in context and context["response_style"] != "brief":
            raise ValueError("page_context.response_style 无效。")
        for key in ("route_name", "captured_at"):
            item = context.get(key)
            if item is not None and (not isinstance(item, str) or len(item) > 100):
                raise ValueError(f"page_context.{key} 无效。")
        module = context.get("module")
        if module is not None and (not isinstance(module, str) or module not in MES_PAGE_MODULES):
            raise ValueError("page_context.module 无效。")
        entity = context.get("selected_entity")
        if entity is not None:
            if not isinstance(entity, dict) or set(entity) - {"type", "id"} or not all(
                isinstance(item, str) and len(item) <= 100 for item in entity.values()
            ):
                raise ValueError("page_context.selected_entity 无效。")
        filters = context.get("filters")
        if filters is not None:
            if not isinstance(filters, dict) or set(filters) - {
                "itemCode", "warehouseId", "workorderCode",
            } or not all(isinstance(item, str | int) and not isinstance(item, bool)
                         and len(str(item)) <= 100 for item in filters.values()):
                raise ValueError("page_context.filters 无效。")
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 2048:
            raise ValueError("page_context 不能超过 2048 字节。")
        return context

