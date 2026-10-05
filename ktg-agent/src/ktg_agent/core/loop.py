"""Budgeted model/tool loop with safe batch scheduling and ordered events."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import monotonic

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
    ToolCall,
    message_to_dict,
    project_model_messages,
)
from ktg_agent.core.queues import InputQueue
from ktg_agent.providers.base import (
    CancellationSignal,
    FinalResponse,
    ModelConfig,
    Provider,
    ProviderRequest,
    ResponseCompleted,
    TextDelta,
    Usage,
)
from ktg_agent.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolRunner,
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

    def __post_init__(self) -> None:
        integer_limits = {
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_parallel_reads": self.max_parallel_reads,
        }
        for name, value in integer_limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            not isinstance(self.max_run_seconds, int | float)
            or isinstance(self.max_run_seconds, bool)
            or self.max_run_seconds <= 0
        ):
            raise ValueError("max_run_seconds must be positive")


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Final response, complete run-local history, and emitted event trace."""

    messages: tuple[Message, ...]
    final_response: FinalResponse
    events: tuple[AgentEvent, ...]
    limit_reason: str | None = None

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
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider
        self._tool_registry = tool_registry
        self._tool_runner = tool_runner
        self._model_config = model_config
        self._event_sink = event_sink
        self._limits = limits or RunLimits()
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
    ) -> AgentRunResult:
        history = list(messages)
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
        try:
            while True:
                if cancellation is not None and cancellation.is_cancelled:
                    return await self._cancelled_result(
                        publisher=publisher,
                        history=history,
                        turns=turn,
                        tool_calls=tool_calls_started,
                    )
                limit_reason = self._next_model_limit(
                    started_at=started_at,
                    completed_turns=turn,
                    tool_calls=tool_calls_started,
                )
                if limit_reason is not None:
                    return await self._limited_result(
                        publisher=publisher,
                        history=history,
                        reason=limit_reason,
                        turns=turn,
                        tool_calls=tool_calls_started,
                    )

                turn += 1
                response = await self._run_model_turn(
                    publisher=publisher,
                    turn=turn,
                    history=history,
                    cancellation=cancellation,
                )

                if response.message is not None:
                    history.append(response.message)

                if response.stop_reason is StopReason.TOOL_CALL:
                    if response.message is None:  # guarded by FinalResponse
                        raise AgentLoopError("tool response is missing its message")
                    calls = tuple(
                        block
                        for block in response.message.content
                        if isinstance(block, ToolCall)
                    )
                    time_exhausted = (
                        self._clock() - started_at
                        >= self._limits.max_run_seconds
                    )
                    remaining = self._limits.max_tool_calls - tool_calls_started
                    runnable_calls = () if time_exhausted else calls[:remaining]
                    skipped_calls = calls[len(runnable_calls) :]
                    results, batch_started = await self._run_tool_batch(
                        publisher=publisher,
                        run_id=run_id,
                        turn=turn,
                        calls=runnable_calls,
                        identity=identity,
                        cancellation=cancellation,
                        ktg_client=ktg_client,
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
                            result=ToolResult(
                                content="Tool call was not executed because the run budget was exhausted.",
                                status=ToolStatus.FAILED,
                                error_code="RUN_LIMIT_REACHED",
                            ),
                        )

                    await publisher.emit(
                        AgentEventType.TURN_END,
                        {
                            "turn": turn,
                            "stop_reason": response.stop_reason.value,
                        },
                    )
                    if skipped_calls:
                        return await self._limited_result(
                            publisher=publisher,
                            history=history,
                            reason=(
                                "max_run_seconds"
                                if time_exhausted
                                else "max_tool_calls"
                            ),
                            turns=turn,
                            tool_calls=tool_calls_started,
                        )
                    if cancellation is not None and cancellation.is_cancelled:
                        return await self._cancelled_result(
                            publisher=publisher,
                            history=history,
                            turns=turn,
                            tool_calls=tool_calls_started,
                        )
                    if input_queue is not None:
                        steering = input_queue.drain_steering()
                        if steering:
                            history.extend(steering)
                    continue

                await publisher.emit(
                    AgentEventType.TURN_END,
                    {
                        "turn": turn,
                        "stop_reason": response.stop_reason.value,
                    },
                )
                if cancellation is not None and cancellation.is_cancelled:
                    return await self._cancelled_result(
                        publisher=publisher,
                        history=history,
                        turns=turn,
                        tool_calls=tool_calls_started,
                    )
                if input_queue is not None:
                    steering = input_queue.drain_steering()
                    if steering:
                        history.extend(steering)
                        continue
                    if response.stop_reason in {
                        StopReason.COMPLETED,
                        StopReason.LENGTH,
                    }:
                        follow_ups = input_queue.drain_follow_ups()
                        if follow_ups:
                            history.extend(follow_ups)
                            continue
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
                    final_response=response,
                    events=publisher.events,
                )
        except BaseException as exc:
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
        self, *, started_at: float, completed_turns: int, tool_calls: int
    ) -> str | None:
        if self._clock() - started_at >= self._limits.max_run_seconds:
            return "max_run_seconds"
        if completed_turns >= self._limits.max_model_turns:
            return "max_model_turns"
        if tool_calls >= self._limits.max_tool_calls:
            return "max_tool_calls"
        return None

    async def _run_tool_batch(
        self,
        *,
        publisher: EventPublisher,
        run_id: str,
        turn: int,
        calls: tuple[ToolCall, ...],
        identity: object | None,
        cancellation: CancellationSignal | None,
        ktg_client: object | None,
    ) -> tuple[tuple[ToolResult, ...], int]:
        started_count = 0

        async def execute(call: ToolCall) -> ToolResult:
            nonlocal started_count
            started_count += 1
            await publisher.emit(
                AgentEventType.TOOL_EXECUTION_START,
                {
                    "turn": turn,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                },
            )
            progress = ToolProgressPublisher(
                publisher,
                tool_call_id=call.id,
                tool_name=call.name,
            )
            try:
                result = await self._tool_runner.run(
                    call,
                    ToolContext(
                        run_id=run_id,
                        tool_call_id=call.id,
                        identity=identity,
                        cancellation=cancellation,
                        progress_publisher=progress,
                        ktg_client=ktg_client,
                    ),
                )
            except BaseException as exc:
                await publisher.emit(
                    AgentEventType.TOOL_EXECUTION_END,
                    {
                        "turn": turn,
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            end_payload: JsonObject = {
                "turn": turn,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "status": result.status.value,
            }
            if result.error_code is not None:
                end_payload["error_code"] = result.error_code
            await publisher.emit(AgentEventType.TOOL_EXECUTION_END, end_payload)
            return result

        if not calls:
            return (), 0
        if not self._tool_runner.can_run_batch_in_parallel(calls):
            sequential_results: list[ToolResult] = []
            for call in calls:
                if cancellation is not None and cancellation.is_cancelled:
                    sequential_results.append(self._cancelled_tool_result())
                else:
                    sequential_results.append(await execute(call))
            return tuple(sequential_results), started_count

        semaphore = asyncio.Semaphore(self._limits.max_parallel_reads)

        async def limited_execute(call: ToolCall) -> ToolResult:
            async with semaphore:
                if cancellation is not None and cancellation.is_cancelled:
                    return self._cancelled_tool_result()
                return await execute(call)

        parallel_results = tuple(
            await asyncio.gather(*(limited_execute(call) for call in calls))
        )
        return parallel_results, started_count

    @staticmethod
    def _cancelled_tool_result() -> ToolResult:
        return ToolResult(
            content="Tool call was not started because the run was cancelled.",
            status=ToolStatus.FAILED,
            error_code="RUN_CANCELLED",
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
        turns: int,
        tool_calls: int,
    ) -> AgentRunResult:
        response = FinalResponse(
            message=None,
            stop_reason=StopReason.CANCELLED,
            usage=Usage(input_tokens=0, output_tokens=0),
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
            final_response=response,
            events=publisher.events,
        )

    @staticmethod
    async def _limited_result(
        *,
        publisher: EventPublisher,
        history: list[Message],
        reason: str,
        turns: int,
        tool_calls: int,
    ) -> AgentRunResult:
        response = FinalResponse(
            message=None,
            stop_reason=StopReason.LIMIT,
            usage=Usage(input_tokens=0, output_tokens=0),
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
            final_response=response,
            events=publisher.events,
            limit_reason=reason,
        )

    async def _run_model_turn(
        self,
        *,
        publisher: EventPublisher,
        turn: int,
        history: list[Message],
        cancellation: CancellationSignal | None,
    ) -> FinalResponse:
        await publisher.emit(AgentEventType.TURN_START, {"turn": turn})
        message_id = f"{publisher.events[0].run_id}:message:{turn}"
        await publisher.emit(
            AgentEventType.MESSAGE_START,
            {"turn": turn, "message_id": message_id, "role": "assistant"},
        )

        response: FinalResponse | None = None
        try:
            request = ProviderRequest(
                messages=project_model_messages(history),
                tools=self._tool_registry.model_schemas(),
                config=self._model_config,
                cancellation=cancellation,
            )
            async for event in self._provider.stream(request):
                if isinstance(event, TextDelta):
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
                    if response is not None:
                        raise AgentLoopError(
                            "provider emitted more than one completed response"
                        )
                    response = event.response
        except BaseException:
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
            await publisher.emit(
                AgentEventType.MESSAGE_END,
                {"turn": turn, "message_id": message_id, "status": "failed"},
            )
            await publisher.emit(
                AgentEventType.TURN_END,
                {"turn": turn, "status": "failed"},
            )
            raise AgentLoopError("provider stream ended without a completed response")

        end_payload: JsonObject = {
            "turn": turn,
            "message_id": message_id,
            "stop_reason": response.stop_reason.value,
        }
        if response.message is not None:
            end_payload["message"] = message_to_dict(response.message)
        await publisher.emit(AgentEventType.MESSAGE_END, end_payload)
        return response

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
