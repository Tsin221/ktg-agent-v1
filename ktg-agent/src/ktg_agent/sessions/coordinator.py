"""Process-owned workers for admitted session runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import traceback
from time import monotonic, sleep
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor

from ktg_agent.core.agent import Agent
from ktg_agent.core.events import AgentEvent, AgentEventType, EventPublisher, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import (JsonObject, JsonValue, Message, StopReason,
                                      SystemMessage, TextBlock, ToolCall, ToolResultMessage)
from ktg_agent.context.compaction import (CompactionFailedError, PreparedCompaction,
                                           SummaryDraft, SummaryNotSmallerError, validate_summary)
from ktg_agent.context.assembler import (AssembledContext, assemble_context, context_snapshot)
from ktg_agent.context.window import ContextOverflowError
from ktg_agent.providers.base import ProviderError
from ktg_agent.sessions.repository import (ControlRecord, MessageRecord, RunBusyError,
                                           RunRecord, SessionRepository)

type RegisterAgent = Callable[[Agent], Callable[[], None]]
RunExecutor = Callable[[str, str, tuple[Message, ...], JsonObject | None, str, EventSink, RegisterAgent], Awaitable[AgentRunResult]]
type CompactionPlanner = Callable[[tuple[Message, ...], JsonObject | None], PreparedCompaction | None]
type CompactionExecutor = Callable[[str, PreparedCompaction, EventSink, EventPublisher, RegisterAgent], Awaitable[AgentRunResult]]
logger = logging.getLogger(__name__)


class _CompactionPublisher(EventPublisher):
    """Keep the loop's end event after the summary outcome in one sequence."""

    def __init__(self, run_id: str, sink: EventSink) -> None:
        super().__init__(run_id, sink)
        self._deferred_end: JsonObject | None = None

    async def emit(self, event_type: AgentEventType, payload: JsonObject) -> AgentEvent:
        if event_type is AgentEventType.AGENT_END:
            self._deferred_end = payload
            return AgentEvent(self.run_id, len(self.events) + 1, event_type, payload)
        return await super().emit(event_type, payload)

    async def flush_end(self) -> None:
        if self._deferred_end is not None:
            await super().emit(AgentEventType.AGENT_END, self._deferred_end)
            self._deferred_end = None


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
                 budget_snapshot: JsonObject | None = None, *,
                 compaction_planner: CompactionPlanner | None = None,
                 compaction_executor: CompactionExecutor | None = None,
                 system_prompt_builder: Callable[[JsonObject | None], str] | None = None,
                 summary_budget_snapshot: JsonObject | None = None) -> None:
        self._repository = repository
        self._executor = executor
        self._budget_snapshot = budget_snapshot
        self._compaction_planner = compaction_planner
        self._compaction_executor = compaction_executor
        self._system_prompt_builder = system_prompt_builder
        self._summary_budget_snapshot = summary_budget_snapshot
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")
        self._handles: dict[str, RunHandle] = {}
        self._lock = Lock()

    def schedule(self, run: RunRecord, message: str, authorization: str, *,
                 budget: JsonObject | None = None) -> None:
        snapshot = budget if budget is not None else self._budget_snapshot
        if snapshot is not None:
            self._repository.set_run_budget(run.owner_user_id, run.id, snapshot)
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

    @property
    def summary_budget_snapshot(self) -> JsonObject | None:
        return self._summary_budget_snapshot

    def _execute(self, run: RunRecord, message: str, authorization: str) -> None:
        write_events: list[AgentEvent] = []
        last_context: JsonObject | None = None
        try:
            with self._lock:
                handle = self._handles[run.id]
                if handle.cancel_requested:
                    if run.kind == "compaction":
                        self._repository.append_event(
                            run.owner_user_id, run.id, 1, "compaction_failed",
                            {"error_code": "COMPACTION_FAILED", "stage": "summary"})
                        self._repository.settle_run(
                            run.owner_user_id, run.id, "cancelled", "cancelled",
                            {"kind": "compaction", "error_code": "COMPACTION_FAILED",
                             "stage": "summary", "context": {"state": "summary_absent"}}, ())
                        return
                    self._repository.settle_run(run.owner_user_id, run.id, "cancelled", "cancelled",
                                                {"stop_reason": "cancelled"}, ())
                    return
                handle.phase = RunPhase.STARTING
                self._repository.update_run_status(run.owner_user_id, run.id, "running")
            records = self._repository.completed_message_records(run.owner_user_id, run.session_id)
            assembled = assemble_context(records, self._repository.active_summary(
                run.owner_user_id, run.session_id))
            history = assembled.messages
            last_context = context_snapshot(assembled, estimated_tokens=None, spill_count=0)
            page_context = run.request_summary.get("page_context")
            if page_context is not None and not isinstance(page_context, dict):
                raise ValueError("stored page_context is invalid")

            def persist_event(event: AgentEvent) -> None:
                nonlocal last_context
                payload = event.payload
                if event.type is AgentEventType.CONTEXT_ASSEMBLED:
                    estimate = payload.get("estimated_tokens")
                    spills = payload.get("spill_count")
                    retained = payload.get("retained_message_count")
                    payload = {**payload, "summary_id": assembled.summary_id,
                               "covers_start_message_id": assembled.covers_start_message_id,
                               "covers_end_message_id": assembled.covers_end_message_id,
                               "retained_message_count": (retained if type(retained) is int
                                                          else assembled.retained_message_count),
                               "summary_absent": assembled.summary_absent,
                               "trimmed": bool(payload.get("trimmed")) or assembled.trimmed}
                    last_context = context_snapshot(
                        assembled, estimated_tokens=estimate if type(estimate) is int else None,
                        spill_count=spills if type(spills) is int else 0)
                    last_context["retained_message_count"] = payload["retained_message_count"]
                    last_context["trimmed"] = payload["trimmed"]
                try:
                    self._repository.append_event(
                        run.owner_user_id, run.id, event.seq, event.type.value, payload,
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

            if run.kind == "compaction":
                asyncio.run(self._compact(run, records, assembled, page_context,
                                          persist_event, lambda: last_context))
                return

            result = asyncio.run(invoke())
            status = ("completed" if result.stop_reason is StopReason.COMPLETED else
                      "cancelled" if result.stop_reason is StopReason.CANCELLED else "failed")
            answer = "".join(
                block.text for block in result.final_message.content
                if isinstance(block, TextBlock)
            ).strip() if result.final_message is not None else ""
            new_messages = result.new_messages
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
                summary, new_messages, context=last_context,
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
                    context=last_context,
                )
            except Exception as settlement_error:
                _log_worker_error(run.id, "settle_after_failure", settlement_error)
                raise
        finally:
            with self._lock:
                self._handles[run.id].phase = RunPhase.SETTLED
                self._handles.pop(run.id, None)

    def _frame(self, history: tuple[Message, ...],
               page_context: JsonObject | None) -> tuple[Message, ...]:
        assert self._system_prompt_builder is not None
        return (SystemMessage((TextBlock(self._system_prompt_builder(page_context)),)), *history)

    def _cancelling(self, run: RunRecord) -> bool:
        current = self._repository.get_run(run.owner_user_id, run.id)
        return current is not None and current.status == "cancelling"

    def compact_before_run(self, owner: str, session_id: str,
                           history: tuple[Message, ...], page_context: JsonObject | None,
                           authorization: str, *, parent_request_id: str) -> JsonObject | None:
        if self._compaction_planner is None:
            return None
        records = self._repository.completed_message_records(owner, session_id)
        history = assemble_context(records, self._repository.active_summary(owner, session_id)).messages
        try:
            prepared = self._compaction_planner(self._frame(history, page_context), page_context)
        except ContextOverflowError:
            # The user run records the exact T26 overflow with its own request frame.
            return None
        if prepared is None:
            return None
        request_id = f"compact:{parent_request_id}"
        identity: JsonObject = {"kind": "compaction", "trigger": "preflight",
                                "covers_start_index": prepared.covers_start_index,
                                "covers_end_index": prepared.covers_end_index,
                                "source_message_count": prepared.source_message_count}
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        summary: JsonObject = {"kind": "compaction", "trigger": "preflight",
                               "parent_run_id": None, "page_context": page_context}
        run, created = self._repository.admit_run(owner, session_id, request_id, digest, summary,
                                                 kind="compaction")
        if created:
            self.schedule(run, "", authorization, budget=self._summary_budget_snapshot)
        configured_seconds = (self._summary_budget_snapshot or {}).get("max_run_seconds", 90)
        assert isinstance(configured_seconds, int | float)
        max_seconds = float(configured_seconds) + 10
        deadline = monotonic() + max_seconds
        while monotonic() < deadline:
            current = self._repository.get_run(owner, run.id)
            assert current is not None
            if current.status in {"completed", "failed", "cancelled", "needs_reconciliation"}:
                context = current.result_summary.get("context") if current.result_summary else None
                return context if isinstance(context, dict) else None
            sleep(0.02)
        self.admit_cancel(owner, run.id, f"timeout:{parent_request_id}")
        # A cancelled worker may still be winding down; give it a bounded grace
        # window so a short settle does not surface as a false RUN_BUSY.
        settle_deadline = monotonic() + 5
        while monotonic() < settle_deadline:
            current = self._repository.get_run(owner, run.id)
            if current is None or current.status in {
                "completed", "failed", "cancelled", "needs_reconciliation",
            }:
                return None
            sleep(0.02)
        raise RunBusyError("压缩运行尚未结束。")

    async def _compact(self, run: RunRecord, records: tuple[MessageRecord, ...],
                       assembled: AssembledContext, page_context: JsonObject | None,
                       sink: EventSink, snapshot: Callable[[], JsonObject]) -> None:
        publisher = _CompactionPublisher(run.id, sink)
        stage = "validate"
        try:
            if self._compaction_planner is None or self._compaction_executor is None:
                raise CompactionFailedError("validate", "compaction is not configured")
            prepared = self._compaction_planner(self._frame(assembled.messages, page_context), page_context)
            if prepared is None:
                raise CompactionFailedError("validate", "no complete message group to compact")
            source_records = tuple(record for record in records
                                   if assembled.covers_end_message_id is None
                                   or record.id > assembled.covers_end_message_id)
            visible_records = tuple(record for record in source_records
                                    if any(isinstance(block, ToolCall)
                                           or isinstance(block, TextBlock) and block.model_visible
                                           for block in record.message.content))
            new_count = prepared.source_message_count - (1 if assembled.summary_id else 0)
            if new_count < 0 or new_count > len(visible_records):
                raise CompactionFailedError("validate", "summary coverage is invalid")
            start_id = (assembled.covers_start_message_id if assembled.summary_id else
                        source_records[0].id if source_records else None)
            end_id = (visible_records[new_count - 1].id if new_count else
                      assembled.covers_end_message_id)
            if start_id is None or end_id is None:
                raise CompactionFailedError("validate", "summary coverage has no canonical messages")
            await publisher.emit(AgentEventType.COMPACTION_STARTED, {
                "session_id": run.session_id,
                "trigger": run.request_summary.get("trigger", "manual"),
                "parent_run_id": run.request_summary.get("parent_run_id"),
                "source_message_count": prepared.source_message_count,
                "covers_start_index": prepared.covers_start_index,
                "covers_end_index": prepared.covers_end_index,
                "covers_start_message_id": start_id,
                "covers_end_message_id": end_id,
            })
            stage = "summary"
            owner_loop = asyncio.get_running_loop()

            def register_agent(agent: Agent) -> Callable[[], None]:
                with self._lock:
                    handle = self._handles[run.id]
                    handle.loop = owner_loop
                    handle.agent = agent
                    handle.phase = RunPhase.RUNNING
                    if handle.cancel_requested:
                        agent.abort_or_on_start()

                def close_inputs() -> None:
                    with self._lock:
                        self._handles[run.id].phase = RunPhase.CLOSING
                return close_inputs

            result = await self._compaction_executor(run.id, prepared, sink, publisher, register_agent)
            if result.stop_reason is not StopReason.COMPLETED:
                raise CompactionFailedError("summary", result.error_code or result.stop_reason.value)
            text = ("".join(block.text for block in result.final_message.content
                            if isinstance(block, TextBlock))
                    if result.final_message is not None else "")
            stage = "validate"
            draft = validate_summary(SummaryDraft(text, 0, prepared.source_token_estimate))
            if self._cancelling(run):
                raise CompactionFailedError("summary", "compaction was cancelled")
            call_id = f"{run.id}:model:1"
            request = result.model_requests[0] if result.model_requests else None
            reported = request.usage if request is not None and request.usage_source == "provider_reported" else None
            usage: JsonObject | None = (
                {"input_tokens": reported.input_tokens,
                 "output_tokens": reported.output_tokens,
                 "cached_input_tokens": reported.cached_input_tokens,
                 "total_tokens": reported.total_tokens}
                if reported is not None else None)
            stage = "commit"
            # Recheck immediately before the durable commit so a cancel that
            # arrived after validation cannot publish an active summary.
            if self._cancelling(run):
                raise CompactionFailedError("summary", "compaction was cancelled")
            try:
                committed = self._repository.commit_summary(
                    run.owner_user_id, run.session_id, run_id=run.id,
                    covers_start_message_id=start_id, covers_end_message_id=end_id,
                    source_message_count=prepared.source_message_count,
                    source_token_estimate=prepared.source_token_estimate,
                    summary_text=draft.text, summary_token_estimate=draft.summary_token_estimate,
                    model=request.model if request is not None else "unknown",
                    call_id=call_id, usage=usage)
            except ValueError as error:
                if str(error) == "SUMMARY_NOT_SMALLER":
                    not_smaller = SummaryNotSmallerError(str(error))
                    setattr(not_smaller, "stage", "commit")
                    raise not_smaller from error
                raise CompactionFailedError("commit", "summary commit failed") from error
            except sqlite3.Error as error:
                raise CompactionFailedError("commit", "summary commit failed") from error
            context: JsonObject = {"state": "summary_ready", "summary_id": committed.summary_id,
                                   "covers_start_index": prepared.covers_start_index,
                                   "covers_end_index": prepared.covers_end_index,
                                   "source_message_count": prepared.source_message_count,
                                   "source_token_estimate": prepared.source_token_estimate,
                                   "summary_token_estimate": draft.summary_token_estimate,
                                   "call_id": call_id, "summary_text": draft.text}
            await publisher.emit(AgentEventType.COMPACTION_SUMMARY, {
                "summary_id": committed.summary_id, "source_token_estimate": prepared.source_token_estimate,
                "summary_token_estimate": draft.summary_token_estimate, "call_id": call_id})
            await publisher.flush_end()
            self._repository.settle_run(run.owner_user_id, run.id, "completed", "completed",
                                        {"kind": "compaction", "context": context}, (),
                                        context=snapshot())
        except (CompactionFailedError, SummaryNotSmallerError,
                ProviderError, ContextOverflowError) as error:
            code = (error.code if isinstance(error, SummaryNotSmallerError)
                    else "COMPACTION_FAILED")
            if isinstance(error, CompactionFailedError):
                stage = error.stage
            elif isinstance(error, SummaryNotSmallerError):
                # validate_summary raises without a stage; only the commit path
                # marks itself, so the default is the validation stage.
                stage = getattr(error, "stage", "validate")
            current = self._repository.get_run(run.owner_user_id, run.id)
            cancelled = current is not None and current.status == "cancelling"
            await publisher.emit(AgentEventType.COMPACTION_FAILED,
                                 {"error_code": code, "stage": stage})
            await publisher.flush_end()
            self._repository.settle_run(run.owner_user_id, run.id,
                                        "cancelled" if cancelled else "failed",
                                        "cancelled" if cancelled else "error",
                                        {"kind": "compaction", "error_code": code,
                                         "stage": stage, "context": {"state": "summary_absent"}}, (),
                                        context=snapshot())

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
