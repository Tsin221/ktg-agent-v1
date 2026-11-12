"""Ordered, provider-independent lifecycle events for an agent run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from inspect import isawaitable
from ktg_agent.core.messages import JsonObject, MessageValidationError, _json_object, _required_text


class AgentEventType(StrEnum):
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_USAGE_REPORTED = "model_usage_reported"
    MODEL_REQUEST_SETTLED = "model_request_settled"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_UPDATE = "tool_execution_update"
    TOOL_EXECUTION_END = "tool_execution_end"
    INPUT_QUEUED = "input_queued"
    CONTEXT_ASSEMBLED = "context_assembled"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_SUMMARY = "compaction_summary"
    COMPACTION_FAILED = "compaction_failed"
    CONTEXT_OVERFLOW = "context_overflow"
    TASK_REPLAN_CHECKPOINT = "task_replan_checkpoint"


def _integer(payload: JsonObject, key: str, *, positive: bool = False) -> None:
    value = payload.get(key)
    if type(value) is not int or value < (1 if positive else 0):
        raise MessageValidationError(f"{key} must be a {'positive' if positive else 'nonnegative'} integer")


def _text(payload: JsonObject, key: str, *, nullable: bool = False) -> None:
    value = payload.get(key)
    if (value is None and nullable and key in payload) or isinstance(value, str):
        return
    raise MessageValidationError(f"{key} must be {'a string or null' if nullable else 'a string'}")


def _choice(payload: JsonObject, key: str, choices: frozenset[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or value not in choices:
        raise MessageValidationError(f"{key} must be one of {sorted(choices)}")


def _context_assembled(payload: JsonObject) -> None:
    _integer(payload, "estimated_tokens", positive=True)
    _integer(payload, "threshold_tokens")
    _text(payload, "summary_id", nullable=True)
    _integer(payload, "retained_message_count")
    _integer(payload, "spill_count")
    if type(payload.get("trimmed")) is not bool:
        raise MessageValidationError("trimmed must be a boolean")


def _compaction_started(payload: JsonObject) -> None:
    _text(payload, "session_id")
    _choice(payload, "trigger", frozenset({"preflight", "manual"}))
    _text(payload, "parent_run_id", nullable=True)
    _integer(payload, "source_message_count", positive=True)
    _integer(payload, "covers_start_index")
    _integer(payload, "covers_end_index")
    start = payload["covers_start_index"]
    end = payload["covers_end_index"]
    assert type(start) is int and type(end) is int
    if start > end:
        raise MessageValidationError("covers_start_index must not exceed covers_end_index")
    for key in ("covers_start_message_id", "covers_end_message_id"):
        value = payload.get(key)
        if key not in payload or (value is not None and (type(value) is not int or value < 0)):
            raise MessageValidationError(f"{key} must be a nonnegative integer or null")


def _compaction_summary(payload: JsonObject) -> None:
    _text(payload, "summary_id", nullable=True)
    _integer(payload, "source_token_estimate", positive=True)
    _integer(payload, "summary_token_estimate", positive=True)
    _text(payload, "call_id")


def _compaction_failed(payload: JsonObject) -> None:
    _choice(payload, "error_code", frozenset({"COMPACTION_FAILED", "SUMMARY_NOT_SMALLER"}))
    _choice(payload, "stage", frozenset({"summary", "validate", "commit"}))


def _context_overflow(payload: JsonObject) -> None:
    _integer(payload, "estimated_tokens", positive=True)
    _integer(payload, "available_tokens")
    _integer(payload, "largest_group_tokens", positive=True)


_PAYLOAD_VALIDATORS: dict[AgentEventType, Callable[[JsonObject], None]] = {
    AgentEventType.CONTEXT_ASSEMBLED: _context_assembled,
    AgentEventType.COMPACTION_STARTED: _compaction_started,
    AgentEventType.COMPACTION_SUMMARY: _compaction_summary,
    AgentEventType.COMPACTION_FAILED: _compaction_failed,
    AgentEventType.CONTEXT_OVERFLOW: _context_overflow,
}


def _validate_payload(event_type: AgentEventType, payload: JsonObject) -> None:
    validator = _PAYLOAD_VALIDATORS.get(event_type)
    if validator is not None:
        validator(payload)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One event in a run-local, monotonically ordered event stream."""

    run_id: str
    seq: int
    type: AgentEventType
    payload: JsonObject

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run id")
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq <= 0:
            raise ValueError("event sequence must be a positive integer")
        if not isinstance(self.type, AgentEventType):
            raise TypeError("event type must be an AgentEventType")
        object.__setattr__(self, "payload", _json_object(self.payload, "event payload"))
        _validate_payload(self.type, self.payload)


type EventSink = Callable[[AgentEvent], Awaitable[None] | None]


class EventPublisher:
    """Assign sequence numbers, retain events, and notify an optional sink."""

    def __init__(self, run_id: str, sink: EventSink | None = None) -> None:
        self._run_id = _required_text(run_id, "run id")
        self._sink = sink
        self._events: list[AgentEvent] = []

    @property
    def events(self) -> tuple[AgentEvent, ...]:
        return tuple(self._events)

    @property
    def run_id(self) -> str:
        return self._run_id

    async def emit(self, event_type: AgentEventType, payload: JsonObject) -> AgentEvent:
        event = AgentEvent(
            run_id=self._run_id,
            seq=len(self._events) + 1,
            type=event_type,
            payload=payload,
        )
        self._events.append(event)
        if self._sink is not None:
            result = self._sink(event)
            if isawaitable(result):
                await result
        return event


class ToolProgressPublisher:
    """Tool-facing publisher scoped to one tool call."""

    def __init__(
        self,
        publisher: EventPublisher,
        *,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        self._publisher = publisher
        self._tool_call_id = tool_call_id
        self._tool_name = tool_name

    async def publish(
        self, message: str, *, details: JsonObject | None = None
    ) -> AgentEvent:
        payload: JsonObject = {
            "tool_call_id": self._tool_call_id,
            "tool_name": self._tool_name,
            "message": _required_text(message, "tool progress message"),
        }
        if details is not None:
            payload["details"] = details
        return await self._publisher.emit(
            AgentEventType.TOOL_EXECUTION_UPDATE,
            payload,
        )
