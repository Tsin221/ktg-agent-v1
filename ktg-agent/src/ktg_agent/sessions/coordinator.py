"""Process-owned workers for admitted session runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor

from ktg_agent.core.agent import Agent
from ktg_agent.core.events import AgentEvent, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import JsonObject, JsonValue, Message, StopReason, TextBlock, ToolResultMessage
from ktg_agent.sessions.repository import RunRecord, SessionRepository

type RegisterAgent = Callable[[Agent], Callable[[], None]]
RunExecutor = Callable[[str, str, tuple[Message, ...], JsonObject | None, str, EventSink, RegisterAgent], Awaitable[AgentRunResult]]


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


class RunNotRunningError(RuntimeError):
    code = "RUN_NOT_RUNNING"


class RunNotActiveError(RuntimeError):
    code = "RUN_NOT_ACTIVE"


class RunCoordinator:
    """Owns run tasks beyond the lifetime of each HTTP request event loop."""

    def __init__(self, repository: SessionRepository, executor: RunExecutor) -> None:
        self._repository = repository
        self._executor = executor
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")
        self._handles: dict[str, RunHandle] = {}
        self._lock = Lock()

    def schedule(self, run: RunRecord, message: str, authorization: str) -> None:
        with self._lock:
            self._handles[run.id] = RunHandle(run.id)
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
        try:
            with self._lock:
                self._handles[run.id].phase = RunPhase.STARTING
            self._repository.update_run_status(run.owner_user_id, run.id, "running")
            history = self._repository.completed_history(run.owner_user_id, run.session_id)
            page_context = run.request_summary.get("page_context")
            if page_context is not None and not isinstance(page_context, dict):
                raise ValueError("stored page_context is invalid")

            def persist_event(event: AgentEvent) -> None:
                self._repository.append_event(
                    run.owner_user_id, run.id, event.seq, event.type.value, event.payload,
                )

            async def invoke() -> AgentRunResult:
                owner_loop = asyncio.get_running_loop()

                def register_agent(agent: Agent) -> Callable[[], None]:
                    with self._lock:
                        handle = self._handles[run.id]
                        handle.loop = owner_loop
                        handle.agent = agent
                        handle.phase = RunPhase.RUNNING

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
            summary: JsonObject = {
                "answer": answer or "查询未生成最终回答。",
                "stop_reason": result.stop_reason.value,
                "results": business_results,
            }
            if result.limit_reason is not None:
                summary["limit_reason"] = result.limit_reason
            self._repository.settle_run(
                run.owner_user_id, run.id, status, result.stop_reason.value,
                summary, new_messages,
            )
        except Exception:
            # Persistence may be unavailable; leave a nonterminal run for recovery
            # instead of reporting an uncommitted successful result.
            try:
                self._repository.settle_run(
                    run.owner_user_id, run.id, "failed", "EXECUTION_FAILED",
                    {"error": "EXECUTION_FAILED"}, (),
                )
            except Exception:
                pass
        finally:
            with self._lock:
                self._handles[run.id].phase = RunPhase.SETTLED
                self._handles.pop(run.id, None)

    def phase(self, run_id: str) -> RunPhase | None:
        with self._lock:
            handle = self._handles.get(run_id)
            return handle.phase if handle is not None else None

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
