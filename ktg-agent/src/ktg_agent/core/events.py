"""Ordered, provider-independent lifecycle events for an agent run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from inspect import isawaitable
from ktg_agent.core.messages import JsonObject, _json_object, _required_text


class AgentEventType(StrEnum):
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_UPDATE = "tool_execution_update"
    TOOL_EXECUTION_END = "tool_execution_end"
    INPUT_QUEUED = "input_queued"


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
