from __future__ import annotations

import pytest

from ktg_agent.core.messages import (
    AssistantMessage,
    MessageValidationError,
    StopReason,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
    project_model_messages,
)


def test_messages_round_trip_through_wire_format() -> None:
    messages = (
        SystemMessage(content=(TextBlock("Be concise."),)),
        UserMessage(content=(TextBlock("Find stock"),)),
        AssistantMessage(
            content=(
                TextBlock("I will check."),
                ToolCall(
                    id="call-1",
                    name="ktg.query_stock",
                    arguments={"item_id": 42},
                ),
            )
        ),
        ToolResultMessage(
            tool_call_id="call-1",
            content=(TextBlock("12 units"),),
            is_error=False,
        ),
    )

    assert tuple(message_from_dict(message_to_dict(item)) for item in messages) == messages


def test_model_projection_filters_ui_only_text_and_preserves_tool_order() -> None:
    messages = (
        UserMessage(
            content=(
                TextBlock("Find stock"),
                TextBlock("Route: /inventory", model_visible=False),
            )
        ),
        AssistantMessage(
            content=(
                TextBlock("Checking"),
                ToolCall("call-1", "ktg.query_stock", {"item_id": 42}),
                ToolCall("call-2", "ktg.query_stock", {"item_id": 43}),
            )
        ),
        ToolResultMessage("call-1", (TextBlock("12 units"),)),
        ToolResultMessage(
            "call-2",
            (
                TextBlock("7 units"),
                TextBlock("Open item 43", model_visible=False),
            ),
        ),
    )

    projected = project_model_messages(messages)

    assert [message.role.value for message in projected] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert projected[0].content == (TextBlock("Find stock"),)
    assert projected[1].content[1:] == messages[1].content[1:]
    assert isinstance(projected[2], ToolResultMessage)
    assert isinstance(projected[3], ToolResultMessage)
    assert projected[2].tool_call_id == "call-1"
    assert projected[3].tool_call_id == "call-2"
    assert projected[3].content == (TextBlock("7 units"),)


@pytest.mark.parametrize("tool_call_id", ["", "   "])
def test_tool_call_rejects_missing_id(tool_call_id: str) -> None:
    with pytest.raises(MessageValidationError, match="tool call id"):
        ToolCall(tool_call_id, "ktg.query_stock", {})


def test_projection_rejects_duplicate_tool_call_id() -> None:
    messages = (
        AssistantMessage(
            content=(
                ToolCall("call-1", "ktg.first", {}),
                ToolCall("call-1", "ktg.second", {}),
            )
        ),
        ToolResultMessage("call-1", (TextBlock("done"),)),
    )

    with pytest.raises(MessageValidationError, match="duplicate tool call id"):
        project_model_messages(messages)


def test_projection_rejects_result_without_matching_call() -> None:
    messages = (ToolResultMessage("call-404", (TextBlock("done"),)),)

    with pytest.raises(MessageValidationError, match="unknown tool call id"):
        project_model_messages(messages)


def test_projection_rejects_call_without_result() -> None:
    messages = (
        AssistantMessage(content=(ToolCall("call-1", "ktg.query_stock", {}),)),
    )

    with pytest.raises(MessageValidationError, match="missing tool results"):
        project_model_messages(messages)


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(MessageValidationError, match="unknown message role"):
        message_from_dict({"role": "operator", "content": []})


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "user"},
        {"role": "assistant", "content": [{"type": "tool_call", "id": "x"}]},
        {"role": "tool", "tool_call_id": "x"},
    ],
)
def test_incomplete_wire_message_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(MessageValidationError):
        message_from_dict(payload)


def test_stop_reasons_distinguish_truncation_error_and_cancellation() -> None:
    assert {reason.value for reason in StopReason} == {
        "completed",
        "tool_call",
        "length",
        "error",
        "cancelled",
        "limit",
    }
