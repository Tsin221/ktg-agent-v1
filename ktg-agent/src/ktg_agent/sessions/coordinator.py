"""Process-owned workers for admitted session runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from ktg_agent.core.events import AgentEvent, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import JsonObject, JsonValue, Message, StopReason, TextBlock, ToolResultMessage
from ktg_agent.sessions.repository import RunRecord, SessionRepository

RunExecutor = Callable[[str, str, tuple[Message, ...], JsonObject | None, str, EventSink], Awaitable[AgentRunResult]]


class RunCoordinator:
    """Owns run tasks beyond the lifetime of each HTTP request event loop."""

    def __init__(self, repository: SessionRepository, executor: RunExecutor) -> None:
        self._repository = repository
        self._executor = executor
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")

    def schedule(self, run: RunRecord, message: str, authorization: str) -> None:
        try:
            self._workers.submit(self._execute, run, message, authorization)
        except RuntimeError:
            self._repository.settle_run(
                run.owner_user_id, run.id, "failed", "SCHEDULING_FAILED",
                {"error": "SCHEDULING_FAILED"}, (),
            )
            raise

    def _execute(self, run: RunRecord, message: str, authorization: str) -> None:
        try:
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
                return await self._executor(run.id, message, history, page_context,
                                            authorization, persist_event)

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

    def close(self) -> None:
        self._workers.shutdown(wait=True)
