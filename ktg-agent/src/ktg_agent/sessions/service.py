"""Authenticated read API for durable Agent sessions."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json

from ktg_agent.core.messages import JsonObject, _json_object, message_to_dict
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.sessions.coordinator import RunCoordinator
from ktg_agent.sessions.repository import Page, SessionRecord, SessionRepository


class SessionService:
    def __init__(self, repository: SessionRepository,
                 client_factory: Callable[[str], KTGClient],
                 coordinator: RunCoordinator | None = None) -> None:
        self._repository = repository
        self._client_factory = client_factory
        self._coordinator = coordinator

    def close(self) -> None:
        if self._coordinator is not None:
            self._coordinator.close()

    async def _owner(self, authorization: str) -> str:
        identity = await self._client_factory(authorization).get_current_user()
        user_id = identity.get("userId")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise KTGError("AUTH_INVALID", "KTG 未返回有效用户身份。", status=401)
        return f"id:{user_id}"

    @staticmethod
    def _session(record: SessionRecord) -> JsonObject:
        return {"session_id": record.id, "title": record.title,
                "created_at": record.created_at, "updated_at": record.updated_at}

    @staticmethod
    def _page(page: Page[SessionRecord]) -> JsonObject:
        return {"items": [SessionService._session(item) for item in page.items],
                "next_cursor": page.next_cursor}

    async def create(self, authorization: str, title: str | None) -> JsonObject:
        owner = await self._owner(authorization)
        return self._session(self._repository.create_session(owner, title=title))

    async def list(self, authorization: str, *, cursor: int | None,
                   limit: int) -> JsonObject:
        owner = await self._owner(authorization)
        return self._page(self._repository.list_sessions(owner, after=cursor, limit=limit))

    async def get(self, authorization: str, session_id: str) -> JsonObject:
        owner = await self._owner(authorization)
        record = self._repository.get_session(owner, session_id)
        if record is None:
            raise KeyError("session not found")
        active = self._repository.get_active_run(owner, session_id)
        return {**self._session(record), "active_run_id": active.id if active else None}

    async def messages(self, authorization: str, session_id: str, *,
                       cursor: int | None, limit: int) -> JsonObject:
        owner = await self._owner(authorization)
        if self._repository.get_session(owner, session_id) is None:
            raise KeyError("session not found")
        page = self._repository.list_messages(owner, session_id, after=cursor, limit=limit)
        return {"items": [{"message_id": item.id, "run_id": item.run_id,
                            "created_at": item.created_at, **message_to_dict(item.message)}
                           for item in page.items], "next_cursor": page.next_cursor}

    async def create_run(self, authorization: str, session_id: str,
                         request_id: object, message: object,
                         page_context: object = None) -> JsonObject:
        if self._coordinator is None:
            raise RuntimeError("run coordinator is not configured")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("request_id 必须是 1–128 个字符。")
        if not isinstance(message, str) or not message.strip() or len(message.strip()) > 2000:
            raise ValueError("message 必须是 1–2000 个字符。")
        clean_context = self._page_context(page_context)
        clean_message = message.strip()
        canonical: JsonObject = {"message": clean_message, "page_context": clean_context}
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        owner = await self._owner(authorization)
        run, created = self._repository.admit_run(
            owner, session_id, request_id.strip(), digest, canonical,
        )
        if created:
            self._coordinator.schedule(run, clean_message, authorization)
        return {"run_id": run.id, "status": run.status}

    async def get_run(self, authorization: str, run_id: str) -> JsonObject:
        owner = await self._owner(authorization)
        run = self._repository.get_run(owner, run_id)
        if run is None:
            raise KeyError("run not found")
        return {"run_id": run.id, "session_id": run.session_id, "status": run.status,
                "stop_reason": run.stop_reason,
                "last_seq": self._repository.last_event_seq(owner, run_id),
                "result_summary": run.result_summary}

    @staticmethod
    def _page_context(value: object) -> JsonObject | None:
        if value is None:
            return None
        context = _json_object(value, "page_context")
        if set(context) - {"route_name", "selected_entity", "filters", "captured_at"}:
            raise ValueError("page_context 包含不允许的字段。")
        for key in ("route_name", "captured_at"):
            item = context.get(key)
            if item is not None and (not isinstance(item, str) or len(item) > 100):
                raise ValueError(f"page_context.{key} 无效。")
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
