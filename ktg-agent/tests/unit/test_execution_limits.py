from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from ktg_agent.core.events import AgentEventType
from ktg_agent.core.agent import CancellationToken
from ktg_agent.core.loop import AgentLoop, AgentRunResult, RunLimits
from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ktg_agent.providers.base import FinalResponse, ModelConfig, RequestStarted, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.tools import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
    ToolRunner,
    ToolStatus,
)


SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


def response(*content: TextBlock | ToolCall, reason: StopReason) -> FinalResponse:
    return FinalResponse(
        message=AssistantMessage(content=content),
        stop_reason=reason,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def definition(
    name: str,
    execute: Any,
    *,
    effect: ToolEffect = ToolEffect.READ,
    mode: ExecutionMode = ExecutionMode.PARALLEL,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1.0",
        description=name,
        label=name,
        input_schema=SCHEMA,
        output_schema=None,
        required_permissions=(),
        effect=effect,
        execution_mode=mode,
        timeout_seconds=2,
        execute=execute,
    )


def make_loop(
    provider: FakeProvider,
    definitions: tuple[ToolDefinition, ...] = (),
    *,
    limits: RunLimits | None = None,
    clock: Any = None,
) -> AgentLoop:
    registry = ToolRegistry(definitions)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return AgentLoop(
        provider=provider,
        tool_registry=registry,
        tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake", max_output_tokens=64),
        limits=limits or RunLimits(),
        **kwargs,
    )


async def execute(loop: AgentLoop) -> AgentRunResult:
    return await loop.run(
        run_id="run-1",
        messages=(UserMessage((TextBlock("start"),)),),
    )


def test_independent_reads_overlap_respect_limit_and_keep_context_order() -> None:
    active = 0
    peak = 0
    first_pair_started = asyncio.Event()
    release = asyncio.Event()

    async def gated(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if peak == 2:
            first_pair_started.set()
        await release.wait()
        active -= 1
        return ToolResult(content=f"result:{arguments['value']}")

    calls = tuple(
        ToolCall(f"call-{value}", "read", {"value": value})
        for value in (1, 2, 3)
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(*calls, reason=StopReason.TOOL_CALL)),)
            ),
            ScriptedTurn(
                actions=(
                    ResponseCompleted(response(TextBlock("done"), reason=StopReason.COMPLETED)),
                )
            ),
        ]
    )
    loop = make_loop(
        provider,
        (definition("read", gated),),
        limits=RunLimits(max_parallel_reads=2),
    )

    async def scenario() -> AgentRunResult:
        task = asyncio.create_task(execute(loop))
        await first_pair_started.wait()
        assert active == 2
        release.set()
        return await task

    result = asyncio.run(scenario())

    assert peak == 2
    results = provider.requests[1].messages[-3:]
    assert [message.tool_call_id for message in results if isinstance(message, ToolResultMessage)] == [
        "call-1",
        "call-2",
        "call-3",
    ]


def test_parallel_end_events_follow_completion_order() -> None:
    release_first = asyncio.Event()
    second_finished = asyncio.Event()

    async def controlled(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        if arguments["value"] == 1:
            await release_first.wait()
        else:
            second_finished.set()
        return ToolResult(content=f"result:{arguments['value']}")

    calls = (
        ToolCall("call-1", "read", {"value": 1}),
        ToolCall("call-2", "read", {"value": 2}),
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(*calls, reason=StopReason.TOOL_CALL)),)
            ),
            ScriptedTurn(
                actions=(
                    ResponseCompleted(response(TextBlock("done"), reason=StopReason.COMPLETED)),
                )
            ),
        ]
    )
    loop = make_loop(provider, (definition("read", controlled),))

    async def scenario() -> AgentRunResult:
        task = asyncio.create_task(execute(loop))
        await second_finished.wait()
        release_first.set()
        return await task

    result = asyncio.run(scenario())
    ends = [
        event.payload["tool_call_id"]
        for event in result.events
        if event.type is AgentEventType.TOOL_EXECUTION_END
    ]

    assert ends == ["call-2", "call-1"]
    results = provider.requests[1].messages[-2:]
    assert [message.tool_call_id for message in results if isinstance(message, ToolResultMessage)] == [
        "call-1",
        "call-2",
    ]


def test_any_write_makes_the_whole_batch_sequential() -> None:
    active = 0
    peak = 0
    order: list[str] = []

    async def record(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(context.tool_call_id)
        await asyncio.sleep(0)
        active -= 1
        return ToolResult(content="ok")

    calls = (
        ToolCall("read-1", "read", {"value": 1}),
        ToolCall("write-1", "write", {"value": 2}),
        ToolCall("read-2", "read", {"value": 3}),
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(*calls, reason=StopReason.TOOL_CALL)),)
            ),
            ScriptedTurn(
                actions=(
                    ResponseCompleted(response(TextBlock("done"), reason=StopReason.COMPLETED)),
                )
            ),
        ]
    )
    loop = make_loop(
        provider,
        (
            definition("read", record),
            definition("write", record, effect=ToolEffect.WRITE),
        ),
    )

    asyncio.run(execute(loop))

    assert peak == 1
    assert order == ["read-1", "write-1", "read-2"]


def test_model_turn_limit_stops_before_another_provider_request() -> None:
    call = ToolCall("call-1", "read", {"value": 1})

    async def tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        return ToolResult(content="ok")

    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(call, reason=StopReason.TOOL_CALL)),)
            ),
            ScriptedTurn(
                actions=(
                    ResponseCompleted(response(TextBlock("unused"), reason=StopReason.COMPLETED)),
                )
            ),
        ]
    )

    result = asyncio.run(
        execute(
            make_loop(
                provider,
                (definition("read", tool),),
                limits=RunLimits(max_model_turns=1),
            )
        )
    )

    assert len(provider.requests) == 1
    assert result.stop_reason is StopReason.LIMIT
    assert result.limit_reason == "max_model_turns"
    assert result.events[-1].payload["limit_reason"] == "max_model_turns"
    assert result.usage.known_total_tokens == 2
    assert result.model_requests[0].usage_source == "provider_reported"
    assert result.final_response.usage is None


def test_tool_call_limit_does_not_start_excess_calls() -> None:
    started: list[str] = []

    async def tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        started.append(context.tool_call_id)
        return ToolResult(content="ok")

    calls = (
        ToolCall("call-1", "read", {"value": 1}),
        ToolCall("call-2", "read", {"value": 2}),
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(*calls, reason=StopReason.TOOL_CALL)),)
            )
        ]
    )

    result = asyncio.run(
        execute(
            make_loop(
                provider,
                (definition("read", tool),),
                limits=RunLimits(max_tool_calls=1),
            )
        )
    )

    assert started == ["call-1"]
    assert result.limit_reason == "max_tool_calls"
    assert len(provider.requests) == 1


def test_total_time_limit_stops_before_first_model_request() -> None:
    moments = iter((10.0, 12.0))
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(
                    ResponseCompleted(response(TextBlock("unused"), reason=StopReason.COMPLETED)),
                )
            )
        ]
    )

    result = asyncio.run(
        execute(
            make_loop(
                provider,
                limits=RunLimits(max_run_seconds=1),
                clock=lambda: next(moments),
            )
        )
    )

    assert provider.requests == ()
    assert result.limit_reason == "max_run_seconds"
    assert result.events[-1].payload["limit_reason"] == "max_run_seconds"
    assert result.model_requests == ()
    assert result.usage.complete is True
    assert result.usage.known_total_tokens == 0


def test_total_time_limit_reached_during_model_turn_starts_no_tools() -> None:
    moments = iter((10.0, 10.0, 10.0, 12.0))
    started: list[str] = []

    async def tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        started.append(context.tool_call_id)
        return ToolResult(content="unused")

    call = ToolCall("call-1", "read", {"value": 1})
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(call, reason=StopReason.TOOL_CALL)),)
            )
        ]
    )

    result = asyncio.run(
        execute(
            make_loop(
                provider,
                (definition("read", tool),),
                limits=RunLimits(max_run_seconds=1),
                clock=lambda: next(moments),
            )
        )
    )

    assert started == []
    assert result.limit_reason == "max_run_seconds"
    assert AgentEventType.TOOL_EXECUTION_START not in {
        event.type for event in result.events
    }
    assert result.usage.known_total_tokens == 2


@pytest.mark.parametrize("outcome", (ToolStatus.SUCCEEDED, ToolStatus.UNKNOWN))
def test_total_time_limit_stops_later_tools_in_a_sequential_batch(outcome: ToolStatus) -> None:
    now = [0.0]
    started: list[str] = []

    async def tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        started.append(context.tool_call_id)
        if context.tool_call_id == "call-1":
            now[0] = 2.0
        return ToolResult(
            content="ok" if outcome is ToolStatus.SUCCEEDED else "unknown",
            status=outcome,
            error_code=None if outcome is ToolStatus.SUCCEEDED else "RESULT_UNKNOWN",
            details={"outcome": outcome.value},
        )

    calls = (
        ToolCall("call-1", "write", {"value": 1}),
        ToolCall("call-2", "write", {"value": 2}),
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(*calls, reason=StopReason.TOOL_CALL)),)
            )
        ]
    )

    result = asyncio.run(
        execute(
            make_loop(
                provider,
                (definition("write", tool, effect=ToolEffect.WRITE),),
                limits=RunLimits(max_run_seconds=1),
                clock=lambda: now[0],
            )
        )
    )

    assert started == ["call-1"]
    assert result.limit_reason == "max_run_seconds"
    tool_results = [item for item in result.messages if isinstance(item, ToolResultMessage)]
    assert tool_results[0].details == {"outcome": outcome.value}
    assert [
        event.payload["tool_call_id"]
        for event in result.events
        if event.type is AgentEventType.TOOL_EXECUTION_START
    ] == ["call-1"]


def test_total_time_limit_stops_parallel_tools_waiting_for_a_slot() -> None:
    now = [0.0]
    started: list[str] = []

    async def tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        started.append(context.tool_call_id)
        if context.tool_call_id == "call-1":
            await asyncio.sleep(0)
            now[0] = 2.0
        return ToolResult(content="ok")

    calls = (
        ToolCall("call-1", "read", {"value": 1}),
        ToolCall("call-2", "read", {"value": 2}),
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ResponseCompleted(response(*calls, reason=StopReason.TOOL_CALL)),)
            )
        ]
    )

    result = asyncio.run(
        execute(
            make_loop(
                provider,
                (definition("read", tool),),
                limits=RunLimits(max_run_seconds=1, max_parallel_reads=1),
                clock=lambda: now[0],
            )
        )
    )

    assert started == ["call-1"]
    assert result.limit_reason == "max_run_seconds"
    assert [
        event.payload["tool_call_id"]
        for event in result.events
        if event.type is AgentEventType.TOOL_EXECUTION_START
    ] == ["call-1"]


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_total_token_limit_requires_positive_integer(value: Any) -> None:
    with pytest.raises(ValueError, match="max_total_tokens"):
        RunLimits(max_total_tokens=value)


@pytest.mark.parametrize("threshold, expected", (
    (3, None), (2, "max_total_tokens"), (1, "max_total_tokens"),
))
def test_token_threshold_on_completed_text_keeps_text_and_actual_usage(
    threshold: int, expected: str | None,
) -> None:
    provider = FakeProvider([ScriptedTurn((ResponseCompleted(
        response(TextBlock("complete"), reason=StopReason.COMPLETED),
    ),))])
    result = asyncio.run(execute(make_loop(provider, limits=RunLimits(max_total_tokens=threshold))))
    assert result.limit_reason == expected
    assert result.stop_reason is (StopReason.LIMIT if expected else StopReason.COMPLETED)
    assert result.final_message is not None
    assert result.final_message.content == (TextBlock("complete"),)
    assert result.usage.known_total_tokens == 2


@pytest.mark.parametrize("usage, reason", (
    (Usage(2, 1), "max_total_tokens"),
    (None, "MODEL_USAGE_UNAVAILABLE"),
))
def test_token_policy_pairs_all_blocked_calls_without_starting_tools(
    usage: Usage | None, reason: str,
) -> None:
    started: list[str] = []

    async def tool(_context: ToolContext, _arguments: Mapping[str, Any]) -> ToolResult:
        started.append("started")
        return ToolResult(content="ok")

    calls = (ToolCall("call-a", "read", {"value": 1}),
             ToolCall("call-b", "read", {"value": 2}))
    provider = FakeProvider([ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage(calls), StopReason.TOOL_CALL, usage,
    )),))])
    result = asyncio.run(execute(make_loop(
        provider, (definition("read", tool),), limits=RunLimits(max_total_tokens=3),
    )))
    assert started == []
    assert len(provider.requests) == 1
    assert result.usage.known_total_tokens == (3 if usage else 0)
    assert result.usage.complete is (usage is not None)
    assert result.model_requests[0].status == "tool_call"
    assert result.stop_reason is (StopReason.LIMIT if usage else StopReason.ERROR)
    assert result.messages[1] == AssistantMessage(calls)
    assert result.new_messages == result.messages
    assert result.limit_reason == (reason if usage else None)
    assert result.error_code == (reason if usage is None else None)
    results = [item for item in result.messages if isinstance(item, ToolResultMessage)]
    assert [item.tool_call_id for item in results] == ["call-a", "call-b"]
    assert all(item.is_error and item.details is not None and
               item.details["execution_status"] == "not_executed" for item in results)
    assert all(item.details is not None and
               item.details.get("error_code" if usage is None else "limit_reason") == reason
               for item in results)
    assert AgentEventType.TOOL_EXECUTION_START not in {event.type for event in result.events}


def test_unknown_usage_without_threshold_remains_incomplete_and_continues() -> None:
    call = ToolCall("call-a", "read", {"value": 1})

    async def tool(_context: ToolContext, _arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(content="ok")

    provider = FakeProvider([
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((call,)), StopReason.TOOL_CALL, None,
        )),)),
        ScriptedTurn((ResponseCompleted(response(
            TextBlock("done"), reason=StopReason.COMPLETED,
        )),)),
    ])
    result = asyncio.run(execute(make_loop(provider, (definition("read", tool),))))
    assert result.stop_reason is StopReason.COMPLETED
    assert result.usage.unknown_request_count == 1
    assert result.usage.complete is False
    assert len(provider.requests) == 2


def test_cancellation_wins_when_token_limit_is_reached_in_same_model_turn() -> None:
    signal = CancellationToken()

    class CancelAtCompletion:
        async def stream(self, _request: Any) -> Any:
            yield RequestStarted()
            signal.cancel()
            yield ResponseCompleted(response(TextBlock("text"), reason=StopReason.COMPLETED))

    registry = ToolRegistry(())
    loop = AgentLoop(
        provider=CancelAtCompletion(), tool_registry=registry,
        tool_runner=ToolRunner(registry), model_config=ModelConfig("fake", 64),
        limits=RunLimits(max_total_tokens=2),
    )
    result = asyncio.run(loop.run(
        run_id="run-1", messages=(UserMessage((TextBlock("start"),)),),
        cancellation=signal,
    ))
    assert result.stop_reason is StopReason.CANCELLED
    assert result.limit_reason is None
