"""Minimal local HTTP entry point for the KTG Agent query panel."""

from __future__ import annotations

import asyncio
import json
import os
import select
import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Event
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from ktg_agent.core.events import EventPublisher, EventSink
from ktg_agent.context.compaction import PreparedCompaction
from ktg_agent.core.loop import AgentRunResult, RunLimits
from ktg_agent.core.messages import JsonObject, Message
from ktg_agent.context.spill import SpillSettings
from ktg_agent.context.window import ContextPolicy
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore
from ktg_agent.ktg.workorders import PreviewStore
from ktg_agent.ktg.dv_machinerytype_writes import DvMachineryTypePreviewStore
from ktg_agent.providers import OpenAICompatibleProvider
from ktg_agent.providers.base import ModelConfig, ProviderError
from ktg_agent.service import QueryService
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.coordinator import RunCoordinator, RegisterAgent
from ktg_agent.sessions.repository import (ControlConflictError, ControlStateError,
                                           EventHistoryExpiredError, RunBusyError, RunConflictError)
from ktg_agent.sessions.service import SessionService


@dataclass(frozen=True, slots=True)
class Settings:
    model_api_key: str
    model_base_url: str
    model_id: str
    ktg_base_url: str
    host: str = "127.0.0.1"
    port: int = 8001
    inventory_db_path: str = ""
    session_db_path: str = ""
    enable_item_receipt: bool = False
    enable_production_issue: bool = False
    enable_workorder_progress: bool = False
    max_total_tokens: int | None = None
    max_tool_calls: int = 12
    max_model_turns: int = 8
    context_window: int | None = None
    context_reserve_tokens: int | None = None
    context_threshold_ratio: float = 0.8
    context_retain_ratio: float = 0.16
    context_summary_max_tokens: int = 1024
    spill_max_inline_tokens: int | None = None
    spill_dir: str = ""
    max_active_tools: int = 8
    max_schema_tokens: int = 6000
    workorder_db_path: str = ""
    dv_machinerytype_db_path: str = ""
    enable_dv_machinerytype_write: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("MODEL_API_KEY", "").strip()
        model_id = os.environ.get("MODEL_ID", "").strip()
        if not api_key or api_key == "CHANGE_ME":
            raise RuntimeError("MODEL_API_KEY must be configured")
        if not model_id or model_id == "CHANGE_ME":
            raise RuntimeError("MODEL_ID must be configured")
        model_base_url = os.environ.get(
            "MODEL_BASE_URL", "https://api.openai.com/v1"
        ).strip()
        ktg_base_url = os.environ.get(
            "KTG_BASE_URL", "http://127.0.0.1:8080"
        ).strip()
        host = os.environ.get("AGENT_HOST", "127.0.0.1").strip()
        for name, value in (
            ("MODEL_BASE_URL", model_base_url),
            ("KTG_BASE_URL", ktg_base_url),
            ("AGENT_HOST", host),
        ):
            if not value:
                raise RuntimeError(f"{name} must not be blank")
        raw_port = os.environ.get("AGENT_PORT", "8001")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise RuntimeError("AGENT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("AGENT_PORT must be between 1 and 65535")
        raw_tokens = os.environ.get("AGENT_MAX_TOTAL_TOKENS")
        try:
            max_total_tokens = int(raw_tokens) if raw_tokens is not None else None
        except ValueError as exc:
            raise RuntimeError("AGENT_MAX_TOTAL_TOKENS must be a positive integer") from exc
        if max_total_tokens is not None and max_total_tokens <= 0:
            raise RuntimeError("AGENT_MAX_TOTAL_TOKENS must be a positive integer")

        def positive_integer(name: str, default: int | None = None) -> int | None:
            raw = os.environ.get(name)
            try:
                value = int(raw) if raw is not None else default
            except ValueError as exc:
                raise RuntimeError(f"{name} must be a positive integer") from exc
            if value is not None and value <= 0:
                raise RuntimeError(f"{name} must be a positive integer")
            return value

        def ratio(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                value = float(raw) if raw is not None else default
            except ValueError as exc:
                raise RuntimeError(f"{name} must be a finite ratio") from exc
            if not 0 < value <= 1:
                raise RuntimeError(f"{name} must be a finite ratio greater than 0 and at most 1")
            return value

        context_window = positive_integer("AGENT_CONTEXT_WINDOW")
        context_reserve_tokens = positive_integer("AGENT_CONTEXT_RESERVE_TOKENS")
        if (context_window is not None and context_reserve_tokens is not None
                and context_reserve_tokens >= context_window):
            raise RuntimeError("AGENT_CONTEXT_RESERVE_TOKENS must be less than AGENT_CONTEXT_WINDOW")
        context_threshold_ratio = ratio("AGENT_CONTEXT_THRESHOLD_RATIO", 0.8)
        context_retain_ratio = ratio("AGENT_CONTEXT_RETAIN_RATIO", 0.16)
        if context_retain_ratio >= context_threshold_ratio:
            raise RuntimeError("AGENT_CONTEXT_RETAIN_RATIO must be less than AGENT_CONTEXT_THRESHOLD_RATIO")
        context_summary_max_tokens = positive_integer("AGENT_CONTEXT_SUMMARY_MAX_TOKENS", 1024)
        assert context_summary_max_tokens is not None
        spill_max_inline_tokens = positive_integer("AGENT_SPILL_MAX_INLINE_TOKENS")
        spill_dir = os.environ.get("AGENT_SPILL_DIR", "").strip()
        if spill_max_inline_tokens is not None and not spill_dir:
            raise RuntimeError("AGENT_SPILL_DIR must be configured when spill is enabled")
        if spill_dir:
            spill_path = Path(spill_dir)
            if not spill_path.is_absolute():
                raise RuntimeError("AGENT_SPILL_DIR must be an absolute path")
            for db_name in ("AGENT_SESSION_DB", "AGENT_INVENTORY_DB"):
                db_path = os.environ.get(db_name, "").strip()
                if db_path and spill_path.resolve() == Path(db_path).resolve():
                    raise RuntimeError(f"AGENT_SPILL_DIR must differ from {db_name}")
        return cls(
            model_api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
            ktg_base_url=ktg_base_url,
            host=host,
            port=port,
            inventory_db_path=os.environ.get("AGENT_INVENTORY_DB", "").strip(),
            session_db_path=os.environ.get("AGENT_SESSION_DB", "").strip(),
            workorder_db_path=os.environ.get("AGENT_WORKORDER_DB", "").strip(),
            dv_machinerytype_db_path=os.environ.get("AGENT_DV_MACHINERYTYPE_DB", "").strip(),
            enable_dv_machinerytype_write=os.environ.get(
                "AGENT_ENABLE_DV_MACHINERYTYPE_WRITE", "false").lower() == "true",
            enable_item_receipt=os.environ.get("AGENT_ENABLE_ITEM_RECEIPT", "false").lower() == "true",
            enable_production_issue=os.environ.get("AGENT_ENABLE_PRODUCTION_ISSUE", "false").lower() == "true",
            enable_workorder_progress=os.environ.get("AGENT_ENABLE_WORKORDER_PROGRESS", "false").lower() == "true",
            max_total_tokens=max_total_tokens,
            max_tool_calls=positive_integer("AGENT_MAX_TOOL_CALLS", 12) or 12,
            max_model_turns=positive_integer("AGENT_MAX_MODEL_TURNS", 8) or 8,
            context_window=context_window,
            context_reserve_tokens=context_reserve_tokens,
            context_threshold_ratio=context_threshold_ratio,
            context_retain_ratio=context_retain_ratio,
            context_summary_max_tokens=context_summary_max_tokens,
            spill_max_inline_tokens=spill_max_inline_tokens,
            spill_dir=spill_dir,
            max_active_tools=positive_integer("AGENT_MAX_ACTIVE_TOOLS", 8) or 8,
            max_schema_tokens=positive_integer("AGENT_MAX_TOOL_SCHEMA_TOKENS", 6000) or 6000,
        )


def create_session_repository(settings: Settings) -> SessionRepository:
    """Open the separately configured, versioned conversation database."""
    session_path = Path(settings.session_db_path)
    if not settings.session_db_path or not session_path.is_absolute():
        raise RuntimeError("AGENT_SESSION_DB must be an absolute path")
    if settings.inventory_db_path and session_path.resolve() == Path(settings.inventory_db_path).resolve():
        raise RuntimeError("AGENT_SESSION_DB must differ from AGENT_INVENTORY_DB")
    return SessionRepository(session_path)


def create_service(settings: Settings) -> QueryService:
    model_config = ModelConfig(model=settings.model_id, max_output_tokens=1024)
    context_policy = None
    if settings.context_window is not None:
        try:
            context_policy = ContextPolicy(
                window_tokens=settings.context_window,
                reserve_tokens=(settings.context_reserve_tokens
                                if settings.context_reserve_tokens is not None
                                else model_config.max_output_tokens),
                threshold_ratio=settings.context_threshold_ratio,
                retain_ratio=settings.context_retain_ratio,
            )
        except ValueError as exc:
            raise RuntimeError(f"invalid context configuration: {exc}") from exc
    provider = OpenAICompatibleProvider(
        api_key=settings.model_api_key,
        base_url=settings.model_base_url,
    )

    def client_factory(authorization: str) -> KTGClient:
        return KTGClient(
            base_url=settings.ktg_base_url,
            authorization=authorization,
        )

    actions = frozenset(
        action for action, enabled in (
            (RECEIPT, settings.enable_item_receipt),
            (ISSUE, settings.enable_production_issue),
        ) if enabled
    )
    if actions and not Path(settings.inventory_db_path).is_absolute():
        raise RuntimeError("AGENT_INVENTORY_DB must be an absolute path when inventory actions are enabled")
    workorder_db_path = settings.workorder_db_path
    if not workorder_db_path:
        if settings.session_db_path:
            session_path = Path(settings.session_db_path)
            if not session_path.is_absolute():
                raise RuntimeError("AGENT_SESSION_DB must be an absolute path")
            workorder_db_path = str(session_path.with_name("agent-workorders.sqlite3"))
        else:
            workorder_db_path = str(
                Path.home() / ".ktg-agent" / "agent-workorders.sqlite3")
    if workorder_db_path:
        proposal_path = Path(workorder_db_path)
        if not proposal_path.is_absolute():
            raise RuntimeError("AGENT_WORKORDER_DB must be an absolute path")
        for db_name, db_path in (("AGENT_SESSION_DB", settings.session_db_path),
                                 ("AGENT_INVENTORY_DB", settings.inventory_db_path)):
            if db_path and proposal_path.resolve() == Path(db_path).resolve():
                raise RuntimeError(f"AGENT_WORKORDER_DB must differ from {db_name}")
    dv_machinerytype_db_path = settings.dv_machinerytype_db_path
    if settings.enable_dv_machinerytype_write:
        if not dv_machinerytype_db_path:
            if settings.session_db_path:
                session_path = Path(settings.session_db_path)
                if not session_path.is_absolute():
                    raise RuntimeError("AGENT_SESSION_DB must be an absolute path")
                dv_machinerytype_db_path = str(
                    session_path.with_name("agent-dv-machinerytype.sqlite3"))
            else:
                dv_machinerytype_db_path = str(
                    Path.home() / ".ktg-agent" / "agent-dv-machinerytype.sqlite3")
        dv_path = Path(dv_machinerytype_db_path)
        if not dv_path.is_absolute():
            raise RuntimeError("AGENT_DV_MACHINERYTYPE_DB must be an absolute path")
        for db_name, db_path in (
                ("AGENT_SESSION_DB", settings.session_db_path),
                ("AGENT_INVENTORY_DB", settings.inventory_db_path),
                ("AGENT_WORKORDER_DB", workorder_db_path)):
            if db_path and dv_path.resolve() == Path(db_path).resolve():
                raise RuntimeError(
                    f"AGENT_DV_MACHINERYTYPE_DB must differ from {db_name}")
    return QueryService(
        provider=provider,
        model_config=model_config,
        ktg_client_factory=client_factory,
        inventory_store=InventoryStore(settings.inventory_db_path) if actions else None,
        inventory_actions=actions,
        workorder_store=PreviewStore(path=workorder_db_path or ":memory:"),
        dv_machinerytype_store=(
            DvMachineryTypePreviewStore(path=dv_machinerytype_db_path)
            if settings.enable_dv_machinerytype_write else None),
        spill_settings=(
            SpillSettings(settings.spill_max_inline_tokens, Path(settings.spill_dir))
            if settings.spill_max_inline_tokens is not None and settings.spill_dir else None
        ),
        context_policy=context_policy,
        summary_max_tokens=settings.context_summary_max_tokens,
        max_active_tools=settings.max_active_tools,
        max_schema_tokens=settings.max_schema_tokens,
        enable_workorder_progress=settings.enable_workorder_progress,
        limits=RunLimits(max_model_turns=settings.max_model_turns, max_tool_calls=settings.max_tool_calls,
                         max_run_seconds=90, max_parallel_reads=3,
                         max_total_tokens=settings.max_total_tokens),
    )


def create_session_service(settings: Settings, service: QueryService) -> SessionService:
    def client_factory(authorization: str) -> KTGClient:
        return KTGClient(base_url=settings.ktg_base_url, authorization=authorization)

    repository = create_session_repository(settings)
    repository.recover_interrupted_runs()
    repository.prune_events()
    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      event_sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_session(
            run_id=run_id, message=message, history=history,
            page_context=page_context, authorization=authorization,
            event_sink=event_sink,
            on_agent_ready=register_agent,
        )

    async def compact(run_id: str, prepared: PreparedCompaction, event_sink: EventSink,
                      publisher: EventPublisher, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_compaction(
            run_id, prepared, event_sink=event_sink, publisher=publisher,
            on_agent_ready=register_agent)

    return SessionService(repository, client_factory,
                          RunCoordinator(repository, execute, service.budget_snapshot,
                                         compaction_planner=service.plan_compaction,
                                         compaction_executor=compact,
                                         system_prompt_builder=service.build_system_prompt,
                                         summary_budget_snapshot=service.summary_budget_snapshot),
                          context_policy=service.context_policy,
                          system_prompt_builder=service.build_system_prompt,
                          tool_schemas=service.model_tool_schemas)


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    sse_poll_seconds = 0.5
    sse_heartbeat_seconds = 15.0

    def __init__(self, address: tuple[str, int], service: QueryService,
                 session_service: SessionService) -> None:
        super().__init__(address, AgentRequestHandler)
        self.query_service = service
        self.session_service = session_service
        self.sse_slots = BoundedSemaphore(32)
        self.closing = Event()

    def shutdown(self) -> None:
        self.closing.set()
        super().shutdown()

    def server_close(self) -> None:
        self.closing.set()
        self.session_service.close()
        super().server_close()


class AgentRequestHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/agent-api/v1/tools" and not urlsplit(self.path).query:
            try:
                response = asyncio.run(self.server.query_service.available_tools(
                    self.headers.get("Authorization", "")))
            except KTGError as exc:
                self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if path.startswith("/agent-api/v1/runs/"):
            if path.endswith("/events"):
                self._get_events(path)
            elif path.endswith("/inputs"):
                self._get_inputs(path)
            else:
                self._get_run(path)
            return
        if path == "/agent-api/v1/sessions" or path.startswith("/agent-api/v1/sessions/"):
            self._get_session(path)
            return
        if self.path.startswith("/agent-api/v1/inventory-operations/"):
            operation_id = self.path.rsplit("/", 1)[-1]
            try:
                response = asyncio.run(self.server.query_service.get_inventory_result(
                    operation_id=operation_id,
                    authorization=self.headers.get("Authorization", ""),
                ))
            except KTGError as exc:
                self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(exc))
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        run_segments = path.removeprefix("/agent-api/v1/sessions/").split("/")
        is_run_create = (path.startswith("/agent-api/v1/sessions/")
                         and len(run_segments) == 2 and bool(run_segments[0])
                         and run_segments[1] == "runs" and not urlsplit(self.path).query)
        is_compact = (path.startswith("/agent-api/v1/sessions/")
                      and len(run_segments) == 2 and bool(run_segments[0])
                      and run_segments[1] == "compact" and not urlsplit(self.path).query)
        cancel_segments = path.removeprefix("/agent-api/v1/runs/").split("/")
        is_cancel = (path.startswith("/agent-api/v1/runs/")
                     and len(cancel_segments) == 2 and bool(cancel_segments[0])
                     and cancel_segments[1] == "cancel" and not urlsplit(self.path).query)
        is_input = (path.startswith("/agent-api/v1/runs/")
                    and len(cancel_segments) == 2 and bool(cancel_segments[0])
                    and cancel_segments[1] == "inputs" and not urlsplit(self.path).query)
        if not is_run_create and not is_compact and not is_cancel and not is_input and self.path not in {
            "/agent-api/v1/query",
            "/agent-api/v1/workorder-drafts/confirm",
            "/agent-api/v1/inventory-operations/confirm",
            "/agent-api/v1/dv-machinerytype/confirm",
            "/agent-api/v1/sessions",
        }:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._write_error(HTTPStatus.BAD_REQUEST,
                              "VALIDATION_ERROR" if is_compact else "INVALID_REQUEST",
                              "请求内容无效。")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", "请求不是有效 JSON。")
            return
        if not isinstance(payload, dict):
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", "请求必须是对象。")
            return
        authorization = self.headers.get("Authorization", "")
        try:
            if self.path == "/agent-api/v1/sessions":
                title = payload.get("title")
                if title is not None and (not isinstance(title, str) or not title.strip()):
                    raise ValueError("title 必须是非空文本。")
                response = asyncio.run(self.server.session_service.create(authorization, title))
            elif is_run_create:
                response = asyncio.run(self.server.session_service.create_run(
                    authorization, run_segments[0], payload.get("request_id"),
                    payload.get("message"), payload.get("page_context"),
                ))
            elif is_compact:
                if set(payload) != {"request_id"}:
                    raise ValueError("压缩请求仅允许 request_id。")
                response = asyncio.run(self.server.session_service.request_compaction(
                    authorization, run_segments[0], payload["request_id"]))
            elif is_cancel:
                if set(payload) != {"request_id"}:
                    raise ValueError("取消请求仅允许 request_id。")
                response = asyncio.run(self.server.session_service.cancel_run(
                    authorization, cancel_segments[0], payload["request_id"],
                ))
            elif is_input:
                if set(payload) != {"request_id", "mode", "message"}:
                    raise ValueError("追加输入仅允许 request_id、mode 和 message。")
                response = asyncio.run(self.server.session_service.add_input(
                    authorization, cancel_segments[0], payload["request_id"],
                    payload["mode"], payload["message"],
                ))
            elif self.path == "/agent-api/v1/query":
                message = payload.get("message")
                if not isinstance(message, str):
                    raise ValueError("message 必须是文本。")
                response = asyncio.run(
                    self.server.query_service.query(
                        message=message,
                        authorization=authorization,
                    )
                )
            elif self.path == "/agent-api/v1/workorder-drafts/confirm":
                preview_id = payload.get("preview_id")
                if not isinstance(preview_id, str):
                    raise ValueError("preview_id 必须是文本。")
                response = asyncio.run(
                    self.server.query_service.confirm(
                        preview_id=preview_id,
                        authorization=authorization,
                    )
                )
            elif self.path == "/agent-api/v1/dv-machinerytype/confirm":
                preview_id = payload.get("preview_id")
                if not isinstance(preview_id, str):
                    raise ValueError("preview_id 必须是文本。")
                response = asyncio.run(
                    self.server.query_service.confirm_dv_machinerytype(
                        preview_id=preview_id, authorization=authorization,
                    )
                )
            else:
                operation_id = payload.get("operation_id")
                if not isinstance(operation_id, str):
                    raise ValueError("operation_id 必须是文本。")
                response = asyncio.run(self.server.query_service.confirm_inventory(
                    operation_id=operation_id, authorization=authorization,
                ))
        except KTGError as exc:
            self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
            return
        except (RunBusyError, RunConflictError, ControlConflictError, ControlStateError) as exc:
            self._write_error(HTTPStatus.CONFLICT, exc.code, str(exc))
            return
        except KeyError:
            code = "RUN_NOT_FOUND" if is_cancel or is_input else "SESSION_NOT_FOUND"
            self._write_error(HTTPStatus.NOT_FOUND, code, "运行不存在。" if is_cancel or is_input else "会话不存在。")
            return
        except ProviderError as exc:
            self._write_error(HTTPStatus.BAD_GATEWAY, exc.code, str(exc))
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", str(exc))
            return
        except Exception:
            self._write_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                "Agent 请求失败，请稍后重试。",
            )
            return
        status = (HTTPStatus.CREATED if self.path == "/agent-api/v1/sessions"
                  else HTTPStatus.ACCEPTED if is_run_create or is_compact or is_input or
                  (is_cancel and response["cancel_status"] == "accepted") else HTTPStatus.OK)
        self._write_json(status, response)

    def _get_run(self, path: str) -> None:
        run_id = path.removeprefix("/agent-api/v1/runs/")
        if not run_id or "/" in run_id or urlsplit(self.path).query:
            self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在。")
            return
        try:
            response = asyncio.run(self.server.session_service.get_run(
                self.headers.get("Authorization", ""), run_id,
            ))
        except KTGError as exc:
            self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
            return
        except KeyError:
            self._write_error(HTTPStatus.NOT_FOUND, "RUN_NOT_FOUND", "运行不存在。")
            return
        except Exception:
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Agent 请求失败，请稍后重试。")
            return
        self._write_json(HTTPStatus.OK, response)

    def _get_inputs(self, path: str) -> None:
        run_id = path.removeprefix("/agent-api/v1/runs/").removesuffix("/inputs")
        if not run_id or "/" in run_id:
            self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在。")
            return
        try:
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                              strict_parsing=True)
            if any(key not in {"cursor", "limit"} or len(values) != 1
                   for key, values in params.items()):
                raise ValueError("分页参数无效。")
            cursor = self._positive_int(params["cursor"][0]) if "cursor" in params else None
            limit = self._positive_int(params["limit"][0]) if "limit" in params else 50
            response = asyncio.run(self.server.session_service.inputs(
                self.headers.get("Authorization", ""), run_id,
                cursor=cursor, limit=min(limit, 100),
            ))
        except KTGError as exc:
            self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
            return
        except KeyError:
            self._write_error(HTTPStatus.NOT_FOUND, "RUN_NOT_FOUND", "运行不存在。")
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", str(exc))
            return
        except Exception:
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Agent 请求失败，请稍后重试。")
            return
        self._write_json(HTTPStatus.OK, response)

    def _get_events(self, path: str) -> None:
        run_id = path.removeprefix("/agent-api/v1/runs/").removesuffix("/events")
        if not run_id or "/" in run_id:
            self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在。")
            return
        try:
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                              strict_parsing=True)
            if any(key != "after_seq" or len(values) != 1 for key, values in params.items()):
                raise ValueError("事件游标无效。")
            raw = params.get("after_seq", ["0"])[0]
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError("after_seq 必须是非负整数。")
            after_seq = int(raw)
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", str(exc))
            return
        if not self.server.sse_slots.acquire(blocking=False):
            self._write_error(HTTPStatus.SERVICE_UNAVAILABLE, "SSE_CAPACITY_EXCEEDED",
                              "事件订阅已达到上限，请稍后重试。")
            return
        try:
            try:
                owner = asyncio.run(self.server.session_service.authorize_run(
                    self.headers.get("Authorization", ""), run_id,
                ))
                batch, settled = self.server.session_service.read_event_batch(
                    owner, run_id, after_seq=after_seq,
                )
            except KTGError as exc:
                self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except KeyError:
                self._write_error(HTTPStatus.NOT_FOUND, "RUN_NOT_FOUND", "运行不存在。")
                return
            except EventHistoryExpiredError:
                self._write_error(HTTPStatus.GONE, "EVENT_HISTORY_EXPIRED", "事件历史已过期。")
                return
            except Exception:
                self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR",
                                  "Agent 请求失败，请稍后重试。")
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last_heartbeat = monotonic()
            try:
                while not self.server.closing.is_set():
                    if self._client_disconnected():
                        return
                    items = batch["items"]
                    assert isinstance(items, list)
                    for item in items:
                        assert isinstance(item, dict)
                        self.wfile.write(_sse_event(item))
                        self.wfile.flush()
                        seq = item["seq"]
                        assert isinstance(seq, int)
                        after_seq = seq
                        if item["type"] == "run_settled":
                            return
                    if settled and batch["next_cursor"] is None:
                        return
                    if monotonic() - last_heartbeat >= self.server.sse_heartbeat_seconds:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat = monotonic()
                    if self.server.closing.wait(self.server.sse_poll_seconds):
                        return
                    batch, settled = self.server.session_service.read_event_batch(
                        owner, run_id, after_seq=after_seq,
                    )
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        finally:
            self.server.sse_slots.release()

    def _client_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def _get_session(self, path: str) -> None:
        segments = path.removeprefix("/agent-api/v1/sessions").strip("/").split("/")
        authorization = self.headers.get("Authorization", "")
        try:
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                              strict_parsing=True)
            if any(key not in {"cursor", "limit"} or len(values) != 1
                   for key, values in params.items()):
                raise ValueError("分页参数无效。")
            cursor = self._positive_int(params["cursor"][0]) if "cursor" in params else None
            limit = self._positive_int(params["limit"][0]) if "limit" in params else 50
            limit = min(limit, 100)
            if segments == [""]:
                response = asyncio.run(self.server.session_service.list(
                    authorization, cursor=cursor, limit=limit))
            elif len(segments) == 1 and segments[0]:
                response = asyncio.run(self.server.session_service.get(
                    authorization, segments[0]))
            elif len(segments) == 2 and segments[0] and segments[1] == "messages":
                response = asyncio.run(self.server.session_service.messages(
                    authorization, segments[0], cursor=cursor, limit=limit))
            elif len(segments) == 2 and segments[0] and segments[1] == "stats":
                if params:
                    raise ValueError("stats 不接受查询参数。")
                response = asyncio.run(self.server.session_service.stats(
                    authorization, segments[0]))
            else:
                self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在。")
                return
        except KTGError as exc:
            self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
            return
        except KeyError:
            self._write_error(HTTPStatus.NOT_FOUND, "SESSION_NOT_FOUND", "会话不存在。")
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", str(exc))
            return
        except Exception:
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Agent 请求失败，请稍后重试。")
            return
        self._write_json(HTTPStatus.OK, response)

    @staticmethod
    def _positive_int(value: str) -> int:
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError("分页参数必须是正整数。")
        return int(value)

    def log_message(self, format: str, *args: object) -> None:
        # Keep request logs free of headers, tokens and prompt bodies.
        super().log_message(format, *args)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # BaseHTTPRequestHandler normally logs the full request line, including query values.
        self.log_message("%s %s", self.command, code)

    def _write_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._write_json(status, {"error": {"code": code, "message": message}})

    def _write_json(self, status: HTTPStatus, payload: JsonObject) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def _sse_event(event: JsonObject) -> bytes:
    encoded = json.dumps(event, ensure_ascii=False, allow_nan=False, indent=2)
    return (f"id: {event['seq']}\nevent: {event['type']}\n" +
            "".join(f"data: {line}\n" for line in encoded.splitlines()) +
            "\n").encode("utf-8")


def main() -> None:
    settings = Settings.from_env()
    service = create_service(settings)
    server = AgentHTTPServer((settings.host, settings.port), service,
                             create_session_service(settings, service))
    print(f"KTG Agent listening on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

