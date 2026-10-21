"""Replayable estimates for model-visible request content, never provider usage."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from ktg_agent.core.messages import Message, TextBlock, ToolCall
from ktg_agent.providers.base import ToolSchema

ASCII_CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_text_tokens(text: str) -> int:
    ascii_count = sum(ch.isascii() for ch in text)
    return (ascii_count + ASCII_CHARS_PER_TOKEN - 1) // ASCII_CHARS_PER_TOKEN + len(text) - ascii_count


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def estimate_message_tokens(message: Message) -> int:
    total = MESSAGE_OVERHEAD_TOKENS
    for block in message.content:
        if isinstance(block, TextBlock) and block.model_visible:
            total += estimate_text_tokens(block.text)
        elif isinstance(block, ToolCall):
            total += sum(estimate_text_tokens(part) for part in (
                block.id, block.name, _canonical_json(block.arguments),
            ))
    return total


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tools_tokens(tools: Sequence[ToolSchema]) -> int:
    return sum(
        estimate_text_tokens(tool.name)
        + estimate_text_tokens(tool.description)
        + estimate_text_tokens(_canonical_json(tool.input_schema))
        for tool in tools
    )


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    messages_tokens: int
    tools_tokens: int
    total_tokens: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_tokens", self.messages_tokens + self.tools_tokens)


def estimate_request(
    messages: Sequence[Message], tools: Sequence[ToolSchema] = (),
) -> TokenEstimate:
    return TokenEstimate(estimate_messages_tokens(messages), estimate_tools_tokens(tools))
