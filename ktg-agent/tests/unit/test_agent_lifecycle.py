from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from ktg_agent.core.agent import (
    Agent,
    AgentBusyError,
    AgentNotRunningError,
    AgentState,
)
from ktg_agent.core.events import AgentEvent, AgentEventType
from ktg_agent.core.loop import AgentLoop, RunLimits
from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ktg_agent.providers.base import (
    FinalResponse,
    ModelConfig,
    ModelEvent,
    ProviderRequest,
    RequestStarted,
    ResponseCompleted,
    Usage,
)
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
    "properties": {},
}


def response(
    *content: TextBlock | ToolCall,
    reason: StopReason = StopReason.COMPLETED,
) -> FinalResponse:
    return FinalResponse(
        message=AssistantMessage(content=content),
        stop_reason=reason,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


class ControlledProvider:
    def __init__(self, responses: tuple[FinalResponse, ...]) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_first = False

    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        async def events() -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            self.started.set()
            if self.block_first and len(self.requests) == 1:
                await self.release.wait()
            if request.cancellation is not None and request.cancellation.is_cancelled:
                yield ResponseCompleted(
                    FinalResponse(
                        message=None,
                        stop_reason=StopReason.CANCELLED,
                        usage=Usage(input_tokens=0, output_tokens=0),
                    )
                )
                return
            yield RequestStarted()
            yield ResponseCompleted(self._responses.pop(0))

        return events()


def make_agent(
    provider: ControlledProvider,
    tool: ToolDefinition | None = None,
) -> Agent:
    registry = ToolRegistry(() if tool is None else (tool,))
    loop = AgentLoop(
        provider=provider,
        tool_registry=registry,
        tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake", max_output_tokens=64),
    )
    return Agent(loop)


def test_second_plain_prompt_is_rejected_while_run_is_active() -> None:
    provider = ControlledProvider((response(TextBlock("done")),))
    provider.block_first = True
    agent = make_agent(provider)

    async def scenario() -> None:
        active = asyncio.create_task(agent.prompt("first", run_id="run-1"))
        await provider.started.wait()

        with pytest.raises(AgentBusyError, match="RUN_BUSY"):
            await agent.prompt("second", run_id="run-2")

        provider.release.set()
        await active
        assert agent.state is AgentState.IDLE

    asyncio.run(scenario())


def test_steer_waits_for_running_tool_then_enters_next_model_request() -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    tool_completed = False

    async def controlled_tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        nonlocal tool_completed
        tool_started.set()
        await release_tool.wait()
        tool_completed = True
        return ToolResult(content="tool finished")

    tool = ToolDefinition(
        name="test.controlled",
        version="1.0",
        description="controlled tool",
        label="controlled tool",
        input_schema=SCHEMA,
        output_schema=None,
        required_permissions=(),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=2,
        execute=controlled_tool,
    )
    call = ToolCall("call-1", "test.controlled", {})
    provider = ControlledProvider(
        (
            response(call, reason=StopReason.TOOL_CALL),
            response(TextBlock("steered result")),
        )
    )
    agent = make_agent(provider, tool)

    async def scenario() -> None:
        task = asyncio.create_task(agent.prompt("start", run_id="run-steer"))
        await tool_started.wait()
        await agent.steer("new direction")
        await agent.steer("and keep the audit trail")
        assert not tool_completed

        release_tool.set()
        result = await task

        assert result.stop_reason is StopReason.COMPLETED
        assert tool_completed
        assert len(provider.requests) == 2
        assert provider.requests[1].messages[-2:] == (
            UserMessage((TextBlock("new direction"),)),
            UserMessage((TextBlock("and keep the audit trail"),)),
        )

    asyncio.run(scenario())


def test_follow_up_enters_only_when_the_run_would_otherwise_finish() -> None:
    provider = ControlledProvider(
        (
            response(TextBlock("first answer")),
            response(TextBlock("follow-up answer")),
        )
    )
    provider.block_first = True
    agent = make_agent(provider)

    async def scenario() -> None:
        task = asyncio.create_task(agent.prompt("start", run_id="run-follow-up"))
        await provider.started.wait()
        await agent.follow_up("one more thing")
        provider.release.set()
        result = await task

        assert result.stop_reason is StopReason.COMPLETED
        assert len(provider.requests) == 2
        assert provider.requests[1].messages[-1] == UserMessage(
            (TextBlock("one more thing"),)
        )

    asyncio.run(scenario())


def test_budget_exhaustion_does_not_consume_unseen_follow_up() -> None:
    provider = ControlledProvider((response(TextBlock("first answer")),))
    provider.block_first = True
    registry = ToolRegistry(())
    agent = Agent(AgentLoop(
        provider=provider, tool_registry=registry, tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake", max_output_tokens=64),
        limits=RunLimits(max_model_turns=1),
    ))

    async def scenario() -> None:
        task = asyncio.create_task(agent.prompt("start", run_id="run-limited"))
        await provider.started.wait()
        await agent.follow_up("unseen", "input-1")
        provider.release.set()
        result = await task
        assert result.stop_reason is StopReason.LIMIT
        assert len(provider.requests) == 1
        assert [item.request_id for item in result.unconsumed_inputs] == ["input-1"]
        assert UserMessage((TextBlock("unseen"),)) not in result.messages

    asyncio.run(scenario())


@pytest.mark.parametrize("effect", (ToolEffect.READ, ToolEffect.WRITE))
def test_abort_during_tool_allows_tool_to_finish_but_starts_no_next_step(
    effect: ToolEffect,
) -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    completed_calls: list[str] = []

    async def controlled_tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        tool_started.set()
        await release_tool.wait()
        completed_calls.append(context.tool_call_id)
        return ToolResult(content="committed result")

    tool = ToolDefinition(
        name="test.controlled",
        version="1.0",
        description="controlled write",
        label="controlled write",
        input_schema=SCHEMA,
        output_schema=None,
        required_permissions=(),
        effect=effect,
        execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=2,
        execute=controlled_tool,
    )
    first_call = ToolCall("call-1", "test.controlled", {})
    second_call = ToolCall("call-2", "test.controlled", {})
    provider = ControlledProvider(
        (
            response(first_call, second_call, reason=StopReason.TOOL_CALL),
            response(TextBlock("must not run")),
        )
    )
    agent = make_agent(provider, tool)

    async def scenario() -> None:
        task = asyncio.create_task(agent.prompt("start", run_id="run-cancel"))
        await tool_started.wait()
        await agent.follow_up("discard me")
        assert agent.abort() is True
        release_tool.set()
        result = await task
        await agent.wait_for_idle()

        assert completed_calls == ["call-1"]
        assert len(provider.requests) == 1
        assert result.stop_reason is StopReason.CANCELLED
        started_tool_ids = [
            event.payload["tool_call_id"]
            for event in result.events
            if event.type is AgentEventType.TOOL_EXECUTION_START
        ]
        assert started_tool_ids == ["call-1"]
        assert result.events[-1].type is AgentEventType.AGENT_END
        assert result.events[-1].payload["status"] == "cancelled"
        assert agent.state is AgentState.IDLE

        with pytest.raises(AgentNotRunningError):
            await agent.follow_up("too late")

    asyncio.run(scenario())


def test_listener_exception_does_not_break_run_or_other_listeners() -> None:
    provider = ControlledProvider((response(TextBlock("done")),))
    agent = make_agent(provider)
    observed: list[AgentEvent] = []

    def broken_listener(event: AgentEvent) -> None:
        raise RuntimeError("listener failed")

    agent.subscribe(broken_listener)
    agent.subscribe(observed.append)

    result = asyncio.run(agent.prompt("start", run_id="run-listeners"))

    assert result.stop_reason is StopReason.COMPLETED
    assert observed
    assert observed[-1].type is AgentEventType.AGENT_END


def test_tool_exception_is_normalized_and_agent_returns_to_idle() -> None:
    async def failing_tool(
        context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        raise RuntimeError("tool offline")

    tool = ToolDefinition(
        name="test.failing",
        version="1.0",
        description="failing tool",
        label="failing tool",
        input_schema=SCHEMA,
        output_schema=None,
        required_permissions=(),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=2,
        execute=failing_tool,
    )
    call = ToolCall("call-1", "test.failing", {})
    provider = ControlledProvider(
        (
            response(call, reason=StopReason.TOOL_CALL),
            response(TextBlock("reported failure")),
        )
    )
    agent = make_agent(provider, tool)

    result = asyncio.run(agent.prompt("start", run_id="run-tool-error"))

    tool_result = provider.requests[1].messages[-1]
    assert isinstance(tool_result, ToolResultMessage)
    assert tool_result.is_error is True
    assert result.stop_reason is StopReason.COMPLETED
    assert agent.state is AgentState.IDLE


def test_final_queue_check_closes_gate_before_agent_end_sink_waits() -> None:
    provider = ControlledProvider((response(TextBlock("done")),))
    entered_end = asyncio.Event()
    release_end = asyncio.Event()

    async def slow_sink(event: AgentEvent) -> None:
        if event.type is AgentEventType.AGENT_END:
            entered_end.set()
            await release_end.wait()

    registry = ToolRegistry(())
    agent = Agent(AgentLoop(
        provider=provider, tool_registry=registry, tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake", max_output_tokens=64), event_sink=slow_sink,
    ))

    async def scenario() -> None:
        task = asyncio.create_task(agent.prompt("start", run_id="run-gate"))
        await entered_end.wait()
        assert agent.accepting_inputs is False
        with pytest.raises(AgentNotRunningError):
            await agent.follow_up("too late")
        release_end.set()
        result = await task
        assert result.unconsumed_inputs == ()
        assert len(provider.requests) == 1

    asyncio.run(scenario())


def test_cancelled_run_exposes_unconsumed_input_before_queue_clear() -> None:
    provider = ControlledProvider((response(TextBlock("unused")),))
    provider.block_first = True
    agent = make_agent(provider)

    async def scenario() -> None:
        task = asyncio.create_task(agent.prompt("start", run_id="run-pending"))
        await provider.started.wait()
        await agent.follow_up("retain this")
        assert agent.abort() is True
        provider.release.set()
        result = await task
        assert result.stop_reason is StopReason.CANCELLED
        assert result.unconsumed_inputs[0].message == UserMessage((TextBlock("retain this"),))
        assert agent.unconsumed_inputs == result.unconsumed_inputs

    asyncio.run(scenario())
