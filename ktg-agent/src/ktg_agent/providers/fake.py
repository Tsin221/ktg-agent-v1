"""Deterministic, script-driven provider for agent tests."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import TypeAlias

from ktg_agent.providers.base import (
    ModelEvent,
    ProviderError,
    ProviderRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)


ScriptAction: TypeAlias = ModelEvent | ProviderError


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One provider invocation, ending in a response or raised provider error."""

    actions: tuple[ScriptAction, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.actions, tuple):
            raise TypeError("scripted turn actions must be a tuple")
        terminal_types = (ResponseCompleted, ProviderError)
        if not self.actions or not isinstance(self.actions[-1], terminal_types):
            raise ValueError(
                "scripted turn requires one terminal action at the end"
            )
        if any(isinstance(action, terminal_types) for action in self.actions[:-1]):
            raise ValueError(
                "scripted turn requires one terminal action at the end"
            )
        event_types = (TextDelta, ToolCallCompleted, ResponseCompleted, ProviderError)
        if not all(isinstance(action, event_types) for action in self.actions):
            raise TypeError("scripted turn contains an unsupported action")


class FakeProvider:
    """Replay predefined model turns and retain every received request."""

    def __init__(self, script: Iterable[ScriptedTurn]) -> None:
        self._script = deque(script)
        self._requests: list[ProviderRequest] = []

    @property
    def requests(self) -> tuple[ProviderRequest, ...]:
        """Requests received so far, in invocation order."""
        return tuple(self._requests)

    @property
    def remaining_turns(self) -> int:
        """Number of scripted provider invocations not yet consumed."""
        return len(self._script)

    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        async def events() -> AsyncIterator[ModelEvent]:
            self._requests.append(request)
            if not self._script:
                completed_requests = len(self._requests) - 1
                raise AssertionError(
                    "FakeProvider script exhausted after "
                    f"{completed_requests} request(s)"
                )

            turn = self._script.popleft()
            for action in turn.actions:
                if isinstance(action, ProviderError):
                    raise action
                yield action

        return events()
