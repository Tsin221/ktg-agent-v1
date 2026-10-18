"""Run-local user input queues with explicit consumption boundaries."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from enum import StrEnum
from dataclasses import dataclass

from ktg_agent.core.messages import UserMessage


class InputMode(StrEnum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"


@dataclass(frozen=True, slots=True)
class QueuedInput:
    mode: InputMode
    message: UserMessage
    request_id: str | None = None


class InputQueue:
    """Keep steering and follow-up messages separate while preserving FIFO order."""

    def __init__(self) -> None:
        self._steering: deque[QueuedInput] = deque()
        self._follow_ups: deque[QueuedInput] = deque()
        self.on_consumed: Callable[[str], None] | None = None

    def enqueue(self, mode: InputMode, message: UserMessage, request_id: str | None = None) -> None:
        if not isinstance(mode, InputMode):
            raise TypeError("input mode must be an InputMode")
        if not isinstance(message, UserMessage):
            raise TypeError("queued input must be a UserMessage")
        target = self._steering if mode is InputMode.STEER else self._follow_ups
        target.append(QueuedInput(mode, message, request_id))

    def drain_steering(self) -> tuple[UserMessage, ...]:
        return tuple(item.message for item in self.take_steering())

    def drain_follow_ups(self) -> tuple[UserMessage, ...]:
        return tuple(item.message for item in self.take_follow_ups())

    def take_steering(self) -> tuple[QueuedInput, ...]:
        return self._drain(self._steering)

    def take_follow_ups(self) -> tuple[QueuedInput, ...]:
        return self._drain(self._follow_ups)

    def mark_consumed(self, items: tuple[QueuedInput, ...]) -> None:
        for item in items:
            if item.request_id is not None and self.on_consumed is not None:
                self.on_consumed(item.request_id)

    def clear(self) -> None:
        self._steering.clear()
        self._follow_ups.clear()

    def drain_pending(self) -> tuple[QueuedInput, ...]:
        pending = (*self._steering, *self._follow_ups)
        self.clear()
        return pending

    @property
    def is_empty(self) -> bool:
        return not self._steering and not self._follow_ups

    def _drain(self, queue: deque[QueuedInput]) -> tuple[QueuedInput, ...]:
        items = tuple(queue)
        queue.clear()
        return items
