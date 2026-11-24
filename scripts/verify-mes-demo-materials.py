"""Compare formal MES material facts with the new read-only Agent tool."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

from ktg_agent.ktg.client import KTGClient

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/mes-demo/agent"
DIRECT_MES = "http://127.0.0.1:18090"
DIRECT_AGENT = "http://127.0.0.1:18091"
GATEWAY = "http://127.0.0.1:18854"
TOOL = "ktg_query_workorder_materials"
EXPECTED_TOOLS = {
    "ktg_count_products", "ktg_query_stock_summary",
    "ktg_query_workorder_overview", TOOL, "ktg_query_workorder_quality",
}
WINTER = "WO-LAMP-20261130-WINTER"
OFFICE = "WO-LAMP-20261124-OFFICE"
STOCK = "WO-LAMP-20261124-STOCK"
HOME = "WO-LAMP-20261120-HOME"
RETAIL = "WO-LAMP-20261124-RETAIL"
CELL = "CELL-18650-2200"
BOARD = "PCB-BMS-1S"
VIRTUAL = "XBK_VIRTUAL"


def request_json(url: str, authorization: str | None = None,
                 body: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url, data=data, headers=headers), timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("Expected a JSON object")
    return value


def login(mes_base: str) -> str:
    account = json.loads((ROOT / ".runtime/mes-demo/accounts.json").read_text(encoding="utf-8-sig"))["leader"]
    if account["username"] != "lamp_leader":
        raise RuntimeError("Unexpected formal demo account")
    result = request_json(mes_base + "/login", body={
        "username": account["username"], "password": account["password"],
    })
    token = result.get("token")
    if result.get("code") != 200 or not isinstance(token, str) or not token:
        raise RuntimeError("Formal demo login failed")
    return "Bearer " + token


def decimal(value: Any) -> Decimal:
    assert value is not None and type(value) is not bool
    quantity = Decimal(str(value))
    assert quantity.is_finite()
    return quantity


def json_decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(type(value).__name__)


async def all_rows(client: KTGClient, path: str,
                   filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    known_total: int | None = None
    for page in range(1, 21):
        payload = await client.get(path, {**(filters or {}), "pageNum": page, "pageSize": 100})
        total, rows = payload["total"], payload["rows"]
        assert type(total) is int and total >= 0 and isinstance(rows, list)
        assert known_total is None or known_total == total, f"Source total changed: {path}"
        result.extend(rows)
        known_total = total
        if len(result) == total:
            return result
        assert rows and len(result) < total, f"Source pagination incomplete: {path}"
    raise RuntimeError(f"Reference pagination exceeded its verification limit: {path}")


def requirements(rows: list[dict[str, Any]], frozen: bool) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in rows:
        def field(camel: str, snake: str) -> Any:
            return source[snake if frozen else camel]
        assert field("itemOrProduct", "item_or_product") == "ITEM"
        item_code = field("itemCode", "item_code")
        row = {
            "item_id": str(field("itemId", "item_id")),
            "item_code": item_code,
            "unit": field("unitOfMeasure", "unit_of_measure"),
            "total": decimal(source["quantity"]),
        }
        assert row["total"] > 0 and row["unit"]
        if item_code in result:
            assert row["item_id"] == result[item_code]["item_id"] and row["unit"] == result[item_code]["unit"]
            result[item_code]["total"] += row["total"]
        else:
            result[item_code] = row
    assert set(result) == {CELL, BOARD}
    return result


async def reference(client: KTGClient) -> dict[str, Any]:
    orders = await all_rows(client, "/mes/pro/workorder/list")
    by_code = {row["workorderCode"]: row for row in orders}
    assert set(by_code) == {WINTER, HOME, OFFICE, RETAIL, STOCK}
    warehouses = await all_rows(client, "/mes/wm/warehouse/list")
    by_warehouse = {str(row["warehouseId"]): row["warehouseCode"] for row in warehouses}
    assert by_warehouse == {"200": VIRTUAL, "930004": "WH-LAMP-RM"}
    warehouse_facts = [{"warehouse_id": str(row["warehouseId"]), "warehouse_code": row["warehouseCode"],
                        "warehouse_name": row["warehouseName"], "virtual": row["warehouseCode"] == VIRTUAL}
                       for row in warehouses]
    draft = by_code[WINTER]
    draft_id = str(draft["workorderId"])
    assert draft["status"] == "PREPARE" and decimal(draft["quantity"]) == 120
    bom = await all_rows(client, "/mes/pro/workorderbom/list", {"workorderId": draft_id})
    assert all(str(row["workorderId"]) == draft_id for row in bom)
    draft_requirements = requirements(bom, frozen=False)
    source_stock: dict[str, list[dict[str, Any]]] = {}
    draft_materials: dict[str, Any] = {}
    for code, req in draft_requirements.items():
        rows = await all_rows(client, "/mes/wm/wmstock/list", {"itemId": req["item_id"]})
        for row in rows:
            assert str(row["itemId"]) == req["item_id"]
            assert row["itemCode"] == code
            assert by_warehouse[str(row["warehouseId"])] == row["warehouseCode"], "Unknown warehouse classification"
        related = [row for row in rows if row["unitOfMeasure"] == req["unit"]]
        eligible = [row for row in related if row["warehouseCode"] != VIRTUAL
                    and row.get("workorderId") is None and row.get("workorderCode") is None
                    and decimal(row["quantityOnhand"]) > 0]
        excluded = [row for row in related if row["materialStockId"] not in {item["materialStockId"] for item in eligible}]
        available = sum((decimal(row["quantityOnhand"]) for row in eligible), Decimal(0))
        summary = (await client.get("/mes/wm/wmstock/summary", {"itemCode": code}))["data"]["groups"]
        summary_physical = sum((decimal(group["quantityOnhand"]) for group in summary
                                if str(group["itemId"]) == req["item_id"] and group["unitOfMeasure"] == req["unit"]
                                and group["warehouseCode"] != VIRTUAL), Decimal(0))
        assert summary_physical == available, "Formal physical warehouse summary differs from unbound detail proof"
        source_stock[code] = related
        draft_materials[code] = {
            **req, "required": req["total"], "available": available,
            "shortage": max(Decimal(0), req["total"] - available),
            "summary_physical": summary_physical,
            "physical_batches": [{
                "stock_id": str(row["materialStockId"]), "batch_code": row["batchCode"],
                "warehouse_id": str(row["warehouseId"]), "quantity": decimal(row["quantityOnhand"]),
                "workorder_id": row.get("workorderId"), "workorder_code": row.get("workorderCode"),
            } for row in eligible],
            "excluded_batches": [{
                "stock_id": str(row["materialStockId"]), "warehouse_code": row["warehouseCode"],
                "batch_code": row["batchCode"], "quantity": decimal(row["quantityOnhand"]),
                "workorder_id": str(row["workorderId"]) if row.get("workorderId") is not None else None,
                "workorder_code": row.get("workorderCode"),
            } for row in excluded],
        }
    assert draft_materials[CELL]["required"] == draft_materials[BOARD]["required"] == 120
    assert draft_materials[CELL]["available"] == 160 and draft_materials[CELL]["shortage"] == 0
    assert draft_materials[BOARD]["available"] == 80 and draft_materials[BOARD]["shortage"] == 40
    assert sum((row["quantity"] for row in draft_materials[CELL]["excluded_batches"]), Decimal(0)) == 16

    released: dict[str, Any] = {}
    line_side_owners: dict[str, str] = {}
    for code in (OFFICE, STOCK, HOME, RETAIL):
        order = by_code[code]
        order_id = str(order["workorderId"])
        detail = (await client.get(f"/mes/pro/execution/workorders/{order_id}"))["data"]
        frozen = detail["order"]
        progress = detail["progress"]
        assert str(frozen["workorderId"]) == order_id and frozen["status"] == order["status"]
        assert str(progress["workorderId"]) == order_id and progress["status"] == order["status"]
        planned = decimal(frozen["plannedUnits"])
        waiting = decimal(progress["waitingAssembly"])
        assert decimal(order["quantity"]) == planned == decimal(progress["planned"])
        assert 0 <= waiting <= planned
        snapshot = json.loads(frozen["materialRequirementsSnapshot"])
        assert all(str(row["workorder_id"]) == order_id for row in snapshot)
        reqs = requirements(snapshot, frozen=True)
        tasks = [row for row in detail["tasks"] if row["taskKind"] == "NORMAL" and row["stepKind"] == "ASSEMBLY"]
        assert len(tasks) == 1
        task_id = str(tasks[0]["taskId"])
        available_rows = await all_rows(client, f"/mes/pro/execution/tasks/{task_id}/available-materials")
        materials: dict[str, Any] = {}
        for item_code, req in reqs.items():
            per_unit = req["total"] / planned
            assert per_unit.quantize(Decimal("0.01")) == per_unit, "Frozen requirement cannot be allocated exactly"
            need = per_unit * waiting
            eligible = [row for row in available_rows if row["itemCode"] == item_code
                        and str(row["itemId"]) == req["item_id"] and row["unitOfMeasure"] == req["unit"]]
            available = sum((decimal(row["quantityOnhand"]) for row in eligible), Decimal(0))
            # Independently prove that every execution material row is the requested
            # workorder's own virtual stock, rather than another order's balance.
            for row in eligible:
                stock_id = str(row["materialStockId"])
                assert stock_id not in line_side_owners, "Line-side stock record shared by separate orders"
                line_side_owners[stock_id] = code
                same = [item for item in source_stock[item_code] if str(item["materialStockId"]) == stock_id]
                assert len(same) == 1
                global_row = same[0]
                assert str(global_row["workorderId"]) == order_id and global_row["workorderCode"] == code
                assert global_row["warehouseCode"] == VIRTUAL
                assert str(global_row["warehouseId"]) == str(row["warehouseId"]) == "200"
                assert global_row["batchCode"] == row["sourceBatchCode"]
                assert decimal(global_row["quantityOnhand"]) == decimal(row["quantityOnhand"])
            materials[item_code] = {
                **req, "per_unit": per_unit, "required": need, "available": available,
                "shortage": max(Decimal(0), need - available),
                "line_side_batches": [{
                    "stock_id": str(row["materialStockId"]), "batch_code": row["sourceBatchCode"],
                    "warehouse_id": str(row["warehouseId"]), "quantity": decimal(row["quantityOnhand"]),
                    "workorder_id": order_id, "workorder_code": code,
                } for row in eligible],
            }
        assert sum(len(row["line_side_batches"]) for row in materials.values()) == len(available_rows)
        released[code] = {
            "workorder_id": order_id, "status": order["status"],
            "planned": planned, "waiting_assembly": waiting, "assembly_task_id": task_id,
            "materials": materials,
        }
    assert released[OFFICE]["waiting_assembly"] == 6
    assert released[STOCK]["waiting_assembly"] == 10
    assert released[OFFICE]["materials"][CELL]["available"] == released[OFFICE]["materials"][BOARD]["available"] == 6
    assert released[STOCK]["materials"][CELL]["available"] == released[STOCK]["materials"][BOARD]["available"] == 10
    assert sorted(row["quantity"] for row in released[STOCK]["materials"][CELL]["line_side_batches"]) == [4, 6]
    assert released[HOME]["waiting_assembly"] == released[RETAIL]["waiting_assembly"] == 0
    return {
        "warehouses": warehouse_facts,
        "draft": {
            "workorder_code": WINTER, "workorder_id": draft_id, "status": draft["status"],
            "planned": decimal(draft["quantity"]), "materials": draft_materials,
        },
        "released": released,
        "line_side_stock_owner": line_side_owners,
        "scope_note": "Physical unbound on-hand is not a reservation or promise. Released remaining need uses the frozen BOM and current waitingAssembly; line-side stock is obtained only from this workorder's assembly task.",
    }


def assert_material_output(details: dict[str, Any], source: dict[str, Any],
                           codes: list[str], include_batches: bool) -> None:
    """Verify the public contract rather than the handler's private helpers."""
    assert details["kind"] == "workorder_materials"
    assert details["filters"] == {"workorder_codes": codes, "include_batches": include_batches}
    assert details["coverage"] == "complete" and details["missing_codes"] == []
    assert [row["workorderCode"] for row in details["orders"]] == codes
    for order in details["orders"]:
        code = order["workorderCode"]
        draft = code == WINTER
        expected = source["draft"] if draft else source["released"][code]
        assert str(order["workorderId"]) == expected["workorder_id"]
        assert order["status"] == expected["status"] and decimal(order["quantity"]) == expected["planned"]
        assert order["material_scope"] == ("warehouse_reference" if draft else "line_side")
        waiting = None if draft else expected["waiting_assembly"]
        assert order["waitingAssembly"] == waiting
        assessment = ("reference_shortage" if draft else
                      "no_remaining" if waiting == 0 else "line_side_sufficient")
        assert order["assessment"] == assessment
        materials = expected["materials"]
        assert {row["itemCode"] for row in order["materials"]} == set(materials)
        for material in order["materials"]:
            fact = materials[material["itemCode"]]
            assert str(material["itemId"]) == fact["item_id"] and material["unitOfMeasure"] == fact["unit"]
            assert decimal(material["planned_required"]) == fact["total"]
            assert decimal(material["remaining_required"]) == fact["required"]
            assert decimal(material["shortage_quantity"]) == fact["shortage"]
            if waiting == 0:
                # The tool intentionally does not imply that it checked remaining
                # stock when there is no outstanding assembly requirement.
                assert material["stock_state"] == "unavailable" and material["stock_quantity"] is None
                assert "batches" not in material
                continue
            assert material["stock_state"] == "verified"
            assert decimal(material["stock_quantity"]) == fact["available"]
            if draft:
                assert [(row["warehouseId"], row["warehouseCode"], decimal(row["quantity"]))
                        for row in material["warehouses"]] == [("930004", "WH-LAMP-RM", fact["available"])]
            else:
                assert not material.get("warehouses")
            if include_batches:
                grouped: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
                stock_rows = fact["physical_batches"] if draft else fact["line_side_batches"]
                for batch in stock_rows:
                    grouped[("WH-LAMP-RM" if draft else VIRTUAL, batch["batch_code"])] += batch["quantity"]
                returned = {(batch["warehouseCode"], batch["batchCode"]): decimal(batch["quantity"])
                            for batch in material["batches"]}
                assert len(returned) == len(material["batches"])
                assert returned == dict(grouped), f"Unexpected batch scope for {code}/{material['itemCode']}"
            else:
                assert "batches" not in material


def sse_events(agent_base: str, authorization: str, run_id: str) -> list[dict[str, Any]]:
    url = agent_base + f"/agent-api/v1/runs/{run_id}/events?after_seq=0"
    with urlopen(Request(url, headers={"Authorization": authorization}), timeout=20) as response:
        assert response.status == 200 and "text/event-stream" in response.headers["Content-Type"]
        body = response.read().decode("utf-8")
    events = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        data = [line[6:] for line in frame.split("\n") if line.startswith("data: ")]
        if data:
            events.append(json.loads("\n".join(data)))
    assert events and events[-1]["type"] == "run_settled"
    return events


async def natural_conversation(agent_base: str, authorization: str) -> dict[str, Any]:
    """One explicit opt-in real model conversation, with no submission retries."""
    session = request_json(agent_base + "/agent-api/v1/sessions", authorization,
                           {"title": "正式演示验收：工单备料多轮"})
    session_id = session["session_id"]
    outcomes = []
    prompts = (
        ("那个冬季新品工单怎么样？", "ktg_query_workorder_overview", WINTER),
        ("那它的料够吗？", TOOL, WINTER),
        ("办公台灯呢？", TOOL, OFFICE),
    )
    for message, required_tool, workorder_code in prompts:
        run = request_json(agent_base + f"/agent-api/v1/sessions/{session_id}/runs", authorization,
                           {"request_id": str(uuid4()), "message": message})
        run_id = run["run_id"]
        print(f"Submitted one natural turn: {run_id}", flush=True)
        deadline = monotonic() + 115
        while True:
            snapshot = request_json(agent_base + f"/agent-api/v1/runs/{run_id}", authorization)
            if snapshot["status"] in {"completed", "failed", "cancelled", "needs_input", "needs_reconciliation"}:
                break
            if monotonic() >= deadline:
                outcomes.append({"question": message, "run_id": run_id, "status": "verification_timeout"})
                return {"session_id": session_id, "turns": outcomes, "object_context_passed": False}
            await asyncio.sleep(1)
        events = sse_events(agent_base, authorization, run_id)
        message_page = request_json(agent_base + f"/agent-api/v1/sessions/{session_id}/messages?limit=100", authorization)
        run_messages = [item for item in message_page["items"] if item["run_id"] == run_id]
        calls = [block for item in run_messages if item["role"] == "assistant"
                 for block in item["content"] if block["type"] == "tool_call"]
        end_events = [event["payload"] for event in events if event["type"] == "tool_execution_end"]
        successful = {item["tool_call_id"] for item in end_events if item["status"] == "succeeded"}
        selected = [call for call in calls if call["name"] == required_tool and call["id"] in successful]
        if required_tool == TOOL:
            object_match = any(call["arguments"].get("workorder_codes") == [workorder_code] for call in selected)
        else:
            object_match = any(workorder_code in json.dumps(item.get("result", {}), ensure_ascii=True)
                               for item in end_events if item["tool_call_id"] in {call["id"] for call in selected})
        assistants = [item for item in run_messages if item["role"] == "assistant"]
        answer = "\n".join(block["text"] for block in assistants[-1]["content"] if block["type"] == "text") if assistants else ""
        outcomes.append({
            "question": message, "run_id": run_id, "status": snapshot["status"],
            "stop_reason": snapshot["stop_reason"], "model_requests": len(snapshot.get("model_requests", [])),
            "calls": calls, "tool_results": end_events, "answer": answer,
            "required_tool": required_tool, "expected_workorder_code": workorder_code,
            "object_context_match": object_match, "event_count": len(events),
        })
        if snapshot["status"] != "completed":
            break
    return {
        "session_id": session_id, "turns": outcomes,
        "object_context_passed": len(outcomes) == len(prompts) and all(
            turn.get("status") == "completed" and turn.get("object_context_match") for turn in outcomes
        ),
    }


async def main_async(via_gateway: bool, reference_only: bool, model_conversation: bool) -> dict[str, Any]:
    mes_base = GATEWAY + "/prod-api" if via_gateway else DIRECT_MES
    agent_base = GATEWAY if via_gateway else DIRECT_AGENT
    authorization = login(mes_base)
    client = KTGClient(base_url=mes_base, authorization=authorization)
    identity = await client.get_current_user()
    assert identity.get("userName") == "lamp_leader"
    info = await client.get("/getInfo")
    permissions = frozenset(info["permissions"])
    assert "*:*:*" not in permissions
    assert {"mes:pro:workorder:list", "mes:pro:workorderbom:list", "mes:pro:execution:query",
            "mes:wm:wmstock:list", "mes:wm:warehouse:list"} <= permissions
    results: dict[str, Any] = {
        "mode": "same-origin-gateway" if via_gateway else "direct",
        "user": identity["userName"],
        "reference": await reference(client),
    }
    if not reference_only:
        from ktg_agent.core.messages import ToolCall
        from ktg_agent.providers.base import ModelConfig
        from ktg_agent.providers.fake import FakeProvider
        from ktg_agent.service import QueryService
        from ktg_agent.tools.catalog import CatalogIdentity
        from ktg_agent.tools.registry import ToolContext

        remote = request_json(agent_base + "/agent-api/v1/tools", authorization)
        assert {item["name"] for item in remote["items"]} == EXPECTED_TOOLS
        service = QueryService(provider=FakeProvider(()), model_config=ModelConfig("offline-verification", 128),
                               ktg_client_factory=lambda _: client)
        run_id = str(uuid4())
        candidate = service._catalog.candidate_snapshot(CatalogIdentity(permissions, run_id), service._switches_provider())
        assert set(candidate.candidates) == EXPECTED_TOOLS
        runner = service._tool_runner(candidate)
        observations = []
        for codes, include_batches in [
            ([WINTER], False), ([OFFICE, STOCK], True),
            ([WINTER, OFFICE, STOCK, HOME, RETAIL], True),
        ]:
            call = ToolCall(str(uuid4()), TOOL, {"workorder_codes": codes, "include_batches": include_batches})
            result = await runner.run(call, ToolContext(run_id, call.id, identity=identity, ktg_client=client))
            if result.is_error or not isinstance(result.details, dict):
                raise RuntimeError(f"Material tool returned an error ({result.error_code})")
            assert_material_output(result.details, results["reference"], codes, include_batches)
            observations.append({"codes": codes, "include_batches": include_batches, "details": result.details})
        results["catalog"] = sorted(EXPECTED_TOOLS)
        results["tool_observations"] = observations
        if model_conversation:
            results["natural_conversation"] = await natural_conversation(agent_base, authorization)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--via-gateway", action="store_true")
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--model-conversation", action="store_true",
                        help="Explicitly opt in to one real three-turn conversation through the formal gateway")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.model_conversation and (not args.via_gateway or args.reference_only):
        parser.error("--model-conversation requires --via-gateway and the full tool verification")
    result = asyncio.run(main_async(args.via_gateway, args.reference_only, args.model_conversation))
    evidence = json.dumps(result, ensure_ascii=False, indent=2, default=json_decimal)
    if args.output:
        output = args.output.resolve()
        if output.parent != RUNTIME.resolve():
            raise RuntimeError("Evidence file must be inside the dedicated Agent runtime")
        with output.open("x", encoding="utf-8") as file:
            file.write(evidence + "\n")
        print(f"Verified formal material data ({result['mode']}); saved evidence to {output}")
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2, default=json_decimal))


if __name__ == "__main__":
    main()
