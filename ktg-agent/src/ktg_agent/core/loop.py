"""Minimal sequential model/tool loop with ordered lifecycle events."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ktg_agent.core.events import (
    AgentEvent,
    AgentEventType,
    EventPublisher,
    EventSink,
    ToolProgressPublisher,
)
from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    StopReason,
    ToolCall,
    message_to_dict,
    project_model_messages,
)
from ktg_agent.providers.base import (
    CancellationSignal,
    FinalResponse,
    ModelConfig,
    Provider,
    ProviderRequest,
    ResponseCompleted,
    TextDelta,
)
from ktg_agent.tools import ToolContext, ToolRegistry, ToolRunner


class AgentLoopError(RuntimeError):
    """Raised when a provider violates the streaming response contract."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Final response, complete run-local history, and emitted event trace."""

    messages: tuple[Message, ...]
    final_response: FinalResponse
    events: tuple[AgentEvent, ...]

    @property
    def stop_reason(self) -> StopReason:
        return self.final_response.stop_reason

    @property
    def final_message(self) -> AssistantMessage | None:
        return self.final_response.message


class AgentLoop:
    """Call a provider and execute complete tool calls until the model stops."""

    def __init__(
        self,
        *,
        provider: Provider,
        tool_registry: ToolRegistry,
        tool_runner: ToolRunner,
        model_config: ModelConfig,
        event_sink: EventSink | None = None,
    ) -> None:
        self._provider = provider
        self._tool_registry = tool_registry
        self._tool_runner = tool_runner
        self._model_config = model_config
        self._event_sink = event_sink

    async def run(
        self,
        *,
        run_id: str,
        messages: Sequence[Message],
        identity: object | None = None,
        cancellation: CancellationSignal | None = None,
        ktg_client: object | None = None,
    ) -> AgentRunResult:
        history = list(messages)
        if not history:
            raise ValueError("agent run requires at least one message")

        publisher = EventPublisher(run_id, self._event_sink)
        await publisher.emit(
            AgentEventType.AGENT_START,
            {"message_count": len(history)},
        )
        agent_status = "failed"
        try:
            turn = 0
            while True:
                turn += 1
                response = await self._run_model_turn(
                    publisher=publisher,
                    turn=turn,
                    history=history,
                    cancellation=cancellation,
                )

                if response.message is not None:
                    history.append(response.message)

                if response.stop_reason is StopReason.TOOL_CALL:
                    if response.message is None:  # guarded by FinalResponse
                        raise AgentLoopError("tool response is missing its message")
                    calls = tuple(
                        block
                        for block in response.message.content
                        if isinstance(block, ToolCall)
                    )
                    for call in calls:
                        await publisher.emit(
                            AgentEventType.TOOL_EXECUTION_START,
                            {
                                "turn": turn,
                                "tool_call_id": call.id,
                                "tool_name": call.name,
                            },
                        )
                        progress = ToolProgressPublisher(
                            publisher,
                            tool_call_id=call.id,
                            tool_name=call.name,
                        )
                        try:
                            result = await self._tool_runner.run(
                                call,
                                ToolContext(
                                    run_id=run_id,
                                    tool_call_id=call.id,
                                    identity=identity,
                                    cancellation=cancellation,
                                    progress_publisher=progress,
                                    ktg_client=ktg_client,
                                ),
                            )
                        except BaseException as exc:
                            await publisher.emit(
                                AgentEventType.TOOL_EXECUTION_END,
                                {
                                    "turn": turn,
                                    "tool_call_id": call.id,
                                    "tool_name": call.name,
                                    "status": "failed",
                                    "error_type": type(exc).__name__,
                                },
                            )
                            await publisher.emit(
                                AgentEventType.TURN_END,
                                {"turn": turn, "status": "failed"},
                            )
                            raise
                        end_payload: JsonObject = {
                            "turn": turn,
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "status": result.status.value,
                        }
                        if result.error_code is not None:
                            end_payload["error_code"] = result.error_code
                        await publisher.emit(
                            AgentEventType.TOOL_EXECUTION_END,
                            end_payload,
                        )
                        tool_message = result.to_message(call.id)
                        history.append(tool_message)
                        tool_message_id = f"{run_id}:tool-result:{call.id}"
                        await publisher.emit(
                            AgentEventType.MESSAGE_START,
                            {
                                "turn": turn,
                                "message_id": tool_message_id,
                                "role": "tool",
                                "tool_call_id": call.id,
                            },
                        )
                        await publisher.emit(
                            AgentEventType.MESSAGE_END,
                            {
                                "turn": turn,
                                "message_id": tool_message_id,
                                "message": message_to_dict(tool_message),
                            },
                        )

                    await publisher.emit(
                        AgentEventType.TURN_END,
                        {
                            "turn": turn,
                            "stop_reason": response.stop_reason.value,
                        },
                    )
                    continue

                await publisher.emit(
                    AgentEventType.TURN_END,
                    {
                        "turn": turn,
                        "stop_reason": response.stop_reason.value,
                    },
                )
                agent_status = self._agent_status(response.stop_reason)
                await self._emit_agent_end_once(
                    publisher,
                    {
                        "status": agent_status,
                        "stop_reason": response.stop_reason.value,
                        "turns": turn,
                    },
                )
                return AgentRunResult(
                    messages=tuple(history),
                    final_response=response,
                    events=publisher.events,
                )
        except BaseException as exc:
            await self._emit_agent_end_once(
                publisher,
                {
                    "status": agent_status,
                    "error_type": type(exc).__name__,
                    "turns": turn,
                },
            )
            raise

    async def _run_model_turn(
        self,
        *,
        publisher: EventPublisher,
        turn: int,
        history: list[Message],
        cancellation: CancellationSignal | None,
    ) -> FinalResponse:
        await publisher.emit(AgentEventType.TURN_START, {"turn": turn})
        message_id = f"{publisher.events[0].run_id}:message:{turn}"
        await publisher.emit(
            AgentEventType.MESSAGE_START,
            {"turn": turn, "message_id": message_id, "role": "assistant"},
        )

        response: FinalResponse | None = None
        try:
            request = ProviderRequest(
                messages=project_model_messages(history),
                tools=self._tool_registry.model_schemas(),
                config=self._model_config,
                cancellation=cancellation,
            )
            async for event in self._provider.stream(request):
                if isinstance(event, TextDelta):
                    await publisher.emit(
                        AgentEventType.MESSAGE_UPDATE,
                        {
                            "turn": turn,
                            "message_id": message_id,
                            "delta_type": "text",
                            "text": event.text,
                        },
                    )
                elif isinstance(event, ResponseCompleted):
                    if response is not None:
                        raise AgentLoopError(
                            "provider emitted more than one completed response"
                        )
                    response = event.response
        except BaseException:
            await publisher.emit(
                AgentEventType.MESSAGE_END,
                {"turn": turn, "message_id": message_id, "status": "failed"},
            )
            await publisher.emit(
                AgentEventType.TURN_END,
                {"turn": turn, "status": "failed"},
            )
            raise

        if response is None:
            await publisher.emit(
                AgentEventType.MESSAGE_END,
                {"turn": turn, "message_id": message_id, "status": "failed"},
            )
            await publisher.emit(
                AgentEventType.TURN_END,
                {"turn": turn, "status": "failed"},
            )
            raise AgentLoopError("provider stream ended without a completed response")

        end_payload: JsonObject = {
            "turn": turn,
            "message_id": message_id,
            "stop_reason": response.stop_reason.value,
        }
        if response.message is not None:
            end_payload["message"] = message_to_dict(response.message)
        await publisher.emit(AgentEventType.MESSAGE_END, end_payload)
        return response

    @staticmethod
    def _agent_status(stop_reason: StopReason) -> str:
        if stop_reason is StopReason.CANCELLED:
            return "cancelled"
        if stop_reason is StopReason.ERROR:
            return "failed"
        return "completed"

    @staticmethod
    async def _emit_agent_end_once(
        publisher: EventPublisher, payload: JsonObject
    ) -> None:
        if any(
            event.type is AgentEventType.AGENT_END for event in publisher.events
        ):
            return
        await publisher.emit(AgentEventType.AGENT_END, payload)
