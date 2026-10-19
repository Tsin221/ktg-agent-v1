"""Process-owned workers for admitted session runs."""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor

from ktg_agent.core.agent import Agent
from ktg_agent.core.events import AgentEvent, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import JsonObject, JsonValue, Message, StopReason, TextBlock, ToolResultMessage
from ktg_agent.providers.base import ProviderError
from ktg_agent.sessions.repository import ControlRecord, RunRecord, SessionRepository

type RegisterAgent = Callable[[Agent], Callable[[], None]]
RunExecutor = Callable[[str, str, tuple[Message, ...], JsonObject | None, str, EventSink, RegisterAgent], Awaitable[AgentRunResult]]
logger = logging.getLogger(__name__)


def _log_worker_error(run_id: str, phase: str, error: Exception) -> None:
    # Exception messages can contain request text or credentials; log stack frames and type only.
    frames = traceback.extract_tb(error.__traceback__)
    stack = " > ".join(f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in frames)
    logger.error("run_id=%s phase=%s error_type=%s stack=%s", run_id, phase,
                 type(error).__name__, stack)


class RunPhase(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    SETTLED = "settled"


@dataclass(slots=True)
class RunHandle:
    run_id: str
    phase: RunPhase = RunPhase.QUEUED
    loop: asyncio.AbstractEventLoop | None = None
    agent: Agent | None = None
    cancel_requested: bool = False


class RunNotRunningError(RuntimeError):
    code = "RUN_NOT_RUNNING"


class RunNotActiveError(RuntimeError):
    code = "RUN_NOT_ACTIVE"


class UsagePersistenceError(RuntimeError):
    code = "USAGE_PERSISTENCE_FAILED"


def _write_steps(events: list[AgentEvent], messages: tuple[Message, ...]) -> JsonObject:
    starts: dict[str, JsonObject] = {
        str(event.payload["tool_call_id"]): event.payload
        for event in events
        if event.type.value == "tool_execution_start" and event.payload.get("effect") == "write"
    }
    ends: dict[str, JsonObject] = {
        str(event.payload["tool_call_id"]): event.payload
        for event in events
        if event.type.value == "tool_execution_end" and event.payload.get("effect") == "write"
    }
    details = {
        item.tool_call_id: item.details for item in messages
        if isinstance(item, ToolResultMessage) and item.details is not None
    }
    steps: JsonObject = {"completed": [], "failed": [], "unknown": []}
    for call_id, start in starts.items():
        end = ends.get(call_id, {})
        if end.get("error_code") == "RUN_CANCELLED":
            continue
        outcome = end.get("status", "unknown")
        if outcome == "failed" and end.get("error_code") == "TOOL_EXECUTION_ERROR":
            outcome = "unknown"
        bucket = ("completed" if outcome == "succeeded" else
                  "failed" if outcome == "failed" else "unknown")
        step: JsonObject = {"tool_call_id": call_id, "tool_name": start["tool_name"]}
        for field in ("operation_id", "verification", "error_code"):
            if field in end:
                step[field] = end[field]
        if call_id in details:
            step["result"] = details[call_id]
        elif "result" in end:
            step["result"] = end["result"]
        entries = steps[bucket]
        assert isinstance(entries, list)
        entries.append(step)
    return steps


class RunCoordinator:
    """Owns run tasks beyond the lifetime of each HTTP request event loop."""

    def __init__(self, repository: SessionRepository, executor: RunExecutor,
                 budget_snapshot: JsonObject | None = None) -> None:
        self._repository = repository
        self._executor = executor
        self._budget_snapshot = budget_snapshot
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")
        self._handles: dict[str, RunHandle] = {}
        self._lock = Lock()

    def schedule(self, run: RunRecord, message: str, authorization: str) -> None:
        if self._budget_snapshot is not None:
            self._repository.set_run_budget(run.owner_user_id, run.id, self._budget_snapshot)
        with self._lock:
            current = self._repository.get_run(run.owner_user_id, run.id)
            assert current is not None
            self._handles[run.id] = RunHandle(
                run.id, cancel_requested=current.status == "cancelling",
            )
        try:
            self._workers.submit(self._execute, run, message, authorization)
        except RuntimeError:
            with self._lock:
                del self._handles[run.id]
            self._repository.settle_run(
                run.owner_user_id, run.id, "failed", "SCHEDULING_FAILED",
                {"error": "SCHEDULING_FAILED"}, (),
            )
            raise

    def _execute(self, run: RunRecord, message: str, authorization: str) -> None:
        write_events: list[AgentEvent] = []
        try:
            with self._lock:
                handle = self._handles[run.id]
                if handle.cancel_requested:
                    self._repository.settle_run(run.owner_user_id, run.id, "cancelled", "cancelled",
                                                {"stop_reason": "cancelled"}, ())
                    return
                handle.phase = RunPhase.STARTING
                self._repository.update_run_status(run.owner_user_id, run.id, "running")
            history = self._repository.completed_history(run.owner_user_id, run.session_id)
            page_context = run.request_summary.get("page_context")
            if page_context is not None and not isinstance(page_context, dict):
                raise ValueError("stored page_context is invalid")

            def persist_event(event: AgentEvent) -> None:
                try:
                    self._repository.append_event(
                        run.owner_user_id, run.id, event.seq, event.type.value, event.payload,
                    )
                except Exception as error:
                    raise UsagePersistenceError("event and usage could not be persisted") from error
                if event.type.value in {"tool_execution_start", "tool_execution_end"}:
                    write_events.append(event)

            async def invoke() -> AgentRunResult:
                owner_loop = asyncio.get_running_loop()

                def register_agent(agent: Agent) -> Callable[[], None]:
                    with self._lock:
                        handle = self._handles[run.id]
                        handle.loop = owner_loop
                        handle.agent = agent
                        handle.phase = RunPhase.RUNNING
                        agent.set_input_consumed_handler(
                            lambda request_id: self._repository.mark_input_consumed(
                                run.owner_user_id, run.id, request_id))
                        if handle.cancel_requested:
                            agent.abort_or_on_start()

                    def close_inputs() -> None:
                        with self._lock:
                            self._handles[run.id].phase = RunPhase.CLOSING

                    return close_inputs

                return await self._executor(run.id, message, history, page_context,
                                            authorization, persist_event, register_agent)

            result = asyncio.run(invoke())
            status = ("completed" if result.stop_reason is StopReason.COMPLETED else
                      "cancelled" if result.stop_reason is StopReason.CANCELLED else "failed")
            answer = "".join(
                block.text for block in result.final_message.content
                if isinstance(block, TextBlock)
            ).strip() if result.final_message is not None else ""
            new_messages = result.messages[1 + len(history):]
            business_results: list[JsonValue] = [
                item.details for item in new_messages
                if isinstance(item, ToolResultMessage) and item.details is not None
            ]
            steps = _write_steps(write_events, new_messages)
            if steps["unknown"]:
                status = "needs_reconciliation"
            summary: JsonObject = {
                "answer": answer or "查询未生成最终回答。",
                "stop_reason": result.stop_reason.value,
                "results": business_results,
                "steps": steps,
            }
            if result.limit_reason is not None:
                summary["limit_reason"] = result.limit_reason
            if result.error_code is not None:
                summary["error_code"] = result.error_code
            self._repository.settle_run(
                run.owner_user_id, run.id, status, result.stop_reason.value,
                summary, new_messages,
            )
        except Exception as error:
            _log_worker_error(run.id, "execute", error)
            # Persistence may be unavailable; leave a nonterminal run for recovery
            # instead of reporting an uncommitted successful result.
            try:
                steps = _write_steps(write_events, ())
                if isinstance(error, UsagePersistenceError):
                    failure_code = error.code
                    stop_reason = StopReason.ERROR.value
                elif isinstance(error, ProviderError):
                    failure_code = error.code
                    stop_reason = StopReason.ERROR.value
                else:
                    failure_code = "EXECUTION_FAILED"
                    stop_reason = "EXECUTION_FAILED"
                self._repository.settle_run(
                    run.owner_user_id, run.id,
                    "needs_reconciliation" if steps["unknown"] else "failed",
                    stop_reason,
                    {"error": failure_code, "error_code": failure_code,
                     "stop_reason": stop_reason, "steps": steps}, (),
                )
            except Exception as settlement_error:
                _log_worker_error(run.id, "settle_after_failure", settlement_error)
                raise
        finally:
            with self._lock:
                self._handles[run.id].phase = RunPhase.SETTLED
                self._handles.pop(run.id, None)

    def phase(self, run_id: str) -> RunPhase | None:
        with self._lock:
            handle = self._handles.get(run_id)
            return handle.phase if handle is not None else None

    def admit_input(self, owner: str, run_id: str, request_id: str,
                    mode: str, message: str) -> ControlRecord:
        with self._lock:
            handle = self._handles.get(run_id)
            ready = (handle is not None and handle.phase is RunPhase.RUNNING
                     and handle.agent is not None and handle.loop is not None)
            accepting = ready and handle is not None and handle.agent is not None and handle.agent.accepting_inputs
            record, created = self._repository.admit_control(
                owner, run_id, request_id, mode, {"mode": mode, "message": message},
                accepting=accepting, ready=ready,
                closed=handle is not None and handle.phase is RunPhase.CLOSING)
            if created:
                assert handle is not None and handle.loop is not None and handle.agent is not None
                loop, agent = handle.loop, handle.agent

                async def deliver() -> None:
                    if mode == "steer":
                        await agent.steer(message, request_id)
                    else:
                        await agent.follow_up(message, request_id)

                asyncio.run_coroutine_threadsafe(deliver(), loop)
            return record

    def admit_cancel(self, owner: str, run_id: str, request_id: str) -> ControlRecord:
        with self._lock:
            handle = self._handles.get(run_id)
            record, created = self._repository.admit_control(
                owner, run_id, request_id, "cancel", {}, accepting=False)
            if created and record.reason is None and handle is not None:
                handle.cancel_requested = True
                if handle.phase is RunPhase.RUNNING and handle.loop is not None and handle.agent is not None:
                    handle.loop.call_soon_threadsafe(handle.agent.abort_or_on_start)
            return record

    def submit_input(self, run_id: str, mode: str, message: str) -> Future[None]:
        with self._lock:
            handle = self._handles.get(run_id)
            if handle is None or handle.phase is RunPhase.CLOSING:
                raise RunNotActiveError(run_id)
            if handle.phase is not RunPhase.RUNNING or handle.loop is None or handle.agent is None:
                raise RunNotRunningError(run_id)
            loop, agent = handle.loop, handle.agent

        async def deliver() -> None:
            if mode == "steer":
                await agent.steer(message)
            elif mode == "follow_up":
                await agent.follow_up(message)
            else:
                raise ValueError("unknown input mode")

        return asyncio.run_coroutine_threadsafe(deliver(), loop)

    def submit_abort(self, run_id: str) -> Future[bool]:
        with self._lock:
            handle = self._handles.get(run_id)
            if handle is None or handle.phase in {RunPhase.CLOSING, RunPhase.SETTLED}:
                raise RunNotActiveError(run_id)
            if handle.phase is not RunPhase.RUNNING or handle.loop is None or handle.agent is None:
                raise RunNotRunningError(run_id)
            loop, agent = handle.loop, handle.agent

        async def abort() -> bool:
            return agent.abort()

        return asyncio.run_coroutine_threadsafe(abort(), loop)

    def close(self) -> None:
        self._workers.shutdown(wait=True)
