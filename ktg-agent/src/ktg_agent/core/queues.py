"""Run-local user input queues with explicit consumption boundaries."""

from __future__ import annotations

from collections import deque
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


class InputQueue:
    """Keep steering and follow-up messages separate while preserving FIFO order."""

    def __init__(self) -> None:
        self._steering: deque[UserMessage] = deque()
        self._follow_ups: deque[UserMessage] = deque()

    def enqueue(self, mode: InputMode, message: UserMessage) -> None:
        if not isinstance(mode, InputMode):
            raise TypeError("input mode must be an InputMode")
        if not isinstance(message, UserMessage):
            raise TypeError("queued input must be a UserMessage")
        target = self._steering if mode is InputMode.STEER else self._follow_ups
        target.append(message)

    def drain_steering(self) -> tuple[UserMessage, ...]:
        return self._drain(self._steering)

    def drain_follow_ups(self) -> tuple[UserMessage, ...]:
        return self._drain(self._follow_ups)

    def clear(self) -> None:
        self._steering.clear()
        self._follow_ups.clear()

    def drain_pending(self) -> tuple[QueuedInput, ...]:
        pending = (
            *(QueuedInput(InputMode.STEER, item) for item in self.drain_steering()),
            *(QueuedInput(InputMode.FOLLOW_UP, item) for item in self.drain_follow_ups()),
        )
        return pending

    @property
    def is_empty(self) -> bool:
        return not self._steering and not self._follow_ups

    @staticmethod
    def _drain(queue: deque[UserMessage]) -> tuple[UserMessage, ...]:
        messages = tuple(queue)
        queue.clear()
        return messages
