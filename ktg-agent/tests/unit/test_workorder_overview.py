from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.workorder_overview import build_workorder_overview_tool
from ktg_agent.tools import ToolContext, ToolEffect, ToolResult, ToolStatus


LIST = "/mes/pro/workorder/list"
EXECUTION = "/mes/pro/execution/workorders/"


def order(number: int, *, code: str | None = None, status: str = "CONFIRMED",
          day: str = "2026-11-24") -> dict[str, object]:
    return {
        "workorderId": number, "workorderCode": code or f"WO-{number}",
        "workorderName": "台灯电池模组", "productCode": "P-01", "productName": "模组",
        "quantity": 12, "unitOfMeasure": "件", "status": status,
        "requestDate": day, "version": 2,
    }


def execution(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "code": 200, "data": {
            "order": {
                "workorderId": row["workorderId"], "workorderCode": row["workorderCode"],
                "status": row["status"], "version": row["version"], "plannedUnits": 12,
            },
            "units": [{"snId": 101, "snCode": "NOT-IN-OVERVIEW"}],
            "progress": {
                "workorderId": row["workorderId"], "status": row["status"],
                "planned": 12, "allocated": 12, "assembled": 11,
                "waitingAssembly": 1, "waitingInspection": 0, "pendingDisposition": 2,
                "reworkInProgress": 0, "waitingReinspection": 0, "qualified": 9,
                "packable": 1, "packed": 8, "scrapped": 0,
                "completionAllowed": False, "completionReasons": [
                    "工单未处于已下达状态" if row["status"] == "FINISHED" else "仍有未完结单件",
                ],
                "tasks": [{"taskId": 500, "stepKind": "ASSEMBLY", "taskKind": "NORMAL",
                           "status": "STARTED"}],
            },
        },
    }


class Mes:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, Mapping[str, object]]] = []
        self.details = {str(r["workorderId"]): execution(r) for r in rows}
        self.list_response: object | None = None

    async def transport(self, url: str, _headers: Mapping[str, str],
                        params: Mapping[str, object], _timeout: float) -> object:
        path = url.removeprefix("http://mes.test")
        self.requests.append((path, dict(params)))
        if path == LIST:
            if self.list_response is not None:
                return self.list_response
            filtered = [row for row in self.rows if all(
                row[key] == params[key] for key in ("workorderCode", "productCode", "status", "requestDate")
                if key in params)]
            if "workorderName" in params:
                filtered = [row for row in filtered if str(params["workorderName"]) in str(row["workorderName"])]
            first = (int(str(params["pageNum"])) - 1) * int(str(params["pageSize"]))
            return {"code": 200, "total": len(filtered),
                    "rows": filtered[first:first + int(str(params["pageSize"]))]}
        assert path.startswith(EXECUTION)
        return self.details[path.removeprefix(EXECUTION)]

    def query(self, arguments: JsonObject) -> ToolResult:
        tool = build_workorder_overview_tool()
        client = KTGClient(base_url="http://mes.test", authorization="Bearer fixture",
                           transport=self.transport)
        async def execute() -> ToolResult:
            return await tool.execute(ToolContext("r", "c", ktg_client=client), arguments)
        return asyncio.run(execute())


def test_definition_has_both_permissions_and_strict_paging_schema() -> None:
    tool = build_workorder_overview_tool()
    assert tool.name == "ktg_query_workorder_overview"
    assert tool.effect is ToolEffect.READ
    assert set(tool.required_permissions) == {"mes:pro:workorder:list", "mes:pro:execution:query"}
    validator = Draft202012Validator(tool.input_schema)
    assert validator.is_valid({})
    assert validator.is_valid({"workorder_codes": ["WO-1", "WO-2"], "include_progress": True})
    assert not validator.is_valid({"page_size": 21})
    assert not validator.is_valid({"workorder_code": "WO-1", "workorder_codes": ["WO-2"]})
    assert not validator.is_valid({"statuses": ["PREPARE"], "unfinished_only": True})
    assert not validator.is_valid({"request_date": "2026-11-24", "request_date_from": "2026-11-01"})


def test_native_filters_use_authoritative_server_total_without_progress_calls() -> None:
    mes = Mes([order(9007199254740993), order(9007199254740994, status="PREPARE")])
    result = mes.query({"workorder_name": "台灯", "product_code": "P-01",
                        "statuses": ["CONFIRMED"], "request_date": "2026-11-24", "page_size": 1})
    assert result.status is ToolStatus.SUCCEEDED and result.details is not None
    data = result.details
    assert data["kind"] == "workorder_overview" and data["total"] == 1 and data["coverage"] == "complete"
    rows = cast(list[JsonObject], data["rows"])
    assert rows[0]["workorderId"] == "9007199254740993"
    assert rows[0]["quantity"] == "12" and rows[0]["progress_state"] == "not_requested"
    assert rows[0]["progress"] is None
    assert mes.requests == [(LIST, {
        "workorderName": "台灯", "productCode": "P-01", "status": "CONFIRMED", "requestDate": "2026-11-24",
        "pageNum": 1, "pageSize": 1, "orderByColumn": "workorderId", "isAsc": "asc", "reasonable": "false",
    })]
    schema = build_workorder_overview_tool().output_schema
    assert schema is not None and Draft202012Validator(schema).is_valid(data)


def test_multiple_exact_codes_query_individually_and_page_the_combined_matches() -> None:
    mes = Mes([order(1), order(2), order(3)])
    result = mes.query({"workorder_codes": ["WO-3", "WO-1", "not-found"], "page": 2, "page_size": 1})
    assert result.details is not None and result.details["total"] == 2
    assert result.details["coverage"] == "complete"
    assert [row["workorderCode"] for row in cast(list[JsonObject], result.details["rows"])] == ["WO-3"]
    assert {params["workorderCode"] for _, params in mes.requests} == {"WO-3", "WO-1", "not-found"}


def test_multi_status_date_range_and_unfinished_filter_use_bounded_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    from ktg_agent.ktg import workorder_overview as overview

    monkeypatch.setattr(overview, "_MAX_SCAN_PAGES", 1)
    rows = [order(i, status="FINISHED" if i % 2 else "CONFIRMED") for i in range(1, 26)]
    mes = Mes(rows)
    result = mes.query({"unfinished_only": True, "request_date_from": "2026-11-24",
                        "request_date_to": "2026-11-25", "page_size": 20})
    assert result.details is not None and result.details["coverage"] == "partial"
    assert result.details["total"] is None
    assert all(row["status"] == "CONFIRMED" for row in cast(list[JsonObject], result.details["rows"]))
    assert len(mes.requests) == 1
    assert "requestDate" not in mes.requests[0][1] and "status" not in mes.requests[0][1]


def test_multi_status_scan_with_complete_source_has_exact_filtered_count() -> None:
    mes = Mes([order(1), order(2, status="PREPARE", day="2026-11-23"),
               order(3, status="FINISHED"), order(4, status="PREPARE")])
    result = mes.query({"statuses": ["CONFIRMED", "PREPARE"], "request_date_from": "2026-11-24"})
    assert result.details is not None and result.details["coverage"] == "complete"
    assert result.details["total"] == 2
    assert [row["workorderCode"] for row in cast(list[JsonObject], result.details["rows"])] == ["WO-1", "WO-4"]


@pytest.mark.parametrize("fragment", ["A_100", "100%折扣", "%", "_", r"A\B"])
def test_name_special_characters_are_literal_and_never_forwarded_as_like_wildcards(fragment: str) -> None:
    matching = {**order(1), "workorderName": "前缀 " + fragment + " 后缀"}
    mes = Mes([matching, {**order(2), "workorderName": "A0100X折扣 AB"}])
    result = mes.query({"workorder_name": fragment})
    assert result.details is not None and result.details["total"] == 1
    rows = cast(list[JsonObject], result.details["rows"])
    assert [row["workorderId"] for row in rows] == ["1"]
    assert all(not any(char in str(params.get("workorderName", "")) for char in "%_\\")
               for _, params in mes.requests)


def test_normal_name_keeps_mes_case_and_accent_collation_results() -> None:
    mes = Mes([])
    mes.list_response = {"code": 200, "total": 1, "rows": [
        {**order(1), "workorderName": "CAFÉ 台灯"},
    ]}
    result = mes.query({"workorder_name": "cafe"})
    assert result.details is not None and result.details["total"] == 1
    assert mes.requests[0][1]["workorderName"] == "cafe"


def test_progress_uses_server_snapshot_ignores_sn_rows_and_marks_drafts() -> None:
    mes = Mes([order(1), order(2, status="PREPARE"), order(3, status="FINISHED")])
    result = mes.query({"include_progress": True})
    assert result.details is not None
    rows = cast(list[JsonObject], result.details["rows"])
    assert [row["progress_state"] for row in rows] == ["available", "draft", "available"]
    assert set(path for path, _ in mes.requests) == {LIST, EXECUTION + "1", EXECUTION + "3"}
    assert rows[1]["progress"] is None
    progress = cast(JsonObject, rows[0]["progress"])
    assert progress["qualified"] == 9 and progress["pendingDisposition"] == 2
    assert "NOT-IN-OVERVIEW" not in repr(result.details) and "NOT-IN-OVERVIEW" not in result.content
    assert "已完成工单" in result.content and "阻塞" in result.content


@pytest.mark.parametrize("response", [
    {"code": 403, "msg": "denied"}, {"code": 404, "msg": "not found"},
    {"code": 200, "data": {"progress": {"planned": 12}}},
])
def test_unavailable_progress_never_invents_counts(response: dict[str, object]) -> None:
    mes = Mes([order(1)])
    mes.details["1"] = response
    result = mes.query({"include_progress": True})
    assert result.details is not None and result.details["total"] == 1
    row = cast(list[JsonObject], result.details["rows"])[0]
    assert row["progress_state"] == "unavailable" and row["progress"] is None
    assert row["progress_reason"]


def test_changed_version_cannot_join_an_older_list_row_to_new_progress() -> None:
    mes = Mes([order(1)])
    detail = cast(dict[str, object], mes.details["1"]["data"])
    head = cast(dict[str, object], detail["order"])
    head["version"] = 3
    result = mes.query({"include_progress": True})
    assert result.details is not None
    row = cast(list[JsonObject], result.details["rows"])[0]
    assert row["progress_state"] == "unavailable" and row["progress"] is None


def test_inconsistent_completion_facts_are_not_shown_as_verified() -> None:
    mes = Mes([order(1)])
    detail = cast(dict[str, object], mes.details["1"]["data"])
    progress = cast(dict[str, object], detail["progress"])
    progress["completionAllowed"] = True  # The result still includes reasons prohibiting completion.
    result = mes.query({"include_progress": True})
    assert result.details is not None
    row = cast(list[JsonObject], result.details["rows"])[0]
    assert row["progress_state"] == "unavailable" and row["progress"] is None


@pytest.mark.parametrize("response", [
    {"code": 200, "rows": [], "total": True},
    {"code": 200, "rows": [], "total": 1},
    {"code": 200, "rows": [order(1)], "total": 0},
    {"code": 200, "rows": [order(1), order(1)], "total": 2},
    {"code": 403, "msg": "denied"},
])
def test_invalid_or_unauthorized_list_is_not_reported_as_a_count(response: object) -> None:
    mes = Mes([])
    mes.list_response = response
    result = mes.query({})
    assert result.is_error and result.details is None


def test_valid_empty_page_does_not_invent_no_match_when_total_is_nonzero() -> None:
    result = Mes([order(1)]).query({"page": 2, "page_size": 1})
    assert result.details is not None
    assert result.details["rows"] == [] and result.details["total"] == 1
    assert result.details["coverage"] == "complete"


@pytest.mark.parametrize("arguments", [
    {"request_date": "2026-02-31"}, {"request_date_from": "2026-12-01", "request_date_to": "2026-11-01"},
    {"workorder_code": "   "}, {"workorder_codes": ["WO-1", "WO-1"]},
    {"statuses": ["UNKNOWN"]}, {"page_size": 21}, {"page": 1.0}, {"page_size": 10.0},
])
def test_invalid_arguments_do_not_make_mes_requests(arguments: JsonObject) -> None:
    mes = Mes([])
    result = mes.query(arguments)
    assert result.error_code == "VALIDATION_ERROR" and not mes.requests


def test_execution_tasks_include_authoritative_process_name_and_sequence() -> None:
    mes = Mes([order(1)])
    mes.details["1"]["data"]["tasks"] = [{
        "taskId": 500, "workorderId": 1, "stepKind": "ASSEMBLY",
        "taskKind": "NORMAL", "status": "STARTED", "normalStepNo": 1,
        "processName": "电池模组装配", "taskCode": "EX-1-1",
    }]
    result = mes.query({"include_progress": True})
    assert result.details is not None
    task = result.details["rows"][0]["progress"]["tasks"][0]
    assert task["processName"] == "电池模组装配"
    assert task["normalStepNo"] == 1 and task["status"] == "STARTED"
    assert Draft202012Validator(build_workorder_overview_tool().output_schema).is_valid(result.details)
    mes.details["1"]["data"]["tasks"][0]["status"] = "WAITING"
    result = mes.query({"include_progress": True})
    assert result.details["rows"][0]["progress_state"] == "unavailable"
