"""Thin application adapter over the native Pydantic AI run lifecycle.

HTTP owns admission, durable settlement, and SSE sequence allocation.  No
legacy execution loop, message protocol, or application-owned input queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace

from pydantic_ai import Agent, AgentInstructions, CancellationToken, ModelRequestContext, RunContext, capture_run_messages
from pydantic_ai.capabilities import AgentCapability, Hooks
from pydantic_ai.exceptions import OutputTokenLimitExceeded, RunCancelled, RunTimedOut, UsageLimitExceeded, UserError
from pydantic_ai.messages import (
    AgentStreamEvent, EnqueuedMessagesEvent, FunctionToolCallEvent, FunctionToolResultEvent,
    ModelMessage, ModelRequest, PartDeltaEvent, PartEndEvent, PartStartEvent, TextPart, TextPartDelta, ToolReturnPart, UserPromptPart,
)
from pydantic_ai.run import AgentRun
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.planning import (
    PlanCompletedEvent, PlanCreatedEvent, PlanDeletedEvent, PlanStatusChangedEvent, PlanUpdatedEvent,
)

from ktg_agent.core.messages import JsonObject
from ktg_agent.core.native_persistence import RecoveryRequired
from ktg_agent.sessions.native_planning import planning_task_id
from ktg_agent.tools.native import MesToolDeps

Publish = Callable[[str, str, JsonObject], None]
Delivered = Callable[[str, str, str], Awaitable[None]]
LoadHistory = Callable[[], Awaitable[Sequence[ModelMessage]]]
ArchiveMessages = Callable[[tuple[ModelMessage, ...]], Awaitable[None]]
CheckpointUsage = Callable[[RunUsage], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class NativeOutcome:
    status: str
    reason: str
    summary: JsonObject
    messages: tuple[ModelMessage, ...]
    new_messages: tuple[ModelMessage, ...]
    usage: RunUsage
    terminal_messages: tuple[ModelMessage, ...]
    working_messages: tuple[ModelMessage, ...]


class HistoryAlignmentError(RuntimeError):
    """The working history no longer follows an observed archive boundary."""


class _ArchiveCursor:
    """Per-run ordered boundary; identical messages at different positions survive.

    The first hook commits new original messages. A second hook after native
    compaction captures its rewritten working view, which is not archived.
    Between those hooks and the next model request, the graph appends new work.
    """

    def __init__(self, run_id: str, archive: ArchiveMessages) -> None:
        self.run_id, self.archive = run_id, archive
        self._prefix: tuple[ModelMessage, ...] | None = None

    def delta(self, current: Sequence[ModelMessage]) -> tuple[ModelMessage, ...]:
        if self._prefix is None:
            index = next((i for i, msg in enumerate(current) if msg.run_id == self.run_id), len(current))
            return tuple(current[index:])
        prefix = self._prefix
        if len(current) < len(prefix):
            raise HistoryAlignmentError('working history became shorter outside the compaction boundary')

        additions: list[ModelMessage] = []
        for index, before in enumerate(prefix):
            after = current[index]
            if before == after:
                continue
            # Native dispatch may stamp additional request metadata or splice
            # an ASAP input after the before_model_request chain has finished.
            # The already-archived request remains authoritative; if a user
            # prompt was spliced into it, preserve that newly delivered part
            # once, without storing the earlier request twice.
            if (index != len(prefix) - 1 or not isinstance(before, ModelRequest)
                    or not isinstance(after, ModelRequest) or before.timestamp != after.timestamp):
                raise HistoryAlignmentError('unexpected rewrite outside the compaction boundary')
            remaining = list(after.parts)
            for existing in before.parts:
                if existing not in remaining:
                    raise HistoryAlignmentError('an archived request part was removed')
                remaining.remove(existing)
            extra_prompts = [part for part in remaining if isinstance(part, UserPromptPart)]
            if extra_prompts:
                additions.append(replace(after, parts=extra_prompts))
        additions.extend(current[len(prefix):])
        return tuple(additions)

    async def before(self, ctx: RunContext[MesToolDeps], request: ModelRequestContext) -> ModelRequestContext:
        delta = self.delta(ctx.messages)
        if delta:
            # A worker's SQLite write must finish before cancellation decides
            # whether these messages belong in the terminal batch.
            task = asyncio.ensure_future(self.archive(deepcopy(delta)))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                self._prefix = deepcopy(tuple(ctx.messages))
                raise
        self._prefix = deepcopy(tuple(ctx.messages))
        return request

    async def after(self, ctx: RunContext[MesToolDeps], request: ModelRequestContext) -> ModelRequestContext:
        self._prefix = deepcopy(tuple(ctx.messages))
        return request


class NativeExecution:
    """Single-use run, constructed using an already admitted identity and scope."""

    def __init__(self, agent: Agent[MesToolDeps, str], deps: MesToolDeps, user_prompt: str, *,
                 message_history: Sequence[ModelMessage] = (), load_history: LoadHistory | None = None,
                 model_settings: ModelSettings | None = None, usage_limits: UsageLimits | None = None,
                 timeout: float | None = 90, publish: Publish | None = None, delivered: Delivered | None = None,
                 capabilities: Sequence[AgentCapability[MesToolDeps]] = (),
                 archive_messages: ArchiveMessages | None = None,
                 checkpoint_usage: CheckpointUsage | None = None,
                 instructions: AgentInstructions[MesToolDeps] = None) -> None:
        self._agent = agent
        self._deps = deps
        self._prompt = user_prompt
        self._history = tuple(message_history)
        self._load_history = load_history
        self._model_settings = model_settings
        self._limits = usage_limits if usage_limits is not None else UsageLimits(request_limit=16, tool_calls_limit=24)
        self._timeout = timeout
        self._publish = publish
        self._delivered = delivered
        self._capabilities = tuple(capabilities)
        self._archive_cursor = None if archive_messages is None else _ArchiveCursor(deps.run_id, archive_messages)
        self._checkpoint_usage = checkpoint_usage
        self._instructions = instructions
        self._last_usage = RunUsage()
        self._token = CancellationToken()
        self._run: AgentRun[MesToolDeps, str] | None = None
        self._started = False
        self._closed = False
        self._enqueue_ids: dict[str, str] = {}
        self._request_ids: set[str] = set()
        self._unknown_result = False
        self._text_parts: dict[int, str] = {}
        self._text_seq = 0
        self.interrupted_outcome: NativeOutcome | None = None

    def stop(self) -> None:
        if not self._closed:
            self._token.cancel()

    def input(self, request_id: str, mode: str, message: str) -> bool:
        """Report admission, NOT delivery. Call only on the owning event loop.

        No waiting for a model, no replay before/after the live native handle;
        Pydantic AI owns queue ordering and the delivery boundary.
        """
        if (self._run is None or self._closed or self._token.cancelled or not message.strip()
                or not request_id or request_id in self._request_ids
                or mode not in ('steer', 'follow_up')):
            return False
        try:
            enqueue_id = self._run.enqueue(message, priority='asap' if mode == 'steer' else 'when_idle')
        except UserError:
            return False
        if enqueue_id is None:
            return False
        self._request_ids.add(request_id)
        self._enqueue_ids[enqueue_id] = request_id
        return True

    def _emit(self, kind: str, payload: JsonObject) -> None:
        if self._publish is not None:
            self._publish(self._deps.run_id, kind, payload)

    async def _checkpoint(self, usage: RunUsage) -> None:
        if self._checkpoint_usage is not None and usage != self._last_usage:
            snapshot = deepcopy(usage)
            await self._checkpoint_usage(snapshot)
            self._last_usage = snapshot
            self._emit('model_usage_reported', {'requests': snapshot.requests, 'tool_calls': snapshot.tool_calls,
                                               'input_tokens': snapshot.input_tokens, 'output_tokens': snapshot.output_tokens,
                                               'complete': snapshot.unmeasured_requests == 0})

    async def _event(self, ctx: RunContext[MesToolDeps], event: AgentStreamEvent) -> None:
        # Never forward whole raw native events, reasoning, raw tool args, metadata,
        # or exception messages: these are not display DTOs.
        if isinstance(event, EnqueuedMessagesEvent):
            request_id = self._enqueue_ids.pop(event.enqueue_id, None)
            if request_id is not None and self._delivered is not None:
                await self._delivered(self._deps.run_id, request_id, event.enqueue_id)
        elif isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            self._text_seq += 1
            message_id = f'{self._deps.run_id}:message:{self._text_seq}'
            self._text_parts[event.index] = message_id
            self._emit('message_start', {'message_id': message_id, 'role': 'assistant'})
            if event.part.content:
                self._emit('message_update', {'message_id': message_id, 'role': 'assistant',
                                             'delta_type': 'text', 'text': event.part.content})
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            if delta_message_id := self._text_parts.get(event.index):
                self._emit('message_update', {'message_id': delta_message_id, 'role': 'assistant',
                                             'delta_type': 'text', 'text': event.delta.content_delta})
        elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
            if ended_message_id := self._text_parts.pop(event.index, None):
                self._emit('message_end', {'message_id': ended_message_id, 'role': 'assistant',
                                           'stop_reason': 'tool_call' if event.next_part_kind == 'tool-call' else 'completed'})
        elif isinstance(event, FunctionToolCallEvent):
            self._emit('tool_execution_start', {'tool_call_id': event.tool_call_id, 'tool_name': event.part.tool_name})
        elif isinstance(event, FunctionToolResultEvent):
            metadata = event.part.metadata if isinstance(event.part.metadata, dict) else {}
            status = metadata.get('status')
            if status not in ('succeeded', 'failed', 'unknown'):
                status = 'succeeded' if event.part.outcome == 'success' else 'failed'
            if event.part.outcome != 'success' and status == 'succeeded':
                status = 'failed'
            if status == 'unknown':
                self._unknown_result = True
            payload: JsonObject = {'tool_call_id': event.tool_call_id, 'tool_name': event.part.tool_name, 'status': status}
            if isinstance(metadata.get('error_code'), str):
                payload['error_code'] = metadata['error_code']
            self._emit('tool_execution_end', payload)
            await self._checkpoint(ctx.usage)
        elif isinstance(event, (PlanCreatedEvent, PlanUpdatedEvent, PlanStatusChangedEvent, PlanCompletedEvent, PlanDeletedEvent)):
            self._emit('plan_changed', {'task_id': planning_task_id(ctx.deps), 'step_id': event.item.id,
                                        'status': event.item.status.value, 'deleted': isinstance(event, PlanDeletedEvent)})

    def _outcome(self, status: str, reason: str, history: Sequence[ModelMessage], new: Sequence[ModelMessage],
                 usage: RunUsage, *, answer: str | None = None) -> NativeOutcome:
        # Unknown business effects always take precedence over a model's prose.
        if self._unknown_result or any(
            isinstance(part, ToolReturnPart) and isinstance(part.metadata, dict) and part.metadata.get('status') == 'unknown'
            for message in new for part in message.parts
        ):
            status, reason = 'needs_reconciliation', 'tool_result_unknown'
        try:
            terminal = tuple(new) if self._archive_cursor is None else self._archive_cursor.delta(history)
        except HistoryAlignmentError:
            # Do not silently publish success or append a guessed overlap.
            status, reason = 'needs_reconciliation', 'history_alignment_failed'
            terminal = ()
        summary: JsonObject = {'usage': {
            'requests': usage.requests, 'tool_calls': usage.tool_calls,
            'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
            'cache_read_tokens': usage.cache_read_tokens, 'cache_write_tokens': usage.cache_write_tokens,
            'known_total_tokens': usage.total_tokens if usage.total_tokens or (usage.requests and not usage.unmeasured_requests) else None,
            'unmeasured_requests': usage.unmeasured_requests,
            'complete': usage.unmeasured_requests == 0 and status == 'completed',
        }}
        if reason == 'timeout':
            summary['limit_reason'] = 'max_run_seconds'
        elif reason == 'output_limit':
            summary['error_code'] = 'MODEL_OUTPUT_LIMIT'
        if status == 'completed' and answer is not None:
            summary['answer'] = answer
        working = tuple(history)
        return NativeOutcome(status, reason, summary, working, tuple(new), deepcopy(usage), terminal, working)

    async def run(self) -> NativeOutcome:
        if self._started:
            raise RuntimeError('NativeExecution is single-use')
        self._started = True
        history = self._history
        usage = RunUsage()
        native: AgentRun[MesToolDeps, str] | None = None
        try:
            if self._token.cancelled:
                return self._outcome('cancelled', 'cancelled', history, (), usage)
            # Runtime has already committed the application running state.
            # Announce execution before potentially slow history recovery;
            # input() still requires the actual native run handle below.
            self._emit('agent_start', {})
            if self._load_history is not None:
                try:
                    history = tuple(await self._load_history())
                except asyncio.CancelledError:
                    self.interrupted_outcome = self._outcome('cancelled', 'execution_interrupted', history, (), usage)
                    raise
                except RecoveryRequired:
                    return self._outcome('needs_reconciliation', 'recovery_required', history, (), usage)
                except Exception:
                    return self._outcome('failed', 'history_load_failed', history, (), usage)
            if self._token.cancelled:
                return self._outcome('cancelled', 'cancelled', history, (), usage)
            # Hooks(event=...) receives the entire native model + tool stream,
            # including the terminal drain, while iter retains the input handle.
            # https://pydantic.dev/docs/ai/core-concepts/agent/#iterating-over-an-agents-graph
            # https://pydantic.dev/docs/ai/core-concepts/message-history/#injecting-messages-mid-run
            with capture_run_messages() as captured:
                try:
                    capabilities = [*self._capabilities, Hooks(event=self._event)]
                    if self._archive_cursor is not None:
                        capabilities = [Hooks(before_model_request=self._archive_cursor.before), *capabilities,
                                        Hooks(before_model_request=self._archive_cursor.after)]
                    async with self._agent.iter(self._prompt, deps=self._deps, message_history=history,
                            run_id=self._deps.run_id, conversation_id=self._deps.session_id,
                            model_settings=self._model_settings, usage_limits=self._limits, instructions=self._instructions,
                            usage=usage, cancellation_token=self._token, timeout=self._timeout,
                            capabilities=capabilities) as native:
                        self._run = native
                        async for _ in native:
                            await self._checkpoint(native.usage)
                        self._run = None  # no admission after graph end
                    result = native.result
                    if result is None:
                        return self._outcome('failed', 'no_result', native.all_messages(), native.new_messages(), native.usage)
                    if result.response.finish_reason == 'length':
                        return self._outcome('failed', 'output_limit', result.all_messages(), result.new_messages(), result.usage)
                    return self._outcome('completed', 'completed', result.all_messages(), result.new_messages(),
                                         result.usage, answer=result.output)
                except RunTimedOut as exc:
                    return self._outcome('failed', 'timeout', exc.all_messages(), exc.new_messages(), exc.usage)
                except RunCancelled as exc:
                    return self._outcome('cancelled', 'cancelled', exc.all_messages(), exc.new_messages(), exc.usage)
                except asyncio.CancelledError as exc:
                    # Infrastructure cancellation must keep propagating; the HTTP
                    # owner can settle using this detached partial native outcome.
                    cancelled = RunCancelled.from_cancellation(exc)
                    self.interrupted_outcome = self._outcome('needs_reconciliation', 'execution_interrupted',
                        cancelled.all_messages() if cancelled else captured or history,
                        cancelled.new_messages() if cancelled else native.new_messages() if native else (),
                        cancelled.usage if cancelled else usage)
                    raise
                except Exception as exc:
                    reason = ('usage_limit' if isinstance(exc, UsageLimitExceeded) else
                              'output_limit' if isinstance(exc, OutputTokenLimitExceeded) else 'execution_failed')
                    return self._outcome('failed', reason, native.all_messages() if native else captured or history,
                                         native.new_messages() if native else (), native.usage if native else usage)
        finally:
            self._closed = True
            self._run = None
            self._enqueue_ids.clear()
