from __future__ import annotations

import pytest

from ktg_agent.context import TokenEstimate, estimate_request, estimate_text_tokens
from ktg_agent.context.tokens import estimate_message_tokens, estimate_tools_tokens
from ktg_agent.core.messages import AssistantMessage, TextBlock, ToolCall, ToolResultMessage
from ktg_agent.providers.base import ToolSchema


@pytest.mark.parametrize(("text", "expected"), [
    ("", 0), ("abcd", 1), ("abcde", 2), ("abcdefgh", 2),
    ("中", 1), ("abcd中文", 3),
])
def test_text_estimate(text: str, expected: int) -> None:
    assert estimate_text_tokens(text) == expected


def test_estimate_uses_model_visible_text_and_ignores_details() -> None:
    first = ToolResultMessage("call", (TextBlock("abcd"), TextBlock("hidden", False)),
                              details={"quantity": 1})
    second = ToolResultMessage("call", (TextBlock("abcd"), TextBlock("different", False)),
                               is_error=True, details={"quantity": 99})
    assert estimate_message_tokens(first) == estimate_message_tokens(second) == 5


def test_request_is_deterministic_and_counts_tool_calls_and_schemas() -> None:
    messages = (AssistantMessage((ToolCall("c1", "read", {"b": 2, "a": "中"}),)),)
    tools = (ToolSchema("read", "find", {"type": "object"}),)
    estimate = estimate_request(messages, tools)
    assert estimate == estimate_request(messages, tools)
    assert estimate.messages_tokens == estimate_message_tokens(messages[0])
    assert estimate.tools_tokens == estimate_tools_tokens(tools)
    assert estimate.total_tokens == estimate.messages_tokens + estimate.tools_tokens
    assert TokenEstimate(1, 2).total_tokens == 3
