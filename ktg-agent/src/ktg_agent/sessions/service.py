"""Authenticated read API for durable Agent sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from ktg_agent.core.messages import JsonObject, JsonValue, _json_object, message_to_dict
from ktg_agent.core.messages import SystemMessage, TextBlock
from ktg_agent.context.assembler import assemble_context
from ktg_agent.context.tokens import estimate_request
from ktg_agent.context.window import ContextPolicy, threshold_tokens
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.providers.base import ToolSchema
from ktg_agent.sessions.coordinator import RunCoordinator
from ktg_agent.sessions.repository import (
    EVENT_RETENTION_DAYS, Page, RunConflictError, SessionRecord, SessionRepository,
)

_PRIVATE_FIELD = re.compile(
    r"authorization|api[_-]?key|access[_-]?token|bearer[_-]?token|secret|"
    r"headers?|cookies?|traceback|stack[_-]?trace|exception[_-]?stack", re.I,
)
_PRIVATE_TEXT = re.compile(
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}\b|\bapi[_ -]?key\s*[:=]\s*\S+|"
    r"Traceback \(most recent call last\):|\bFile \"[^\"]+\", line \d+", re.I,
)


def _public_value(value: JsonValue) -> JsonValue:
    """Defend reads of events written before the current storage checks."""
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items()
                if not _PRIVATE_FIELD.search(key)}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str) and _PRIVATE_TEXT.search(value):
        return "[redacted]"
    return value


class SessionService:
    def __init__(self, repository: SessionRepository,
                 client_factory: Callable[[str], KTGClient],
                 coordinator: RunCoordinator | None = None, *,
                 context_policy: ContextPolicy | None = None,
                 system_prompt_builder: Callable[[JsonObject | None], str] | None = None,
                 tool_schemas: tuple[ToolSchema, ...] = ()) -> None:
        self._repository = repository
        self._client_factory = client_factory
        self._coordinator = coordinator
        self._context_policy = context_policy
        self._system_prompt_builder = system_prompt_builder
        self._tool_schemas = tool_schemas

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

    async def stats(self, authorization: str, session_id: str) -> JsonObject:
        owner = await self._owner(authorization)
        result = self._repository.session_stats(owner, session_id)
        summary = cast(JsonObject | None, result.pop("active_summary"))
        last_context = cast(JsonObject | None, result.pop("last_context"))
        active_run_id = result.pop("active_run_id")
        policy = self._context_policy
        context: JsonObject = {
            "configured": policy is not None,
            "window_tokens": policy.window_tokens if policy else None,
            "reserve_tokens": policy.reserve_tokens if policy else None,
            "threshold_tokens": threshold_tokens(policy) if policy else None,
            "estimated_tokens": None, "estimate_kind": None,
            "summary_id": summary["summary_id"] if summary else None,
            "covers_start_message_id": summary["covers_start_message_id"] if summary else None,
            "covers_end_message_id": summary["covers_end_message_id"] if summary else None,
            "summary_token_estimate": summary["summary_token_estimate"] if summary else None,
            "retained_message_count": None, "spill_count": None,
            "active_run_id": active_run_id,
        }
        if policy is not None and self._system_prompt_builder is not None:
            records = self._repository.completed_message_records(owner, session_id)
            assembled = assemble_context(records, self._repository.active_summary(owner, session_id))
            frame = (SystemMessage((TextBlock(self._system_prompt_builder(None)),)),
                     *assembled.messages)
            context["estimated_tokens"] = estimate_request(frame, self._tool_schemas).total_tokens
            context["estimate_kind"] = "estimated"
            context["retained_message_count"] = assembled.retained_message_count
            context["spill_count"] = last_context.get("spill_count") if last_context else 0
        result["context"] = context
        return result

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
        if self._repository.get_session(owner, session_id) is None:
            raise KeyError("session not found")
        clean_request_id = request_id.strip()
        existing = self._repository.find_run_by_request(owner, session_id, clean_request_id)
        if existing is not None:
            # Resolve replay before pre-flight compaction: history growth must not
            # change the compaction digest of an already admitted request.
            if existing.request_hash != digest:
                raise RunConflictError("request_id 已用于不同请求。")
            return {"run_id": existing.id, "status": existing.status}
        history = self._repository.completed_history(owner, session_id)
        self._coordinator.compact_before_run(
            owner, session_id, history, clean_context, authorization,
            parent_request_id=clean_request_id)
        run, created = self._repository.admit_run(
            owner, session_id, clean_request_id, digest, canonical,
        )
        if created:
            self._coordinator.schedule(run, clean_message, authorization)
        return {"run_id": run.id, "status": run.status}

    async def request_compaction(self, authorization: str, session_id: str,
                                 request_id: object) -> JsonObject:
        if self._coordinator is None:
            raise RuntimeError("run coordinator is not configured")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("request_id 必须是 1–128 个字符。")
        owner = await self._owner(authorization)
        if self._repository.get_session(owner, session_id) is None:
            raise KeyError("session not found")
        if self._context_policy is None:
            raise ValueError("上下文窗口未配置，无法压缩。")
        clean_id = request_id.strip()
        canonical: JsonObject = {"kind": "compaction", "trigger": "manual"}
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        run, created = self._repository.admit_run(
            owner, session_id, f"compact:{clean_id}", digest, canonical,
            kind="compaction")
        if created:
            self._coordinator.schedule(run, "", authorization,
                                       budget=self._coordinator.summary_budget_snapshot)
        return {"run_id": run.id, "status": run.status}

    async def get_run(self, authorization: str, run_id: str) -> JsonObject:
        owner = await self._owner(authorization)
        run = self._repository.get_run(owner, run_id)
        if run is None:
            raise KeyError("run not found")
        return {"run_id": run.id, "session_id": run.session_id, "status": run.status,
                "stop_reason": run.stop_reason,
                "last_seq": self._repository.last_event_seq(owner, run_id),
                "result_summary": run.result_summary,
                "budget": run.budget, "usage": run.usage,
                "model_requests": list(run.model_requests),
                "kind": run.kind, "context": run.context}

    async def add_input(self, authorization: str, run_id: str,
                        request_id: object, mode: object, message: object) -> JsonObject:
        if self._coordinator is None:
            raise RuntimeError("run coordinator is not configured")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("request_id 必须是 1–128 个字符。")
        if mode not in ("steer", "follow_up"):
            raise ValueError("mode 必须是 steer 或 follow_up。")
        if not isinstance(message, str) or not message.strip() or len(message.strip()) > 2000:
            raise ValueError("message 必须是 1–2000 个字符。")
        assert isinstance(mode, str)
        owner = await self.authorize_run(authorization, run_id)
        record = self._coordinator.admit_input(owner, run_id, request_id.strip(), mode, message.strip())
        return {"request_id": record.request_id, "run_id": run_id,
                "mode": record.kind, "input_status": record.status}

    async def cancel_run(self, authorization: str, run_id: str,
                         request_id: object) -> JsonObject:
        if self._coordinator is None:
            raise RuntimeError("run coordinator is not configured")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("request_id 必须是 1–128 个字符。")
        owner = await self.authorize_run(authorization, run_id)
        record = self._coordinator.admit_cancel(owner, run_id, request_id.strip())
        run = self._repository.get_run(owner, run_id)
        assert run is not None
        return {"run_id": run_id, "request_id": record.request_id,
                "status": "cancelling" if record.reason is None else run.status,
                "cancel_status": record.reason or record.status}

    async def inputs(self, authorization: str, run_id: str, *,
                     cursor: int | None, limit: int) -> JsonObject:
        owner = await self.authorize_run(authorization, run_id)
        page = self._repository.list_inputs(owner, run_id, after=cursor, limit=limit)
        return {"items": [{"request_id": item.request_id, "mode": item.kind,
                           "message": item.request["message"], "status": item.status,
                           "reason": item.reason, "created_at": item.created_at,
                           "updated_at": item.updated_at} for item in page.items],
                "next_cursor": page.next_cursor}

    async def events(self, authorization: str, run_id: str, *,
                     after_seq: int = 0, limit: int = 50) -> JsonObject:
        owner = await self.authorize_run(authorization, run_id)
        return self.read_event_batch(owner, run_id, after_seq=after_seq, limit=limit)[0]

    async def authorize_run(self, authorization: str, run_id: str) -> str:
        """Authenticate once before opening a long-lived event connection."""
        owner = await self._owner(authorization)
        if self._repository.get_run(owner, run_id) is None:
            raise KeyError("run not found")
        return owner

    def read_event_batch(self, owner: str, run_id: str, *, after_seq: int,
                         limit: int = 100) -> tuple[JsonObject, bool]:
        """Read durable events and the run state without executing the run."""
        run = self._repository.get_run(owner, run_id)
        if run is None:
            raise KeyError("run not found")
        if run.ended_at is not None and datetime.fromisoformat(run.ended_at) < (
            datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)
        ):
            self._repository.prune_events()
        page = self._repository.list_events(owner, run_id, after=after_seq, limit=limit)
        response: JsonObject = {"items": [{"schema_version": 1, "session_id": run.session_id,
                           "run_id": event.run_id, "seq": event.seq,
                           "type": event.type, "timestamp": event.created_at,
                           "payload": _public_value(event.payload)} for event in page.items],
                "next_cursor": page.next_cursor}
        return response, run.status in {
            "completed", "needs_input", "failed", "cancelled", "needs_reconciliation",
        }

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
