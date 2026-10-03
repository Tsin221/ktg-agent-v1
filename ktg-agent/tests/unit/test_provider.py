from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from ktg_agent.core.messages import (
    AssistantMessage,
    MessageValidationError,
    StopReason,
    TextBlock,
    ToolCall,
    UserMessage,
)
from ktg_agent.providers.base import (
    FinalResponse,
    ModelEvent,
    ModelConfig,
    Provider,
    ProviderRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    ToolSchema,
    Usage,
)


class FakeProvider:
    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        async def events() -> AsyncIterator[ModelEvent]:
            yield TextDelta("Checking")
            call = ToolCall("call-1", "ktg.query_stock", {"item_id": 42})
            yield ToolCallCompleted(call)
            yield ResponseCompleted(
                FinalResponse(
                    message=AssistantMessage(content=(TextBlock("Checking"), call)),
                    stop_reason=StopReason.TOOL_CALL,
                    usage=Usage(input_tokens=10, output_tokens=5),
                )
            )

        return events()


def test_provider_protocol_streams_provider_independent_events() -> None:
    provider: Provider = FakeProvider()
    request = ProviderRequest(
        messages=(UserMessage(content=(TextBlock("Find stock"),)),),
        tools=(
            ToolSchema(
                name="ktg.query_stock",
                description="Find current stock",
                input_schema={"type": "object", "properties": {}},
            ),
        ),
        config=ModelConfig(model="test-model", max_output_tokens=256),
    )

    async def collect() -> list[object]:
        return [event async for event in provider.stream(request)]

    events = asyncio.run(collect())

    assert isinstance(events[0], TextDelta)
    assert isinstance(events[1], ToolCallCompleted)
    assert isinstance(events[2], ResponseCompleted)
    assert events[2].response.usage.total_tokens == 15


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1)],
)
def test_usage_rejects_negative_token_counts(input_tokens: int, output_tokens: int) -> None:
    with pytest.raises(ValueError, match="token counts"):
        Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def test_text_delta_allows_whitespace_from_provider_stream() -> None:
    assert TextDelta(" ").text == " "


@pytest.mark.parametrize("stop_reason", [StopReason.ERROR, StopReason.CANCELLED])
def test_early_failure_can_complete_without_an_assistant_message(
    stop_reason: StopReason,
) -> None:
    response = FinalResponse(
        message=None,
        stop_reason=stop_reason,
        usage=Usage(0, 0),
    )

    assert response.message is None


def test_tool_call_stop_requires_a_complete_tool_call() -> None:
    with pytest.raises(MessageValidationError, match="requires at least one tool call"):
        FinalResponse(
            message=AssistantMessage(content=(TextBlock("Checking"),)),
            stop_reason=StopReason.TOOL_CALL,
            usage=Usage(1, 1),
        )


@pytest.mark.parametrize(
    "stop_reason",
    [StopReason.LENGTH, StopReason.ERROR, StopReason.CANCELLED],
)
def test_non_tool_terminal_reason_cannot_release_tool_calls(stop_reason: StopReason) -> None:
    call = ToolCall("call-1", "ktg.query_stock", {})

    with pytest.raises(MessageValidationError, match="cannot contain executable tool calls"):
        FinalResponse(
            message=AssistantMessage(content=(call,)),
            stop_reason=stop_reason,
            usage=Usage(1, 1),
        )
