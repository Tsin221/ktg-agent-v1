"""Exact cross-domain lookup without guessing prefixes or hiding colliding identities."""
import asyncio
from collections.abc import Mapping

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.business_reference import build_business_reference_tool
from ktg_agent.tools import ToolContext

CODE = "LAMPBAT-00000023"


def execute(code: str, responses: dict[str, object]):
    calls: list[str] = []

    async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        path = url.removeprefix("http://mes.test")
        calls.append(path)
        assert code in params.values()
        return responses.get(path, {"code": 404} if path.endswith("/units/trace") else {"code": 200, "total": 0, "rows": []})

    client = KTGClient(base_url="http://mes.test", authorization="Bearer test", transport=transport)
    result = asyncio.run(build_business_reference_tool().execute(ToolContext("run", "call", ktg_client=client), {"code": code}))
    assert result.details is not None
    return result.details, calls


def page(*rows: dict[str, object]) -> dict[str, object]:
    return {"code": 200, "total": len(rows), "rows": list(rows)}


def test_bare_sn_returns_workorder_and_current_state_even_when_no_stock_records():
    details, calls = execute(CODE, {"/mes/pro/execution/units/trace": {"code": 200, "data": {
        "traceStatus": "TRACKED", "snId": "23", "snCode": CODE, "workorderId": "98",
        "workorderCode": "WO-LAMP-RETAIL", "workorderName": "阅读灯补单", "itemCode": "LAMP-BATT-MODULE", "itemName": "电池模组", "unitState": "WAIT_DISPOSITION"}}})
    assert details["coverage"] == "complete"
    assert details["matches"] == [{"type": "sn", "code": CODE, "sn_id": "23", "workorder_id": "98", "workorder_code": "WO-LAMP-RETAIL", "workorder_name": "阅读灯补单", "item_code": "LAMP-BATT-MODULE", "item_name": "电池模组", "unit_state": "WAIT_DISPOSITION", "trace_status": "TRACKED"}]
    assert len(calls) == 5


def test_identifier_collision_is_preserved_instead_of_guessing_a_single_type():
    code = "SHARED-123"
    details, _ = execute(code, {
        "/mes/pro/workorder/list": page({"workorderId": 100, "workorderCode": code, "workorderName": "工单", "status": "PREPARE"}, {"workorderId": 101, "workorderCode": code+"-other"}),
        "/mes/md/mditem/list": page({"itemId": 9, "itemCode": code, "itemName": "物料", "itemOrProduct": "ITEM"}),
    })
    assert {m["type"] for m in details["matches"]} == {"item", "workorder"}
    assert details["resolution"] == "ambiguous"


def test_batch_identified_from_receipt_when_no_onhand_and_deduplicated_across_sources():
    code = "FULLY-CONSUMED"
    details, _ = execute(code, {
        "/mes/wm/wmstock/list": page({"materialStockId": 1,"batchCode": code,"itemId": 9,"itemCode": "CELL","itemName":"电芯"}),
        "/mes/wm/itemrecptline/list": page({"lineId": 21,"batchCode": code,"itemId": 9,"itemCode": "CELL","itemName":"电芯"},
            {"lineId": 22,"batchCode": code,"itemId": 10,"itemCode": "PCB","itemName":"保护板"}),
    })
    batches = {m["item_code"]: m for m in details["matches"]}
    assert batches["CELL"]["sources"] == ["stock", "receipt_line"]
    assert batches["PCB"]["sources"] == ["receipt_line"]
    assert details["resolution"] == "ambiguous"


def test_unavailable_source_never_becomes_a_complete_negative():
    details, _ = execute("UNKNOWN-123", {"/mes/md/mditem/list": {"code": 403}})
    assert details["coverage"] == "partial"
    assert details["resolution"] == "unconfirmed"
    assert details["checks"]["item"] == "unavailable"


def test_malformed_sn_response_is_not_treated_as_proof_of_absence():
    details, _ = execute(CODE, {"/mes/pro/execution/units/trace": {"code": 200, "data": {"snCode": CODE, "traceStatus": []}}})
    assert details["resolution"] == "unconfirmed"
    assert details["checks"]["sn"] == "unavailable"
