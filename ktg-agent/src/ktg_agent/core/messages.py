"""Stable, provider-independent message and response contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TypeAlias, cast
from collections.abc import Mapping, Sequence

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
JsonObject: TypeAlias = dict[str, JsonValue]


class MessageValidationError(ValueError):
    """Raised when a message cannot satisfy the core message contract."""


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    COMPLETED = "completed"
    TOOL_CALL = "tool_call"
    LENGTH = "length"
    ERROR = "error"
    CANCELLED = "cancelled"
    LIMIT = "limit"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MessageValidationError(f"{label} must be a non-empty string")
    return value


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_json_value(item, label) for item in value]
    if isinstance(value, Mapping):
        converted: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MessageValidationError(f"{label} keys must be strings")
            converted[key] = _json_value(item, label)
        return converted
    raise MessageValidationError(f"{label} must contain only JSON values")


def _json_object(value: object, label: str) -> JsonObject:
    converted = _json_value(value, label)
    if not isinstance(converted, dict):
        raise MessageValidationError(f"{label} must be an object")
    return converted


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    model_visible: bool = True

    def __post_init__(self) -> None:
        _required_text(self.text, "text")
        if not isinstance(self.model_visible, bool):
            raise MessageValidationError("model_visible must be a boolean")


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        _required_text(self.id, "tool call id")
        _required_text(self.name, "tool name")
        object.__setattr__(self, "arguments", _json_object(self.arguments, "tool arguments"))


@dataclass(frozen=True, slots=True)
class SystemMessage:
    content: tuple[TextBlock, ...]
    role: Role = field(default=Role.SYSTEM, init=False)

    def __post_init__(self) -> None:
        _validate_content(self.content, TextBlock)


@dataclass(frozen=True, slots=True)
class UserMessage:
    content: tuple[TextBlock, ...]
    role: Role = field(default=Role.USER, init=False)

    def __post_init__(self) -> None:
        _validate_content(self.content, TextBlock)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    content: tuple[TextBlock | ToolCall, ...]
    role: Role = field(default=Role.ASSISTANT, init=False)

    def __post_init__(self) -> None:
        _validate_content(self.content, (TextBlock, ToolCall))


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    tool_call_id: str
    content: tuple[TextBlock, ...]
    is_error: bool = False
    details: JsonObject | None = None
    role: Role = field(default=Role.TOOL, init=False)

    def __post_init__(self) -> None:
        _required_text(self.tool_call_id, "tool call id")
        _validate_content(self.content, TextBlock)
        if not isinstance(self.is_error, bool):
            raise MessageValidationError("is_error must be a boolean")
        if self.details is not None:
            object.__setattr__(self, "details", _json_object(self.details, "tool details"))


Message: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
ContentBlock: TypeAlias = TextBlock | ToolCall


def _validate_content(content: object, allowed: type | tuple[type, ...]) -> None:
    if not isinstance(content, tuple) or not content:
        raise MessageValidationError("message content must be a non-empty tuple")
    if not all(isinstance(block, allowed) for block in content):
        raise MessageValidationError("message contains an invalid content block")


def project_model_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Return model-visible history after validating tool call/result linkage."""
    projected: list[Message] = []
    pending_calls: set[str] = set()
    all_calls: set[str] = set()

    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    if block.id in all_calls:
                        raise MessageValidationError(f"duplicate tool call id: {block.id}")
                    all_calls.add(block.id)
                    pending_calls.add(block.id)
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id not in all_calls:
                raise MessageValidationError(
                    f"tool result references unknown tool call id: {message.tool_call_id}"
                )
            if message.tool_call_id not in pending_calls:
                raise MessageValidationError(
                    f"duplicate result for tool call id: {message.tool_call_id}"
                )
            pending_calls.remove(message.tool_call_id)

        if isinstance(message, ToolResultMessage):
            tool_content = tuple(block for block in message.content if block.model_visible)
            projected.append(replace(message, content=tool_content, details=None))
        elif isinstance(message, AssistantMessage):
            assistant_content = tuple(
                block
                for block in message.content
                if not isinstance(block, TextBlock) or block.model_visible
            )
            if assistant_content:
                projected.append(replace(message, content=assistant_content))
        else:
            text_content = tuple(block for block in message.content if block.model_visible)
            if text_content:
                projected.append(replace(message, content=text_content))

    if pending_calls:
        missing = ", ".join(sorted(pending_calls))
        raise MessageValidationError(f"missing tool results for: {missing}")
    return tuple(projected)


def message_to_dict(message: Message) -> JsonObject:
    payload: JsonObject = {
        "role": message.role.value,
        "content": [_block_to_dict(block) for block in message.content],
    }
    if isinstance(message, ToolResultMessage):
        payload["tool_call_id"] = message.tool_call_id
        payload["is_error"] = message.is_error
        if message.details is not None:
            payload["details"] = message.details
    return payload


def _block_to_dict(block: ContentBlock) -> JsonObject:
    if isinstance(block, TextBlock):
        return {
            "type": "text",
            "text": block.text,
            "model_visible": block.model_visible,
        }
    return {
        "type": "tool_call",
        "id": block.id,
        "name": block.name,
        "arguments": block.arguments,
    }


def message_from_dict(payload: Mapping[str, object]) -> Message:
    raw_role = payload.get("role")
    if not isinstance(raw_role, str):
        raise MessageValidationError(f"unknown message role: {raw_role!r}")
    try:
        role = Role(raw_role)
    except ValueError as exc:
        raise MessageValidationError(f"unknown message role: {raw_role!r}") from exc

    raw_content = payload.get("content")
    if not isinstance(raw_content, list):
        raise MessageValidationError("message content must be a list")
    content = tuple(_block_from_dict(block) for block in raw_content)

    if role is Role.SYSTEM:
        return SystemMessage(content=_text_blocks(content))
    if role is Role.USER:
        return UserMessage(content=_text_blocks(content))
    if role is Role.ASSISTANT:
        return AssistantMessage(content=content)

    tool_call_id = _required_text(payload.get("tool_call_id"), "tool call id")
    is_error = payload.get("is_error", False)
    if not isinstance(is_error, bool):
        raise MessageValidationError("is_error must be a boolean")
    details = payload.get("details")
    return ToolResultMessage(
        tool_call_id=tool_call_id,
        content=_text_blocks(content),
        is_error=is_error,
        details=None if details is None else _json_object(details, "tool details"),
    )


def _block_from_dict(payload: object) -> ContentBlock:
    if not isinstance(payload, Mapping):
        raise MessageValidationError("content block must be an object")
    block_type = payload.get("type")
    if block_type == "text":
        model_visible = payload.get("model_visible", True)
        if not isinstance(model_visible, bool):
            raise MessageValidationError("model_visible must be a boolean")
        return TextBlock(
            _required_text(payload.get("text"), "text"),
            model_visible,
        )
    if block_type == "tool_call":
        return ToolCall(
            _required_text(payload.get("id"), "tool call id"),
            _required_text(payload.get("name"), "tool name"),
            _json_object(payload.get("arguments"), "tool arguments"),
        )
    raise MessageValidationError(f"unknown content block type: {block_type!r}")


def _text_blocks(content: tuple[ContentBlock, ...]) -> tuple[TextBlock, ...]:
    if not all(isinstance(block, TextBlock) for block in content):
        raise MessageValidationError("this message role only accepts text blocks")
    return cast(tuple[TextBlock, ...], content)
