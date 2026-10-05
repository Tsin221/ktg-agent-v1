from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from ktg_agent.core.events import AgentEvent, AgentEventType, ToolProgressPublisher
from ktg_agent.core.loop import AgentLoop, AgentRunResult
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
    ProviderError,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    Usage,
)
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


INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


class RecordingTool:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.values: list[int] = []
        self.error = error

    async def __call__(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        value = arguments["value"]
        assert isinstance(value, int)
        self.values.append(value)
        if self.error is not None:
            raise self.error
        return ToolResult(content=f"result:{value}", details={"value": value})


class ProgressRecordingTool(RecordingTool):
    async def __call__(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        publisher = context.progress_publisher
        assert isinstance(publisher, ToolProgressPublisher)
        await publisher.publish("halfway", details={"percent": 50})
        return await super().__call__(context, arguments)


def tool_definition(execute: RecordingTool) -> ToolDefinition:
    return ToolDefinition(
        name="test.lookup",
        version="1.0",
        description="Look up a test value",
        label="Test lookup",
        input_schema=INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
        required_permissions=(),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=1,
        execute=execute,
    )


def response(
    *content: TextBlock | ToolCall,
    stop_reason: StopReason = StopReason.COMPLETED,
) -> FinalResponse:
    return FinalResponse(
        message=AssistantMessage(content=content),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def make_loop(
    provider: FakeProvider,
    tool: RecordingTool | None = None,
) -> AgentLoop:
    registry = ToolRegistry(() if tool is None else (tool_definition(tool),))
    return AgentLoop(
        provider=provider,
        tool_registry=registry,
        tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake-model", max_output_tokens=128),
    )


def run(loop: AgentLoop, *, run_id: str = "run-1") -> AgentRunResult:
    return asyncio.run(
        loop.run(
            run_id=run_id,
            messages=(UserMessage((TextBlock("start"),)),),
        )
    )


def event_types(result: AgentRunResult) -> list[AgentEventType]:
    return [event.type for event in result.events]


def test_plain_text_response_returns_final_message_and_ordered_events() -> None:
    final = response(TextBlock("done"))
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(
                    TextDelta("do"),
                    TextDelta("ne"),
                    ResponseCompleted(final),
                )
            )
        ]
    )

    result = run(make_loop(provider))

    assert result.final_response == final
    assert result.messages[-1] == final.message
    assert event_types(result) == [
        AgentEventType.AGENT_START,
        AgentEventType.TURN_START,
        AgentEventType.MESSAGE_START,
        AgentEventType.MESSAGE_UPDATE,
        AgentEventType.MESSAGE_UPDATE,
        AgentEventType.MESSAGE_END,
        AgentEventType.TURN_END,
        AgentEventType.AGENT_END,
    ]
    assert [event.seq for event in result.events] == list(
        range(1, len(result.events) + 1)
    )
    assert result.events[3].payload["text"] == "do"
    assert result.events[4].payload["text"] == "ne"


def test_single_tool_result_is_added_to_the_next_provider_request() -> None:
    call = ToolCall("call-1", "test.lookup", {"value": 7})
    tool_turn = response(TextBlock("checking"), call, stop_reason=StopReason.TOOL_CALL)
    final = response(TextBlock("answer"))
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ToolCallCompleted(call), ResponseCompleted(tool_turn))
            ),
            ScriptedTurn(actions=(ResponseCompleted(final),)),
        ]
    )
    tool = RecordingTool()

    result = run(make_loop(provider, tool))

    assert tool.values == [7]
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-2] == tool_turn.message
    tool_message = provider.requests[1].messages[-1]
    assert isinstance(tool_message, ToolResultMessage)
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.content == (TextBlock("result:7"),)
    assert result.messages[-1] == final.message
    ended_messages = [
        event.payload["message"]
        for event in result.events
        if event.type is AgentEventType.MESSAGE_END
        and "message" in event.payload
    ]
    assert all(isinstance(message, dict) for message in ended_messages)
    ended_roles = [
        message["role"] for message in ended_messages if isinstance(message, dict)
    ]
    assert ended_roles == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert event_types(result).count(AgentEventType.TURN_START) == 2
    assert event_types(result).count(AgentEventType.AGENT_END) == 1


def test_multiple_calls_and_turns_preserve_tool_call_order() -> None:
    first = ToolCall("call-1", "test.lookup", {"value": 1})
    second = ToolCall("call-2", "test.lookup", {"value": 2})
    third = ToolCall("call-3", "test.lookup", {"value": 3})
    first_turn = response(first, second, stop_reason=StopReason.TOOL_CALL)
    second_turn = response(third, stop_reason=StopReason.TOOL_CALL)
    final = response(TextBlock("finished"))
    provider = FakeProvider(
        [
            ScriptedTurn(actions=(ResponseCompleted(first_turn),)),
            ScriptedTurn(actions=(ResponseCompleted(second_turn),)),
            ScriptedTurn(actions=(ResponseCompleted(final),)),
        ]
    )
    tool = RecordingTool()

    result = run(make_loop(provider, tool))

    assert tool.values == [1, 2, 3]
    second_request_results = provider.requests[1].messages[-2:]
    assert all(
        isinstance(message, ToolResultMessage) for message in second_request_results
    )
    result_ids = [
        message.tool_call_id
        for message in second_request_results
        if isinstance(message, ToolResultMessage)
    ]
    assert result_ids == [
        "call-1",
        "call-2",
    ]
    assert result.final_response == final
    tool_end_events = [
        event
        for event in result.events
        if event.type is AgentEventType.TOOL_EXECUTION_END
    ]
    assert [event.payload["tool_call_id"] for event in tool_end_events] == [
        "call-1",
        "call-2",
        "call-3",
    ]


def test_truncated_response_never_executes_streamed_tool_call() -> None:
    streamed_call = ToolCall("call-1", "test.lookup", {"value": 7})
    truncated = response(TextBlock("partial"), stop_reason=StopReason.LENGTH)
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(
                    ToolCallCompleted(streamed_call),
                    ResponseCompleted(truncated),
                )
            )
        ]
    )
    tool = RecordingTool()

    result = run(make_loop(provider, tool))

    assert tool.values == []
    assert result.stop_reason is StopReason.LENGTH
    assert AgentEventType.TOOL_EXECUTION_START not in event_types(result)
    assert event_types(result).count(AgentEventType.AGENT_END) == 1


def test_tool_can_publish_progress_between_start_and_end() -> None:
    call = ToolCall("call-1", "test.lookup", {"value": 7})
    tool_turn = response(call, stop_reason=StopReason.TOOL_CALL)
    final = response(TextBlock("answer"))
    provider = FakeProvider(
        [
            ScriptedTurn(actions=(ResponseCompleted(tool_turn),)),
            ScriptedTurn(actions=(ResponseCompleted(final),)),
        ]
    )

    result = run(make_loop(provider, ProgressRecordingTool()))

    tool_events = [
        event
        for event in result.events
        if event.type
        in {
            AgentEventType.TOOL_EXECUTION_START,
            AgentEventType.TOOL_EXECUTION_UPDATE,
            AgentEventType.TOOL_EXECUTION_END,
        }
    ]
    assert [event.type for event in tool_events] == [
        AgentEventType.TOOL_EXECUTION_START,
        AgentEventType.TOOL_EXECUTION_UPDATE,
        AgentEventType.TOOL_EXECUTION_END,
    ]
    assert tool_events[1].payload["message"] == "halfway"
    assert tool_events[1].payload["details"] == {"percent": 50}


def test_tool_failure_is_a_result_in_the_next_model_context() -> None:
    call = ToolCall("call-1", "test.lookup", {"value": 7})
    tool_turn = response(call, stop_reason=StopReason.TOOL_CALL)
    final = response(TextBlock("could not look it up"))
    provider = FakeProvider(
        [
            ScriptedTurn(actions=(ResponseCompleted(tool_turn),)),
            ScriptedTurn(actions=(ResponseCompleted(final),)),
        ]
    )

    result = run(make_loop(provider, RecordingTool(error=RuntimeError("offline"))))

    tool_message = provider.requests[1].messages[-1]
    assert isinstance(tool_message, ToolResultMessage)
    assert tool_message.is_error is True
    assert result.final_response == final
    tool_end = next(
        event
        for event in result.events
        if event.type is AgentEventType.TOOL_EXECUTION_END
    )
    assert tool_end.payload["status"] == "failed"


def test_provider_error_still_publishes_exactly_one_agent_end() -> None:
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(
                    TextDelta("partial"),
                    ProviderError("UPSTREAM", "provider unavailable"),
                )
            )
        ]
    )
    observed: list[AgentEvent] = []
    registry = ToolRegistry()
    loop = AgentLoop(
        provider=provider,
        tool_registry=registry,
        tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake-model", max_output_tokens=128),
        event_sink=observed.append,
    )

    with pytest.raises(ProviderError, match="provider unavailable"):
        run(loop)

    assert [event.type for event in observed].count(AgentEventType.AGENT_END) == 1
    assert observed[-1].type is AgentEventType.AGENT_END
    assert observed[-1].payload["status"] == "failed"
