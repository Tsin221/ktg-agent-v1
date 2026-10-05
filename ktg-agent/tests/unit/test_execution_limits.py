from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ktg_agent.core.events import AgentEventType
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
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.tools import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
    ToolRunner,
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


def test_total_time_limit_reached_during_model_turn_starts_no_tools() -> None:
    moments = iter((10.0, 10.0, 12.0))
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
