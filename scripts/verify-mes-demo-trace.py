"""Independently verify the formal demo's actual production trace records."""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
from collections import Counter
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ktg_agent.ktg.client import KTGClient, KTGError

_DEMO = runpy.run_path(str(Path(__file__).with_name("verify-mes-demo-materials.py")))
RUNTIME, DIRECT_MES, DIRECT_AGENT, GATEWAY = (_DEMO[key] for key in ("RUNTIME", "DIRECT_MES", "DIRECT_AGENT", "GATEWAY"))
login, request_json, sse_events = (_DEMO[key] for key in ("login", "request_json", "sse_events"))
TOOL = "ktg_query_production_trace"
EXPECTED_TOOLS = set(_DEMO["EXPECTED_TOOLS"]) | {TOOL}
RETAIL, HOME, OFFICE, STOCK = (_DEMO[key] for key in ("RETAIL", "HOME", "OFFICE", "STOCK"))
CELL, BOARD = (_DEMO[key] for key in ("CELL", "BOARD"))
REJECTED = "LAMPBAT-00000023"
PASSED = "LAMPBAT-00000021"
UNINSPECTED = "LAMPBAT-00000011"
WAITING = "LAMPBAT-00000031"
PACKED = "LAMPBAT-00000001"
MISSING = "LAMPBAT-DOES-NOT-EXIST-DEMO"
RETAIL_CELL_BATCH = "CELL-20261124-RETAIL-MORNING"
RETAIL_BOARD_BATCH = "BMS-20261124-RETAIL-INBOUND"
HOME_CELL_BATCH = "CELL-20261120-HOME-MORNING"
OFFICE_CELL_BATCH = "CELL-20261124-OFFICE-MORNING"
SCOPE_PERMISSIONS = {"mes:pro:execution:trace", "mes:pro:execution:query", "mes:md:mditem:list"}


def numbered(n: int) -> str:
    return f"LAMPBAT-{n:08d}"


def batch_key(item_code: str, batch_code: str) -> str:
    return f"{item_code}|{batch_code}"


async def reverse_pages(client: KTGClient, item_id: str, batch_code: str, size: int = 3) -> dict[str, Any]:
    """Deliberately small pages exercise independent API total, dedup and coverage."""
    rows: list[dict[str, Any]] = []
    total = None
    for page in range(1, 21):
        response = await client.get("/mes/pro/execution/materials/trace", {
            "itemId": item_id, "batchCode": batch_code, "pageNum": page, "pageSize": size,
        })
        assert response.get("code") == 200
        known_total, page_rows = response["total"], response["rows"]
        assert type(known_total) is int and known_total >= 0
        assert total is None or total == known_total, "Source total changed while reading pages"
        assert isinstance(page_rows, list) and len(page_rows) <= size
        rows.extend(page_rows)
        total = known_total
        assert len({row["snId"] for row in rows}) == len(rows)
        if len(rows) == total:
            return {"rows": rows, "total": total, "pages": page, "page_size": size}
        assert page_rows and len(rows) < total
    raise RuntimeError("Source reverse trace pagination exceeded verification limit")


async def unit_reference(client: KTGClient, code: str) -> dict[str, Any]:
    response = await client.get("/mes/pro/execution/units/trace", {"snCode": code})
    assert response.get("code") == 200
    data = response["data"]
    assert isinstance(data, dict) and data.get("snCode") == code
    return data


async def reference(client: KTGClient) -> dict[str, Any]:
    items: dict[str, Any] = {}
    for code in (CELL, BOARD):
        response = await client.get("/mes/md/mditem/list", {"itemCode": code, "pageNum": 1, "pageSize": 10})
        rows = [row for row in response["rows"] if row.get("itemCode") == code]
        assert len(rows) == 1 and rows[0]["itemOrProduct"] == "ITEM"
        items[code] = {key: rows[0][key] for key in ("itemId", "itemCode", "itemName", "itemOrProduct")}

    units = {code: await unit_reference(client, code) for code in (REJECTED, PASSED, UNINSPECTED, WAITING, PACKED)}
    rejected = units[REJECTED]
    assert rejected["workorderCode"] == RETAIL and rejected["unitState"] == "WAIT_DISPOSITION"
    assert rejected["traceStatus"] == "TRACKED" and rejected["packing"] is None
    assert [(row["roundNo"], row["checkResult"]) for row in rejected["inspections"]] == [(1, "REJECT")]
    assert len(rejected["inputs"]) == 2
    assert {(row["itemCode"], row["sourceBatchCode"], row["quantity"], row["unitOfMeasure"])
            for row in rejected["inputs"]} == {(CELL, RETAIL_CELL_BATCH, 1, "PCS"), (BOARD, RETAIL_BOARD_BATCH, 1, "PCS")}
    # Per-SN facts differ from shared consumption-document transaction quantities.
    assert sorted(tx["transactionQuantity"] for row in rejected["inputs"] for tx in row["transactions"]) == [-10, -4]
    assert units[PASSED]["unitState"] == "WAIT_PACKING" and units[PASSED]["inspections"][0]["checkResult"] == "ACCEPT"
    assert units[UNINSPECTED]["unitState"] == "WAIT_INSPECTION" and len(units[UNINSPECTED]["inputs"]) == 2 and not units[UNINSPECTED]["inspections"]
    assert units[WAITING]["unitState"] == "WAIT_ASSEMBLY" and not units[WAITING]["inputs"] and not units[WAITING]["inspections"]
    assert units[PACKED]["unitState"] == "PACKED" and units[PACKED]["packing"]
    missing = None
    try:
        await unit_reference(client, MISSING)
    except KTGError as error:
        assert error.code not in {"AUTH_EXPIRED", "PERMISSION_DENIED"}
        missing = error.code
    assert missing is not None

    batches: dict[str, Any] = {}
    for code, batch in ((CELL, RETAIL_CELL_BATCH), (BOARD, RETAIL_BOARD_BATCH), (CELL, HOME_CELL_BATCH),
                        (CELL, OFFICE_CELL_BATCH), (BOARD, RETAIL_CELL_BATCH), (CELL, RETAIL_CELL_BATCH.lower())):
        result = await reverse_pages(client, str(items[code]["itemId"]), batch)
        batches[batch_key(code, batch)] = result
    for code, batch, expected_sn, states, order_code in (
        (CELL, RETAIL_CELL_BATCH, range(21, 25), {"WAIT_PACKING": 3, "WAIT_DISPOSITION": 1}, RETAIL),
        (BOARD, RETAIL_BOARD_BATCH, range(21, 31), {"WAIT_PACKING": 9, "WAIT_DISPOSITION": 1}, RETAIL),
        (CELL, HOME_CELL_BATCH, range(1, 5), {"PACKED": 4}, HOME),
        (CELL, OFFICE_CELL_BATCH, range(11, 15), {"WAIT_INSPECTION": 4}, OFFICE),
    ):
        result = batches[batch_key(code, batch)]
        assert [row["snCode"] for row in result["rows"]] == [numbered(n) for n in expected_sn]
        assert Counter(row["unitState"] for row in result["rows"]) == states
        assert all(row["workorderCode"] == order_code and row["itemCode"] == "LAMP-BATT-MODULE" for row in result["rows"])
    assert batches[batch_key(BOARD, RETAIL_CELL_BATCH)]["total"] == 0
    assert batches[batch_key(CELL, RETAIL_CELL_BATCH.lower())]["total"] == 0
    # Source truth for each affected unit is independent of reverse-row state.
    for row in batches[batch_key(CELL, RETAIL_CELL_BATCH)]["rows"]:
        code = row["snCode"]
        if code not in units:
            units[code] = await unit_reference(client, code)
        data = units[code]
        assert data["workorderCode"] == RETAIL and data["unitState"] == row["unitState"]
        assert any(inp["itemCode"] == CELL and inp["sourceBatchCode"] == RETAIL_CELL_BATCH for inp in data["inputs"])
        assert data["inspections"][-1]["checkResult"] == ("REJECT" if code == REJECTED else "ACCEPT")
    return {"items": items, "units": units, "batches": batches, "missing_unit": {"snCode": MISSING, "error_code": missing}}


def compare_unit(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    for field in ("snId", "snCode", "itemId", "itemCode", "itemName", "workorderId", "workorderCode", "workorderName", "orderStatus", "unitState"):
        assert actual.get(field) == expected.get(field), (field, actual.get(field), expected.get(field))
    assert actual["trace_state"] == "tracked"
    assert actual["input_state"] == ("waiting_assembly" if expected["unitState"] == "WAIT_ASSEMBLY" else "recorded")
    fields = ("inputId", "itemId", "itemCode", "itemName", "sourceBatchCode", "unitOfMeasure", "usedAt")
    assert len(actual["inputs"]) == len(expected["inputs"])
    old = {row["inputId"]: row for row in expected["inputs"]}
    assert len({row["inputId"] for row in actual["inputs"]}) == len(old)
    for row in actual["inputs"]:
        source = old[row["inputId"]]
        for key in fields:
            assert row[key] == source[key], (key, row)
        assert _DEMO["decimal"](row["quantity"]) == _DEMO["decimal"](source["quantity"])


def compare_batch(actual: dict[str, Any], source: dict[str, Any], item_code: str, batch_code: str, page: int, page_size: int) -> None:
    expected = source["batches"][batch_key(item_code, batch_code)]
    item = source["items"][item_code]
    assert actual["itemId"] == str(item["itemId"]) and actual["itemCode"] == item_code
    assert actual["batchCode"] == batch_code and actual["itemName"] == item["itemName"]
    assert actual["total_units"] == expected["total"]
    assert actual["page"] == page and actual["page_size"] == page_size
    expected_rows = expected["rows"][(page - 1) * page_size:page * page_size]
    assert actual["returned_units"] == len(expected_rows)
    fields = ("snId", "snCode", "itemCode", "itemName", "workorderId", "workorderCode", "workorderName", "orderStatus", "unitState", "firstUsedAt", "lastUsedAt", "inputCount")
    assert [{key: row.get(key) for key in fields} for row in actual["rows"]] == [{key: row.get(key) for key in fields} for row in expected_rows]
    complete = page == 1 and len(expected_rows) == expected["total"]
    assert actual["coverage"] == ("complete" if complete else "partial")
    c = Counter(row["unitState"] for row in expected_rows)
    # When a batch is partial, these counts describe this page only.
    assert actual["state_counts"] == {
        "waitingInspection": c["WAIT_INSPECTION"], "pendingDisposition": c["WAIT_DISPOSITION"],
        "reworkInProgress": c["REWORK_IN_PROGRESS"], "waitingReinspection": c["WAIT_REINSPECTION"],
        "waitingPacking": c["WAIT_PACKING"], "packed": c["PACKED"], "scrapped": c["SCRAPPED"],
        "qualified": c["WAIT_PACKING"] + c["PACKED"],
    }


async def natural_conversation(authorization: str) -> dict[str, Any]:
    """One opted-in conversation only. No submission retries or fixed tool calls."""
    session = request_json(GATEWAY + "/agent-api/v1/sessions", authorization, {"title": "正式演示验收：方向C双向追溯"})
    session_id = session["session_id"]
    print(f"Trace verification session: {session_id}", flush=True)
    prompts = (
        "床头阅读灯不合格的那件实际用了哪些原料？",
        "用同一批电芯的还有哪些？",
        "它们也都不合格吗？",
        "有多少已经包装了？",
        "能确定是电芯批次导致的吗？",
    )
    outcomes: list[dict[str, Any]] = []
    for prompt in prompts:
        run = request_json(GATEWAY + f"/agent-api/v1/sessions/{session_id}/runs", authorization,
                           {"request_id": str(uuid4()), "message": prompt})
        run_id = run["run_id"]
        print(f"Submitted one natural turn: {run_id}", flush=True)
        deadline = monotonic() + 115
        while True:
            snapshot = request_json(GATEWAY + f"/agent-api/v1/runs/{run_id}", authorization)
            if snapshot["status"] in {"completed", "failed", "cancelled", "needs_input", "needs_reconciliation"}:
                break
            if monotonic() >= deadline:
                outcomes.append({"question": prompt, "run_id": run_id, "status": "verification_timeout"})
                return {"session_id": session_id, "turns": outcomes, "completed_all": False}
            await asyncio.sleep(1)
        events = sse_events(GATEWAY, authorization, run_id)
        messages = request_json(GATEWAY + f"/agent-api/v1/sessions/{session_id}/messages?limit=100", authorization)
        assistant = [row for row in messages["items"] if row["run_id"] == run_id and row["role"] == "assistant"]
        calls = [block for message in assistant for block in message["content"] if block["type"] == "tool_call"]
        end_events = [event["payload"] for event in events if event["type"] == "tool_execution_end"]
        answer = "\n".join(block["text"] for block in assistant[-1]["content"] if block["type"] == "text") if assistant else ""
        outcomes.append({"question": prompt, "run_id": run_id, "status": snapshot["status"],
                         "calls": calls, "tool_results": end_events, "answer": answer, "event_count": len(events)})
        if snapshot["status"] != "completed":
            break
    successful = [event for row in outcomes for event in row["tool_results"] if event["status"] == "succeeded"]
    selected = [event["result"] for event in successful if event["tool_name"] == TOOL]
    unit_found = any(any(unit.get("snCode") == REJECTED and unit.get("input_state") == "recorded"
                         and {(row.get("itemCode"), row.get("sourceBatchCode")) for row in unit.get("inputs", [])}
                         == {(CELL, RETAIL_CELL_BATCH), (BOARD, RETAIL_BOARD_BATCH)}
                         for unit in result.get("units", [])) for result in selected)
    cell_batch_results = [batch for result in selected for batch in result.get("batches", [])
                          if batch.get("itemCode") == CELL and batch.get("batchCode") == RETAIL_CELL_BATCH and batch.get("total_units") == 4]
    batch_found = {row.get("snCode") for batch in cell_batch_results for row in batch.get("rows", [])} == {numbered(n) for n in range(21, 25)}
    quality_found = any(any(order.get("workorderCode") == RETAIL and any(unit.get("snCode") == REJECTED and unit.get("latest_result") == "REJECT"
                         for unit in order.get("units", [])) for order in event["result"].get("orders", []))
                        for event in successful if event["tool_name"] == "ktg_query_workorder_quality")
    return {"session_id": session_id, "turns": outcomes,
            "completed_all": len(outcomes) == len(prompts) and all(row["status"] == "completed" for row in outcomes),
            "required_tool_evidence": {"unit_found": unit_found, "batch_found": batch_found, "quality_found": quality_found,
                                       "all_successful_calls_read_only": all(event["effect"] == "read" for event in successful)}}


async def main_async(via_gateway: bool, reference_only: bool, source_only: bool, model_conversation: bool) -> dict[str, Any]:
    mes_base = GATEWAY + "/prod-api" if via_gateway else DIRECT_MES
    authorization = login(mes_base)
    client = KTGClient(base_url=mes_base, authorization=authorization)
    identity = await client.get_current_user()
    assert identity.get("userName") == "lamp_leader"
    permissions = frozenset((await client.get("/getInfo"))["permissions"])
    assert "*:*:*" not in permissions and SCOPE_PERMISSIONS <= permissions
    facts = await reference(client)
    evidence: dict[str, Any] = {"mode": "gateway" if via_gateway else "direct", "user": identity["userName"],
                                "permissions": sorted(SCOPE_PERMISSIONS), "legacy_sn_query_allowed": "mes:wm:sn:query" in permissions,
                                "reference": facts}
    if reference_only:
        return evidence
    from ktg_agent.core.messages import ToolCall
    from ktg_agent.providers.base import ModelConfig
    from ktg_agent.providers.fake import FakeProvider
    from ktg_agent.service import QueryService
    from ktg_agent.tools.catalog import CatalogIdentity
    from ktg_agent.tools.registry import ToolContext

    if not source_only:
        remote = request_json((GATEWAY if via_gateway else DIRECT_AGENT) + "/agent-api/v1/tools", authorization)
        assert {row["name"] for row in remote["items"]} == EXPECTED_TOOLS
    service = QueryService(provider=FakeProvider(()), model_config=ModelConfig("offline-verification", 128),
                           ktg_client_factory=lambda _: client)
    run_id = str(uuid4())
    candidate = service._catalog.candidate_snapshot(CatalogIdentity(permissions, run_id), service._switches_provider())
    assert set(candidate.candidates) == EXPECTED_TOOLS
    runner = service._tool_runner(candidate)
    evidence["catalog"] = sorted(EXPECTED_TOOLS)
    evidence["observations"] = observations = []

    async def query(args: dict[str, Any]) -> dict[str, Any]:
        call = ToolCall(str(uuid4()), TOOL, args)
        result = await runner.run(call, ToolContext(run_id, call.id, identity=identity, ktg_client=client))
        assert not result.is_error and isinstance(result.details, dict), result.error_code
        details = result.details
        assert details["kind"] == "production_trace" and details["direction"] == args["direction"]
        observations.append({"args": args, "details": details})
        return details

    u = await query({"direction": "unit_to_material", "sn_codes": [REJECTED, PASSED, UNINSPECTED, WAITING, PACKED]})
    assert u["coverage"] == "complete" and len(u["units"]) == 5
    for row in u["units"]:
        compare_unit(row, facts["units"][row["snCode"]])
    u = await query({"direction": "unit_to_material", "sn_codes": [MISSING]})
    assert u["coverage"] != "complete" and u["units"][0]["trace_state"] == "unavailable"

    for pairs, page, size in (
        ([(CELL, RETAIL_CELL_BATCH), (BOARD, RETAIL_BOARD_BATCH)], 1, 20),
        ([(CELL, HOME_CELL_BATCH), (CELL, OFFICE_CELL_BATCH)], 1, 20),
        ([(BOARD, RETAIL_CELL_BATCH), (CELL, RETAIL_CELL_BATCH.lower())], 1, 20),
        ([(BOARD, RETAIL_BOARD_BATCH)], 1, 3),
        ([(BOARD, RETAIL_BOARD_BATCH)], 2, 3),
    ):
        details = await query({"direction": "material_to_unit", "batches": [{"item_code": item, "batch_code": batch} for item, batch in pairs],
                               "page": page, "page_size": size})
        assert len(details["batches"]) == len(pairs)
        for result in details["batches"]:
            compare_batch(result, facts, result["itemCode"], result["batchCode"], page, size)
    if model_conversation:
        evidence["natural_conversation"] = await natural_conversation(authorization)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--via-gateway", action="store_true")
    parser.add_argument("--model-conversation", action="store_true", help="Explicitly authorized one real five-turn conversation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.model_conversation and (args.reference_only or args.source_only or not args.via_gateway):
        parser.error("--model-conversation requires --via-gateway and full tool verification")
    evidence = asyncio.run(main_async(args.via_gateway, args.reference_only, args.source_only, args.model_conversation))
    if args.output:
        output = args.output.resolve()
        assert output.parent == RUNTIME.resolve(), "Evidence must stay inside dedicated demo runtime"
        with output.open("x", encoding="utf-8") as file:
            json.dump(evidence, file, ensure_ascii=False, indent=2, default=_DEMO["json_decimal"])
            file.write("\n")
        print(f"Trace evidence saved to {output}")
    else:
        print(json.dumps(evidence, ensure_ascii=True, indent=2, default=_DEMO["json_decimal"]))


if __name__ == "__main__":
    main()
