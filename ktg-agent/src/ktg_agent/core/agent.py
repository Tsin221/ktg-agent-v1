"""Stateful single-run wrapper around the provider-independent agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from inspect import isawaitable
from dataclasses import replace

from ktg_agent.core.events import (
    AgentEvent,
    AgentEventType,
    EventPublisher,
)
from ktg_agent.core.loop import AgentLoop, AgentRunResult
from ktg_agent.core.messages import Message, TextBlock, UserMessage
from ktg_agent.core.queues import InputMode, InputQueue, QueuedInput


class AgentState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"


class AgentBusyError(RuntimeError):
    """Raised when a second ordinary prompt targets an active agent."""

    code = "RUN_BUSY"

    def __init__(self) -> None:
        super().__init__("RUN_BUSY: agent already has an active run")


class AgentNotRunningError(RuntimeError):
    """Raised when queued input targets an agent without an active run."""

    code = "RUN_NOT_ACTIVE"

    def __init__(self) -> None:
        super().__init__("RUN_NOT_ACTIVE: agent has no active run")


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


type AgentListener = Callable[[AgentEvent], Awaitable[None] | None]
type Unsubscribe = Callable[[], None]


class Agent:
    """Own one active run, its cancellation token, input queues, and listeners."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
        self._state = AgentState.IDLE
        self._idle = asyncio.Event()
        self._idle.set()
        self._queue = InputQueue()
        self._cancellation: CancellationToken | None = None
        self._abort_on_start = False
        self._publisher: EventPublisher | None = None
        self._accepting_inputs = False
        self._listeners: list[AgentListener] = []
        self._unconsumed_inputs: tuple[QueuedInput, ...] = ()
        self._on_inputs_closed: Callable[[], None] | None = None
        self._input_consumed_handler: Callable[[str], None] | None = None

    def set_input_consumed_handler(self, handler: Callable[[str], None]) -> None:
        self._input_consumed_handler = handler

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state is AgentState.RUNNING

    @property
    def accepting_inputs(self) -> bool:
        return self._accepting_inputs

    @property
    def unconsumed_inputs(self) -> tuple[QueuedInput, ...]:
        return self._unconsumed_inputs

    def subscribe(self, listener: AgentListener) -> Unsubscribe:
        if not callable(listener):
            raise TypeError("agent listener must be callable")
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    async def prompt(
        self,
        message: str | UserMessage,
        *,
        run_id: str,
        history: tuple[Message, ...] = (),
        identity: object | None = None,
        ktg_client: object | None = None,
        on_inputs_closed: Callable[[], None] | None = None,
        publisher: EventPublisher | None = None,
    ) -> AgentRunResult:
        if self.is_running:
            raise AgentBusyError()
        if publisher is not None and publisher.run_id != run_id:
            raise ValueError("event publisher run id does not match the agent run")

        user_message = self._user_message(message)
        self._state = AgentState.RUNNING
        self._idle.clear()
        self._queue.clear()
        self._queue.on_consumed = self._input_consumed_handler
        self._unconsumed_inputs = ()
        self._cancellation = CancellationToken()
        if self._abort_on_start:
            self._cancellation.cancel()
            self._abort_on_start = False
        self._publisher = publisher if publisher is not None else EventPublisher(run_id, self._dispatch_event)
        self._accepting_inputs = True
        self._on_inputs_closed = on_inputs_closed
        try:
            result = await self._loop.run(
                run_id=run_id,
                messages=(*history, user_message),
                new_input_count=1,
                identity=identity,
                cancellation=self._cancellation,
                ktg_client=ktg_client,
                input_queue=self._queue,
                publisher=self._publisher,
                close_inputs=self.close_inputs,
            )
            self._unconsumed_inputs = self._queue.drain_pending()
            return replace(result, unconsumed_inputs=self._unconsumed_inputs)
        finally:
            self.close_inputs()
            self._on_inputs_closed = None
            self._queue.on_consumed = None
            self._input_consumed_handler = None
            self._unconsumed_inputs = self._queue.drain_pending() or self._unconsumed_inputs
            self._cancellation = None
            self._publisher = None
            self._state = AgentState.IDLE
            self._idle.set()

    async def steer(self, message: str | UserMessage, request_id: str | None = None) -> None:
        await self._enqueue(InputMode.STEER, message, request_id)

    async def follow_up(self, message: str | UserMessage, request_id: str | None = None) -> None:
        await self._enqueue(InputMode.FOLLOW_UP, message, request_id)

    def abort(self) -> bool:
        if self._cancellation is None or not self._accepting_inputs:
            return False
        self._cancellation.cancel()
        self.close_inputs()
        return True

    def abort_or_on_start(self) -> None:
        if self._cancellation is None:
            self._abort_on_start = True
        else:
            self.abort()

    def close_inputs(self) -> None:
        if self._accepting_inputs:
            self._accepting_inputs = False
            if self._on_inputs_closed is not None:
                self._on_inputs_closed()

    async def wait_for_idle(self) -> None:
        await self._idle.wait()

    async def _enqueue(
        self,
        mode: InputMode,
        message: str | UserMessage,
        request_id: str | None,
    ) -> None:
        if not self._accepting_inputs or self._publisher is None:
            raise AgentNotRunningError()
        user_message = self._user_message(message)
        self._queue.enqueue(mode, user_message, request_id)
        if request_id is None:
            await self._publisher.emit(
                AgentEventType.INPUT_QUEUED,
                {"mode": mode.value},
            )

    async def _dispatch_event(self, event: AgentEvent) -> None:
        loop_sink = self._loop.event_sink
        if loop_sink is not None:
            result = loop_sink(event)
            if isawaitable(result):
                await result
        for listener in tuple(self._listeners):
            try:
                result = listener(event)
                if isawaitable(result):
                    await result
            except Exception:
                continue

    @staticmethod
    def _user_message(message: str | UserMessage) -> UserMessage:
        if isinstance(message, UserMessage):
            return message
        if isinstance(message, str):
            return UserMessage((TextBlock(message),))
        raise TypeError("agent input must be text or a UserMessage")
