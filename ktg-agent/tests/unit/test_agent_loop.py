from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from ktg_agent.core.events import AgentEvent, AgentEventType, ToolProgressPublisher
from ktg_agent.core.loop import AgentLoop, AgentRunResult
from ktg_agent.core.queues import InputQueue, InputMode
from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    SystemMessage,
    UserMessage,
)
from ktg_agent.providers.base import (
    FinalResponse,
    ModelConfig,
    ProviderError,
    RequestStarted,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    Usage,
    UsageReported,
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


def test_new_messages_are_a_suffix_and_can_be_empty() -> None:
    initial = UserMessage((TextBlock("start"),))
    final = response(TextBlock("done"))
    assert final.message is not None
    with pytest.raises(ValueError, match="suffix"):
        AgentRunResult((initial, final.message), final, (), new_messages=(final.message, initial))
    assert AgentRunResult((initial,), final, ()).new_messages == ()


def test_existing_history_is_preserved_before_new_messages() -> None:
    old = UserMessage((TextBlock("old"),))
    current = UserMessage((TextBlock("current"),))
    final = response(TextBlock("done"))
    result = asyncio.run(make_loop(FakeProvider((ScriptedTurn((ResponseCompleted(final),)),))).run(
        run_id="run-1", messages=(old, current), new_input_count=1,
    ))
    assert result.messages[:1] == (old,)
    assert result.new_messages == (current, final.message)
    assert len(result.messages) == 1 + len(result.new_messages)


def test_cancellation_before_any_new_message_allows_empty_suffix() -> None:
    class Cancelled:
        is_cancelled = True

    original = SystemMessage((TextBlock("instructions"),))
    result = asyncio.run(make_loop(FakeProvider(())).run(
        run_id="run-1", messages=(original,), new_input_count=0, cancellation=Cancelled(),
    ))
    assert result.messages == (original,)
    assert result.new_messages == ()
    assert result.stop_reason is StopReason.CANCELLED


def test_usage_snapshot_is_retained_when_provider_fails_after_reporting_it() -> None:
    observed: list[AgentEvent] = []
    provider = FakeProvider([ScriptedTurn(actions=(
        UsageReported(Usage(9, 2, 3)),
        ProviderError("MODEL_INVALID_RESPONSE", "broken tail"),
    ))])
    registry = ToolRegistry(())
    loop = AgentLoop(provider=provider, tool_registry=registry,
                     tool_runner=ToolRunner(registry),
                     model_config=ModelConfig("fake-model", 128),
                     event_sink=observed.append)

    with pytest.raises(ProviderError, match="broken tail"):
        run(loop)

    settled = [event for event in observed if event.type is AgentEventType.MODEL_REQUEST_SETTLED]
    assert len(settled) == 1
    assert settled[0].payload == {
        "call_id": "run-1:model:1", "turn": 1, "model": "fake-model",
        "request_started": True, "status": "failed", "usage_source": "provider_reported",
        "input_tokens": 9, "output_tokens": 2, "cached_input_tokens": 3,
        "total_tokens": 11,
    }
    assert provider.requests[0].call_id == "run-1:model:1"


def test_started_request_without_usage_settles_with_null_counts() -> None:
    provider = FakeProvider([ScriptedTurn(actions=(ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock("done"),)), StopReason.COMPLETED,
    )),))])

    result = run(make_loop(provider))

    settled = [event for event in result.events if event.type is AgentEventType.MODEL_REQUEST_SETTLED]
    assert len(settled) == 1
    assert settled[0].payload["usage_source"] == "unknown"
    assert settled[0].payload["total_tokens"] is None
    assert result.stop_reason is StopReason.COMPLETED
    assert result.usage.unknown_request_count == 1
    assert result.usage.complete is False


def test_run_usage_counts_each_request_once_across_tool_turn_and_follow_up() -> None:
    call = ToolCall("call-1", "test.lookup", {"value": 7})
    first = FinalResponse(AssistantMessage((call,)), StopReason.TOOL_CALL, Usage(10, 3, 4))
    second = FinalResponse(AssistantMessage((TextBlock("answer"),)), StopReason.COMPLETED, Usage(5, 2, 1))
    third = FinalResponse(AssistantMessage((TextBlock("follow up"),)), StopReason.COMPLETED, Usage(8, 6, 2))
    provider = FakeProvider([
        ScriptedTurn((UsageReported(Usage(10, 3, 4)), UsageReported(Usage(10, 3, 4)), ResponseCompleted(first))),
        ScriptedTurn((ResponseCompleted(second),)),
        ScriptedTurn((ResponseCompleted(third),)),
    ])
    queue = InputQueue()
    queue.enqueue(InputMode.FOLLOW_UP, UserMessage((TextBlock("more"),)), None)
    loop = make_loop(provider, RecordingTool())

    result = asyncio.run(loop.run(
        run_id="run-1", messages=(UserMessage((TextBlock("start"),)),), input_queue=queue,
    ))

    assert [request.call_id for request in result.model_requests] == [
        "run-1:model:1", "run-1:model:2", "run-1:model:3",
    ]
    assert result.usage.known_input_tokens == 23
    assert result.usage.known_output_tokens == 11
    assert result.usage.known_cached_input_tokens == 7
    assert result.usage.known_total_tokens == 34
    assert result.usage.unknown_request_count == 0
    assert result.usage.complete is True
    assert result.final_response.usage == Usage(8, 6, 2)
    assert [message.content[0].text for message in result.new_messages
            if isinstance(message, UserMessage)] == ["start", "more"]


def test_steering_message_is_recorded_in_actual_order() -> None:
    call = ToolCall("call-1", "test.lookup", {"value": 7})
    tool_turn = response(call, stop_reason=StopReason.TOOL_CALL)
    final = response(TextBlock("done"))
    provider = FakeProvider((
        ScriptedTurn((ResponseCompleted(tool_turn),)),
        ScriptedTurn((ResponseCompleted(final),)),
    ))
    queue = InputQueue()
    steer = UserMessage((TextBlock("steer"),))
    queue.enqueue(InputMode.STEER, steer, None)
    result = asyncio.run(make_loop(provider, RecordingTool()).run(
        run_id="run-1", messages=(UserMessage((TextBlock("start"),)),), input_queue=queue,
    ))
    assert isinstance(result.new_messages[2], ToolResultMessage)
    assert result.new_messages[3:] == (steer, final.message)


def test_zero_usage_and_unknown_usage_have_distinct_run_results() -> None:
    provider = FakeProvider([
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((TextBlock("zero"),)), StopReason.COMPLETED, Usage(0, 0),
        )),)),
    ])
    result = run(make_loop(provider))
    assert result.usage.known_total_tokens == 0
    assert result.usage.complete is True
    assert result.model_requests[0].usage_source == "provider_reported"


def test_cancelled_before_request_has_no_unknown_usage() -> None:
    class Cancelled:
        is_cancelled = True

    provider = FakeProvider([ScriptedTurn((ResponseCompleted(FinalResponse(
        None, StopReason.CANCELLED, Usage(0, 0),
    )),))])
    loop = make_loop(provider)
    result = asyncio.run(loop.run(
        run_id="run-1", messages=(UserMessage((TextBlock("start"),)),),
        cancellation=Cancelled(),
    ))

    assert provider.requests == ()
    assert result.model_requests == ()
    assert result.usage.complete is True
    assert result.usage.unknown_request_count == 0
    assert result.final_response.usage is None
    assert result.new_messages == (UserMessage((TextBlock("start"),)),)


def test_provider_cancelled_before_dispatch_records_not_started() -> None:
    class PreDispatchCancelled:
        async def stream(self, request: Any) -> Any:
            yield ResponseCompleted(FinalResponse(None, StopReason.CANCELLED))

    registry = ToolRegistry(())
    loop = AgentLoop(provider=PreDispatchCancelled(), tool_registry=registry,
                     tool_runner=ToolRunner(registry), model_config=ModelConfig("fake-model", 128))

    result = run(loop)

    assert result.model_requests[0].usage_source == "not_started"
    assert result.model_requests[0].usage == Usage(0, 0)
    assert result.usage.complete is True
    assert result.usage.unknown_request_count == 0


def test_blocked_tail_keeps_text_live_and_settles_usage_before_run_end() -> None:
    text_sent = asyncio.Event()
    release_tail = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request: Any) -> Any:
            yield RequestStarted()
            yield TextDelta("live")
            text_sent.set()
            await release_tail.wait()
            yield UsageReported(Usage(4, 2, 1))
            yield ResponseCompleted(FinalResponse(
                AssistantMessage((TextBlock("live"),)), StopReason.COMPLETED, Usage(4, 2, 1),
            ))

    observed: list[AgentEvent] = []
    registry = ToolRegistry(())
    loop = AgentLoop(provider=BlockingProvider(), tool_registry=registry,
                     tool_runner=ToolRunner(registry), model_config=ModelConfig("fake-model", 128),
                     event_sink=observed.append)

    async def scenario() -> AgentRunResult:
        task = asyncio.create_task(loop.run(
            run_id="run-1", messages=(UserMessage((TextBlock("start"),)),),
        ))
        await text_sent.wait()
        assert AgentEventType.MESSAGE_UPDATE in [event.type for event in observed]
        assert AgentEventType.MODEL_REQUEST_SETTLED not in [event.type for event in observed]
        release_tail.set()
        return await task

    result = asyncio.run(scenario())
    types = event_types(result)
    assert types.index(AgentEventType.MODEL_REQUEST_SETTLED) < types.index(AgentEventType.TURN_END)
    assert types.index(AgentEventType.TURN_END) < types.index(AgentEventType.AGENT_END)
    assert result.usage.known_total_tokens == 6


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
    assert result.new_messages == (UserMessage((TextBlock("start"),)), final.message)
    assert result.messages == result.new_messages
    assert result.messages[-1] == final.message
    assert event_types(result) == [
        AgentEventType.AGENT_START,
        AgentEventType.TURN_START,
        AgentEventType.MESSAGE_START,
        AgentEventType.MODEL_REQUEST_STARTED,
        AgentEventType.MESSAGE_UPDATE,
        AgentEventType.MESSAGE_UPDATE,
        AgentEventType.MODEL_USAGE_REPORTED,
        AgentEventType.MODEL_REQUEST_SETTLED,
        AgentEventType.MESSAGE_END,
        AgentEventType.TURN_END,
        AgentEventType.AGENT_END,
    ]
    assert [event.seq for event in result.events] == list(
        range(1, len(result.events) + 1)
    )
    assert result.events[4].payload["text"] == "do"
    assert result.events[5].payload["text"] == "ne"


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
    assert result.new_messages == (
        UserMessage((TextBlock("start"),)), tool_turn.message, result.messages[2], final.message,
    )
    assert isinstance(result.new_messages[2], ToolResultMessage)
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
