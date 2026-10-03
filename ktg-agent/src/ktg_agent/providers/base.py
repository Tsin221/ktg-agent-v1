"""Provider protocol with no dependency on a model vendor SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeAlias

from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    MessageValidationError,
    StopReason,
    ToolCall,
    _json_object,
    _required_text,
)


class CancellationSignal(Protocol):
    @property
    def is_cancelled(self) -> bool:
        """Whether the caller has requested cancellation."""
        ...


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    input_schema: JsonObject

    def __post_init__(self) -> None:
        _required_text(self.name, "tool name")
        _required_text(self.description, "tool description")
        object.__setattr__(
            self,
            "input_schema",
            _json_object(self.input_schema, "tool input schema"),
        )


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    max_output_tokens: int
    temperature: float | None = None

    def __post_init__(self) -> None:
        _required_text(self.model, "model")
        if not isinstance(self.max_output_tokens, int) or self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    config: ModelConfig
    cancellation: CancellationSignal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ValueError("provider request requires at least one message")
        if not isinstance(self.tools, tuple):
            raise TypeError("tools must be a tuple")


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        counts = (self.input_tokens, self.output_tokens, self.cached_input_tokens)
        if any(not isinstance(count, int) or count < 0 for count in counts):
            raise ValueError("token counts must be non-negative integers")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class FinalResponse:
    message: AssistantMessage | None
    stop_reason: StopReason
    usage: Usage

    def __post_init__(self) -> None:
        if self.message is None:
            if self.stop_reason not in (StopReason.ERROR, StopReason.CANCELLED):
                raise MessageValidationError(
                    f"{self.stop_reason.value} response requires an assistant message"
                )
            return
        tool_calls = tuple(
            block for block in self.message.content if isinstance(block, ToolCall)
        )
        if self.stop_reason is StopReason.TOOL_CALL and not tool_calls:
            raise MessageValidationError(
                "tool_call stop reason requires at least one tool call"
            )
        if self.stop_reason is not StopReason.TOOL_CALL and tool_calls:
            raise MessageValidationError(
                f"{self.stop_reason.value} response cannot contain executable tool calls"
            )


class ModelEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    RESPONSE_COMPLETED = "response_completed"


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str
    type: ModelEventType = field(default=ModelEventType.TEXT_DELTA, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise MessageValidationError("text delta must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    tool_call: ToolCall
    type: ModelEventType = field(default=ModelEventType.TOOL_CALL_COMPLETED, init=False)


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    response: FinalResponse
    type: ModelEventType = field(default=ModelEventType.RESPONSE_COMPLETED, init=False)


ModelEvent: TypeAlias = TextDelta | ToolCallCompleted | ResponseCompleted


class Provider(Protocol):
    """Structural interface implemented by concrete model provider adapters."""

    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        """Return an asynchronous stream ending with one ResponseCompleted event."""
        ...


class ProviderError(RuntimeError):
    """Vendor-neutral provider failure surfaced by an adapter."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = _required_text(code, "provider error code")
        self.retryable = retryable
