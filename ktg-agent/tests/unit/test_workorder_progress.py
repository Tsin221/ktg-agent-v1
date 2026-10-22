"""MES-07 independent synthetic oracle against the experimental view."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from ktg_agent.ktg.client import KTGClient
from ktg_agent.core.messages import ToolCall
from ktg_agent.ktg.workorder_progress import (
    MAX_REQUESTS, build_workorder_progress_tool, workorder_progress_factories,
    workorder_progress_manifests,
)
from ktg_agent.tools.catalog import CatalogIdentity, CatalogSwitches, ToolCatalog
from ktg_agent.tools.permissions import PermissionAll
from ktg_agent.tools.registry import ToolContext, ToolRegistry, ToolResult, ToolStatus
from ktg_agent.tools.runner import CallAuthorization, ToolRunner

CASES = json.loads((Path(__file__).parents[1] / "fixtures/mes_progress/cases.json").read_text(encoding="utf-8"))["cases"]


def _rows(source: Mapping[str, object], workorder_id: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    tasks = [{"taskId": task["id"], "workorderId": workorder_id,
              "processId": task["process_id"], "quantity": task["quantity"],
              "quantityProduced": task["quantityProduced"], "unitOfMeasure": task["unit"]}
             for task in cast(list[dict[str, object]], source["tasks"])]
    feedback = list(cast(list[dict[str, object]], source.get("feedback", [])))
    for batch in cast(list[dict[str, object]], source.get("feedback_batches", [])):
        feedback.extend({**batch, "id": cast(int, batch["id_start"]) + offset}
                        for offset in range(cast(int, batch["count"])))
    rows = [{"recordId": row["id"], "workorderId": workorder_id,
             "taskId": row["task_id"] if row["task_id"] != 0 else None,
             "processId": row["process_id"], "status": row["status"],
             "quantityFeedback": row["quantityFeedback"],
             "feedbackTime": f"2026-01-01 00:00:{cast(int, row['id']) % 60:02d}"}
            for row in feedback]
    return tasks, rows


def _run(case: Mapping[str, object], *, limit: int = 20,
         missing_feedback_time: bool = False,
         unsorted_feedback: bool = False) -> tuple[ToolResult, list[str]]:
    source = cast(dict[str, object], case["input"])
    workorder = cast(dict[str, object], source["workorder"])
    workorder_id = cast(int, workorder["id"])
    tasks, feedback = _rows(source, workorder_id)
    if missing_feedback_time and feedback:
        feedback[0]["feedbackTime"] = None
    if unsorted_feedback:
        feedback.reverse()
    calls: list[str] = []

    async def transport(url: str, _headers: Mapping[str, str], params: Mapping[str, object], _timeout: float) -> object:
        calls.append(url)
        if url.endswith(f"/workorder/{workorder_id}"):
            changed = len([call for call in calls if call.endswith(f"/workorder/{workorder_id}")]) == 2
            produced = source.get("workorder_produced_after_reads") if changed else None
            return {"code": 200, "data": {"workorderId": workorder_id,
                "quantity": workorder["quantity"],
                "quantityProduced": produced if produced is not None else workorder["quantityProduced"],
                "unitOfMeasure": workorder["unit"], "status": workorder["status"],
                "version": source.get("workorder_version_after_reads", workorder.get("version"))}}
        domain = "protask" if url.endswith("/protask/list") else "feedback"
        if domain == "feedback" and cast(dict[str, bool], source.get("permissions", {})).get("feedback_list") is False:
            return {"code": 403, "msg": "denied"}
        rows = tasks if domain == "protask" else feedback
        page = cast(int, params["pageNum"])
        size = cast(int, params["pageSize"])
        if domain == "feedback" and page == 2 and source.get("read_error"):
            return {"code": 500, "msg": "simulated timeout"}
        total = cast(int, source.get("feedback_total", len(rows))) if domain == "feedback" else len(rows)
        return {"code": 200, "rows": rows[(page - 1) * size:page * size], "total": total}

    client = KTGClient(base_url="http://localhost", authorization="Bearer synthetic", transport=transport)
    async def invoke() -> ToolResult:
        return await build_workorder_progress_tool().execute(
            ToolContext(run_id="test", tool_call_id="progress", ktg_client=client),
            {"workorder_id": workorder_id, "recent_feedback_limit": limit})
    return asyncio.run(invoke()), calls


@pytest.mark.parametrize("case", CASES, ids=[case["case_id"] for case in CASES])
def test_mes07_oracle(case: dict[str, object]) -> None:
    result, calls = _run(case)
    expected = cast(dict[str, object], case["expected"])
    if not expected["available"]:
        assert result.status is ToolStatus.FAILED
        assert result.error_code == expected["unavailable_reason"]
        assert result.details is not None and result.details["available"] is False
        return
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    details = result.details
    metrics = cast(dict[str, object], details["metrics"])
    assert metrics["workorder_produced_fact"] == expected["workorder_produced_fact"]
    assert metrics["remaining_raw"] == expected["remaining_raw"]
    if expected["remaining_raw"] is None:
        assert metrics["remaining_reason"] == expected["remaining_reason"]
    if expected["ratio_percent"] is None:
        assert metrics["ratio_percent"] is None
        assert metrics["ratio_reason"] == expected["ratio_reason"]
    else:
        from decimal import Decimal
        assert Decimal(cast(str, metrics["ratio_percent"])) == Decimal(cast(str, expected["ratio_percent"]))
    processes = cast(list[dict[str, object]], details["processes"])
    assert all(process["metric"] is None for process in processes)
    if case["case_id"] == "unit_mismatch":
        assert processes[0]["reason"] == "UNIT_MISMATCH"
    completeness = cast(dict[str, object], details["completeness"])
    assert completeness["mes_requests"] == len(calls) <= MAX_REQUESTS
    assert completeness["snapshot_consistent"] is False
    assert completeness["observation_grade"] == "MULTI_REQUEST_NON_ATOMIC"
    assert completeness["business_progress_available"] is False
    assert len(cast(list[object], details["recent_feedback"])) <= 20
    if case["case_id"] == "large_pagination":
        assert completeness["feedback_total"] == 25
        assert completeness["recent_feedback_truncated"] is True
        assert set(cast(list[int], cast(dict[str, object], details["trace"])["recent_feedback_ids"])) <= set(range(100, 125))


def test_default_off_manifest_and_permissions() -> None:
    factories = workorder_progress_factories()
    manifest, = workorder_progress_manifests()
    assert isinstance(manifest.permission_policy, PermissionAll)
    assert len(manifest.permission_policy.all_of) == 3
    catalog = ToolCatalog(factories)
    catalog.register_factory(manifest, factories[manifest.handler_ref])
    assert catalog.resolve_definition(manifest.name, manifest.version).required_permissions == (
        "mes:pro:workorder:query", "mes:pro:protask:list", "mes:pro:feedback:list")
    snapshot = catalog.candidate_snapshot(
        CatalogIdentity(frozenset({"*:*:*"}), "test"),
        CatalogSwitches(modules={"pro": True}, tools={manifest.name: False}))
    assert manifest.name not in snapshot.candidates


def test_limit_zero_keeps_full_source_count() -> None:
    case = next(case for case in CASES if case["case_id"] == "large_pagination")
    result, calls = _run(case, limit=0)
    assert result.details is not None
    assert result.details["recent_feedback"] == []
    assert cast(dict[str, object], result.details["completeness"])["feedback_total"] == 25
    assert len(calls) == 5


def test_missing_feedback_time_disables_recent_summary_without_losing_facts() -> None:
    case = next(case for case in CASES if case["case_id"] == "trace_feedback")
    result, _ = _run(case, missing_feedback_time=True)
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["recent_feedback"] == []
    completeness = cast(dict[str, object], result.details["completeness"])
    assert completeness["recent_feedback_available"] is False
    assert completeness["recent_feedback_reason"] == "MISSING_FEEDBACK_TIME"
    assert completeness["feedback_total"] == 2


def test_unsorted_source_cannot_claim_complete_pagination() -> None:
    case = next(case for case in CASES if case["case_id"] == "trace_feedback")
    result, _ = _run(case, unsorted_feedback=True)
    assert result.error_code == "SOURCE_INCOMPLETE"
    assert result.details is not None and result.details["available"] is False


def test_runner_blocks_missing_source_permission_before_mes_read() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def transport(url: str, _headers: Mapping[str, str],
                            _params: Mapping[str, object], _timeout: float) -> object:
            calls.append(url)
            return {"code": 200, "data": {"workorderId": 101}}

        client = KTGClient(base_url="http://localhost", authorization="Bearer test", transport=transport)
        tool = build_workorder_progress_tool()
        manifest, = workorder_progress_manifests()
        factories = workorder_progress_factories()
        catalog = ToolCatalog(factories)
        catalog.register_factory(manifest, factories[manifest.handler_ref])
        switches = CatalogSwitches({"pro": True}, {tool.name: True})
        all_permissions = frozenset(tool.required_permissions)
        candidate = catalog.candidate_snapshot(CatalogIdentity(all_permissions, "run"), switches)
        permissions: dict[str, frozenset[str] | None] = {"current": all_permissions}

        async def authorize(_context: ToolContext) -> CallAuthorization:
            return CallAuthorization(permissions["current"], switches, candidate, frozenset({tool.name}))

        runner = ToolRunner(ToolRegistry((tool,)), catalog=catalog,
                            authorization_provider=authorize)
        call = ToolCall("progress", tool.name, {"workorder_id": 101})
        context = ToolContext(run_id="run", tool_call_id=call.id,
                              identity={"userId": 7}, ktg_client=client)
        for missing in tool.required_permissions:
            permissions["current"] = all_permissions - {missing}
            assert (await runner.run(call, context)).error_code == "PERMISSION_DENIED"
            assert not calls
        permissions["current"] = None
        assert (await runner.run(call, context)).error_code == "PERMISSION_UNAVAILABLE"
        assert not calls

    asyncio.run(scenario())
