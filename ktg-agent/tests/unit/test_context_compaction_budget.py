"""The summary request itself must fit the model window."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from time import monotonic, sleep
from typing import cast

from ktg_agent.context.compaction import PreparedCompaction, prepare_compaction
from ktg_agent.context.tokens import estimate_messages_tokens
from ktg_agent.context.window import ContextPolicy, fit_window
from ktg_agent.core.events import EventPublisher, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import (AssistantMessage, JsonObject, Message, StopReason,
                                      SystemMessage, TextBlock, UserMessage)
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import (FinalResponse, ModelConfig, ResponseCompleted,
                                       Usage)
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.sessions.coordinator import RegisterAgent, RunCoordinator
from ktg_agent.sessions.repository import RunRecord, SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.service import QueryService


class FakeClient:
    async def get_current_user(self) -> dict[str, object]:
        return {"userId": 7}


def _turn(text: str) -> ScriptedTurn:
    return ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock(text),)), StopReason.COMPLETED, Usage(12, 3))),))


def _long_history(count: int = 100) -> tuple[UserMessage, ...]:
    return tuple(UserMessage((TextBlock("历史" * 40),)) for _ in range(count))


def test_prepare_compaction_shrinks_coverage_until_the_request_fits() -> None:
    frame: tuple[Message, ...] = (
        SystemMessage((TextBlock("system"),)),
        *_long_history(),
    )
    policy = ContextPolicy(8192, 1024)
    plan = fit_window(frame, policy, tool_schema_tokens=0)
    assert plan.trimmed is True
    assert plan.kept_start_index > 1

    prepared = prepare_compaction(frame, policy, tool_schema_tokens=0)
    assert prepared is not None
    # Coverage still starts at the oldest history message, but ends earlier than
    # the raw window cut so the serialized source fits the summary request.
    assert prepared.covers_start_index == 1
    assert prepared.covers_end_index < plan.kept_start_index
    assert prepared.source_message_count == prepared.covers_end_index - 1
    assert prepared.source_message_count < plan.kept_start_index - 1
    assert estimate_messages_tokens(prepared.summary_messages) <= (
        policy.window_tokens - policy.reserve_tokens)


def test_prepare_compaction_returns_none_when_one_message_cannot_fit() -> None:
    policy = ContextPolicy(2048, 128)
    frame: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("x" * 7000),)),
        UserMessage((TextBlock("next"),)),
    )
    # The user window can trim the old message, but serializing it as the one
    # remaining summary source still exceeds the summary request budget.
    plan = fit_window(frame, policy, tool_schema_tokens=0)
    assert plan.trimmed is True
    assert plan.kept_start_index == 2
    assert prepare_compaction(frame, policy, tool_schema_tokens=0) is None


def _settled(repo: SessionRepository, run_id: str) -> RunRecord:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        run = repo.get_run("id:7", run_id)
        assert run is not None
        if run.status in {"completed", "failed", "cancelled"}:
            return run
        sleep(0.01)
    raise AssertionError("run did not settle")


def test_preflight_compaction_of_a_long_session_completes(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    prior = repo.create_run("id:7", session.id, "prior", "prior", {})
    repo.settle_run("id:7", prior.id, "completed", "completed", {}, _long_history())

    query = QueryService(
        provider=FakeProvider([_turn("短摘要")]),
        model_config=ModelConfig("fake", 256),
        ktg_client_factory=lambda _: cast(KTGClient, FakeClient()),
        context_policy=ContextPolicy(8192, 1024),
        summary_max_tokens=64,
    )

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        new = UserMessage((TextBlock(message),))
        return AgentRunResult(
            messages=(*history, new), new_messages=(new,),
            final_response=FinalResponse(AssistantMessage((TextBlock("ok"),)),
                                         StopReason.COMPLETED), events=())

    async def compact(run_id: str, prepared: PreparedCompaction, sink: EventSink,
                      publisher: EventPublisher,
                      register_agent: RegisterAgent) -> AgentRunResult:
        return await query.run_compaction(
            run_id, prepared, event_sink=sink, publisher=publisher,
            on_agent_ready=register_agent)

    coordinator = RunCoordinator(
        repo, execute, query.budget_snapshot,
        compaction_planner=query.plan_compaction,
        compaction_executor=compact,
        system_prompt_builder=query.build_system_prompt,
        summary_budget_snapshot=query.summary_budget_snapshot)
    service = SessionService(
        repo, lambda _: cast(KTGClient, FakeClient()), coordinator,
        context_policy=query.context_policy,
        system_prompt_builder=query.build_system_prompt,
        tool_schemas=query.model_tool_schemas)
    try:
        response = asyncio.run(service.create_run("Bearer alice", session.id, "u1", "next"))
        user_run = _settled(repo, cast(str, response["run_id"]))
        assert user_run.status == "completed"
        with sqlite3.connect(repo.path) as db:
            row = db.execute(
                "SELECT id FROM runs WHERE session_id=? AND request_id=?",
                (session.id, "compact:u1")).fetchone()
        assert row is not None
        compact_run = repo.get_run("id:7", row[0])
        assert compact_run is not None
        assert compact_run.status == "completed"
        assert compact_run.result_summary is not None
        context = cast(JsonObject, compact_run.result_summary["context"])
        assert context["state"] == "summary_ready"
        assert repo.active_summary("id:7", session.id) is not None
    finally:
        coordinator.close()
