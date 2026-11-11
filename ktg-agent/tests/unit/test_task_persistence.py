from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from functools import partial
from threading import Barrier
from unittest.mock import Mock

import pytest

from ktg_agent.core.events import AgentEvent, AgentEventType
from ktg_agent.core.messages import (
    AssistantMessage, JsonObject, Message, StopReason, TextBlock, ToolCall, ToolResultMessage,
    message_to_dict,
)
from ktg_agent.ktg.workorders import PreviewStore
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, ToolCallCompleted
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RunCoordinator
from ktg_agent.sessions.repository import SessionRepository, TaskSelectionRequired, TaskVersionConflict
from ktg_agent.sessions.service import SessionService
from ktg_agent.sessions.task_tools import task_run_configuration
from ktg_agent.tools.registry import ToolContext, ToolRegistry, ToolStatus
from ktg_agent.tools.runner import ToolRunner


def step(step_id: str = "stable", description: str = "First", status: str = "pending") -> JsonObject:
    return {"step_id": step_id, "description": description, "status": status}


def setup_run(path: Path) -> tuple[SessionRepository, str, str]:
    repo = SessionRepository(path)
    session = repo.create_session("id:1")
    run = repo.create_run("id:1", session.id, "first", "digest", {})
    repo.update_run_status("id:1", run.id, "running")
    return repo, session.id, run.id


def test_concurrent_writes_keep_one_snapshot_and_report_latest(tmp_path: Path) -> None:
    repo, session, run = setup_run(tmp_path / "tasks.sqlite")
    created = repo.save_task("id:1", session, run, [step()], title="Task")
    task = created["task_id"]
    assert isinstance(task, str)
    barrier = Barrier(2)

    def update(description: str) -> tuple[bool, JsonObject]:
        barrier.wait(timeout=5)
        try:
            return True, repo.save_task("id:1", session, run, [step(description=description)],
                                        task_id=task, expected_version=1)
        except TaskVersionConflict as exc:
            return False, exc.latest

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(update, ["left", "right"]))
    assert sorted(success for success, _ in results) == [False, True]
    assert results[0][1] == results[1][1]
    assert repo.get_task("id:1", session, task, history=True)["versions"] == [results[0][1], created]


def test_rollback_restart_and_cancellation_preserve_full_history(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    repo, session, run = setup_run(path)
    original = repo.save_task("id:1", session, run,
                              [step(description=" First "), step("second")], title="Task")
    task = original["task_id"]
    assert isinstance(task, str)
    assert original["steps"] == [step(), step("second")]
    updated = repo.save_task("id:1", session, run,
                             [step("second"), step(description="Renamed", status="in_progress")],
                             task_id=task, expected_version=1)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER fail_pointer BEFORE UPDATE ON tasks
                      BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        repo.save_task("id:1", session, run, [step(status="completed")],
                       task_id=task, expected_version=2)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        repo.save_task("id:1", session, run, [step()], title="Aborted creation")
    repo.update_run_status("id:1", run, "cancelled")
    with pytest.raises(KeyError):
        repo.save_task("id:1", session, run, [], task_id=task, expected_version=2)
    reopened = SessionRepository(path)
    assert len(reopened.list_tasks("id:1", session)) == 1
    assert reopened.get_task("id:1", session, task, history=True)["versions"] == [updated, original]


def test_scope_and_selected_run_context(tmp_path: Path) -> None:
    repo, session, run = setup_run(tmp_path / "tasks.sqlite")
    selected = repo.save_task("id:1", session, run, [step()], title="Selected")["task_id"]
    other = repo.save_task("id:1", session, run, [step("other")], title="Other")["task_id"]
    assert isinstance(selected, str) and isinstance(other, str)
    separate_session = repo.create_session("id:1").id
    separate_run = repo.create_run("id:1", separate_session, "separate", "digest", {})
    repo.update_run_status("id:1", separate_run.id, "running")
    with pytest.raises(KeyError):
        repo.list_tasks("id:2", session)
    with pytest.raises(KeyError):
        repo.get_task("id:2", session, selected)
    with pytest.raises(KeyError):
        repo.save_task("id:2", session, run, [], task_id=selected, expected_version=1)
    with pytest.raises(KeyError):
        repo.save_task("id:1", separate_session, separate_run.id, [], task_id=selected, expected_version=1)
    repo.update_run_status("id:1", run, "failed")

    async def get_identity() -> JsonObject:
        return {"userId": 1}

    async def other_identity() -> JsonObject:
        return {"userId": 2}

    coordinator = Mock(spec=RunCoordinator)
    client = Mock(get_current_user=get_identity)
    service = SessionService(repo, lambda _: client, coordinator)

    async def continue_task() -> None:
        with pytest.raises(TaskSelectionRequired):
            await service.create_run("authorization", session, "ambiguous", "继续")
        with pytest.raises(KeyError):
            await service.create_run("authorization", separate_session, "wrong", "继续", task_id=selected)
        with pytest.raises(KeyError):
            await SessionService(repo, lambda _: Mock(get_current_user=other_identity), coordinator).create_run(
                    "authorization", session, "unauthorized", "继续", task_id=selected)
        accepted = await service.create_run("authorization", session, "selected", "继续", task_id=selected)
        run_id = accepted["run_id"]
        assert isinstance(run_id, str)
        repo.update_run_status("id:1", run_id, "running")
        identity: JsonObject = {"userId": 1}
        definitions, prompt = task_run_configuration(repo, run_id, identity)
        assert selected in prompt and other not in prompt and "stable" in prompt
        runner = ToolRunner(ToolRegistry(definitions))
        context = ToolContext(run_id, "call", identity=identity)
        for target, status in [(other, ToolStatus.FAILED), (selected, ToolStatus.SUCCEEDED)]:
            result = await runner.run(ToolCall("call", "update_task", {
                "task_id": target, "expected_version": 1, "steps": [step(status="completed")],
            }), context)
            assert result.status == status
        assert repo.get_task("id:1", session, selected)["current_version"] == 2
        assert repo.get_task("id:1", session, other)["current_version"] == 1

    asyncio.run(continue_task())


def test_session_model_tool_is_advertised_and_emits_after_save(tmp_path: Path) -> None:
    repo, session, run = setup_run(tmp_path / "tasks.sqlite")
    identity: JsonObject = {"userId": 1}

    async def get_identity() -> JsonObject:
        return identity

    call = ToolCall("create", "create_task", {"title": "Model task", "steps": [step()]})
    provider = FakeProvider((
        ScriptedTurn((ToolCallCompleted(call), ResponseCompleted(
            FinalResponse(AssistantMessage((call,)), StopReason.TOOL_CALL)))),
        ScriptedTurn((ResponseCompleted(
            FinalResponse(AssistantMessage((TextBlock("Created"),)), StopReason.COMPLETED)),)),
    ))
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda _: Mock(get_current_user=get_identity),
        workorder_store=PreviewStore(),
    )
    saved_versions: list[object] = []

    def observe(event: AgentEvent) -> None:
        if event.type is AgentEventType.TOOL_EXECUTION_END:
            assert event.payload["status"] == "succeeded"
            task = repo.list_tasks("id:1", session)[0]
            assert isinstance(task, dict)
            saved_versions.append(task["current_version"])

    result = asyncio.run(service.run_session(
        run_id=run, message="Plan and verify two activities", history=(),
        authorization="authorization", event_sink=observe,
        task_configuration=partial(task_run_configuration, repo),
    ))
    assert result.stop_reason is StopReason.COMPLETED
    assert {"create_task", "update_task"} <= {tool.name for tool in provider.requests[0].tools}
    assert saved_versions == [1]


@pytest.mark.parametrize("continuation", [
    "继续",
    "继续，用刚才查到的结果，简单告诉我那两张工单的产品各在哪个仓库、还有多少。已有结果就直接用。",
])
def test_continue_recovers_budget_limited_task(tmp_path: Path, continuation: str) -> None:
    repo = SessionRepository(tmp_path / "resume.sqlite")
    session = repo.create_session("id:1").id
    original = repo.create_run("id:1", session, "first", "digest",
                               {"message": "Check workorders and stock"})
    repo.update_run_status("id:1", original.id, "running")
    task_id = repo.save_task("id:1", session, original.id, [step()], title="Check stock")["task_id"]
    assert isinstance(task_id, str)
    transcript: tuple[Message, ...] = (
        AssistantMessage((
            ToolCall("read", "list_workorders", {}),
            ToolCall("skipped", "stock", {"item": "A-01"}),
            ToolCall("unknown", "write_tool", {"item": "B-02"}),
        )),
        ToolResultMessage(
            "read", (TextBlock("Confirmed workorder WO-1"), TextBlock("UI_ONLY", model_visible=False)),
        ),
        ToolResultMessage(
            "skipped", (TextBlock("Tool call was not executed because the run budget was exhausted."),),
            is_error=True,
        ),
    )
    for seq, message in enumerate(transcript, start=1):
        if continuation == "继续":
            repo.append_message("id:1", original.id, message)
        else:
            repo.append_event("id:1", original.id, seq, "message_end", {"message": message_to_dict(message)})
    repo.append_event("id:1", original.id, len(transcript) + 1, "tool_execution_start",
                      {"tool_call_id": "unknown", "effect": "write"})
    repo.update_run_status("id:1", original.id, "failed", stop_reason="limit")
    assert repo.completed_history("id:1", session) == ()

    async def get_identity() -> JsonObject:
        return {"userId": 1}

    async def resume() -> None:
        coordinator = Mock(spec=RunCoordinator)
        client = Mock(get_current_user=get_identity)
        service = SessionService(repo, lambda _: client, coordinator)
        accepted = await service.create_run("authorization", session, "next", continuation)
        run_id = accepted["run_id"]
        assert isinstance(run_id, str)
        run = repo.get_run("id:1", run_id)
        assert run is not None and run.request_summary["task_id"] == task_id
        assert run.request_summary["message"] == continuation
        _, prompt = task_run_configuration(repo, run_id, await get_identity())
        assert "Check workorders and stock" in prompt
        assert "Confirmed workorder WO-1" in prompt
        assert "UI_ONLY" not in prompt
        assert '"status": "not_started"' in prompt
        assert '"status": "outcome_unknown"' in prompt
        assert await service.create_run("authorization", session, "next", continuation) == accepted
        coordinator.schedule.assert_called_once()

    asyncio.run(resume())
