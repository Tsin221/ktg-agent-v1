from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.production_trace import build_production_trace_tool
from ktg_agent.tools import ToolContext, ToolEffect, ToolResult


UNIT = "/mes/pro/execution/units/trace"
BATCH = "/mes/pro/execution/materials/trace"
ITEM = "/mes/md/mditem/list"


def unit(code: str, state: str = "WAIT_DISPOSITION") -> dict[str, object]:
    return {"traceStatus": "TRACKED", "snId": "9007199254740993", "snCode": code,
            "itemId": "37", "itemCode": "MODULE", "itemName": "灯电池模组", "workorderId": "30",
            "workorderCode": "WO-1", "workorderName": "阅读灯补单", "orderStatus": "CONFIRMED",
            "unitState": state, "assemblyFeedbackId": "42" if state != "WAIT_ASSEMBLY" else None,
            "inputs": [input_record()] if state != "WAIT_ASSEMBLY" else []}


def input_record() -> dict[str, object]:
    return {"inputId": "9007199254741023", "taskId": "1", "feedbackId": "42", "itemId": "38",
            "itemCode": "CELL", "itemName": "电芯", "sourceBatchCode": " Batch-A ", "quantity": 1.25,
            "unitOfMeasure": "PCS", "usedAt": "2026-11-24T10:00:01.000+08:00",
            "transactions": [{"transactionQuantity": -4}], "usedBy": "secret-actor"}


class Mes:
    def __init__(self) -> None:
        self.units: dict[str, object] = {" SN-1 ": unit(" SN-1 "), "SN-2": unit("SN-2", "WAIT_ASSEMBLY"),
            "SN-OLD": {"traceStatus": "LEGACY_NO_RECORD", "snId": "7", "snCode": "SN-OLD", "itemId": "37",
                       "itemCode": "MODULE", "itemName": "灯电池模组", "workorderId": None, "inputs": []}}
        self.items = [{"itemId": 38, "itemCode": "CELL", "itemName": "电芯"},
                      {"itemId": 39, "itemCode": "PCB", "itemName": "保护板"},
                      {"itemId": 40, "itemCode": "CELL-OTHER", "itemName": "另一电芯"}]
        self.batch_rows = [{"snId": str(i), "snCode": f"SN-{i}", "itemId": "37", "itemCode": "MODULE",
                            "itemName": "灯电池模组", "workorderId": "30", "workorderCode": "WO-1",
                            "workorderName": "阅读灯补单", "unitState": state, "orderStatus": "CONFIRMED",
                            "firstUsedAt": "2026-11-24T10:00:01+08:00", "lastUsedAt": "2026-11-24T10:00:01+08:00",
                            "inputCount": 1}
                           for i, state in enumerate(["WAIT_INSPECTION", "WAIT_DISPOSITION", "PACKED", "WAIT_PACKING"], 1)]
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.overrides: dict[str, object] = {}

    async def transport(self, url: str, _headers: Mapping[str, str], params: Mapping[str, object],
                        _timeout: float) -> object:
        path = url.removeprefix("http://mes.test")
        self.requests.append((path, dict(params)))
        if path in self.overrides:
            return self.overrides[path]
        if path == UNIT:
            data = self.units.get(str(params['snCode']))
            return {"code": 200, "data": data} if data is not None else {"code": 404, "msg": "Not found"}
        if path == ITEM:
            rows = [row for row in self.items if str(params["itemCode"]) in str(row["itemCode"])]
            rows.sort(key=lambda row: int(str(row["itemId"])))
        elif path == BATCH:
            assert params["batchCode"] == " Batch-A "
            rows = self.batch_rows if str(params["itemId"]) == "38" else []
        else:
            raise AssertionError(path)
        start = (int(str(params["pageNum"])) - 1) * int(str(params["pageSize"]))
        return {"code": 200, "total": len(rows), "rows": rows[start:start + int(str(params["pageSize"]))]}

    def query(self, **arguments: object) -> ToolResult:
        client = KTGClient(base_url="http://mes.test", authorization="Bearer fake", transport=self.transport)
        async def execute() -> ToolResult:
            return await build_production_trace_tool().execute(ToolContext("r", "c", ktg_client=client), arguments)
        return asyncio.run(execute())


def project(result: ToolResult) -> JsonObject:
    assert result.details is not None
    schema = build_production_trace_tool().output_schema
    assert schema is not None and Draft202012Validator(schema).is_valid(result.details)
    return result.details


def forward(result: ToolResult) -> list[JsonObject]:
    return cast(list[JsonObject], project(result)["units"])


def backward(result: ToolResult) -> list[JsonObject]:
    return cast(list[JsonObject], project(result)["batches"])


def test_forward_preserves_identity_and_only_actual_single_unit_input() -> None:
    mes = Mes()
    result = mes.query(direction="unit_to_material", sn_codes=[" SN-1 "])
    target = forward(result)[0]
    assert target["snCode"] == " SN-1 " and target["snId"] == "9007199254740993"
    assert target["trace_state"] == "tracked" and target["input_state"] == "recorded"
    entry = cast(list[JsonObject], target["inputs"])[0]
    assert entry["quantity"] == "1.25" and entry["sourceBatchCode"] == " Batch-A "
    assert "transactions" not in entry and "usedBy" not in entry
    assert project(result)["coverage"] == "complete"
    assert mes.requests == [(UNIT, {"snCode": " SN-1 "})]
    tool = build_production_trace_tool()
    assert tool.effect is ToolEffect.READ
    assert set(tool.required_permissions) == {"mes:pro:execution:trace", "mes:pro:execution:query", "mes:md:mditem:list"}


def test_forward_distinguishes_unreported_legacy_and_unknown() -> None:
    result = Mes().query(direction="unit_to_material", sn_codes=["SN-2", "SN-OLD", "SN-MISSING"])
    waiting, legacy, missing = forward(result)
    assert waiting["input_state"] == "waiting_assembly" and waiting["inputs"] == []
    assert legacy["trace_state"] == "legacy" and legacy["input_state"] == "legacy"
    assert missing["trace_state"] == "unavailable" and missing["input_state"] == "unavailable"
    assert project(result)["coverage"] == "partial"


@pytest.mark.parametrize("reason", ["mismatch", "negative", "duplicated", "missing", "unreported_input"])
def test_forward_invalid_or_contradictory_inputs_do_not_become_actual_inputs(reason: str) -> None:
    mes = Mes()
    data = unit("SN-1")
    if reason == "mismatch":
        data["snCode"] = "sn-1"
    elif reason == "negative":
        data["inputs"] = [{**input_record(), "quantity": -2}]
    elif reason == "duplicated":
        data["inputs"] = [input_record(), input_record()]
    elif reason == "missing":
        data["inputs"] = []
    else:
        data["unitState"] = "WAIT_ASSEMBLY"
    mes.units["SN-1"] = data
    result = mes.query(direction="unit_to_material", sn_codes=["SN-1"])
    row = forward(result)[0]
    assert row["input_state"] == "unavailable" and row["inputs"] == []
    assert project(result)["coverage"] == "partial"


def test_reverse_resolves_exact_material_and_does_not_mix_other_material_same_batch() -> None:
    mes = Mes()
    result = mes.query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "},
                                                           {"item_code": "PCB", "batch_code": " Batch-A "}])
    cell, pcb = backward(result)
    assert cell["itemId"] == "38" and cell["total_units"] == cell["returned_units"] == 4
    assert pcb["itemId"] == "39" and pcb["total_units"] == pcb["returned_units"] == 0
    assert cell["coverage"] == pcb["coverage"] == "complete"
    counts = cast(JsonObject, cell["state_counts"])
    assert counts["waitingInspection"] == 1 and counts["pendingDisposition"] == 1
    assert counts["packed"] == 1 and counts["qualified"] == 2
    assert cast(list[JsonObject], cell["rows"])[0]["itemCode"] == "MODULE"


def test_reverse_total_is_full_scope_but_counts_are_page_only() -> None:
    cell = backward(Mes().query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "}],
                               page=2, page_size=2))[0]
    assert cell["total_units"] == 4 and cell["returned_units"] == 2 and cell["coverage"] == "partial"
    assert cast(JsonObject, cell["state_counts"])["qualified"] == 2


@pytest.mark.parametrize("reason", ["duplicate", "inconsistent_total", "unauthorized", "waiting_assembly"])
def test_reverse_unsafe_results_are_unknown_not_zero(reason: str) -> None:
    mes = Mes()
    if reason == "duplicate":
        mes.batch_rows[1] = mes.batch_rows[0]
    elif reason == "inconsistent_total":
        mes.overrides[BATCH] = {"code": 200, "total": 10, "rows": mes.batch_rows}
    elif reason == "unauthorized":
        mes.overrides[BATCH] = {"code": 403}
    else:
        mes.batch_rows[0]["unitState"] = "WAIT_ASSEMBLY"
    cell = backward(mes.query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "}]))[0]
    assert cell["coverage"] == "unavailable" and cell["total_units"] is None and cell["state_counts"] is None
    assert cell["rows"] == []


def test_reverse_no_exact_item_is_unknown_without_trace_endpoint_call() -> None:
    mes = Mes()
    result = mes.query(direction="material_to_unit", batches=[{"item_code": "CEL", "batch_code": " Batch-A "}])
    assert backward(result)[0]["total_units"] is None
    assert all(path != BATCH for path, _ in mes.requests)


def test_reverse_material_identity_must_be_unique_across_all_search_pages() -> None:
    mes = Mes()
    mes.items = [{"itemId": 100 + n, "itemCode": f"CELL-{n}", "itemName": "其他"} for n in range(50)] + [
        {"itemId": 38, "itemCode": "CELL", "itemName": "电芯"}]
    found = backward(mes.query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "}]))[0]
    assert found["itemId"] == "38" and found["coverage"] == "complete"
    assert [params["pageNum"] for path, params in mes.requests if path == ITEM] == [1, 2]
    mes.items.append({"itemId": 151, "itemCode": "CELL", "itemName": "同编码但不同身份"})
    mes.requests.clear()
    unknown = backward(mes.query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "}]))[0]
    assert unknown["total_units"] is None and unknown["coverage"] == "unavailable"
    assert all(path != BATCH for path, _ in mes.requests)


def test_reverse_valid_batch_stays_available_when_another_is_unknown() -> None:
    result = Mes().query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "},
                                                              {"item_code": "CEL", "batch_code": " Batch-A "}])
    valid, unknown = backward(result)
    assert valid["total_units"] == 4 and unknown["total_units"] is None
    assert project(result)["coverage"] == "partial"


def test_finished_workorder_with_nonterminal_unit_is_inconsistent() -> None:
    mes = Mes()
    mes.batch_rows[0]["orderStatus"] = "FINISHED"
    cell = backward(mes.query(direction="material_to_unit", batches=[{"item_code": "CELL", "batch_code": " Batch-A "}]))[0]
    assert cell["coverage"] == "unavailable" and cell["total_units"] is None


@pytest.mark.parametrize("arguments", [
    {"direction": "unit_to_material", "sn_codes": [" "]},
    {"direction": "unit_to_material", "sn_codes": ["SN-1", "SN-1"]},
    {"direction": "unit_to_material", "sn_codes": ["SN-1"], "page": 2},
    {"direction": "material_to_unit", "batches": [{"item_code": "CELL", "batch_code": " Batch-A "}], "sn_codes": ["X"]},
    {"direction": "material_to_unit", "batches": [{"item_code": "CELL", "batch_code": " Batch-A "}], "page_size": 51},
])
def test_invalid_tool_parameters_make_no_request(arguments: dict[str, object]) -> None:
    mes = Mes()
    result = mes.query(**arguments)
    assert result.is_error and result.error_code == "VALIDATION_ERROR" and mes.requests == []


def test_authentication_expiration_returns_error_without_partial_identity() -> None:
    mes = Mes()
    mes.overrides[UNIT] = {"code": 401}
    result = mes.query(direction="unit_to_material", sn_codes=["SN-1"])
    assert result.is_error and result.error_code == "AUTH_EXPIRED" and result.details is None
