"""Window assembly keeps canonical history intact and emits durable events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from ktg_agent.context.window import (
    ContextOverflowError, ContextPolicy, fit_window, group_messages, threshold_tokens,
)
from ktg_agent.core.events import AgentEventType, EventSink
from ktg_agent.core.loop import AgentLoop, AgentRunResult
from ktg_agent.core.messages import (
    AssistantMessage, JsonObject, Message, MessageValidationError, StopReason,
    SystemMessage, TextBlock, ToolCall, ToolResultMessage, UserMessage,
    project_model_messages,
)
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.server import Settings, create_service
from ktg_agent.sessions.coordinator import RegisterAgent, RunCoordinator
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.tools import ToolRegistry, ToolRunner


def _turn() -> ScriptedTurn:
    return ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock("done"),)), StopReason.COMPLETED, Usage(1, 1),
    )),))


def _tool_pair(call_id: str, *, spilled: bool = False) -> tuple[Message, Message]:
    return (
        AssistantMessage((ToolCall(call_id, "t", {}),)),
        ToolResultMessage(call_id, (TextBlock("x"),),
                          details={"spill": {"path": "saved"}} if spilled else None),
    )


@pytest.mark.parametrize(("values", "field"), [
    ({"window_tokens": 0, "reserve_tokens": 1}, "window_tokens"),
    ({"window_tokens": -1, "reserve_tokens": 1}, "window_tokens"),
    ({"window_tokens": 10, "reserve_tokens": 10}, "reserve_tokens"),
    ({"window_tokens": 10, "reserve_tokens": 1, "threshold_ratio": 0}, "threshold_ratio"),
    ({"window_tokens": 10, "reserve_tokens": 1, "threshold_ratio": 1.1}, "threshold_ratio"),
    ({"window_tokens": 10, "reserve_tokens": 1, "retain_ratio": 0.8}, "retain_ratio"),
])
def test_invalid_policy(values: dict[str, int | float], field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ContextPolicy(**values)  # type: ignore[arg-type]


def test_groups_keep_all_tool_results_together() -> None:
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        AssistantMessage((ToolCall("a", "t", {}), ToolCall("b", "t", {}))),
        ToolResultMessage("b", (TextBlock("b"),)),
        ToolResultMessage("a", (TextBlock("a"),)),
        UserMessage((TextBlock("next"),)),
    )
    groups = group_messages(messages)
    assert [(group.start_index, len(group.messages), group.is_system)
            for group in groups] == [(0, 1, True), (1, 3, False), (4, 1, False)]


@pytest.mark.parametrize(("messages", "error"), [
    ((ToolResultMessage("orphan", (TextBlock("x"),)),), "unknown tool call id"),
    ((AssistantMessage((ToolCall("a", "t", {}),)), UserMessage((TextBlock("x"),))),
     "missing tool results"),
    ((UserMessage((TextBlock("x"),)), SystemMessage((TextBlock("s"),))),
     "system message must be leading"),
])
def test_invalid_groups(messages: tuple[Message, ...], error: str) -> None:
    with pytest.raises(MessageValidationError, match=error):
        group_messages(messages)


def test_fit_trims_old_whole_groups_and_counts_only_retained_spills() -> None:
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("abcdefgh"),)),
        UserMessage((TextBlock("abcdefgh"),)),
        *_tool_pair("a", spilled=True),
        *_tool_pair("b"),
        *_tool_pair("c", spilled=True),
    )
    projected = project_model_messages(messages)
    flags = tuple(isinstance(message, ToolResultMessage)
                  and message.details is not None and "spill" in message.details
                  for message in messages)
    plan = fit_window(projected, ContextPolicy(64, 8, retain_ratio=0.25),
                      tool_schema_tokens=0, spill_flags=flags)
    assert plan.messages == projected[:1] + projected[7:]
    assert (plan.trimmed, plan.trimmed_group_count, plan.kept_start_index) == (True, 4, 7)
    assert (plan.estimated_before, plan.estimated_after) == (53, 17)
    assert (plan.retained_message_count, plan.retained_group_count, plan.spill_count) == (3, 2, 1)
    assert project_model_messages(plan.messages) == plan.messages


def test_single_tool_group_overflows_with_diagnostics() -> None:
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        AssistantMessage((ToolCall("a", "t", {}),)),
        ToolResultMessage("a", (TextBlock("x" * 100),)),
    )
    with pytest.raises(ContextOverflowError) as caught:
        fit_window(messages, ContextPolicy(40, 8), tool_schema_tokens=0)
    assert caught.value.available_tokens == 32
    assert caught.value.largest_group_tokens == 36
    assert caught.value.estimated_tokens > caught.value.available_tokens


def test_old_oversized_group_is_trimmed_instead_of_overflowing() -> None:
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("x" * 800),)),
        UserMessage((TextBlock("next"),)),
    )
    plan = fit_window(messages, ContextPolicy(100, 10), tool_schema_tokens=0)
    assert plan.trimmed is True
    assert plan.kept_start_index == 2
    assert plan.messages == (messages[0], messages[2])
    assert plan.estimated_after <= plan.available_tokens


def test_overflow_diagnostics_ignore_trimmed_old_groups() -> None:
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("x" * 4000),)),
        UserMessage((TextBlock("y" * 800),)),
    )
    with pytest.raises(ContextOverflowError) as caught:
        fit_window(messages, ContextPolicy(100, 10), tool_schema_tokens=0)
    assert caught.value.estimated_tokens == 209
    assert caught.value.largest_group_tokens == 204


def test_hard_budget_trims_even_below_the_soft_threshold() -> None:
    policy = ContextPolicy(2048, 1024)
    assert threshold_tokens(policy) > policy.window_tokens - policy.reserve_tokens
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("你" * 519),)),
        UserMessage((TextBlock("你" * 519),)),
    )
    plan = fit_window(messages, policy, tool_schema_tokens=0)
    assert plan.trimmed is True
    assert plan.estimated_before <= plan.threshold_tokens
    assert plan.estimated_before > plan.available_tokens
    assert plan.kept_start_index == 2
    assert plan.messages == (messages[0], messages[2])
    assert plan.estimated_after <= plan.available_tokens


def test_service_rejects_window_smaller_than_default_output_reserve() -> None:
    settings = Settings("key", "http://localhost", "fake", "http://localhost",
                        context_window=100)
    with pytest.raises(RuntimeError, match="reserve_tokens"):
        create_service(settings)


def test_unconfigured_loop_preserves_projection_and_events() -> None:
    provider = FakeProvider((_turn(),))
    registry = ToolRegistry(())
    messages: tuple[Message, ...] = (
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("question"), TextBlock("hidden", False))),
    )
    result = asyncio.run(AgentLoop(
        provider=provider, tool_registry=registry, tool_runner=ToolRunner(registry),
        model_config=ModelConfig("fake", 128),
    ).run(run_id="plain", messages=messages))
    assert provider.requests[0].messages == project_model_messages(messages)
    assert AgentEventType.CONTEXT_ASSEMBLED not in [event.type for event in result.events]


def test_loop_overflow_emits_before_failure_without_starting_turn() -> None:
    provider = FakeProvider(())
    registry = ToolRegistry(())
    result = asyncio.run(AgentLoop(
        provider=provider, tool_registry=registry, tool_runner=ToolRunner(registry),
        model_config=ModelConfig("fake", 128), context_policy=ContextPolicy(20, 5),
    ).run(run_id="overflow", messages=(
        SystemMessage((TextBlock("s"),)),
        UserMessage((TextBlock("x" * 100),)),
    )))
    assert [event.type for event in result.events] == [
        AgentEventType.AGENT_START, AgentEventType.CONTEXT_OVERFLOW,
        AgentEventType.AGENT_END,
    ]
    assert result.error_code == "CONTEXT_OVERFLOW"
    assert result.stop_reason is StopReason.ERROR
    assert provider.requests == ()


class _Client:
    async def get_current_user(self) -> dict[str, object]:
        return {"userId": 7}


def test_coordinator_persists_assembly_before_turn_and_settles_last(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    prior = repo.create_run("id:7", session.id, "prior", "prior", {})
    repo.settle_run("id:7", prior.id, "completed", "completed", {},
                    tuple(UserMessage((TextBlock("x" * 500),)) for _ in range(20)))
    run, _ = repo.admit_run("id:7", session.id, "r1", "h1", {})
    provider = FakeProvider((_turn(),))
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda _: cast(KTGClient, _Client()),
        context_policy=ContextPolicy(4096, 128),
    )

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_session(
            run_id=run_id, message=message, history=history,
            page_context=page_context, authorization=authorization,
            event_sink=sink, on_agent_ready=register_agent,
        )

    coordinator = RunCoordinator(repo, execute)
    coordinator.schedule(run, "question", "Bearer test")
    coordinator.close()
    events = repo.list_events("id:7", run.id).items
    seq = {event.type: event.seq for event in events}
    assert seq["context_assembled"] < seq["turn_start"] < seq["model_request_started"]
    assert events[-1].type == "run_settled"
    assembled = next(event.payload for event in events if event.type == "context_assembled")
    assert set(assembled) == {
        "estimated_tokens", "threshold_tokens", "summary_id",
        "retained_message_count", "spill_count", "trimmed", "summary_absent",
        "covers_start_message_id", "covers_end_message_id",
    }
    assert assembled["summary_id"] is None
    saved = repo.get_run("id:7", run.id)
    assert saved is not None and saved.context is not None
    assert saved.context["retained_message_count"] == assembled["retained_message_count"]
    assert cast(int, assembled["retained_message_count"]) < 20
