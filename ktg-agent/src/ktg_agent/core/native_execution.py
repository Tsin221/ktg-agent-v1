"""Thin application adapter over the native Pydantic AI run lifecycle.

HTTP owns admission, durable settlement, and SSE sequence allocation.  No
legacy execution loop, message protocol, or application-owned input queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic_ai import Agent, CancellationToken, RunContext, capture_run_messages
from pydantic_ai.capabilities import AgentCapability, Hooks
from pydantic_ai.exceptions import OutputTokenLimitExceeded, RunCancelled, RunTimedOut, UsageLimitExceeded, UserError
from pydantic_ai.messages import (
    AgentStreamEvent, EnqueuedMessagesEvent, FunctionToolCallEvent, FunctionToolResultEvent,
    ModelMessage, PartDeltaEvent, PartEndEvent, PartStartEvent, TextPart, TextPartDelta, ToolReturnPart,
)
from pydantic_ai.run import AgentRun
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.planning import (
    PlanCompletedEvent, PlanCreatedEvent, PlanDeletedEvent, PlanStatusChangedEvent, PlanUpdatedEvent,
)

from ktg_agent.core.messages import JsonObject
from ktg_agent.sessions.native_planning import planning_task_id
from ktg_agent.tools.native import MesToolDeps

Publish = Callable[[str, str, JsonObject], None]
Delivered = Callable[[str, str, str], Awaitable[None]]
LoadHistory = Callable[[], Awaitable[Sequence[ModelMessage]]]


@dataclass(frozen=True, slots=True)
class NativeOutcome:
    status: str
    reason: str
    summary: JsonObject
    messages: tuple[ModelMessage, ...]
    new_messages: tuple[ModelMessage, ...]


class NativeExecution:
    """Single-use run, constructed using an already admitted identity and scope."""

    def __init__(self, agent: Agent[MesToolDeps, str], deps: MesToolDeps, user_prompt: str, *,
                 message_history: Sequence[ModelMessage] = (), load_history: LoadHistory | None = None,
                 model_settings: ModelSettings | None = None, usage_limits: UsageLimits | None = None,
                 timeout: float | None = 90, publish: Publish | None = None, delivered: Delivered | None = None,
                 capabilities: Sequence[AgentCapability[MesToolDeps]] = ()) -> None:
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
        summary: JsonObject = {'usage': {
            'requests': usage.requests, 'tool_calls': usage.tool_calls,
            'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
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
        return NativeOutcome(status, reason, summary, tuple(history), tuple(new))

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
            if self._load_history is not None:
                try:
                    history = tuple(await self._load_history())
                except asyncio.CancelledError:
                    self.interrupted_outcome = self._outcome('cancelled', 'execution_interrupted', history, (), usage)
                    raise
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
                    async with self._agent.iter(self._prompt, deps=self._deps, message_history=history,
                            run_id=self._deps.run_id, model_settings=self._model_settings, usage_limits=self._limits,
                            usage=usage, cancellation_token=self._token, timeout=self._timeout,
                            capabilities=[*self._capabilities, Hooks(event=self._event)]) as native:
                        self._run = native
                        async for _ in native:
                            pass
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
