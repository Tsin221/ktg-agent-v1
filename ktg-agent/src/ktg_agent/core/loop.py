"""Budgeted model/tool loop with safe batch scheduling and ordered events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from math import isfinite
from time import monotonic
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ktg_agent.context.window import ContextPolicy
from ktg_agent.core.events import (
    AgentEvent,
    AgentEventType,
    EventPublisher,
    EventSink,
    ToolProgressPublisher,
)
from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    message_to_dict,
    project_model_messages,
)
from ktg_agent.core.queues import InputQueue, QueuedInput
from ktg_agent.providers.base import (
    CancellationSignal,
    FinalResponse,
    ModelEvent,
    ModelConfig,
    Provider,
    ProviderRequest,
    RequestStarted,
    ResponseCompleted,
    TextDelta,
    Usage,
    UsageReported,
)
from ktg_agent.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolRunner,
    ToolEffect,
    ToolStatus,
)


class AgentLoopError(RuntimeError):
    """Raised when a provider violates the streaming response contract."""


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Hard boundaries applied before starting model and tool work."""

    max_model_turns: int = 32
    max_tool_calls: int = 128
    max_run_seconds: float = 300.0
    max_parallel_reads: int = 4
    max_total_tokens: int | None = None

    def __post_init__(self) -> None:
        integer_limits = {
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_parallel_reads": self.max_parallel_reads,
        }
        for name, value in integer_limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_total_tokens is not None and (
            not isinstance(self.max_total_tokens, int)
            or isinstance(self.max_total_tokens, bool)
            or self.max_total_tokens <= 0
        ):
            raise ValueError("max_total_tokens must be a positive integer or None")
        if (
            not isinstance(self.max_run_seconds, int | float)
            or isinstance(self.max_run_seconds, bool)
            or not isfinite(self.max_run_seconds)
            or self.max_run_seconds <= 0
        ):
            raise ValueError("max_run_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ModelRequestUsage:
    call_id: str
    turn: int
    model: str
    request_started: bool
    status: str
    usage_source: str
    usage: Usage | None


@dataclass(frozen=True, slots=True)
class RunUsage:
    known_input_tokens: int = 0
    known_output_tokens: int = 0
    known_cached_input_tokens: int = 0
    known_total_tokens: int = 0
    unknown_request_count: int = 0
    complete: bool = True


@dataclass(slots=True)
class _UsageAccumulator:
    requests: list[ModelRequestUsage] = field(default_factory=list)

    def record(self, request: ModelRequestUsage) -> None:
        self.requests.append(request)

    def snapshot(self) -> RunUsage:
        known = [request.usage for request in self.requests if request.usage_source == "provider_reported"]
        return RunUsage(
            known_input_tokens=sum(usage.input_tokens for usage in known if usage is not None),
            known_output_tokens=sum(usage.output_tokens for usage in known if usage is not None),
            known_cached_input_tokens=sum(usage.cached_input_tokens for usage in known if usage is not None),
            known_total_tokens=sum(usage.total_tokens for usage in known if usage is not None),
            unknown_request_count=sum(request.usage_source == "unknown" for request in self.requests),
            complete=all(request.usage_source != "unknown" for request in self.requests),
        )


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Final response, complete run-local history, and emitted event trace."""

    messages: tuple[Message, ...]
    new_messages: tuple[Message, ...] = field(default=(), kw_only=True)
    final_response: FinalResponse
    events: tuple[AgentEvent, ...]
    limit_reason: str | None = None
    error_code: str | None = None
    unconsumed_inputs: tuple[QueuedInput, ...] = ()
    model_requests: tuple[ModelRequestUsage, ...] = ()
    usage: RunUsage = field(default_factory=RunUsage)

    def __post_init__(self) -> None:
        if self.new_messages and self.messages[-len(self.new_messages):] != self.new_messages:
            raise ValueError("new_messages must be a suffix of messages")

    @property
    def stop_reason(self) -> StopReason:
        return self.final_response.stop_reason

    @property
    def final_message(self) -> AssistantMessage | None:
        return self.final_response.message


class AgentLoop:
    """Call a provider and execute complete tool calls until the model stops."""

    def __init__(
        self,
        *,
        provider: Provider,
        tool_registry: ToolRegistry,
        tool_runner: ToolRunner,
        model_config: ModelConfig,
        event_sink: EventSink | None = None,
        limits: RunLimits | None = None,
        context_policy: ContextPolicy | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider
        self._tool_registry = tool_registry
        self._tool_runner = tool_runner
        self._model_config = model_config
        self._event_sink = event_sink
        self._limits = limits or RunLimits()
        self._context_policy = context_policy
        self._clock = clock

    @property
    def event_sink(self) -> EventSink | None:
        return self._event_sink

    async def run(
        self,
        *,
        run_id: str,
        messages: Sequence[Message],
        identity: object | None = None,
        cancellation: CancellationSignal | None = None,
        ktg_client: object | None = None,
        input_queue: InputQueue | None = None,
        publisher: EventPublisher | None = None,
        close_inputs: Callable[[], None] | None = None,
        new_input_count: int = 1,
    ) -> AgentRunResult:
        from ktg_agent.context.window import ContextOverflowError

        history = list(messages)
        if type(new_input_count) is not int or not 0 <= new_input_count <= len(history):
            raise ValueError("new_input_count must fit within messages")
        # The current input is already present; only its explicit count marks the run boundary.
        baseline = len(history) - new_input_count
        if not history:
            raise ValueError("agent run requires at least one message")

        started_at = self._clock()
        if publisher is None:
            publisher = EventPublisher(run_id, self._event_sink)
        elif publisher.run_id != run_id:
            raise ValueError("event publisher run id does not match the agent run")
        await publisher.emit(
            AgentEventType.AGENT_START,
            {"message_count": len(history)},
        )
        agent_status = "failed"
        turn = 0
        tool_calls_started = 0
        usage_accumulator = _UsageAccumulator()
        try:
            while True:
                if cancellation is not None and cancellation.is_cancelled:
                    if close_inputs is not None:
                        close_inputs()
                    return await self._cancelled_result(
                        publisher=publisher,
                        history=history, baseline=baseline,
                        turns=turn,
                        tool_calls=tool_calls_started,
                        usage_accumulator=usage_accumulator,
                    )
                checked_at = self._clock()
                limit_reason = self._next_model_limit(
                    started_at=started_at,
                    completed_turns=turn,
                    tool_calls=tool_calls_started,
                    usage=usage_accumulator.snapshot(),
                    now=checked_at,
                )
                if limit_reason == "MODEL_USAGE_UNAVAILABLE":
                    if close_inputs is not None:
                        close_inputs()
                    return await self._usage_unavailable_result(
                        publisher=publisher, history=history, baseline=baseline, turns=turn,
                        tool_calls=tool_calls_started, usage_accumulator=usage_accumulator,
                    )
                if limit_reason is not None:
                    if close_inputs is not None:
                        close_inputs()
                    return await self._limited_result(
                        publisher=publisher,
                        history=history, baseline=baseline,
                        reason=limit_reason,
                        turns=turn,
                        tool_calls=tool_calls_started,
                        usage_accumulator=usage_accumulator,
                    )

                turn += 1
                response = await self._run_model_turn(
                    publisher=publisher,
                    turn=turn,
                    history=history,
                    cancellation=cancellation,
                    usage_accumulator=usage_accumulator,
                    started_at=started_at,
                )

                if response.message is not None:
                    history.append(response.message)

                # A completed model call can consume the budget before any of its tools start.
                budget_reason = (
                    "max_run_seconds" if self._clock() - started_at >= self._limits.max_run_seconds
                    else self._usage_limit(usage_accumulator.snapshot())
                )

                if response.stop_reason is StopReason.TOOL_CALL:
                    if response.message is None:  # guarded by FinalResponse
                        raise AgentLoopError("tool response is missing its message")
                    calls = tuple(
                        block
                        for block in response.message.content
                        if isinstance(block, ToolCall)
                    )
                    if cancellation is not None and cancellation.is_cancelled:
                        budget_reason = "cancelled"
                    if budget_reason is not None:
                        for call in calls:
                            await self._append_tool_message(
                                publisher=publisher, history=history, run_id=run_id,
                                turn=turn, call=call,
                                result=self._not_executed_tool_result(budget_reason),
                            )
                        await publisher.emit(AgentEventType.TURN_END, {
                            "turn": turn, "stop_reason": response.stop_reason.value,
                        })
                        if close_inputs is not None:
                            close_inputs()
                        if budget_reason == "cancelled":
                            return await self._cancelled_result(
                                publisher=publisher, history=history, baseline=baseline, turns=turn,
                                tool_calls=tool_calls_started, usage_accumulator=usage_accumulator,
                            )
                        if budget_reason == "MODEL_USAGE_UNAVAILABLE":
                            return await self._usage_unavailable_result(
                                publisher=publisher, history=history, baseline=baseline, turns=turn,
                                tool_calls=tool_calls_started, usage_accumulator=usage_accumulator,
                            )
                        return await self._limited_result(
                            publisher=publisher, history=history, baseline=baseline, reason=budget_reason,
                            turns=turn, tool_calls=tool_calls_started,
                            usage_accumulator=usage_accumulator,
                        )
                    time_exhausted = (
                        self._clock() - started_at
                        >= self._limits.max_run_seconds
                    )
                    remaining = self._limits.max_tool_calls - tool_calls_started
                    runnable_calls = () if time_exhausted else calls[:remaining]
                    skipped_calls = calls[len(runnable_calls) :]
                    results, batch_started, batch_time_exhausted = (
                        await self._run_tool_batch(
                            publisher=publisher,
                            run_id=run_id,
                            turn=turn,
                            calls=runnable_calls,
                            started_at=started_at,
                            identity=identity,
                            cancellation=cancellation,
                            ktg_client=ktg_client,
                        )
                    )
                    tool_calls_started += batch_started
                    for call, result in zip(runnable_calls, results, strict=True):
                        await self._append_tool_message(
                            publisher=publisher,
                            history=history,
                            run_id=run_id,
                            turn=turn,
                            call=call,
                            result=result,
                        )
                    for call in skipped_calls:
                        await self._append_tool_message(
                            publisher=publisher,
                            history=history,
                            run_id=run_id,
                            turn=turn,
                            call=call,
                            result=self._limited_tool_result(),
                        )

                    await publisher.emit(
                        AgentEventType.TURN_END,
                        {
                            "turn": turn,
                            "stop_reason": response.stop_reason.value,
                        },
                    )
                    if skipped_calls or batch_time_exhausted:
                        if close_inputs is not None:
                            close_inputs()
                        return await self._limited_result(
                            publisher=publisher,
                            history=history, baseline=baseline,
                            reason=(
                                "max_run_seconds"
                                if time_exhausted or batch_time_exhausted
                                else "max_tool_calls"
                            ),
                            turns=turn,
                            tool_calls=tool_calls_started,
                            usage_accumulator=usage_accumulator,
                        )
                    if cancellation is not None and cancellation.is_cancelled:
                        if close_inputs is not None:
                            close_inputs()
                        return await self._cancelled_result(
                            publisher=publisher,
                            history=history, baseline=baseline,
                            turns=turn,
                            tool_calls=tool_calls_started,
                            usage_accumulator=usage_accumulator,
                        )
                    if input_queue is not None and self._next_model_limit(
                        started_at=started_at, completed_turns=turn,
                        tool_calls=tool_calls_started,
                        usage=usage_accumulator.snapshot(),
                    ) is None:
                        steering = input_queue.take_steering()
                        if steering:
                            history.extend(item.message for item in steering)
                            input_queue.mark_consumed(steering)
                    continue

                await publisher.emit(
                    AgentEventType.TURN_END,
                    {
                        "turn": turn,
                        "stop_reason": response.stop_reason.value,
                    },
                )
                if cancellation is not None and cancellation.is_cancelled:
                    if close_inputs is not None:
                        close_inputs()
                    return await self._cancelled_result(
                        publisher=publisher,
                        history=history, baseline=baseline,
                        turns=turn,
                        tool_calls=tool_calls_started,
                        usage_accumulator=usage_accumulator,
                    )
                if response.stop_reason is StopReason.CANCELLED:
                    if close_inputs is not None:
                        close_inputs()
                    return await self._cancelled_result(
                        publisher=publisher, history=history, baseline=baseline, turns=turn,
                        tool_calls=tool_calls_started, usage_accumulator=usage_accumulator,
                    )
                if response.stop_reason is StopReason.LIMIT:
                    if close_inputs is not None:
                        close_inputs()
                    return await self._limited_result(
                        publisher=publisher, history=history, baseline=baseline, reason="max_run_seconds",
                        turns=turn, tool_calls=tool_calls_started,
                        usage_accumulator=usage_accumulator,
                    )
                if response.stop_reason is StopReason.ERROR:
                    if close_inputs is not None:
                        close_inputs()
                    await self._emit_agent_end_once(publisher, {
                        "status": "failed", "stop_reason": StopReason.ERROR.value,
                        "turns": turn,
                    })
                    return AgentRunResult(
                        messages=tuple(history), final_response=response,
                        new_messages=tuple(history[baseline:]),
                        events=publisher.events,
                        model_requests=tuple(usage_accumulator.requests),
                        usage=usage_accumulator.snapshot(),
                    )
                elif budget_reason is not None:
                    if close_inputs is not None:
                        close_inputs()
                    if budget_reason == "MODEL_USAGE_UNAVAILABLE":
                        return await self._usage_unavailable_result(
                            publisher=publisher, history=history, baseline=baseline, turns=turn,
                            tool_calls=tool_calls_started, usage_accumulator=usage_accumulator,
                            message=response.message,
                        )
                    return await self._limited_result(
                        publisher=publisher, history=history, baseline=baseline, reason=budget_reason,
                        turns=turn, tool_calls=tool_calls_started,
                        usage_accumulator=usage_accumulator, message=response.message,
                    )
                if input_queue is not None and not input_queue.is_empty and self._next_model_limit(
                    started_at=started_at, completed_turns=turn,
                    tool_calls=tool_calls_started,
                    usage=usage_accumulator.snapshot(),
                ) is not None:
                    continue
                if input_queue is not None:
                    steering = input_queue.take_steering()
                    if steering:
                        history.extend(item.message for item in steering)
                        input_queue.mark_consumed(steering)
                        continue
                    if response.stop_reason in {
                        StopReason.COMPLETED,
                        StopReason.LENGTH,
                    }:
                        follow_ups = input_queue.take_follow_ups()
                        if follow_ups:
                            history.extend(item.message for item in follow_ups)
                            input_queue.mark_consumed(follow_ups)
                            continue
                if close_inputs is not None:
                    close_inputs()
                agent_status = self._agent_status(response.stop_reason)
                await self._emit_agent_end_once(
                    publisher,
                    {
                        "status": agent_status,
                        "stop_reason": response.stop_reason.value,
                        "turns": turn,
                    },
                )
                return AgentRunResult(
                    messages=tuple(history),
                    new_messages=tuple(history[baseline:]),
                    final_response=response,
                    events=publisher.events,
                    model_requests=tuple(usage_accumulator.requests),
                    usage=usage_accumulator.snapshot(),
                )
        except ContextOverflowError:
            if close_inputs is not None:
                close_inputs()
            await self._emit_agent_end_once(publisher, {
                "status": "failed", "stop_reason": "error",
                "error_code": "CONTEXT_OVERFLOW", "turns": turn,
            })
            return AgentRunResult(
                messages=tuple(history), new_messages=tuple(history[baseline:]),
                final_response=FinalResponse(None, StopReason.ERROR),
                events=publisher.events, error_code="CONTEXT_OVERFLOW",
                model_requests=tuple(usage_accumulator.requests),
                usage=usage_accumulator.snapshot(),
            )
        except BaseException as exc:
            if close_inputs is not None:
                close_inputs()
            await self._emit_agent_end_once(
                publisher,
                {
                    "status": agent_status,
                    "error_type": type(exc).__name__,
                    "turns": turn,
                },
            )
            raise

    def _next_model_limit(
        self, *, started_at: float, completed_turns: int, tool_calls: int,
        usage: RunUsage, now: float | None = None,
    ) -> str | None:
        if (self._clock() if now is None else now) - started_at >= self._limits.max_run_seconds:
            return "max_run_seconds"
        if completed_turns >= self._limits.max_model_turns:
            return "max_model_turns"
        if tool_calls >= self._limits.max_tool_calls:
            return "max_tool_calls"
        return self._usage_limit(usage)

    def _usage_limit(self, usage: RunUsage) -> str | None:
        threshold = self._limits.max_total_tokens
        if threshold is None:
            return None
        if not usage.complete:
            return "MODEL_USAGE_UNAVAILABLE"
        if usage.known_total_tokens >= threshold:
            return "max_total_tokens"
        return None

    async def _run_tool_batch(
        self,
        *,
        publisher: EventPublisher,
        run_id: str,
        turn: int,
        calls: tuple[ToolCall, ...],
        started_at: float,
        identity: object | None,
        cancellation: CancellationSignal | None,
        ktg_client: object | None,
    ) -> tuple[tuple[ToolResult, ...], int, bool]:
        started_count = 0
        time_limit_reached = False

        def run_time_exhausted() -> bool:
            return self._clock() - started_at >= self._limits.max_run_seconds

        async def execute(call: ToolCall) -> ToolResult:
            nonlocal started_count
            try:
                tool = self._tool_registry.get(call.name)
            except KeyError:
                tool = None
            started_count += 1
            await publisher.emit(
                AgentEventType.TOOL_EXECUTION_START,
                {
                    "turn": turn,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "effect": tool.effect.value if tool is not None else "unavailable",
                },
            )
            progress = ToolProgressPublisher(
                publisher,
                tool_call_id=call.id,
                tool_name=call.name,
            )
            try:
                result = (self._cancelled_tool_result() if cancellation is not None
                          and cancellation.is_cancelled else await self._tool_runner.run(
                    call,
                    ToolContext(
                        run_id=run_id,
                        tool_call_id=call.id,
                        identity=identity,
                        cancellation=cancellation,
                        progress_publisher=progress,
                        ktg_client=ktg_client,
                    ),
                ))
            except BaseException as exc:
                await publisher.emit(
                    AgentEventType.TOOL_EXECUTION_END,
                    {
                        "turn": turn,
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "effect": tool.effect.value if tool is not None else "unavailable",
                        "status": "unknown" if tool is not None and tool.effect is ToolEffect.WRITE else "failed",
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            end_payload: JsonObject = {
                "turn": turn,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "effect": tool.effect.value if tool is not None else "unavailable",
                "status": result.status.value,
            }
            if result.error_code is not None:
                end_payload["error_code"] = result.error_code
            if result.operation_id is not None:
                end_payload["operation_id"] = result.operation_id
            if result.verification is not None:
                end_payload["verification"] = result.verification
            if result.details is not None:
                end_payload["result"] = result.details
            await publisher.emit(AgentEventType.TOOL_EXECUTION_END, end_payload)
            return result

        if not calls:
            return (), 0, False
        if not self._tool_runner.can_run_batch_in_parallel(calls):
            sequential_results: list[ToolResult] = []
            for call in calls:
                if cancellation is not None and cancellation.is_cancelled:
                    sequential_results.append(self._cancelled_tool_result())
                elif run_time_exhausted():
                    time_limit_reached = True
                    sequential_results.append(self._limited_tool_result())
                else:
                    sequential_results.append(await execute(call))
            return tuple(sequential_results), started_count, time_limit_reached

        semaphore = asyncio.Semaphore(self._limits.max_parallel_reads)

        async def limited_execute(call: ToolCall) -> ToolResult:
            nonlocal time_limit_reached
            async with semaphore:
                if cancellation is not None and cancellation.is_cancelled:
                    return self._cancelled_tool_result()
                if run_time_exhausted():
                    time_limit_reached = True
                    return self._limited_tool_result()
                return await execute(call)

        parallel_results = tuple(
            await asyncio.gather(*(limited_execute(call) for call in calls))
        )
        return parallel_results, started_count, time_limit_reached

    @staticmethod
    def _cancelled_tool_result() -> ToolResult:
        return ToolResult(
            content="Tool call was not started because the run was cancelled.",
            status=ToolStatus.FAILED,
            error_code="RUN_CANCELLED",
        )

    @staticmethod
    def _limited_tool_result() -> ToolResult:
        return ToolResult(
            content="Tool call was not executed because the run budget was exhausted.",
            status=ToolStatus.FAILED,
            error_code="RUN_LIMIT_REACHED",
        )

    @staticmethod
    def _not_executed_tool_result(reason: str) -> ToolResult:
        return ToolResult(
            content=f"Tool call was not executed: {reason}.",
            details={
                "execution_status": "not_executed",
                "stop_reason": "error" if reason == "MODEL_USAGE_UNAVAILABLE" else
                               "cancelled" if reason == "cancelled" else "limit",
                **({"error_code": reason} if reason == "MODEL_USAGE_UNAVAILABLE" else
                   {"limit_reason": reason} if reason != "cancelled" else {}),
            },
            status=ToolStatus.FAILED,
            error_code=reason,
        )

    @staticmethod
    async def _append_tool_message(
        *,
        publisher: EventPublisher,
        history: list[Message],
        run_id: str,
        turn: int,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        tool_message = result.to_message(call.id)
        history.append(tool_message)
        tool_message_id = f"{run_id}:tool-result:{call.id}"
        await publisher.emit(
            AgentEventType.MESSAGE_START,
            {
                "turn": turn,
                "message_id": tool_message_id,
                "role": "tool",
                "tool_call_id": call.id,
            },
        )
        await publisher.emit(
            AgentEventType.MESSAGE_END,
            {
                "turn": turn,
                "message_id": tool_message_id,
                "message": message_to_dict(tool_message),
            },
        )

    @staticmethod
    async def _cancelled_result(
        *,
        publisher: EventPublisher,
        history: list[Message],
        baseline: int,
        turns: int,
        tool_calls: int,
        usage_accumulator: _UsageAccumulator,
    ) -> AgentRunResult:
        response = FinalResponse(
            message=None,
            stop_reason=StopReason.CANCELLED,
            usage=None,
        )
        await AgentLoop._emit_agent_end_once(
            publisher,
            {
                "status": "cancelled",
                "stop_reason": StopReason.CANCELLED.value,
                "turns": turns,
                "tool_calls": tool_calls,
            },
        )
        return AgentRunResult(
            messages=tuple(history),
            new_messages=tuple(history[baseline:]),
            final_response=response,
            events=publisher.events,
            model_requests=tuple(usage_accumulator.requests),
            usage=usage_accumulator.snapshot(),
        )

    @staticmethod
    async def _limited_result(
        *,
        publisher: EventPublisher,
        history: list[Message],
        baseline: int,
        reason: str,
        turns: int,
        tool_calls: int,
        usage_accumulator: _UsageAccumulator,
        message: AssistantMessage | None = None,
    ) -> AgentRunResult:
        response = FinalResponse(
            message=message,
            stop_reason=StopReason.LIMIT,
            usage=None,
        )
        await AgentLoop._emit_agent_end_once(
            publisher,
            {
                "status": "limited",
                "stop_reason": StopReason.LIMIT.value,
                "limit_reason": reason,
                "turns": turns,
                "tool_calls": tool_calls,
            },
        )
        return AgentRunResult(
            messages=tuple(history),
            new_messages=tuple(history[baseline:]),
            final_response=response,
            events=publisher.events,
            limit_reason=reason,
            model_requests=tuple(usage_accumulator.requests),
            usage=usage_accumulator.snapshot(),
        )

    @staticmethod
    async def _usage_unavailable_result(
        *, publisher: EventPublisher, history: list[Message], baseline: int, turns: int,
        tool_calls: int, usage_accumulator: _UsageAccumulator,
        message: AssistantMessage | None = None,
    ) -> AgentRunResult:
        response = FinalResponse(message, StopReason.ERROR, None)
        await AgentLoop._emit_agent_end_once(publisher, {
            "status": "failed", "stop_reason": StopReason.ERROR.value,
            "error_code": "MODEL_USAGE_UNAVAILABLE", "turns": turns,
            "tool_calls": tool_calls,
        })
        return AgentRunResult(
            messages=tuple(history), final_response=response,
            new_messages=tuple(history[baseline:]),
            events=publisher.events, error_code="MODEL_USAGE_UNAVAILABLE",
            model_requests=tuple(usage_accumulator.requests),
            usage=usage_accumulator.snapshot(),
        )

    async def _run_model_turn(
        self,
        *,
        publisher: EventPublisher,
        turn: int,
        history: list[Message],
        cancellation: CancellationSignal | None,
        usage_accumulator: _UsageAccumulator,
        started_at: float,
    ) -> FinalResponse:
        from ktg_agent.context.tokens import estimate_tools_tokens
        from ktg_agent.context.window import ContextOverflowError, fit_window

        projected = project_model_messages(history)
        schemas = self._tool_registry.model_schemas()
        if self._context_policy is not None:
            # Projection removes invisible messages and clears result details. Preserve
            # the original spill markers in the projected sequence's coordinates.
            visible = (
                message for message in history
                if any(isinstance(block, ToolCall)
                       or isinstance(block, TextBlock) and block.model_visible
                       for block in message.content)
            )
            spill_flags = tuple(
                isinstance(message, ToolResultMessage)
                and isinstance(message.details, dict) and "spill" in message.details
                for message in visible
            )
            try:
                plan = fit_window(
                    projected, self._context_policy,
                    tool_schema_tokens=estimate_tools_tokens(schemas),
                    spill_flags=spill_flags,
                )
            except ContextOverflowError as exc:
                await publisher.emit(AgentEventType.CONTEXT_OVERFLOW, {
                    "estimated_tokens": exc.estimated_tokens,
                    "available_tokens": exc.available_tokens,
                    "largest_group_tokens": exc.largest_group_tokens,
                })
                raise
            await publisher.emit(AgentEventType.CONTEXT_ASSEMBLED, {
                "estimated_tokens": plan.estimated_after,
                "threshold_tokens": plan.threshold_tokens,
                "summary_id": None,
                "retained_message_count": plan.retained_message_count,
                "spill_count": plan.spill_count,
                "trimmed": plan.trimmed,
            })
            request_messages = plan.messages
        else:
            request_messages = projected
        await publisher.emit(AgentEventType.TURN_START, {"turn": turn})
        message_id = f"{publisher.events[0].run_id}:message:{turn}"
        await publisher.emit(
            AgentEventType.MESSAGE_START,
            {"turn": turn, "message_id": message_id, "role": "assistant"},
        )

        response: FinalResponse | None = None
        request_started = False
        reported_usage: Usage | None = None
        call_id = f"{publisher.run_id}:model:{turn}"

        async def settle_request(status: str) -> None:
            usage_source = (
                "not_started" if not request_started else
                "provider_reported" if reported_usage is not None else "unknown"
            )
            usage = reported_usage if request_started else Usage(0, 0)
            usage_accumulator.record(ModelRequestUsage(
                call_id=call_id,
                turn=turn,
                model=self._model_config.model,
                request_started=request_started,
                status=status,
                usage_source=usage_source,
                usage=usage,
            ))
            await publisher.emit(AgentEventType.MODEL_REQUEST_SETTLED, {
                "call_id": call_id,
                "turn": turn,
                "model": self._model_config.model,
                "request_started": request_started,
                "status": status,
                "usage_source": usage_source,
                "input_tokens": usage.input_tokens if usage is not None else None,
                "output_tokens": usage.output_tokens if usage is not None else None,
                "cached_input_tokens": usage.cached_input_tokens if usage is not None else None,
                "total_tokens": usage.total_tokens if usage is not None else None,
            })

        try:
            remaining_run_seconds = self._limits.max_run_seconds - (self._clock() - started_at)
            if remaining_run_seconds <= 0:
                response = FinalResponse(None, StopReason.LIMIT)
                await settle_request(StopReason.LIMIT.value)
                await publisher.emit(AgentEventType.MESSAGE_END, {
                    "turn": turn, "message_id": message_id,
                    "stop_reason": StopReason.LIMIT.value,
                })
                return response
            request = ProviderRequest(
                messages=request_messages,
                tools=schemas,
                config=self._model_config,
                cancellation=cancellation,
                call_id=call_id,
                remaining_run_seconds=remaining_run_seconds,
            )
            async for event in self._model_events(request):
                if response is not None:
                    raise AgentLoopError("provider emitted an event after completed response")
                if isinstance(event, RequestStarted):
                    if request_started:
                        raise AgentLoopError("provider started a model request twice")
                    request_started = True
                    await publisher.emit(AgentEventType.MODEL_REQUEST_STARTED, {
                        "call_id": request.call_id,
                        "turn": turn,
                        "model": request.config.model,
                    })
                elif isinstance(event, UsageReported):
                    if not request_started:
                        raise AgentLoopError("provider reported usage before request start")
                    reported_usage = event.usage
                    await publisher.emit(AgentEventType.MODEL_USAGE_REPORTED, {
                        "call_id": request.call_id,
                        "turn": turn,
                        "input_tokens": event.usage.input_tokens,
                        "output_tokens": event.usage.output_tokens,
                        "cached_input_tokens": event.usage.cached_input_tokens,
                    })
                elif isinstance(event, TextDelta):
                    await publisher.emit(
                        AgentEventType.MESSAGE_UPDATE,
                        {
                            "turn": turn,
                            "message_id": message_id,
                            "delta_type": "text",
                            "text": event.text,
                        },
                    )
                elif isinstance(event, ResponseCompleted):
                    if not request_started and event.response.stop_reason is not StopReason.CANCELLED:
                        raise AgentLoopError("provider completed a request without request start")
                    response = event.response
                    if not request_started and response.usage not in (None, Usage(0, 0)):
                        raise AgentLoopError("unstarted request cannot report nonzero usage")
                    if request_started and response.usage is not None:
                        if reported_usage is not None and reported_usage != response.usage:
                            raise AgentLoopError("final usage conflicts with reported usage")
                        if reported_usage is None:
                            reported_usage = response.usage
                            await publisher.emit(AgentEventType.MODEL_USAGE_REPORTED, {
                                "call_id": request.call_id,
                                "turn": turn,
                                "input_tokens": reported_usage.input_tokens,
                                "output_tokens": reported_usage.output_tokens,
                                "cached_input_tokens": reported_usage.cached_input_tokens,
                            })
        except BaseException:
            await settle_request("failed")
            await publisher.emit(
                AgentEventType.MESSAGE_END,
                {"turn": turn, "message_id": message_id, "status": "failed"},
            )
            await publisher.emit(
                AgentEventType.TURN_END,
                {"turn": turn, "status": "failed"},
            )
            raise

        if response is None:
            await settle_request("failed")
            await publisher.emit(
                AgentEventType.MESSAGE_END,
                {"turn": turn, "message_id": message_id, "status": "failed"},
            )
            await publisher.emit(
                AgentEventType.TURN_END,
                {"turn": turn, "status": "failed"},
            )
            raise AgentLoopError("provider stream ended without a completed response")

        await settle_request(response.stop_reason.value)

        end_payload: JsonObject = {
            "turn": turn,
            "message_id": message_id,
            "stop_reason": response.stop_reason.value,
        }
        if response.message is not None:
            end_payload["message"] = message_to_dict(response.message)
        await publisher.emit(AgentEventType.MESSAGE_END, end_payload)
        return response

    async def _model_events(self, request: ProviderRequest) -> AsyncGenerator[ModelEvent, None]:
        stream = cast(AsyncGenerator[ModelEvent, None], self._provider.stream(request))
        try:
            while True:
                next_event = asyncio.create_task(anext(stream, None))
                try:
                    while True:
                        done, _ = await asyncio.wait({next_event}, timeout=0.05)
                        if request.cancellation is not None and request.cancellation.is_cancelled:
                            next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
                            yield ResponseCompleted(FinalResponse(None, StopReason.CANCELLED))
                            return
                        if done:
                            break
                    event = next_event.result()
                except BaseException:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    raise
                if event is None:
                    return
                yield event
        finally:
            await stream.aclose()

    @staticmethod
    def _agent_status(stop_reason: StopReason) -> str:
        if stop_reason is StopReason.CANCELLED:
            return "cancelled"
        if stop_reason is StopReason.ERROR:
            return "failed"
        return "completed"

    @staticmethod
    async def _emit_agent_end_once(
        publisher: EventPublisher, payload: JsonObject
    ) -> None:
        if any(
            event.type is AgentEventType.AGENT_END for event in publisher.events
        ):
            return
        await publisher.emit(AgentEventType.AGENT_END, payload)
