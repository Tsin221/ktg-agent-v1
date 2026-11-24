from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.workorder_materials import build_workorder_materials_tool
from ktg_agent.tools import ToolContext, ToolEffect, ToolResult


ORDER = "/mes/pro/workorder/list"
BOM = "/mes/pro/workorderbom/list"
STOCK = "/mes/wm/wmstock/list"
EXECUTION = "/mes/pro/execution/workorders/"
AVAILABLE = "/mes/pro/execution/tasks/71/available-materials"


def head(status: str = "PREPARE", quantity: int = 120) -> dict[str, object]:
    return {"workorderId": 11, "workorderCode": "WO-11", "workorderName": "台灯",
            "status": status, "quantity": quantity, "unitOfMeasure": "件", "version": 2}


def bom(line: int, item: int, quantity: object = 120, unit: str = "pcs") -> dict[str, object]:
    return {"lineId": line, "workorderId": 11, "itemId": item, "itemCode": f"MAT-{item}",
            "itemName": f"原料{item}", "unitOfMeasure": unit, "itemOrProduct": "ITEM", "quantity": quantity}


def stock(row_id: int, item: int, quantity: object, *, unit: str = "pcs", warehouse: str = "RM",
          bound: int | None = None, batch: str = "B-1") -> dict[str, object]:
    return {"materialStockId": row_id, "itemId": item, "itemCode": f"MAT-{item}",
            "itemName": f"原料{item}", "unitOfMeasure": unit, "quantityOnhand": quantity,
            "warehouseId": 5 if warehouse == "RM" else 6, "warehouseCode": warehouse,
            "warehouseName": "仓库", "workorderId": bound, "batchCode": batch}


def available(row_id: int, item: int, quantity: object, batch: str) -> dict[str, object]:
    return {"materialStockId": row_id, "itemId": item, "itemCode": f"MAT-{item}",
            "itemName": f"原料{item}", "unitOfMeasure": "pcs", "quantityOnhand": quantity,
            "warehouseId": 6, "sourceBatchCode": batch}


def detail(status: str = "CONFIRMED", planned: int = 12, waiting: int = 6,
           quantity: object = 12) -> dict[str, object]:
    rows = [{"line_id": item, "workorder_id": 11, "item_id": item, "item_code": f"MAT-{item}",
             "item_name": f"原料{item}", "unit_of_measure": "pcs", "item_or_product": "ITEM",
             "quantity": quantity} for item in (1, 2)]
    return {"code": 200, "data": {
        "order": {"workorderId": 11, "workorderCode": "WO-11", "version": 2, "status": status,
                  "plannedUnits": planned, "materialRequirementsSnapshot": json.dumps(rows)},
        "progress": {"workorderId": 11, "status": status, "planned": planned, "allocated": planned,
                     "waitingAssembly": waiting, "assembled": planned - waiting,
                     "tasks": [{"taskId": 71, "taskKind": "NORMAL",
                                                            "stepKind": "ASSEMBLY", "status": "CONFIRMED"}]},
        "units": [{"snCode": "NOT-IN-MATERIALS"}],
    }}


class Mes:
    def __init__(self, header: dict[str, object] | None = None) -> None:
        self.header = header or head()
        self.bom_rows = [bom(1, 1), bom(2, 2)]
        self.stock_rows = [stock(1, 1, 160), stock(2, 2, 80),
                           stock(3, 2, 16, warehouse="XBK_VIRTUAL"), stock(4, 2, 900, bound=99)]
        self.execution = detail()
        self.available_rows = [available(1, 1, 4, "BAT-A"), available(2, 1, 2, "BAT-B"),
                               available(3, 2, 6, "PCB-A")]
        self.requests: list[tuple[str, Mapping[str, object]]] = []
        self.override: dict[str, object] = {}
        self.on_request: Callable[[str], None] | None = None

    async def transport(self, url: str, _headers: Mapping[str, str],
                        params: Mapping[str, object], _timeout: float) -> object:
        path = url.removeprefix("http://mes.test")
        self.requests.append((path, dict(params)))
        if self.on_request is not None:
            self.on_request(path)
        if path in self.override:
            return self.override[path]
        if path == ORDER:
            rows = [self.header] if params["workorderCode"] == self.header["workorderCode"] else []
        elif path == BOM:
            rows = self.bom_rows
        elif path == STOCK:
            rows = [row for row in self.stock_rows if str(row["itemId"]) == str(params["itemId"])]
        elif path == EXECUTION + "11":
            return self.execution
        elif path == AVAILABLE:
            rows = self.available_rows
        else:
            raise AssertionError(path)
        start = (int(str(params["pageNum"])) - 1) * int(str(params["pageSize"]))
        return {"code": 200, "total": len(rows), "rows": rows[start:start + int(str(params["pageSize"]))]}

    def query(self, arguments: JsonObject | None = None) -> ToolResult:
        tool = build_workorder_materials_tool()
        client = KTGClient(base_url="http://mes.test", authorization="Bearer fixture", transport=self.transport)

        async def run() -> ToolResult:
            return await tool.execute(ToolContext("r", "c", ktg_client=client),
                                      arguments if arguments is not None else {"workorder_codes": ["WO-11"]})
        return asyncio.run(run())


def first(result: ToolResult) -> JsonObject:
    assert result.details is not None
    return cast(list[JsonObject], result.details["orders"])[0]


def materials(order: JsonObject) -> list[JsonObject]:
    return cast(list[JsonObject], order["materials"])


def test_contract_and_permissions() -> None:
    tool = build_workorder_materials_tool()
    assert tool.effect is ToolEffect.READ and tool.name == "ktg_query_workorder_materials"
    assert set(tool.required_permissions) == {"mes:pro:workorder:list", "mes:pro:workorderbom:list",
                                               "mes:pro:execution:query", "mes:wm:wmstock:list"}
    schema = Draft202012Validator(tool.input_schema)
    assert schema.is_valid({"workorder_codes": ["WO-11"]})
    assert not schema.is_valid({"workorder_codes": []})
    assert not schema.is_valid({"workorder_codes": ["WO-11"] * 6})
    assert not schema.is_valid({"workorder_codes": ["WO-11"], "unknown": True})


def test_draft_uses_total_bom_and_only_unbound_physical_stock() -> None:
    mes = Mes()
    result = mes.query({"workorder_codes": ["WO-11"], "include_batches": True})
    order = first(result)
    assert order["material_scope"] == "warehouse_reference"
    assert order["assessment"] == "reference_shortage"
    assert order["waitingAssembly"] is None
    cell, pcb = materials(order)
    assert cell["planned_required"] == cell["remaining_required"] == "120"
    assert cell["stock_quantity"] == "160" and cell["shortage_quantity"] == "0"
    assert pcb["stock_quantity"] == "80" and pcb["shortage_quantity"] == "40"
    assert pcb["warehouses"] == [{"warehouseId": "5", "warehouseCode": "RM", "warehouseName": "仓库", "quantity": "80"}]
    assert pcb["batches"] == [{"batchCode": "B-1", "quantity": "80", "warehouseCode": "RM"}]
    assert all(not path.startswith(EXECUTION) for path, _ in mes.requests)
    assert result.details is not None and result.details["coverage"] == "complete"
    schema = build_workorder_materials_tool().output_schema
    assert schema is not None and Draft202012Validator(schema).is_valid(result.details)


def test_draft_no_eligible_or_incompatible_records_is_unknown_not_zero() -> None:
    mes = Mes()
    mes.stock_rows = [stock(1, 1, 160, warehouse="XBK_VIRTUAL"), stock(2, 2, 80, unit="kg")]
    result = mes.query()
    order = first(result)
    assert order["assessment"] == "unavailable"
    assert result.details is not None and result.details["coverage"] == "partial"
    assert all(row["stock_quantity"] is None and row["shortage_quantity"] is None
               and row["stock_state"] == "unavailable" for row in materials(order))


def test_negative_or_missing_scope_stock_does_not_make_a_false_claim() -> None:
    mes = Mes()
    unknown = stock(4, 2, 80)
    unknown.pop("workorderId")
    mes.stock_rows = [stock(1, 1, 160), stock(2, 1, -1), unknown]
    order = first(mes.query())
    assert order["assessment"] == "unavailable"
    assert all(row["stock_quantity"] is None for row in materials(order))


@pytest.mark.parametrize("inconsistent_code", ["OTHER-ORDER", 99, True, []])
def test_unbound_stock_with_inconsistent_workorder_code_is_unavailable(inconsistent_code: object) -> None:
    mes = Mes()
    mes.stock_rows[0]["workorderCode"] = inconsistent_code
    row = materials(first(mes.query()))[0]
    assert row["stock_state"] == "unavailable" and row["stock_quantity"] is None


def test_other_unit_is_excluded_and_disclosed_when_matching_stock_exists() -> None:
    mes = Mes()
    mes.stock_rows.append(stock(8, 1, 900, unit="kg"))
    row = materials(first(mes.query()))[0]
    assert row["stock_quantity"] == "160" and row["stock_state"] == "verified"
    assert "单位不一致" in repr(row.get("warnings"))


def test_released_uses_frozen_total_fraction_and_only_authorized_line_side_batches() -> None:
    mes = Mes(head("CONFIRMED", 12))
    result = mes.query({"workorder_codes": ["WO-11"], "include_batches": True})
    order = first(result)
    cell, pcb = materials(order)
    assert order["material_scope"] == "line_side" and order["waitingAssembly"] == 6
    assert order["assessment"] == "line_side_sufficient"
    assert cell["planned_required"] == "12" and cell["remaining_required"] == "6"
    assert cell["stock_quantity"] == pcb["stock_quantity"] == "6"
    assert cell["batches"] == [{"batchCode": "BAT-A", "quantity": "4", "warehouseCode": "XBK_VIRTUAL"},
                               {"batchCode": "BAT-B", "quantity": "2", "warehouseCode": "XBK_VIRTUAL"}]
    assert {path for path, _ in mes.requests} == {ORDER, EXECUTION + "11", AVAILABLE}
    assert "NOT-IN-MATERIALS" not in repr(result.details) and "NOT-IN-MATERIALS" not in result.content


def test_line_side_complete_empty_collection_proves_no_eligible_consumable_stock() -> None:
    mes = Mes(head("CONFIRMED", 12))
    mes.available_rows = []
    result = mes.query()
    assert first(result)["assessment"] == "line_side_shortage"
    assert all(row["stock_quantity"] == "0" and row["shortage_quantity"] == "6"
               and row["stock_state"] == "verified" for row in materials(first(result)))
    assert result.details is not None and result.details["coverage"] == "complete"


def test_inconsistent_assembled_and_waiting_counts_never_query_line_side_inventory() -> None:
    mes = Mes(head("CONFIRMED", 12))
    data = cast(dict[str, object], mes.execution["data"])
    progress = cast(dict[str, object], data["progress"])
    progress["assembled"] = 3
    result = mes.query()
    assert first(result)["assessment"] == "unavailable"
    assert result.details is not None and result.details["coverage"] == "partial"
    assert AVAILABLE not in {path for path, _ in mes.requests}


def _assert_invalidated(result: ToolResult) -> None:
    order = first(result)
    assert order["assessment"] == "unavailable"
    assert order["material_scope"] == "unavailable"
    assert order["waitingAssembly"] is None and order["materials"] == []
    assert order["workorderId"] == "11" and order["workorderCode"] == "WO-11"
    assert order.get("warnings")
    assert result.details is not None and result.details["coverage"] == "partial"


def test_report_between_execution_detail_and_line_stock_invalidates_old_remaining() -> None:
    mes = Mes(head("CONFIRMED", 12))

    def report(path: str) -> None:
        if path == AVAILABLE:
            mes.header["version"] = 3
            mes.available_rows = []

    mes.on_request = report
    result = mes.query()
    _assert_invalidated(result)
    assert [path for path, _ in mes.requests].count(ORDER) == 2


def test_draft_release_between_bom_and_stock_invalidates_warehouse_comparison() -> None:
    mes = Mes()

    def release(path: str) -> None:
        if path == STOCK:
            mes.header["version"] = 3
            mes.header["status"] = "CONFIRMED"

    mes.on_request = release
    result = mes.query()
    _assert_invalidated(result)
    assert [path for path, _ in mes.requests].count(ORDER) == 2


def test_independent_draft_bom_change_with_same_order_version_invalidates_requirements() -> None:
    mes = Mes()

    def edit_bom(path: str) -> None:
        if path == STOCK:
            mes.bom_rows[0]["quantity"] = 240

    mes.on_request = edit_bom
    result = mes.query()
    _assert_invalidated(result)
    assert [path for path, _ in mes.requests].count(BOM) == 2


def test_recheck_failure_and_missing_initial_version_cannot_confirm_draft_stock() -> None:
    mes = Mes()

    def deny_again(path: str) -> None:
        if path == STOCK:
            mes.override[ORDER] = {"code": 403, "msg": "denied"}

    mes.on_request = deny_again
    _assert_invalidated(mes.query())
    missing_version = Mes()
    missing_version.header.pop("version")
    _assert_invalidated(missing_version.query())


@pytest.mark.parametrize("status", ["CONFIRMED", "FINISHED"])
def test_no_waiting_assembly_never_queries_stock_or_claims_quality_completion(status: str) -> None:
    mes = Mes(head(status, 12))
    mes.execution = detail(status=status, waiting=0)
    order = first(mes.query())
    assert order["assessment"] == "no_remaining" and order["waitingAssembly"] == 0
    assert all(row["remaining_required"] == "0" and row["stock_quantity"] is None
               for row in materials(order))
    assert AVAILABLE not in {path for path, _ in mes.requests}


def test_incomplete_pagination_never_uses_partial_stock_as_total(monkeypatch: pytest.MonkeyPatch) -> None:
    from ktg_agent.ktg import workorder_materials as module
    monkeypatch.setattr(module, "_MAX_PAGES", 1)
    mes = Mes()
    mes.stock_rows = [stock(i, 1, 200) for i in range(1, 22)] + [stock(22, 2, 80)]
    result = mes.query()
    assert result.details is not None and result.details["coverage"] == "partial"
    assert materials(first(result))[0]["stock_quantity"] is None


@pytest.mark.parametrize("path", [BOM, STOCK, EXECUTION + "11", AVAILABLE])
def test_backend_permission_failure_is_not_zero(path: str) -> None:
    mes = Mes(head("CONFIRMED", 12) if path in {EXECUTION + "11", AVAILABLE} else head())
    mes.override[path] = {"code": 403, "msg": "denied"}
    result = mes.query()
    assert first(result)["assessment"] == "unavailable"
    assert result.details is not None and result.details["coverage"] == "partial"


def test_expired_auth_fails_instead_of_reusing_results() -> None:
    mes = Mes()
    mes.override[STOCK] = {"code": 401, "msg": "expired"}
    assert mes.query().error_code == "AUTH_EXPIRED"


def test_missing_code_and_unknown_status_are_explicit() -> None:
    mes = Mes(head("APPROVING"))
    result = mes.query({"workorder_codes": ["WO-11", "MISSING"]})
    assert first(result)["assessment"] == "unavailable"
    assert result.details is not None and result.details["missing_codes"] == ["MISSING"]
    assert result.details["coverage"] == "partial"


@pytest.mark.parametrize("arguments", [{"workorder_codes": []}, {"workorder_codes": ["WO-11", " wo-11 "]},
                                        {"workorder_codes": [" "]}, {"workorder_codes": ["WO-11"], "include_batches": 1}])
def test_invalid_params_make_no_mes_requests(arguments: JsonObject) -> None:
    mes = Mes()
    assert mes.query(arguments).error_code == "VALIDATION_ERROR"
    assert not mes.requests
