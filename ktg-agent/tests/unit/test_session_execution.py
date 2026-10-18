"""Durable session turns use canonical history and settle only after commit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from threading import Event
import sqlite3
from typing import Any, cast

from ktg_agent.core.events import EventSink
from ktg_agent.core.loop import AgentLoop, AgentRunResult
from ktg_agent.core.messages import (
    AssistantMessage, JsonObject, Message, StopReason, TextBlock, ToolCall, ToolResultMessage,
    UserMessage,
)
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ModelEvent, ProviderError, ProviderRequest, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RunCoordinator, RegisterAgent, RunPhase
from ktg_agent.sessions.repository import RunRecord, SessionRepository
from ktg_agent.tools import (
    ExecutionMode, ToolContext, ToolDefinition, ToolEffect, ToolRegistry,
    ToolResult, ToolRunner,
)


def turn(text: str) -> ScriptedTurn:
    return ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock(text),)), StopReason.COMPLETED, Usage(1, 1),
    )),))


class FakeClient:
    async def get_current_user(self) -> dict[str, object]:
        return {"userId": 7}


type RunCoordinatorExecutor = Callable[
    [str, str, tuple[Message, ...], JsonObject | None, str, EventSink, RegisterAgent], Awaitable[AgentRunResult],
]


def query_executor(provider: FakeProvider) -> RunCoordinatorExecutor:
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda _: cast(KTGClient, FakeClient()),
    )

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_session(
            run_id=run_id, message=message, history=history,
            page_context=page_context, authorization=authorization, event_sink=sink,
            on_agent_ready=register_agent,
        )

    return execute


def test_two_turns_are_isolated_and_reopenable(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    first_session = repo.create_session("id:7")
    other_session = repo.create_session("id:7")
    provider = FakeProvider((turn("first answer"), turn("second answer"), turn("other answer")))
    coordinator = RunCoordinator(repo, query_executor(provider))
    for session, number, prompt in (
        (first_session, 1, "first question"),
        (first_session, 2, "second question"),
        (other_session, 3, "other question"),
    ):
        run, _ = repo.admit_run("id:7", session.id, f"request-{number}", f"hash-{number}", {})
        coordinator.schedule(run, prompt, "Bearer test")
        coordinator.close()
        saved = repo.get_run("id:7", run.id)
        assert saved is not None and saved.status == "completed"
        events = repo.list_events("id:7", run.id).items
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert events[-2].type == "agent_end"
        assert events[-1].type == "run_settled"
        coordinator = RunCoordinator(repo, query_executor(provider))
    coordinator.close()

    assert [item.content[0].text for item in provider.requests[1].messages
            if isinstance(item.content[0], TextBlock)][-3:] == [
                "first question", "first answer", "second question",
            ]
    assert all("first question" not in str(item) for item in provider.requests[2].messages)
    reopened = SessionRepository(path)
    assert [item.message.role.value for item in reopened.list_messages("id:7", first_session.id).items] == [
        "user", "assistant", "user", "assistant",
    ]
    assert reopened.completed_history("id:7", other_session.id)[0].content[0] == TextBlock("other question")


async def audit_tool(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult("public summary", details={"audit": "full private result"})


def test_tool_audit_is_durable_but_projection_uses_summary(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    call = ToolCall("call-1", "test.audit", {})
    provider = FakeProvider((
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((call,)), StopReason.TOOL_CALL, Usage(1, 1),
        )),)),
        turn("tool answer"), turn("follow-up answer"),
    ))
    registry = ToolRegistry((ToolDefinition(
        name="test.audit", version="1.0", description="audit", label="audit",
        input_schema={"type": "object", "properties": {}}, output_schema=None,
        required_permissions=(), effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL, timeout_seconds=1,
        execute=audit_tool,
    ),))

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        loop = AgentLoop(provider=provider, tool_registry=registry,
                         tool_runner=ToolRunner(registry),
                         model_config=ModelConfig("fake", 128), event_sink=sink)
        from ktg_agent.core.messages import UserMessage
        return await loop.run(run_id=run_id, messages=(*history, UserMessage((TextBlock(message),))))

    coordinator = RunCoordinator(repo, execute)
    for number in (1, 2):
        run, _ = repo.admit_run("id:7", session.id, f"r{number}", f"h{number}", {})
        coordinator.schedule(run, f"question {number}", "Bearer test")
        coordinator.close()
        assert repo.get_run("id:7", run.id).status == "completed"  # type: ignore[union-attr]
        coordinator = RunCoordinator(repo, execute)
    coordinator.close()
    stored = repo.list_messages("id:7", session.id).items
    audit = next(item.message for item in stored if isinstance(item.message, ToolResultMessage))
    assert audit.details == {"audit": "full private result"}
    projected = provider.requests[2].messages
    visible = next(item for item in projected if isinstance(item, ToolResultMessage))
    assert visible.content == (TextBlock("public summary"),)
    assert visible.details is None
    assert call in next(item.content for item in projected if isinstance(item, AssistantMessage) and call in item.content)


def test_provider_and_storage_failure_never_complete(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    provider = FakeProvider((ScriptedTurn((ProviderError("UPSTREAM", "provider failed"),)), turn("would complete")))
    coordinator = RunCoordinator(repo, query_executor(provider))
    failed, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    coordinator.schedule(failed, "question", "Bearer test")
    coordinator.close()
    assert repo.get_run("id:7", failed.id).status == "failed"  # type: ignore[union-attr]
    assert repo.list_events("id:7", failed.id).items[-1].payload["status"] == "failed"

    original = repo.settle_run

    def fail_success(owner_user_id: str, run_id: str, status: str, stop_reason: str,
                     result_summary: JsonObject, messages: tuple[Message, ...]) -> RunRecord:
        if status == "completed":
            raise OSError("database unavailable")
        return original(owner_user_id, run_id, status, stop_reason, result_summary, messages)

    repo.settle_run = fail_success  # type: ignore[method-assign]
    next_run, _ = repo.admit_run("id:7", session.id, "r2", "h2", {})
    coordinator = RunCoordinator(repo, query_executor(provider))
    coordinator.schedule(next_run, "question two", "Bearer test")
    coordinator.close()
    saved = repo.get_run("id:7", next_run.id)
    assert saved is not None and saved.status == "failed"
    assert not any(item.run_id == next_run.id for item in repo.list_messages("id:7", session.id).items)


def test_cancelled_provider_response_is_not_completed(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    provider = FakeProvider((ScriptedTurn((ResponseCompleted(FinalResponse(
        None, StopReason.CANCELLED, Usage(0, 0),
    )),)),))
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    coordinator = RunCoordinator(repo, query_executor(provider))
    coordinator.schedule(run, "question", "Bearer test")
    coordinator.close()
    saved = repo.get_run("id:7", run.id)
    assert saved is not None and saved.status == "cancelled"
    assert repo.list_events("id:7", run.id).items[-1].payload["status"] == "cancelled"


def test_settlement_transaction_rolls_back_messages_and_status(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    repo.update_run_status("id:7", run.id, "running")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_settlement BEFORE INSERT ON events "
                   "WHEN NEW.type='run_settled' BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        repo.settle_run("id:7", run.id, "completed", "completed", {"answer": "done"},
                        (UserMessage((TextBlock("question"),)),
                         AssistantMessage((TextBlock("done"),))))
    assert repo.get_run("id:7", run.id).status == "running"  # type: ignore[union-attr]
    assert repo.list_messages("id:7", session.id).items == ()


def test_coordinator_delivers_input_from_different_thread_and_loop(tmp_path: Path) -> None:
    class PausedProvider:
        def __init__(self) -> None:
            self.started = Event()
            self.release = asyncio.Event()
            self.owner_loop: asyncio.AbstractEventLoop | None = None
            self.requests: list[ProviderRequest] = []

        def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
            async def events() -> AsyncIterator[ModelEvent]:
                self.requests.append(request)
                if len(self.requests) == 1:
                    self.owner_loop = asyncio.get_running_loop()
                    self.started.set()
                    await self.release.wait()
                yield ResponseCompleted(FinalResponse(
                    AssistantMessage((TextBlock("answer"),)), StopReason.COMPLETED, Usage(1, 1),
                ))
            return events()

    provider = PausedProvider()
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda _: cast(KTGClient, FakeClient()),
    )

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_session(
            run_id=run_id, message=message, history=history, page_context=page_context,
            authorization=authorization, event_sink=sink, on_agent_ready=register_agent,
        )

    coordinator = RunCoordinator(repo, execute)
    coordinator.schedule(run, "first", "Bearer test")
    assert provider.started.wait(5)
    assert coordinator.phase(run.id) is RunPhase.RUNNING
    accepted = coordinator.admit_input("id:7", run.id, "input-1", "follow_up", "second")
    assert accepted.status == "queued"
    assert provider.owner_loop is not None
    provider.owner_loop.call_soon_threadsafe(provider.release.set)
    coordinator.close()

    assert repo.get_run("id:7", run.id).status == "completed"  # type: ignore[union-attr]
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1] == UserMessage((TextBlock("second"),))
    assert repo.list_inputs("id:7", run.id).items[0].status == "consumed"
    assert [item.type for item in repo.list_events("id:7", run.id).items][-1] == "run_settled"
    assert coordinator.phase(run.id) is None


def test_queued_cancel_settles_without_starting_provider(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "run", "hash", {})
    provider = FakeProvider((turn("must not run"),))
    coordinator = RunCoordinator(repo, query_executor(provider))
    release = Event()
    started = [Event() for _ in range(4)]

    def occupy(signal: Event) -> None:
        signal.set()
        release.wait()

    occupied = [coordinator._workers.submit(occupy, signal) for signal in started]
    try:
        assert all(signal.wait(5) for signal in started)
        coordinator.schedule(run, "question", "Bearer test")
        first = coordinator.admit_cancel("id:7", run.id, "cancel-1")
        second = coordinator.admit_cancel("id:7", run.id, "cancel-1")
        assert first == second
    finally:
        release.set()
        for future in occupied:
            future.result(5)
        coordinator.close()
    assert provider.requests == ()
    assert repo.get_run("id:7", run.id).status == "cancelled"  # type: ignore[union-attr]
    assert [item.type for item in repo.list_events("id:7", run.id).items] == [
        "cancel_requested", "run_settled"]
