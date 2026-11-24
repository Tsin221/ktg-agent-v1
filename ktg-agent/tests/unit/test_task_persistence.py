from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from functools import partial
from threading import Barrier
from unittest.mock import Mock

import pytest

from ktg_agent.context.window import ContextPolicy
from ktg_agent.core.events import AgentEvent, AgentEventType
from ktg_agent.core.loop import RunLimits
from ktg_agent.core.messages import (
    AssistantMessage, JsonObject, JsonValue, Message, StopReason, SystemMessage, TextBlock, ToolCall, ToolResultMessage, UserMessage,
    message_to_dict,
)
from ktg_agent.core.task_budget import TaskBudget
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, ToolCallCompleted
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RunCoordinator
from ktg_agent.sessions.repository import SessionRepository, TaskSelectionRequired, TaskVersionConflict
from ktg_agent.sessions.service import SessionService
from ktg_agent.sessions.task_tools import _read_evidence, task_run_configuration
from ktg_agent.tools.registry import ToolContext, ToolDefinition, ToolRegistry, ToolResult, ToolStatus
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
        definitions, prompt, budget = task_run_configuration(repo, run_id, identity)
        assert selected not in prompt and other not in prompt
        assert budget.task_id == selected and budget.steps == (("stable", "pending"),)
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
    assert result.stop_reason is StopReason.LIMIT
    assert result.limit_reason == "task_incomplete"
    assert {"create_task", "update_task"} <= {tool.name for tool in provider.requests[0].tools}
    assert saved_versions == [1]
    first, second = [system_text(request.messages) for request in provider.requests]
    assert '"current_step_id"' not in first
    assert second.count('"current_step_id": "stable"') == 1
    assert '"version": 1' in second


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
        _, _, budget = task_run_configuration(repo, run_id, await get_identity())
        assert budget.resume_request is not None
        assert budget.evidence is not None
        preferred, fallback = budget.evidence(task_id, (), (), "stable", frozenset())
        prompt = json.dumps([budget.resume_request, preferred, fallback])
        assert "Check workorders and stock" in prompt
        assert "Confirmed workorder WO-1" in prompt
        assert "UI_ONLY" not in prompt
        assert '"status": "not_started"' in prompt
        assert '"status": "outcome_unknown"' in prompt
        assert await service.create_run("authorization", session, "next", continuation) == accepted
        coordinator.schedule.assert_called_once()

    asyncio.run(resume())


def test_step_budget_reallocates_unused_calls_after_saving_progress() -> None:
    budget = TaskBudget()
    budget.set_task("task", [step("first", status="in_progress"), step("second"), step("third")])
    first_calls = tuple(ToolCall(str(i), "query", {}) for i in range(5))
    assert budget.admit(first_calls, 6) == (first_calls[:4], {"4": "TASK_STEP_LIMIT"})
    assert budget.admit((first_calls[4],), 6) == ((), {"4": "TASK_STEP_LIMIT"})
    budget.set_task("task", [step("first", status="completed"), step("second", status="in_progress"),
                             step("third", status="completed")])
    next_calls = (ToolCall("5", "query", {}), ToolCall("6", "query", {}))
    assert budget.admit(next_calls, 6) == (next_calls, {})


def test_failed_save_refunds_reserved_slot_and_eight_steps_can_checkpoint() -> None:
    budget = TaskBudget()
    budget.set_task("task", [step(str(i)) for i in range(8)])
    invalid = ToolCall("invalid", "update_task", {})
    assert budget.admit((invalid,), 30) == ((invalid,), {})
    budget.settle_save(invalid, False)
    assert budget.saves == 0
    for i in range(9):
        call = ToolCall(str(i), "update_task", {})
        assert budget.admit((call,), 30) == ((call,), {})
        budget.settle_save(call, True)
    assert budget.saves_exhausted
    reserved = ToolCall("reserved", "update_task", {"task_id": "task"})
    assert budget.admit((reserved,), 30) == ((), {"reserved": "TASK_SAVE_LIMIT"})
    budget.closeout_turns = 1
    assert budget.admit((reserved,), 30) == ((reserved,), {})


def test_eight_step_task_saves_each_step_after_invalid_update(tmp_path: Path) -> None:
    repo, session, run = setup_run(tmp_path / "eight.sqlite")
    created = repo.save_task("id:1", session, run, [step(str(i)) for i in range(8)], title="Eight")
    task_id = created["task_id"]
    calls = [ToolCall("invalid", "update_task", {
        "task_id": task_id, "expected_version": 1,
        "steps": [{"description": "Missing id", "status": "completed"}],
    })]
    for i in range(8):
        calls.append(ToolCall(str(i), "update_task", {
            "task_id": task_id, "expected_version": i + 1,
            "steps": [step(str(j), status="completed" if j <= i else "pending") for j in range(8)],
        }))
    provider = FakeProvider((
        *(ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((call,)), StopReason.TOOL_CALL,
        )),)) for call in calls),
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((TextBlock("Saved all eight"),)), StopReason.COMPLETED,
        )),)),
    ))

    async def get_identity() -> JsonObject:
        return {"userId": 1}

    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        limits=RunLimits(max_model_turns=20),
        ktg_client_factory=lambda _: Mock(get_current_user=get_identity),
    )
    result = asyncio.run(service.run_session(
        run_id=run, message="Verify and save each step", history=(), authorization="authorization",
        event_sink=lambda _: None, task_configuration=partial(task_run_configuration, repo),
    ))
    assert result.stop_reason is StopReason.COMPLETED
    saved = repo.get_task("id:1", session, str(task_id), history=True)
    assert saved["current_version"] == 9
    versions = saved["versions"]
    assert isinstance(versions, list) and len(versions) == 9
    assert repo.unfinished_tasks("id:1", session) == []


@pytest.mark.parametrize("limits", [
    RunLimits(max_model_turns=8, max_tool_calls=12),
    RunLimits(max_model_turns=8, max_tool_calls=2),
])
@pytest.mark.parametrize("finished", [True, False])
@pytest.mark.parametrize("save_exhausted", [False, True])
def test_task_can_save_and_answer_after_work_budget_runs_out(
    tmp_path: Path, limits: RunLimits, finished: bool, save_exhausted: bool,
) -> None:
    repo, session, previous = setup_run(tmp_path / "closeout.sqlite")
    task_id = repo.save_task("id:1", session, previous, [step(status="in_progress")], title="Stock")["task_id"]
    assert isinstance(task_id, str)
    repo.update_run_status("id:1", previous, "failed")
    run = repo.create_run("id:1", session, "resume", "digest", {"task_id": task_id})
    repo.update_run_status("id:1", run.id, "running")

    async def get_identity() -> JsonObject:
        return {"userId": 1}

    def tool_turn(*calls: ToolCall) -> ScriptedTurn:
        return ScriptedTurn((ResponseCompleted(FinalResponse(AssistantMessage(calls), StopReason.TOOL_CALL)),))

    work_turns = 0 if save_exhausted else min(limits.max_model_turns, limits.max_tool_calls)
    work_calls = tuple(ToolCall(f"work-{i}", "search_tools", {"query": "stock"}) for i in range(work_turns))
    provider = FakeProvider((
        *(tool_turn(call) for call in work_calls),
        tool_turn(ToolCall("save", "update_task", {
            "task_id": task_id, "expected_version": 1,
            "steps": [step(status="completed" if finished else "in_progress")],
        }), ToolCall("blocked", "search_tools", {"query": "extra"})),
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((TextBlock("Saved and summarized"),)), StopReason.COMPLETED,
        )),)),
    ))
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128), limits=limits,
        ktg_client_factory=lambda _: Mock(get_current_user=get_identity),
    )
    def configure(run_id: str, identity: JsonObject) -> tuple[tuple[ToolDefinition, ...], str, TaskBudget]:
        tools, prompt, budget = task_run_configuration(repo, run_id, identity)
        if save_exhausted:
            budget.saves = budget.save_limit - 1
        return tools, prompt, budget

    result = asyncio.run(service.run_session(
        run_id=run.id, message="继续", history=(), authorization="authorization",
        event_sink=lambda _: None, task_configuration=configure,
    ))
    assert result.stop_reason is (StopReason.COMPLETED if finished else StopReason.LIMIT)
    expected_limit = ("task_save_limit" if save_exhausted else
                      "max_tool_calls" if work_turns < limits.max_model_turns else "max_model_turns")
    assert result.limit_reason == (None if finished else expected_limit)
    assert result.final_message == AssistantMessage((TextBlock("Saved and summarized"),))
    assert [tool.name for tool in provider.requests[work_turns].tools] == ["update_task"]
    assert provider.requests[work_turns + 1].tools == ()
    assert system_text(provider.requests[work_turns].messages).count('"version": 1') == 1
    assert system_text(provider.requests[work_turns + 1].messages).count('"version": 2') == 1
    assert repo.get_task("id:1", session, task_id)["current_version"] == 2
    assert [event.payload["tool_call_id"] for event in result.events
            if event.type is AgentEventType.TOOL_EXECUTION_START] == [*(call.id for call in work_calls), "save"]


def system_text(messages: tuple[Message, ...]) -> str:
    return "\n".join(block.text for message in messages if isinstance(message, SystemMessage)
                     for block in message.content)


def test_resume_keeps_early_step_evidence_and_user_input_under_window(tmp_path: Path) -> None:
    repo, session, original = setup_run(tmp_path / "evidence.sqlite")
    task_id = repo.save_task("id:1", session, original, [
        step("first", status="in_progress"), step("next"),
    ], title="Product and stock")["task_id"]
    assert isinstance(task_id, str)
    history: list[Message] = []
    records: list[tuple[ToolCall, JsonObject | None, str, str | None]] = [
        (ToolCall("early", "ktg_count_products", {}),
         {"kind": "product_count", "total": 17, "queried_at": "now"},
         "COUNT-EARLY", "first"),
        (ToolCall("stock", "ktg_query_stock_summary", {"item_code": "PRODUCT-OTHER"}),
         {"kind": "stock_summary", "filters": {"item_code": "PRODUCT-OTHER", "warehouse_id": None},
          "queried_at": "before", "no_records": False,
          "groups": [{"warehouseName": "ZERO-WH", "quantityOnhand": "0", "unitOfMeasure": "pcs"}]},
         "ZERO-WH " * 1000, "next"),
        (ToolCall("unrelated", "unrelated_read", {}), None, "unrelated " * 3500, None),
    ]
    for seq, (call, details, content, step_id) in enumerate(records, start=1):
        pair = (AssistantMessage((call,)), ToolResultMessage(call.id, (TextBlock(content),), details=details))
        history.extend(pair)
        for entry in pair:
            repo.append_message("id:1", original, entry)
        repo.append_event("id:1", original, seq, "tool_execution_start", {
            "tool_call_id": call.id, "effect": "read",
            **({"task_id": task_id, "step_id": step_id, "task_version": seq} if step_id else {}),
        })
    repo.save_task("id:1", session, original, [
        step("first", status="completed"), step("next", status="in_progress"),
    ], task_id=task_id, expected_version=1)
    repo.update_run_status("id:1", original, "cancelled")
    run = repo.create_run("id:1", session, "resume", "digest", {"task_id": task_id})
    repo.update_run_status("id:1", run.id, "running")

    async def get_identity() -> JsonObject:
        return {"userId": 1}

    provider = FakeProvider((ScriptedTurn((ResponseCompleted(
        FinalResponse(AssistantMessage((TextBlock("Summary"),)), StopReason.COMPLETED),
    ),)),))
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        context_policy=ContextPolicy(5500, 128),
        ktg_client_factory=lambda _: Mock(get_current_user=get_identity),
    )
    result = asyncio.run(service.run_session(
        run_id=run.id, message="Resume current stock check", history=tuple(history),
        authorization="authorization", event_sink=lambda _: None,
        task_configuration=partial(task_run_configuration, repo),
    ))
    assert result.stop_reason is StopReason.LIMIT, result.error_code
    assert result.limit_reason == "task_incomplete"
    messages = provider.requests[0].messages
    system = system_text(messages)
    assert system.count('"current_step_id": "next"') == 1
    assert system.count('"version": 2') == 1
    assert system.index('"tool_call_id": "stock"') < system.index('"tool_call_id": "early"')
    assert '"quantityOnhand"' not in system
    assert '"stock_usage": "historical_only"' in system
    assert '"item_code": "PRODUCT-OTHER"' in system
    assert '"total": 17' in system
    assert UserMessage((TextBlock("Resume current stock check"),)) in messages
    assert not any(isinstance(m, ToolResultMessage) and m.tool_call_id == "unrelated" for m in messages)
    for message in messages:
        if isinstance(message, ToolResultMessage):
            assert message.content[0].text not in {"COUNT-EARLY", "ZERO-WH " * 1000}
    assert result.messages[1:1 + len(history)] == tuple(history)


@pytest.mark.parametrize("groups", [[], [{"warehouseName": "WH", "quantityOnhand": "0"}]])
def test_stock_summary_evidence_distinguishes_zero_and_no_records(groups: list[JsonValue]) -> None:
    details: JsonObject = {
        "kind": "stock_summary",
        "filters": {"item_code": "PRODUCT", "warehouse_id": 17},
        "queried_at": "now",
        "no_records": not groups,
        "groups": groups,
    }
    evidence = _read_evidence("ktg_query_stock_summary", details,
                              {"item_code": "PRODUCT", "warehouse_id": 17})
    assert evidence is not None
    assert evidence["filters"] == {"item_code": "PRODUCT", "warehouse_id": 17}
    assert evidence["queried_at"] == "now"
    assert evidence["no_records"] is (not groups)
    assert evidence["groups"] == groups


def test_workorder_evidence_preserves_scope_and_unavailable_progress() -> None:
    details: JsonObject = {
        "kind": "workorder_overview", "filters": {"unfinished_only": True},
        "queried_at": "now", "page": 1, "page_size": 10, "coverage": "partial", "total": None,
        "rows": [{"workorderId": "123", "progress_state": "unavailable", "progress": None}],
        "warnings": ["部分进度待核实"],
    }
    evidence = _read_evidence("ktg_query_workorder_overview", details, {})
    assert evidence is not None
    assert evidence == {key: value for key, value in details.items() if key != "kind"}


def test_material_evidence_preserves_assessment_scope_and_missing_records() -> None:
    details: JsonObject = {
        "kind": "workorder_materials", "filters": {"workorder_codes": ["WO-A", "WO-B"]},
        "queried_at": "now", "coverage": "partial", "missing_codes": ["WO-B"],
        "orders": [{"workorderId": "123", "assessment": "unavailable", "material_scope": "line_side"}],
        "warnings": ["部分材料库存待核实"],
    }
    evidence = _read_evidence("ktg_query_workorder_materials", details, {})
    assert evidence is not None
    assert evidence == {key: value for key, value in details.items() if key != "kind"}


def test_quality_evidence_preserves_history_scope_and_unknown_results() -> None:
    details: JsonObject = {
        "kind": "workorder_quality", "filters": {"workorder_codes": ["WO-A"], "sn_codes": ["SN-1"]},
        "queried_at": "now", "coverage": "partial", "missing_codes": [],
        "orders": [{"workorderId": "123", "history_coverage": "partial", "summary": None,
                    "units": [{"snCode": "SN-1", "history_state": "unavailable", "ever_rejected": None}]}],
        "warnings": ["检验历史待核实"],
    }
    evidence = _read_evidence("ktg_query_workorder_quality", details, {})
    assert evidence is not None
    assert evidence == {key: value for key, value in details.items() if key != "kind"}


def test_task_product_and_stock_keeps_tool_activation_and_links_evidence(tmp_path: Path) -> None:
    repo, _, run = setup_run(tmp_path / "chain.sqlite")
    requests: list[tuple[str, Mapping[str, object]]] = []

    async def transport(url: str, _headers: Mapping[str, str], params: Mapping[str, object],
                        _timeout_seconds: float) -> dict[str, object]:
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": 1}, "permissions": ["*:*:*"]}
        requests.append((url, params))
        if url.endswith("/mditem/list"):
            return {"code": 200, "total": 17, "rows": []}
        assert url.endswith("/wmstock/summary")
        return {"code": 200, "data": {
            "itemCode": "STOCK-17", "warehouseId": None,
            "hasRecords": True, "queriedAt": "2026-11-14T00:00:00Z",
            "groups": [{"itemId": 17, "itemCode": "STOCK-17", "warehouseId": 3,
                        "warehouseCode": "MAIN-WH", "warehouseName": "MAIN-WH",
                        "quantityOnhand": "0", "unitOfMeasure": "pcs", "sourceRowCount": 1}],
        }}

    calls = (
        ToolCall("create", "create_task", {"title": "Check products and stock", "steps": [step(status="in_progress")]}),
        ToolCall("load", "load_tools", {"tools": [
            {"name": "ktg_count_products", "version": "1.0"},
            {"name": "ktg_query_stock_summary", "version": "1.0"},
        ]}),
        ToolCall("product_count", "ktg_count_products", {}),
        ToolCall("stock", "ktg_query_stock_summary", {"item_code": "STOCK-17"}),
    )
    provider = FakeProvider((
        *(ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((call,)), StopReason.TOOL_CALL,
        )),)) for call in calls),
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((TextBlock("MAIN-WH: 0 pcs"),)), StopReason.COMPLETED,
        )),)),
    ))
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda token: KTGClient(
            base_url="http://mes.test", authorization=token, transport=transport,
        ),
    )
    result = asyncio.run(service.run_session(
        run_id=run, message="Check products and STOCK-17 stock", history=(), authorization="Bearer test",
        event_sink=lambda _: None, task_configuration=partial(task_run_configuration, repo),
    ))
    assert result.stop_reason is StopReason.LIMIT
    assert result.limit_reason == "task_incomplete"
    business_tools = {"ktg_count_products", "ktg_query_stock_summary"}
    assert not business_tools.intersection(tool.name for tool in provider.requests[1].tools)
    assert business_tools <= {tool.name for tool in provider.requests[2].tools}
    assert requests[0][0].endswith("/mditem/list")
    assert requests[1][0].endswith("/wmstock/summary")
    assert requests[1][1]["itemCode"] == "STOCK-17"
    assert len(requests) == 2
    assert '"total": 17' in system_text(provider.requests[3].messages)
    final = system_text(provider.requests[4].messages)
    assert '"warehouseName": "MAIN-WH"' in final and '"quantityOnhand": "0"' in final
    starts = [event.payload for event in result.events if event.type is AgentEventType.TOOL_EXECUTION_START]
    assert [payload.get("step_id") for payload in starts] == [None, None, "stable", "stable"]


@pytest.mark.parametrize(("status", "queried_code", "warehouse_id", "save_allowed", "old_completed"), [
    ("succeeded", "A-01", None, True, False),
    ("failed", "A-01", None, False, False),
    ("succeeded", "OTHER", None, False, False),
    ("succeeded", "A-01", 1, False, False),
    ("not_started", "A-01", None, False, False),
    ("not_started", "A-01", None, True, True),
])
def test_resumed_stock_summary_step_requires_fresh_matching_evidence(
    tmp_path: Path, status: str, queried_code: str, warehouse_id: int | None, save_allowed: bool,
    old_completed: bool,
) -> None:
    repo = SessionRepository(tmp_path / "resumed-stock.sqlite")
    session = repo.create_session("id:1").id
    original = repo.create_run("id:1", session, "start", "digest", {"message": "核对工单的分仓库存"}).id
    repo.update_run_status("id:1", original, "running")
    step_status = "completed" if old_completed else "in_progress"
    task_id = repo.save_task("id:1", session, original, [step(status=step_status)], title="Order")["task_id"]
    assert isinstance(task_id, str)
    old_call = ToolCall("old", "ktg_query_stock_summary", {"item_code": "A-01"})
    repo.append_message("id:1", original, AssistantMessage((old_call,)))
    repo.append_message("id:1", original, ToolResultMessage(
        old_call.id, (TextBlock("old quantity"),),
        details={"kind": "stock_summary", "filters": {"item_code": "A-01", "warehouse_id": None},
                 "queried_at": "before", "no_records": True, "groups": []},
    ))
    repo.append_event("id:1", original, 1, "tool_execution_start", {
        "tool_call_id": old_call.id, "task_id": task_id, "step_id": "stable",
    })
    repo.update_run_status("id:1", original, "cancelled")
    resumed = repo.create_run("id:1", session, "resume", "digest", {"task_id": task_id, "message": "沿用已经核对过的结果"}).id
    repo.update_run_status("id:1", resumed, "running")

    tools, _, budget = task_run_configuration(repo, resumed, {"userId": 1})
    assert budget.evidence is not None
    save = next(tool for tool in tools if tool.name == "update_task")
    arguments: JsonObject = {"item_code": queried_code}
    if warehouse_id is not None:
        arguments["warehouse_id"] = warehouse_id
    call = ToolCall("fresh", "ktg_query_stock_summary", arguments)
    data: JsonObject = {
        "kind": "stock_summary", "filters": {"item_code": queried_code, "warehouse_id": warehouse_id},
        "queried_at": "2026-11-14T00:00:00Z", "no_records": True, "groups": [],
    }
    messages = (
        AssistantMessage((call,)),
        ToolResultMessage(call.id, (TextBlock(status),), is_error=status != "succeeded", details=data),
    )
    events = (
        AgentEvent(resumed, 1, AgentEventType.TOOL_EXECUTION_START, {
            "tool_call_id": call.id, "task_id": task_id, "step_id": "stable",
        }),
        AgentEvent(resumed, 2, AgentEventType.TOOL_EXECUTION_END, {"tool_call_id": call.id, "status": status}),
    )
    completed = frozenset({"stable"} if old_completed else ())
    preferred, fallback = budget.evidence(task_id, messages, events, None if old_completed else "stable", completed)
    old = next(record for record in (*preferred, *fallback) if record["tool_call_id"] == old_call.id)
    assert old["stock_usage"] == ("reuse_only_when_user_explicitly_requests_original_result"
                                  if old_completed else "historical_only")
    assert ("evidence" in old) is old_completed
    assert "result" not in old
    async def execute() -> ToolResult:
        return await save.execute(ToolContext(resumed, "save", identity={"userId": 1}), {
            "task_id": task_id, "expected_version": 1, "steps": [step(status="completed")],
        })

    saved = asyncio.run(execute())
    assert (saved.status is ToolStatus.SUCCEEDED) is save_allowed
    assert repo.get_task("id:1", session, task_id)["current_version"] == (2 if save_allowed else 1)
    if not save_allowed:
        assert saved.error_code == "STOCK_SUMMARY_UNVERIFIED"


@pytest.mark.parametrize("successful", [True, False])
def test_redirected_stock_step_uses_a_new_scope_after_replan(tmp_path: Path, successful: bool) -> None:
    repo, session, run = setup_run(tmp_path / "redirected-stock.sqlite")
    task_id = repo.save_task("id:1", session, run, [step(status="in_progress")], title="Stock")["task_id"]
    assert isinstance(task_id, str)
    tools, _, budget = task_run_configuration(repo, run, {"userId": 1})
    budget.set_task(task_id, [step(status="in_progress")], version=1)
    assert budget.evidence is not None
    save = next(tool for tool in tools if tool.name == "update_task")

    def stock_result(call_id: str, step_id: str, code: str, succeeded: bool) -> tuple[
        tuple[Message, ...], tuple[AgentEvent, ...]
    ]:
        call = ToolCall(call_id, "ktg_query_stock_summary", {"item_code": code})
        messages: tuple[Message, ...] = (AssistantMessage((call,)), ToolResultMessage(
            call_id, (TextBlock("returned"),), is_error=not succeeded, details={
                "kind": "stock_summary", "filters": {"item_code": code, "warehouse_id": None},
                "queried_at": "2026-11-14T00:00:00Z", "no_records": True, "groups": [],
            } if succeeded else None,
        ))
        events = (AgentEvent(run, 1, AgentEventType.TOOL_EXECUTION_START, {
            "tool_call_id": call_id, "task_id": task_id, "step_id": step_id,
        }),)
        return messages, events

    old_messages, old_events = stock_result("old", "stable", "A-01", False)
    budget.evidence(task_id, old_messages, old_events, "stable", frozenset())

    async def update(version: int, status: str) -> ToolResult:
        return await save.execute(ToolContext(run, "save", identity={"userId": 1}), {
            "task_id": task_id, "expected_version": version,
            "steps": [step("replaced", description="Check B-02", status=status)],
        })

    assert asyncio.run(update(1, "in_progress")).status is ToolStatus.SUCCEEDED
    new_messages, new_events = stock_result("new", "replaced", "B-02", successful)
    budget.evidence(task_id, (*old_messages, *new_messages), (*old_events, *new_events), "replaced", frozenset())
    result = asyncio.run(update(2, "completed"))
    assert result.error_code == (None if successful else "STOCK_SUMMARY_UNVERIFIED")
    assert repo.get_task("id:1", session, task_id)["current_version"] == (3 if successful else 2)
