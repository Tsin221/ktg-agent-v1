"""Durable compaction run and HTTP contract tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import cast

import pytest

from ktg_agent.context.compaction import (PreparedCompaction, SummaryDraft,
                                           SummaryNotSmallerError, prepare_compaction,
                                           validate_summary)
from ktg_agent.context.window import ContextPolicy
from ktg_agent.core.events import EventPublisher, EventSink
from ktg_agent.core.loop import AgentLoop, AgentRunResult
from ktg_agent.core.messages import (AssistantMessage, JsonObject, Message, StopReason,
                                      SystemMessage, TextBlock, UserMessage)
from ktg_agent.providers.base import (FinalResponse, ModelConfig, ModelEvent,
                                       ProviderError, ProviderRequest, RequestStarted,
                                       ResponseCompleted, Usage)
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.ktg import KTGClient
from ktg_agent.server import AgentHTTPServer
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RegisterAgent, RunCoordinator
from ktg_agent.sessions.repository import (RunBusyError, RunConflictError, RunRecord,
                                           SessionRepository)
from ktg_agent.sessions.service import SessionService
from ktg_agent.sessions import coordinator as coordinator_module
from ktg_agent.tools import ToolRegistry, ToolRunner


class FakeClient:
    async def get_current_user(self) -> dict[str, object]:
        return {"userId": 7}


def _prepared() -> PreparedCompaction:
    return PreparedCompaction(
        (SystemMessage((TextBlock("summarize"),)), UserMessage((TextBlock("old history"),))),
        1, 2, 1, 100, 200, 50)


def _turn(text: str) -> ScriptedTurn:
    return ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock(text),)), StopReason.COMPLETED, Usage(12, 3))),))


def _setup(tmp_path: Path, provider: FakeProvider, *, cutpoint: bool = True,
           user_provider: FakeProvider | None = None, configured: bool = True
           ) -> tuple[SessionRepository, SessionService, RunCoordinator, str]:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    prior = repo.create_run("id:7", session.id, "prior", "prior", {})
    repo.settle_run("id:7", prior.id, "completed", "completed", {},
                    (UserMessage((TextBlock("old history"),)),))
    query = QueryService(provider=provider, model_config=ModelConfig("fake", 256),
                         ktg_client_factory=lambda _: cast(KTGClient, FakeClient()),
                         summary_max_tokens=64)

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      event_sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        new = UserMessage((TextBlock(message),))
        if user_provider is not None:
            registry = ToolRegistry(())
            loop = AgentLoop(provider=user_provider, tool_registry=registry,
                             tool_runner=ToolRunner(registry),
                             model_config=ModelConfig("fake", 64),
                             context_policy=ContextPolicy(300, 20),
                             event_sink=event_sink)
            return await loop.run(run_id=run_id,
                                  messages=(SystemMessage((TextBlock("system"),)),
                                            *history, new))
        return AgentRunResult(
            messages=(*history, new), new_messages=(new,),
            final_response=FinalResponse(AssistantMessage((TextBlock("ok"),)),
                                         StopReason.COMPLETED), events=())

    async def compact(run_id: str, prepared: PreparedCompaction, sink: EventSink,
                      publisher: EventPublisher, register_agent: RegisterAgent) -> AgentRunResult:
        return await query.run_compaction(run_id, prepared, event_sink=sink,
                                          publisher=publisher, on_agent_ready=register_agent)

    coordinator = RunCoordinator(
        repo, execute, query.budget_snapshot,
        compaction_planner=lambda frame, page: _prepared() if cutpoint else None,
        compaction_executor=compact,
        system_prompt_builder=lambda page: "system",
        summary_budget_snapshot=query.summary_budget_snapshot)
    service = SessionService(
        repo, lambda _: cast(KTGClient, FakeClient()), coordinator,
        context_policy=ContextPolicy(300, 20) if configured else None)
    return repo, service, coordinator, session.id


def _settled(repo: SessionRepository, run_id: str) -> RunRecord:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        run = repo.get_run("id:7", run_id)
        assert run is not None
        if run.status in {"completed", "failed", "cancelled"}:
            return run
        sleep(0.01)
    pytest.fail("run did not settle")


@contextmanager
def _server(service: SessionService) -> Iterator[AgentHTTPServer]:
    server = AgentHTTPServer(("127.0.0.1", 0), cast(QueryService, object()), service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _post(server: AgentHTTPServer, path: str, body: dict[str, object] | None
          ) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", server.server_port)
    connection.request("POST", path, body=json.dumps(body).encode() if body is not None else None,
                       headers={"Authorization": "Bearer alice", "Content-Type": "application/json"})
    response = connection.getresponse()
    value = json.loads(response.read())
    status = response.status
    connection.close()
    return status, value


def _get(server: AgentHTTPServer, path: str) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", server.server_port)
    connection.request("GET", path, headers={"Authorization": "Bearer alice"})
    response = connection.getresponse()
    value = json.loads(response.read())
    status = response.status
    connection.close()
    return status, value


def test_summary_validation_requires_smaller_text() -> None:
    assert validate_summary(SummaryDraft(" 短 ", 0, 10)).text == "短"
    with pytest.raises(SummaryNotSmallerError):
        validate_summary(SummaryDraft("too long", 0, 1))


def test_prepare_compaction_keeps_group_boundary_and_omits_system() -> None:
    frame: tuple[Message, ...] = (
        SystemMessage((TextBlock("system"),)),
        *(UserMessage((TextBlock("x" * 100),)) for _ in range(10)),
    )
    prepared = prepare_compaction(frame, ContextPolicy(300, 20), tool_schema_tokens=0)
    assert prepared is not None
    assert prepared.covers_start_index == 1
    assert 1 < prepared.covers_end_index < len(frame)
    assert prepared.source_message_count == prepared.covers_end_index - 1
    serialized = cast(TextBlock, prepared.summary_messages[1].content[0]).text
    assert '"role":"user"' in serialized
    assert '"role":"system"' not in serialized


def test_manual_compaction_persists_usage_and_no_business_messages(tmp_path: Path) -> None:
    provider = FakeProvider([_turn("短摘要")])
    repo, service, coordinator, sid = _setup(tmp_path, provider)
    try:
        before = repo.completed_history("id:7", sid)
        with _server(service) as server:
            path = f"/agent-api/v1/sessions/{sid}/compact"
            status, response = _post(server, path, {"request_id": "c1"})
            assert status == 202
            assert _post(server, path, {"request_id": "c1"})[1]["run_id"] == response["run_id"]
            run_id = cast(str, response["run_id"])
            run = _settled(repo, run_id)
            assert run.status == "completed"
            assert run.result_summary is not None
            context = cast(JsonObject, run.result_summary["context"])
            assert context["state"] == "summary_ready"
            assert isinstance(context["summary_id"], str)
            assert repo.active_summary("id:7", sid) is not None
            assert run.budget is not None and run.budget["max_output_tokens"] == 64
            assert run.model_requests[0]["usage_source"] == "provider_reported"
            get_status, snapshot = _get(server, f"/agent-api/v1/runs/{run_id}")
            assert get_status == 200
            assert cast(dict[str, object], snapshot["budget"])["max_output_tokens"] == 64
            assert cast(list[dict[str, object]], snapshot["model_requests"])[0]["usage_source"] == "provider_reported"
            assert repo.completed_history("id:7", sid) == before
            types = [event.type for event in repo.list_events("id:7", run_id).items]
            assert types.index("compaction_started") < types.index("model_request_started")
            assert types.index("model_request_settled") < types.index("compaction_summary")
            assert types.index("compaction_summary") < types.index("agent_end")
            assert types[-1] == "run_settled"
            assert _post(server, path, {})[0] == 400
            assert _post(server, path, {"request_id": "x", "extra": 1})[0] == 400
            assert _post(server, path, None)[0] == 400
    finally:
        coordinator.close()


def test_user_run_reuses_persisted_summary_and_exposes_context(tmp_path: Path) -> None:
    user_provider = FakeProvider([_turn("answer")])
    repo, service, coordinator, sid = _setup(
        tmp_path, FakeProvider([_turn("short summary")]), user_provider=user_provider)
    try:
        compact = repo.admit_run("id:7", sid, "compact", "hash", {"kind": "compaction"},
                                 kind="compaction")[0]
        coordinator.schedule(compact, "", "Bearer test", budget=coordinator.summary_budget_snapshot)
        assert _settled(repo, compact.id).status == "completed"
        active = repo.active_summary("id:7", sid)
        assert active is not None
        user = repo.admit_run("id:7", sid, "user", "hash-2", {})[0]
        coordinator.schedule(user, "next question", "Bearer test")
        assert _settled(repo, user.id).status == "completed"
        request = user_provider.requests[0]
        assert any(isinstance(item, UserMessage) and "<session_summary" in item.content[0].text
                   for item in request.messages)
        saved = SessionRepository(repo.path).get_run("id:7", user.id)
        assert saved is not None and saved.context is not None
        assert saved.context["summary_id"] == active.summary_id
        assert saved.context["covers_end_message_id"] == active.covers_end_message_id
        settled = repo.list_events("id:7", user.id).items[-1].payload
        assert settled["context"] == saved.context
        assert settled["kind"] == "user"
        with _server(service) as server:
            status, public = _get(server, f"/agent-api/v1/runs/{user.id}")
            assert status == 200
            assert public["context"] == saved.context
            assert public["kind"] == "user"
    finally:
        coordinator.close()


def test_larger_summary_fails_and_preflight_user_run_continues(tmp_path: Path) -> None:
    provider = FakeProvider([_turn("this summary is too large to fit")])
    repo, service, coordinator, sid = _setup(
        tmp_path, provider, user_provider=FakeProvider([_turn("ok")]))
    try:
        # Force the source estimate below the response size for this failure path.
        coordinator._compaction_planner = lambda frame, page: PreparedCompaction(
            _prepared().summary_messages, 1, 2, 1, 1, 200, 50)
        response = asyncio.run(service.create_run("Bearer alice", sid, "u1", "next"))
        user_run = _settled(repo, cast(str, response["run_id"]))
        assert user_run.status == "completed"
        with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
            compact_id = db.execute(
                "SELECT id FROM runs WHERE session_id=? AND request_id=?",
                (sid, "compact:u1")).fetchone()[0]
        compact_run = repo.get_run("id:7", compact_id)
        assert compact_run is not None
        assert compact_run.status == "failed"
        assert compact_run.result_summary is not None
        assert compact_run.result_summary["error_code"] == "SUMMARY_NOT_SMALLER"
        assert compact_run.result_summary["stage"] == "validate"
        assert compact_run.ended_at is not None
        assert user_run.created_at >= compact_run.ended_at
        assert len(provider.requests) == 1
        assert "context_assembled" in [event.type for event in
                                       repo.list_events("id:7", user_run.id).items]
    finally:
        coordinator.close()


def test_commit_rejects_not_smaller_summary_as_commit_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, service, coordinator, sid = _setup(tmp_path, FakeProvider([_turn("短摘要")]))

    def reject_commit(*_args: object, **_kwargs: object) -> object:
        raise ValueError("SUMMARY_NOT_SMALLER")

    monkeypatch.setattr(repo, "commit_summary", reject_commit)
    try:
        response = asyncio.run(service.request_compaction("Bearer alice", sid, "commit"))
        run = _settled(repo, cast(str, response["run_id"]))
        assert run.status == "failed"
        assert run.result_summary is not None
        assert run.result_summary["error_code"] == "SUMMARY_NOT_SMALLER"
        assert run.result_summary["stage"] == "commit"
        assert repo.active_summary("id:7", sid) is None
        assert "compaction_summary" not in [event.type for event in
                                            repo.list_events("id:7", run.id).items]
    finally:
        coordinator.close()


def test_manual_compaction_requires_configured_window(tmp_path: Path) -> None:
    repo, service, coordinator, sid = _setup(tmp_path, FakeProvider([]), configured=False)
    try:
        with pytest.raises(ValueError, match="上下文窗口未配置"):
            asyncio.run(service.request_compaction("Bearer alice", sid, "c1"))
        with _server(service) as server:
            status, body = _post(server, f"/agent-api/v1/sessions/{sid}/compact",
                                 {"request_id": "c1"})
        assert status == 400
        assert cast(dict[str, object], body["error"])["code"] == "VALIDATION_ERROR"
        stats = repo.session_stats("id:7", sid)
        assert stats["run_count"] == 1
        assert stats["compaction_count"] == 0
        with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
            assert db.execute(
                "SELECT count(*) FROM runs WHERE session_id=? AND kind='compaction'", (sid,)
            ).fetchone()[0] == 0
            event_types = [row[0] for row in db.execute(
                "SELECT e.type FROM events e JOIN runs r ON r.id=e.run_id WHERE r.session_id=?",
                (sid,),
            )]
        assert not [kind for kind in event_types if kind.startswith("compaction_")]
    finally:
        coordinator.close()


def test_user_request_replay_skips_preflight_compaction(tmp_path: Path) -> None:
    provider = FakeProvider([_turn("short summary")])
    repo, service, coordinator, sid = _setup(tmp_path, provider)

    def varying_planner(frame: tuple[Message, ...], page: JsonObject | None
                        ) -> PreparedCompaction:
        # Preflight identity covers indexes and counts derived from history, so
        # any history growth changes the digest for the same request_id.
        count = len(frame) - 1
        return PreparedCompaction(_prepared().summary_messages, 1, count + 1,
                                  count, 500, 600, 50)

    coordinator._compaction_planner = varying_planner
    try:
        first = asyncio.run(service.create_run("Bearer alice", sid, "u1", "next"))
        user_run = _settled(repo, cast(str, first["run_id"]))
        assert user_run.status == "completed"
        assert len(provider.requests) == 1
        assert repo.active_summary("id:7", sid) is not None
        replay = asyncio.run(service.create_run("Bearer alice", sid, "u1", "next"))
        assert replay == {"run_id": first["run_id"], "status": "completed"}
        # History grew after the first run; a replay must resolve the committed
        # run before re-deriving a preflight compaction digest.
        assert len(provider.requests) == 1
        with pytest.raises(RunConflictError):
            asyncio.run(service.create_run("Bearer alice", sid, "u1", "different"))
        assert len(provider.requests) == 1
        with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
            # Only the fixture's prior run, the first user run and its compaction.
            assert db.execute(
                "SELECT count(*) FROM runs WHERE session_id=? AND kind='user'", (sid,)
            ).fetchone()[0] == 2
            assert db.execute(
                "SELECT count(*) FROM runs WHERE session_id=? AND kind='compaction'", (sid,)
            ).fetchone()[0] == 1
    finally:
        coordinator.close()


def test_preflight_timeout_waits_grace_before_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = Event()
    entered = Event()
    clock: dict[str, float] = {"now": 0.0}

    async def stuck(run_id: str, prepared: PreparedCompaction, sink: EventSink,
                    publisher: EventPublisher, register_agent: RegisterAgent
                    ) -> AgentRunResult:
        entered.set()
        await asyncio.to_thread(release.wait)
        raise ProviderError("UPSTREAM", "cancelled")

    def fake_sleep(seconds: float) -> None:
        # Do not advance the fake clock until the worker is inside the executor,
        # otherwise the run could settle through the early-cancel path.
        assert entered.wait(5)
        clock["now"] += seconds

    repo, service, coordinator, sid = _setup(tmp_path, FakeProvider([]))
    coordinator._compaction_executor = stuck
    coordinator._summary_budget_snapshot = {"max_run_seconds": 0}
    monkeypatch.setattr(coordinator_module, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(coordinator_module, "sleep", fake_sleep)
    try:
        with pytest.raises(RunBusyError):
            asyncio.run(service.create_run("Bearer alice", sid, "u1", "next"))
        # max_run_seconds + 10s preflight budget, then a 5s settle grace.
        assert clock["now"] >= 15
        with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
            compact_id = db.execute(
                "SELECT id FROM runs WHERE request_id='compact:u1'").fetchone()[0]
            assert db.execute(
                "SELECT count(*) FROM runs WHERE request_id='u1'").fetchone()[0] == 0
        release.set()
        assert _settled(repo, compact_id).status == "cancelled"
    finally:
        release.set()
        coordinator.close()


def test_manual_no_cutpoint_is_explicit_failure(tmp_path: Path) -> None:
    repo, service, coordinator, sid = _setup(tmp_path, FakeProvider([]), cutpoint=False)
    try:
        response = asyncio.run(service.request_compaction("Bearer alice", sid, "empty"))
        run = _settled(repo, cast(str, response["run_id"]))
        assert run.status == "failed"
        assert run.result_summary is not None
        assert run.result_summary["error_code"] == "COMPACTION_FAILED"
        assert run.result_summary["stage"] == "validate"
    finally:
        coordinator.close()


def test_provider_failure_is_recorded_once(tmp_path: Path) -> None:
    provider = FakeProvider([ScriptedTurn((ProviderError("UPSTREAM", "offline"),))])
    repo, service, coordinator, sid = _setup(tmp_path, provider)
    try:
        response = asyncio.run(service.request_compaction("Bearer alice", sid, "failure"))
        run = _settled(repo, cast(str, response["run_id"]))
        assert run.status == "failed"
        assert run.result_summary is not None
        assert run.result_summary["error_code"] == "COMPACTION_FAILED"
        assert run.result_summary["stage"] == "summary"
        assert len(provider.requests) == 1
        assert "compaction_failed" in [event.type for event in
                                       repo.list_events("id:7", run.id).items]
    finally:
        coordinator.close()


def test_http_compaction_rejects_active_run(tmp_path: Path) -> None:
    repo, service, coordinator, sid = _setup(tmp_path, FakeProvider([]))
    try:
        repo.create_run("id:7", sid, "busy", "busy", {})
        with _server(service) as server:
            status, body = _post(server, f"/agent-api/v1/sessions/{sid}/compact",
                                 {"request_id": "c1"})
            assert status == 409
            assert cast(dict[str, object], body["error"])["code"] == "RUN_BUSY"
    finally:
        coordinator.close()


class WaitingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.started = Event()
        self.calls = 0

    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        async def events() -> AsyncIterator[ModelEvent]:
            self.calls += 1
            self.started.set()
            yield RequestStarted()
            while request.cancellation is not None and not request.cancellation.is_cancelled:
                await asyncio.sleep(0.01)
            raise ProviderError("UPSTREAM", "cancelled")
            yield RequestStarted()
        return events()


def test_cancel_compaction_records_failure_without_retry(tmp_path: Path) -> None:
    provider = WaitingProvider()
    repo, service, coordinator, sid = _setup(tmp_path, provider)
    try:
        response = asyncio.run(service.request_compaction("Bearer alice", sid, "cancel"))
        assert provider.started.wait(2)
        run_id = cast(str, response["run_id"])
        asyncio.run(service.cancel_run("Bearer alice", run_id, "cancel-1"))
        run = _settled(repo, run_id)
        assert run.status == "cancelled"
        assert run.result_summary is not None
        assert run.result_summary["stage"] == "summary"
        assert provider.calls == 1
        assert "compaction_failed" in [event.type for event in
                                       repo.list_events("id:7", run_id).items]
    finally:
        coordinator.close()
