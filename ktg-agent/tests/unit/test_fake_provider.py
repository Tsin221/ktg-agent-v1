from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from ktg_agent.core.messages import (
    AssistantMessage,
    Message,
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
    Provider,
    ProviderError,
    ProviderRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    Usage,
)
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn


def request_with(*messages: Message) -> ProviderRequest:
    return ProviderRequest(
        messages=messages,
        tools=(),
        config=ModelConfig(model="fake-model", max_output_tokens=128),
    )


def collect(stream: AsyncIterator[ModelEvent]) -> list[ModelEvent]:
    async def consume() -> list[ModelEvent]:
        return [event async for event in stream]

    return asyncio.run(consume())


def test_script_controls_complete_text_and_delta_order() -> None:
    response = FinalResponse(
        message=AssistantMessage(content=(TextBlock("Hello world"),)),
        stop_reason=StopReason.COMPLETED,
        usage=Usage(input_tokens=3, output_tokens=2),
    )
    provider: Provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(
                    TextDelta("Hello"),
                    TextDelta(" "),
                    TextDelta("world"),
                    ResponseCompleted(response),
                )
            )
        ]
    )

    events = collect(provider.stream(request_with(UserMessage((TextBlock("Hi"),)))))

    assert events == [
        TextDelta("Hello"),
        TextDelta(" "),
        TextDelta("world"),
        ResponseCompleted(response),
    ]


def test_script_controls_tool_call_and_records_next_round_tool_result() -> None:
    call = ToolCall("call-1", "ktg.query_stock", {"item_id": 42})
    tool_response = FinalResponse(
        message=AssistantMessage(content=(TextBlock("Checking"), call)),
        stop_reason=StopReason.TOOL_CALL,
        usage=Usage(input_tokens=4, output_tokens=3),
    )
    final_response = FinalResponse(
        message=AssistantMessage(content=(TextBlock("Stock is 8"),)),
        stop_reason=StopReason.COMPLETED,
        usage=Usage(input_tokens=8, output_tokens=4),
    )
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(ToolCallCompleted(call), ResponseCompleted(tool_response))
            ),
            ScriptedTurn(
                actions=(TextDelta("Stock is 8"), ResponseCompleted(final_response))
            ),
        ]
    )
    first_request = request_with(UserMessage((TextBlock("Find stock"),)))
    tool_result = ToolResultMessage(
        tool_call_id="call-1",
        content=(TextBlock("8"),),
        details={"quantity": 8},
    )
    assert tool_response.message is not None
    second_request = request_with(
        *first_request.messages,
        tool_response.message,
        tool_result,
    )

    first_events = collect(provider.stream(first_request))
    second_events = collect(provider.stream(second_request))

    assert first_events[0] == ToolCallCompleted(call)
    assert second_events[-1] == ResponseCompleted(final_response)
    assert provider.requests == (first_request, second_request)
    assert provider.requests[1].messages[-1] == tool_result


@pytest.mark.parametrize("stop_reason", [StopReason.LENGTH, StopReason.CANCELLED])
def test_script_supports_truncated_and_cancelled_responses(
    stop_reason: StopReason,
) -> None:
    response = FinalResponse(
        message=(
            AssistantMessage(content=(TextBlock("partial"),))
            if stop_reason is StopReason.LENGTH
            else None
        ),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=2, output_tokens=1),
    )
    provider = FakeProvider(
        [ScriptedTurn(actions=(TextDelta("partial"), ResponseCompleted(response)))]
    )

    events = collect(provider.stream(request_with(UserMessage((TextBlock("Go"),)))))

    assert events[-1] == ResponseCompleted(response)


def test_script_can_raise_provider_error_after_text_deltas() -> None:
    provider = FakeProvider(
        [
            ScriptedTurn(
                actions=(
                    TextDelta("partial"),
                    ProviderError("UPSTREAM_TIMEOUT", "model timed out", retryable=True),
                )
            )
        ]
    )

    async def consume() -> list[ModelEvent]:
        events: list[ModelEvent] = []
        with pytest.raises(ProviderError, match="model timed out") as raised:
            async for event in provider.stream(
                request_with(UserMessage((TextBlock("Go"),)))
            ):
                events.append(event)
        assert raised.value.code == "UPSTREAM_TIMEOUT"
        assert raised.value.retryable is True
        return events

    assert asyncio.run(consume()) == [TextDelta("partial")]


def test_exhausted_script_fails_explicitly() -> None:
    response = FinalResponse(
        message=AssistantMessage(content=(TextBlock("done"),)),
        stop_reason=StopReason.COMPLETED,
        usage=Usage(1, 1),
    )
    provider = FakeProvider(
        [ScriptedTurn(actions=(ResponseCompleted(response),))]
    )
    request = request_with(UserMessage((TextBlock("Again"),)))
    collect(provider.stream(request))

    with pytest.raises(AssertionError, match="script exhausted after 1 request"):
        collect(provider.stream(request))


@pytest.mark.parametrize(
    "actions",
    [
        (),
        (TextDelta("unterminated"),),
        (
            ResponseCompleted(
                FinalResponse(
                    message=AssistantMessage(content=(TextBlock("done"),)),
                    stop_reason=StopReason.COMPLETED,
                    usage=Usage(1, 1),
                )
            ),
            TextDelta("too late"),
        ),
    ],
)
def test_script_rejects_turns_without_one_terminal_action(
    actions: tuple[ModelEvent, ...],
) -> None:
    with pytest.raises(ValueError, match="terminal action"):
        ScriptedTurn(actions=actions)
