from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.workorder_quality import build_workorder_quality_tool
from ktg_agent.tools import ToolContext, ToolEffect, ToolResult


ORDER = "/mes/pro/workorder/list"
DETAIL = "/mes/pro/execution/workorders/11"
UNITS = "/mes/pro/execution/tasks/71/units"
HISTORY = "/mes/pro/execution/units/"


def unit(sn: int, state: str) -> dict[str, object]:
    return {"snId": sn, "snCode": f"SN-{sn}", "unitState": state, "version": 3,
            "currentTaskId": 71 if state not in {"WAIT_PACKING", "PACKED"} else 72}


def inspection(sn: int, round_no: int, reject: bool) -> dict[str, object]:
    ipqc_id = sn * 10 + round_no
    line_id = ipqc_id * 100
    line = {"lineId": line_id, "ipqcId": ipqc_id, "indexId": 18, "indexCode": "VOLT",
            "indexName": "输出电压", "indexType": "QUANTITATIVE", "unitOfMeasure": "V",
            "thresholdMin": 3.5, "thresholdMax": 4.2, "crQuantity": 0,
            "majQuantity": 1 if reject else 0, "minQuantity": 0,
            "remark": json.dumps({"value": 3.1 if reject else 4.1, "passed": None,
                                  "defectId": 7 if reject else None})}
    defects = ([{"recordId": ipqc_id * 1000, "qcType": "IPQC", "qcId": ipqc_id,
                 "lineId": line_id, "defectName": "电压偏低", "defectLevel": "MAJ",
                 "defectQuantity": 1, "remark": "测量值低于下限"}] if reject else [])
    return {"snId": sn, "taskId": 71, "inspectionId": ipqc_id, "ipqcId": ipqc_id,
            "roundNo": round_no, "sourceDispositionId": None if round_no == 1 else 50,
            "produceLineId": None if reject else 100,
            "checkResult": "REJECT" if reject else "ACCEPT", "quantityCheck": 1,
            "quantityQualified": 0 if reject else 1, "quantityUnqualified": 1 if reject else 0,
            "inspectDate": "2026-11-24 12:00:00", "lines": [line], "defects": defects}


class Mes:
    def __init__(self) -> None:
        self.header: dict[str, object] = {"workorderId": 11, "workorderCode": "WO-11",
                                         "workorderName": "台灯", "status": "CONFIRMED", "quantity": 8, "version": 2}
        self.units = [unit(1, "WAIT_INSPECTION"), unit(2, "WAIT_DISPOSITION"),
                      unit(3, "REWORK_IN_PROGRESS"), unit(4, "WAIT_REINSPECTION"),
                      unit(5, "WAIT_PACKING"), unit(6, "SCRAPPED"), unit(7, "PACKED")]
        self.history = {1: [], 2: [inspection(2, 1, True)], 3: [inspection(3, 1, True)],
                        4: [inspection(4, 1, True)], 5: [inspection(5, 1, True), inspection(5, 2, False)],
                        6: [inspection(6, 1, True)], 7: [inspection(7, 1, False)]}
        self.progress: dict[str, object] = {"workorderId": 11, "status": "CONFIRMED", "planned": 8,
                                           "allocated": 8, "assembled": 7, "waitingAssembly": 1,
                                           "waitingInspection": 1, "pendingDisposition": 1, "reworkInProgress": 1,
                                           "waitingReinspection": 1, "qualified": 2, "packable": 1,
                                           "packed": 1, "scrapped": 1, "firstInspectionRejected": 5,
                                           "reinspectionCount": 1, "tasks": [{"taskId": 71,
                                               "taskKind": "NORMAL", "stepKind": "INSPECTION"}]}
        self.overrides: dict[str, object] = {}
        self.requests: list[str] = []
        self.bump_on_history = False

    async def transport(self, url: str, _headers: Mapping[str, str], params: Mapping[str, object],
                        _timeout: float) -> object:
        path = url.removeprefix("http://mes.test")
        self.requests.append(path)
        if path in self.overrides:
            return copy.deepcopy(self.overrides[path])
        if path == ORDER:
            rows = [self.header] if params["workorderCode"] == "WO-11" else []
        elif path == DETAIL:
            return {"code": 200, "data": {"order": {**self.header, "plannedUnits": self.progress["planned"]}, "progress": self.progress}}
        elif path == UNITS:
            assert params["includeHistory"] in (True, "true")
            rows = [u for u in self.units if str(params.get("snCode", "")) in str(u["snCode"])]
        elif path.startswith(HISTORY):
            if self.bump_on_history:
                self.header["version"] = 3
            sn = int(path.removeprefix(HISTORY).split("/")[0])
            return {"code": 200, "data": copy.deepcopy(self.history[sn])}
        else:
            raise AssertionError(path)
        start = (int(str(params["pageNum"])) - 1) * int(str(params["pageSize"]))
        return {"code": 200, "total": len(rows), "rows": copy.deepcopy(rows[start:start + int(str(params["pageSize"]))])}

    def query(self, **args: object) -> ToolResult:
        tool = build_workorder_quality_tool()
        client = KTGClient(base_url="http://mes.test", authorization="Bearer fixture", transport=self.transport)
        async def run() -> ToolResult:
            return await tool.execute(ToolContext("r", "c", ktg_client=client), {"workorder_codes": ["WO-11"], **args})
        return asyncio.run(run())


def first(result: ToolResult) -> JsonObject:
    assert result.details is not None
    schema = build_workorder_quality_tool().output_schema
    assert schema is not None and Draft202012Validator(schema).is_valid(result.details)
    return cast(list[JsonObject], result.details["orders"])[0]


def units(result: ToolResult) -> list[JsonObject]:
    return cast(list[JsonObject], first(result)["units"])


def test_default_includes_historical_reject_and_current_abnormal_states_not_pending_first_inspection() -> None:
    mes = Mes()
    result = mes.query()
    assert build_workorder_quality_tool().effect == ToolEffect.READ
    assert first(result)["history_coverage"] == "complete"
    assert [entry["snCode"] for entry in units(result)] == ["SN-2", "SN-3", "SN-4", "SN-5", "SN-6"]
    recovered = units(result)[3]
    assert recovered["unitState"] == "WAIT_PACKING" and recovered["first_result"] == "REJECT"
    assert recovered["latest_result"] == "ACCEPT" and recovered["inspection_count"] == 2
    rows = cast(list[JsonObject], recovered["inspections"])
    failed = cast(list[JsonObject], rows[0]["failed_indices"])[0]
    assert failed["value"] == "3.1" and failed["thresholdMin"] == "3.5"
    assert failed["reason"] == "测量值低于下限" and failed["defectName"] == "电压偏低"


def test_verified_empty_history_is_not_a_completed_first_inspection() -> None:
    mes = Mes()
    mes.header["quantity"] = 10
    mes.units = [unit(sn, "WAIT_INSPECTION") for sn in range(1, 5)]
    mes.history = {sn: [] for sn in range(1, 5)}
    mes.progress.update({"planned": 10, "allocated": 10, "assembled": 4, "waitingAssembly": 6,
                         "waitingInspection": 4, "pendingDisposition": 0, "reworkInProgress": 0,
                         "waitingReinspection": 0, "qualified": 0, "packable": 0, "packed": 0,
                         "scrapped": 0, "firstInspectionRejected": 0, "reinspectionCount": 0})
    result = mes.query()
    order = first(result)
    summary = cast(JsonObject, order["summary"])
    assert order["history_coverage"] == "complete" and order["scanned_units"] == 4
    assert summary["firstInspectionCompleted"] == 0 and summary["firstInspectionAccepted"] == 0
    assert summary["waitingInspection"] == 4 and summary["qualified"] == 0
    assert "历史核实件数不等于已检验件数" in result.content
    assert "尚无首检结果" in result.content
    assert "只支持返工和报废" in result.content


def test_first_inspection_counts_do_not_promote_reinspection_accept_to_first_pass() -> None:
    order = first(Mes().query(sn_codes=["SN-5"]))
    summary = cast(JsonObject, order["summary"])
    assert summary["firstInspectionCompleted"] == 6
    assert summary["firstInspectionAccepted"] == 1
    assert summary["firstInspectionRejected"] == 5
    assert summary["qualified"] == 2 and order["scanned_units"] == 1


def test_naive_inspection_time_is_preserved_without_assuming_utc() -> None:
    result = Mes().query(sn_codes=["SN-2"])
    row = units(result)[0]
    inspection_row = cast(list[JsonObject], row["inspections"])[0]
    assert inspection_row["inspect_date"] == "2026-11-24 12:00:00"
    assert "检验时间无时区时按MES原样" in result.content


def test_exact_requested_sn_can_show_uninspected_or_first_pass_and_missing_is_not_nonexistent() -> None:
    result = Mes().query(sn_codes=["SN-1", "SN-7", "SN-70"])
    assert first(result)["missing_sn_codes"] == ["SN-70"]
    assert units(result)[0]["inspection_count"] == 0 and units(result)[0]["ever_rejected"] is False
    assert units(result)[1]["first_result"] == "ACCEPT"


def test_draft_has_no_quality_execution_and_makes_no_detail_reads() -> None:
    mes = Mes()
    mes.header["status"] = "PREPARE"
    order = first(mes.query())
    assert order["history_coverage"] == "not_applicable" and order["summary"] is None
    assert DETAIL not in mes.requests


def test_current_abnormal_unit_without_history_preserves_state_and_unknown_history() -> None:
    mes = Mes()
    mes.history[2] = []
    order = first(mes.query())
    assert order["history_coverage"] == "partial"
    row = cast(list[JsonObject], order["units"])[0]
    assert row["unitState"] == "WAIT_DISPOSITION" and row["history_state"] == "unavailable"
    assert row["inspection_count"] is None


def test_scope_denial_does_not_become_no_anomaly() -> None:
    mes = Mes()
    mes.overrides[UNITS] = {"code": 403}
    order = first(mes.query())
    assert order["history_coverage"] == "partial" and order["summary"] is not None


@pytest.mark.parametrize("field,value", [("snId", 9), ("roundNo", 3), ("quantityQualified", 1), ("taskId", 99)])
def test_wrong_history_row_cannot_confirm_its_failure(field: str, value: object) -> None:
    mes = Mes()
    mes.history[2][0][field] = value
    order = first(mes.query())
    row = cast(list[JsonObject], order["units"])[0]
    assert row["unitState"] == "WAIT_DISPOSITION" and row["history_state"] == "unavailable"
    assert row["inspections"] == [] and order["history_coverage"] == "partial"


def test_order_revision_changes_invalidate_all_quality_facts() -> None:
    mes = Mes()
    mes.bump_on_history = True
    order = first(mes.query())
    assert order["history_coverage"] == "partial" and order["summary"] is None and order["units"] == []


def test_auth_expiry_fails_entire_tool() -> None:
    mes = Mes()
    mes.overrides[HISTORY + "2/inspections"] = {"code": 401}
    assert mes.query().error_code == "AUTH_EXPIRED"


def test_qualitative_failed_indicator_and_successful_reinspection_are_distinct() -> None:
    mes = Mes()
    for record in mes.history[5]:
        failed = record["checkResult"] == "REJECT"
        row = cast(list[dict[str, object]], record["lines"])[0]
        row.update({"indexType": "QUALITATIVE", "indexName": "外观", "thresholdMin": None,
                    "thresholdMax": None, "unitOfMeasure": "", "remark": json.dumps({
                        "value": None, "passed": not failed, "defectId": 7 if failed else None})})
    found = units(mes.query(sn_codes=["SN-5"]))[0]
    inspections = cast(list[JsonObject], found["inspections"])
    indicator = cast(list[JsonObject], inspections[0]["failed_indices"])[0]
    assert found["latest_result"] == "ACCEPT" and indicator["passed"] is False
    assert indicator["value"] is None and indicator["thresholdMin"] is None
    assert inspections[1]["failed_indices"] == []


@pytest.mark.parametrize("corrupt", ["threshold", "defect_line", "missing_defect"])
def test_invalid_indicator_evidence_is_not_a_confirmed_cause(corrupt: str) -> None:
    mes = Mes()
    record = mes.history[2][0]
    if corrupt == "threshold":
        cast(list[dict[str, object]], record["lines"])[0]["thresholdMin"] = 4.3
    elif corrupt == "defect_line":
        cast(list[dict[str, object]], record["defects"])[0]["lineId"] = 999
    else:
        record["defects"] = []
    row = units(mes.query(sn_codes=["SN-2"]))[0]
    assert row["unitState"] == "WAIT_DISPOSITION" and row["history_state"] == "unavailable"
    assert row["inspections"] == []


def test_bounded_member_scan_is_partial_even_when_first_fifty_have_no_failures() -> None:
    mes = Mes()
    mes.units = [unit(sn, "WAIT_INSPECTION") for sn in range(1, 52)]
    mes.history = {sn: [] for sn in range(1, 52)}
    mes.header["quantity"] = 51
    mes.progress.update({"planned": 51, "allocated": 51, "assembled": 51, "waitingAssembly": 0,
                         "waitingInspection": 51, "pendingDisposition": 0, "reworkInProgress": 0,
                         "waitingReinspection": 0, "qualified": 0, "packable": 0, "packed": 0,
                         "scrapped": 0, "firstInspectionRejected": 0, "reinspectionCount": 0})
    order = first(mes.query())
    assert order["history_coverage"] == "partial" and order["total_units"] == 51
    assert order["scanned_units"] == 50 and order["units"] == []


def test_output_budget_retains_only_whole_units_and_has_partial_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    from ktg_agent.ktg import workorder_quality as module
    monkeypatch.setattr(module, "_HISTORY_OUTPUT_BYTES", 1200)
    order = first(Mes().query())
    assert order["history_coverage"] == "partial" and order["summary"] is not None
    projected = cast(list[JsonObject], order["units"])
    assert len(projected) < 5 and order["scanned_units"] == 7
    assert all(len(cast(list[JsonObject], row["inspections"])) == row["inspection_count"] for row in projected)
