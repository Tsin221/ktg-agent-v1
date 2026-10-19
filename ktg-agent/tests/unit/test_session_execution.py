"""Durable session turns use canonical history and settle only after commit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from threading import Event
import sqlite3
import logging
from typing import Any, cast

from ktg_agent.core.events import AgentEvent, AgentEventType, EventSink
from ktg_agent.core.agent import Agent
from ktg_agent.core.loop import AgentLoop, AgentRunResult
from ktg_agent.core.messages import (
    AssistantMessage, JsonObject, Message, StopReason, TextBlock, ToolCall, ToolResultMessage,
    UserMessage,
)
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ModelEvent, ProviderError, ProviderRequest, RequestStarted, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RunCoordinator, RegisterAgent, RunPhase
from ktg_agent.sessions.repository import ControlStateError, RunRecord, SessionRepository
from ktg_agent.tools import (
    ExecutionMode, ToolContext, ToolDefinition, ToolEffect, ToolRegistry,
    ToolResult, ToolRunner, ToolStatus,
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


def test_execution_and_settlement_failures_are_diagnosed_and_recovered(
    tmp_path: Path, caplog: Any,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})

    async def broken_executor(*_: Any) -> AgentRunResult:
        raise RuntimeError("Bearer secret-token message body")

    caplog.set_level(logging.ERROR)
    coordinator = RunCoordinator(repo, broken_executor)
    coordinator.schedule(run, "private question", "Bearer secret-token")
    coordinator.close()
    assert repo.get_run("id:7", run.id).status == "failed"  # type: ignore[union-attr]
    assert f"run_id={run.id} phase=execute error_type=RuntimeError" in caplog.text
    assert "secret-token" not in caplog.text
    assert "private question" not in caplog.text

    failing, _ = repo.admit_run("id:7", session.id, "r2", "h2", {})
    def unavailable(owner_user_id: str, run_id: str, status: str, stop_reason: str,
                    result_summary: JsonObject, messages: tuple[Message, ...]) -> RunRecord:
        raise OSError("Bearer secret-token database unavailable")
    repo.settle_run = unavailable  # type: ignore[method-assign]
    coordinator = RunCoordinator(repo, broken_executor)
    coordinator.schedule(failing, "private question", "Bearer secret-token")
    coordinator.close()
    assert repo.get_run("id:7", failing.id).status == "running"  # type: ignore[union-attr]
    assert f"run_id={failing.id} phase=settle_after_failure error_type=OSError" in caplog.text
    assert "secret-token" not in caplog.text
    reopened = SessionRepository(path)
    assert reopened.recover_interrupted_runs() == 1
    assert reopened.get_run("id:7", failing.id).status == "needs_reconciliation"  # type: ignore[union-attr]


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
                yield RequestStarted()
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


def test_cancel_before_schedule_never_starts_provider(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    repo.admit_control("id:7", run.id, "cancel-1", "cancel", {}, accepting=False)
    provider = FakeProvider((turn("must not run"),))
    coordinator = RunCoordinator(repo, query_executor(provider))
    coordinator.schedule(run, "question", "Bearer test")
    coordinator.close()
    assert provider.requests == ()
    assert repo.get_run("id:7", run.id).status == "cancelled"  # type: ignore[union-attr]


def test_cancel_during_starting_prevents_provider_call(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    provider = FakeProvider((turn("must not run"),))
    delegate = query_executor(provider)
    starting, release = Event(), Event()

    async def paused_executor(run_id: str, message: str, history: tuple[Message, ...],
                              page_context: JsonObject | None, authorization: str,
                              sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        starting.set()
        await asyncio.to_thread(release.wait)
        return await delegate(run_id, message, history, page_context, authorization, sink, register_agent)

    coordinator = RunCoordinator(repo, paused_executor)
    try:
        coordinator.schedule(run, "question", "Bearer test")
        assert starting.wait(5)
        assert coordinator.phase(run.id) is RunPhase.STARTING
        receipt = coordinator.admit_cancel("id:7", run.id, "cancel-1")
        assert receipt.status == "accepted"
    finally:
        release.set()
        coordinator.close()
    assert provider.requests == ()
    assert repo.get_run("id:7", run.id).status == "cancelled"  # type: ignore[union-attr]
    assert repo.list_events("id:7", run.id).items[-1].type == "run_settled"


def test_cancel_waits_for_started_write_and_preserves_outcome(tmp_path: Path) -> None:
    for outcome in (ToolStatus.SUCCEEDED, ToolStatus.FAILED, ToolStatus.UNKNOWN):
        started, release = Event(), Event()
        writes: list[str] = []

        async def fake_write(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
            writes.append(context.tool_call_id)
            started.set()
            await asyncio.to_thread(release.wait)
            return ToolResult(
                "write checked", details={"outcome": outcome.value, "operation_id": "op-1"},
                status=outcome,
                error_code=None if outcome is ToolStatus.SUCCEEDED else "WRITE_FAILED" if outcome is ToolStatus.FAILED else "RESULT_UNKNOWN",
                operation_id="op-1", verification={"checked": True},
            )

        call = ToolCall("write-1", "test.write", {})
        provider = FakeProvider((
            ScriptedTurn((ResponseCompleted(FinalResponse(
                AssistantMessage((call,)), StopReason.TOOL_CALL, Usage(1, 1),
            )),)),
            turn("must not continue"),
        ))
        registry = ToolRegistry((ToolDefinition(
            name="test.write", version="1.0", description="write", label="write",
            input_schema={"type": "object", "properties": {}}, output_schema=None,
            required_permissions=(), effect=ToolEffect.WRITE,
            execution_mode=ExecutionMode.SEQUENTIAL, timeout_seconds=0.01,
            execute=fake_write,
        ),))
        repo = SessionRepository(tmp_path / f"{outcome.value}.sqlite3")
        session = repo.create_session("id:7")
        run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})

        async def execute(run_id: str, message: str, history: tuple[Message, ...],
                          page_context: JsonObject | None, authorization: str,
                          sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
            loop = AgentLoop(provider=provider, tool_registry=registry,
                             tool_runner=ToolRunner(registry),
                             model_config=ModelConfig("fake", 128), event_sink=sink)
            agent = Agent(loop)
            close_inputs = register_agent(agent)
            return await agent.prompt(message, run_id=run_id, history=history,
                                      on_inputs_closed=close_inputs)

        coordinator = RunCoordinator(repo, execute)
        try:
            coordinator.schedule(run, "write", "Bearer test")
            assert started.wait(5)
            receipt = coordinator.admit_cancel("id:7", run.id, "cancel-1")
            assert receipt.status == "accepted"
            assert repo.get_run("id:7", run.id).status == "cancelling"  # type: ignore[union-attr]
            release.set()
        finally:
            release.set()
            coordinator.close()
        saved = repo.get_run("id:7", run.id)
        assert saved is not None and saved.result_summary is not None
        assert saved.status == ("needs_reconciliation" if outcome is ToolStatus.UNKNOWN else "cancelled")
        steps = saved.result_summary["steps"]
        assert isinstance(steps, dict)
        bucket = "completed" if outcome is ToolStatus.SUCCEEDED else "failed" if outcome is ToolStatus.FAILED else "unknown"
        assert steps[bucket] == [{
            "tool_call_id": "write-1", "tool_name": "test.write", "operation_id": "op-1",
            "verification": {"checked": True},
            **({} if outcome is ToolStatus.SUCCEEDED else
               {"error_code": "WRITE_FAILED" if outcome is ToolStatus.FAILED else "RESULT_UNKNOWN"}),
            "result": {"outcome": outcome.value, "operation_id": "op-1"},
        }]
        assert writes == ["write-1"]
        assert len(provider.requests) == 1
        assert repo.list_events("id:7", run.id).items[-1].type == "run_settled"


def test_cancel_and_completion_commit_order(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    before, _ = repo.admit_run("id:7", session.id, "before", "h1", {})
    repo.update_run_status("id:7", before.id, "running")
    repo.admit_control("id:7", before.id, "cancel", "cancel", {}, accepting=False)
    settled = repo.settle_run("id:7", before.id, "completed", "completed",
                              {"answer": "model finished", "steps": {"completed": [], "failed": [], "unknown": []}}, ())
    assert settled.status == "cancelled"
    assert settled.result_summary is not None and settled.result_summary["stop_reason"] == "cancelled"

    after, _ = repo.admit_run("id:7", session.id, "after", "h2", {})
    repo.update_run_status("id:7", after.id, "running")
    repo.settle_run("id:7", after.id, "completed", "completed", {"answer": "done"}, ())
    before_seq = repo.last_event_seq("id:7", after.id)
    no_op, created = repo.admit_control("id:7", after.id, "late", "cancel", {}, accepting=False)
    assert created and no_op.reason == "already_settled"
    assert repo.last_event_seq("id:7", after.id) == before_seq
    assert repo.get_run("id:7", after.id).status == "completed"  # type: ignore[union-attr]


def test_cancel_during_closing_preserves_verified_write_result(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    closing, release = Event(), Event()

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        agent = Agent(AgentLoop(provider=FakeProvider(()), tool_registry=ToolRegistry(()),
                                tool_runner=ToolRunner(ToolRegistry(())),
                                model_config=ModelConfig("fake", 128)))
        close_inputs = register_agent(agent)
        sink(AgentEvent(run_id, 1, AgentEventType.TOOL_EXECUTION_START,
                        {"tool_call_id": "write-1", "tool_name": "test.write", "effect": "write"}))
        sink(AgentEvent(run_id, 2, AgentEventType.TOOL_EXECUTION_END,
                        {"tool_call_id": "write-1", "effect": "write", "status": "succeeded",
                         "operation_id": "op-1", "verification": {"checked": True}}))
        close_inputs()
        closing.set()
        await asyncio.to_thread(release.wait)
        final = AssistantMessage((TextBlock("completed action"),))
        return AgentRunResult((UserMessage((TextBlock(message),)), final),
                              FinalResponse(final, StopReason.COMPLETED, Usage(1, 1)), ())

    coordinator = RunCoordinator(repo, execute)
    try:
        coordinator.schedule(run, "write", "Bearer test")
        assert closing.wait(5)
        assert coordinator.phase(run.id) is RunPhase.CLOSING
        receipt = coordinator.admit_cancel("id:7", run.id, "cancel-1")
        assert receipt.status == "accepted"
    finally:
        release.set()
        coordinator.close()
    saved = repo.get_run("id:7", run.id)
    assert saved is not None and saved.status == "cancelled"
    assert saved.result_summary is not None
    steps = saved.result_summary["steps"]
    assert isinstance(steps, dict)
    completed = steps["completed"]
    assert isinstance(completed, list) and isinstance(completed[0], dict)
    assert completed[0]["operation_id"] == "op-1"
    events = repo.list_events("id:7", run.id).items
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type == "run_settled"


def test_input_accepted_before_cancel_is_resolved_and_later_input_is_rejected(
    tmp_path: Path,
) -> None:
    import pytest

    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    repo.update_run_status("id:7", run.id, "running")
    accepted, _ = repo.admit_control(
        "id:7", run.id, "input-1", "follow_up",
        {"mode": "follow_up", "message": "next"}, accepting=True,
    )
    assert accepted.status == "queued"
    repo.admit_control("id:7", run.id, "cancel-1", "cancel", {}, accepting=False)
    with pytest.raises(ControlStateError, match="RUN_NOT_ACTIVE"):
        repo.admit_control(
            "id:7", run.id, "input-2", "follow_up",
            {"mode": "follow_up", "message": "later"}, accepting=True,
        )
    repo.settle_run("id:7", run.id, "cancelled", "cancelled", {}, ())
    inputs = repo.list_inputs("id:7", run.id).items
    assert [(item.request_id, item.status, item.reason) for item in inputs] == [
        ("input-1", "not_consumed", "cancelled")]
