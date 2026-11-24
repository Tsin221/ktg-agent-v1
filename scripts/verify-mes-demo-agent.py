"""Read-only formal MES demo verification without a real model request."""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

from ktg_agent.core.messages import ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import ModelConfig
from ktg_agent.providers.fake import FakeProvider
from ktg_agent.service import QueryService
from ktg_agent.tools.catalog import CatalogIdentity
from ktg_agent.tools.registry import ToolContext

ROOT = Path(__file__).resolve().parents[1]
DIRECT_MES = "http://127.0.0.1:18090"
DIRECT_AGENT = "http://127.0.0.1:18091"
GATEWAY = "http://127.0.0.1:18854"
EXPECTED = {"ktg_count_products", "ktg_query_stock_summary", "ktg_query_workorder_overview", "ktg_query_workorder_materials", "ktg_query_workorder_quality", "ktg_query_production_trace"}
PROGRESS_COUNTS = (
    "planned", "allocated", "assembled", "waitingAssembly", "waitingInspection",
    "pendingDisposition", "reworkInProgress", "waitingReinspection", "qualified",
    "packable", "packed", "scrapped",
)


def request_json(url: str, authorization: str | None = None, body: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url, data=data, headers=headers), timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Expected an object response")
    return payload


def login(mes_base: str) -> str:
    account = json.loads((ROOT / ".runtime/mes-demo/accounts.json").read_text(encoding="utf-8-sig"))["leader"]
    if account["username"] != "lamp_leader":
        raise RuntimeError("Unexpected formal demo account")
    response = request_json(mes_base + "/login", body={
        "username": account["username"], "password": account["password"],
    })
    token = response.get("token")
    if response.get("code") != 200 or not isinstance(token, str) or not token:
        raise RuntimeError("Formal demo login failed")
    return "Bearer " + token


async def check(via_gateway: bool) -> dict[str, Any]:
    mes_base = GATEWAY + "/prod-api" if via_gateway else DIRECT_MES
    agent_base = GATEWAY if via_gateway else DIRECT_AGENT
    authorization = login(mes_base)
    client = KTGClient(base_url=mes_base, authorization=authorization)
    identity = await client.get_current_user()
    assert identity.get("userName") == "lamp_leader"
    info = await client.get("/getInfo")
    permissions = frozenset(info["permissions"])
    assert "*:*:*" not in permissions
    assert {"mes:md:mditem:list", "mes:wm:wmstock:list", "mes:pro:workorder:list",
            "mes:pro:execution:query"} <= permissions
    remote_catalog = request_json(agent_base + "/agent-api/v1/tools", authorization)
    remote_names = {item["name"] for item in remote_catalog["items"]}
    assert remote_names == EXPECTED, "Unexpected Agent tool catalog"
    service = QueryService(
        provider=FakeProvider(()), model_config=ModelConfig("offline-verification", 128),
        ktg_client_factory=lambda _: client,
    )
    run_id = str(uuid4())
    candidate = service._catalog.candidate_snapshot(CatalogIdentity(permissions, run_id), service._switches_provider())
    assert set(candidate.candidates) == EXPECTED
    runner = service._tool_runner(candidate)

    async def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        call = ToolCall(str(uuid4()), name, arguments)
        result = await runner.run(call, ToolContext(run_id, call.id, identity=identity, ktg_client=client))
        if result.is_error or not isinstance(result.details, dict):
            raise RuntimeError(f"{name} did not return verified results ({result.error_code})")
        return result.details

    product = await tool("ktg_count_products", {})
    product_ref = await client.get("/mes/md/mditem/list", {"itemOrProduct": "PRODUCT", "pageNum": 1, "pageSize": 1})
    assert product["kind"] == "product_count" and product["total"] == product_ref["total"]
    stock = await tool("ktg_query_stock_summary", {"item_code": "CELL-18650-2200"})
    stock_ref = await client.get("/mes/wm/wmstock/summary", {"itemCode": "CELL-18650-2200"})
    assert stock["kind"] == "stock_summary"
    assert stock["groups"] == stock_ref["data"]["groups"]

    order_ref = await client.get("/mes/pro/workorder/list", {"pageNum": 1, "pageSize": 20})
    source_rows = order_ref["rows"]
    assert len(source_rows) == order_ref["total"], "Demo verification requires a fully covered reference list"
    by_code = {row["workorderCode"]: row for row in source_rows}
    expected_unfinished = {row["workorderCode"] for row in source_rows if row["status"] != "FINISHED"}
    bed = [row for row in source_rows if "床头阅读灯" in row["workorderName"]]
    home = [row for row in source_rows if "护眼台灯" in row["workorderName"]]
    office = [row for row in source_rows if "悦读办公" in row["workorderName"]]
    assert len(bed) == len(home) == len(office) == 1

    async def verify_order(args: dict[str, Any], expected_codes: set[str]) -> dict[str, Any]:
        result = await tool("ktg_query_workorder_overview", args)
        assert result["coverage"] == "complete" and result["total"] == len(expected_codes)
        assert {row["workorderCode"] for row in result["rows"]} == expected_codes
        rows: list[dict[str, Any]] = []
        for row in result["rows"]:
            code = row["workorderCode"]
            base = by_code[code]
            assert row["workorderId"] == str(base["workorderId"])
            assert row["status"] == base["status"]
            assert Decimal(row["quantity"]) == Decimal(str(base["quantity"]))
            if base["status"] == "PREPARE":
                assert row["progress_state"] == "draft" and row["progress"] is None
            else:
                execution = await client.get(f"/mes/pro/execution/workorders/{row['workorderId']}")
                progress = execution["data"]["progress"]
                assert row["progress_state"] == "available", f"Progress unavailable for {code}"
                assert all(row["progress"][field] == progress[field] for field in PROGRESS_COUNTS)
                assert row["progress"]["completionAllowed"] == progress["completionAllowed"]
                assert row["progress"]["completionReasons"] == progress["completionReasons"]
            rows.append({key: row.get(key) for key in (
                "workorderCode", "workorderName", "status", "quantity", "progress_state", "progress"
            )})
        return {"total": result["total"], "coverage": result["coverage"], "rows": rows}

    unfinished = await verify_order({"unfinished_only": True, "include_progress": True}, expected_unfinished)
    reading_lamp = await verify_order(
        {"workorder_name": "床头阅读灯", "include_progress": True}, {bed[0]["workorderCode"]})
    comparison_codes = {home[0]["workorderCode"], office[0]["workorderCode"]}
    comparison = await verify_order(
        {"workorder_codes": sorted(comparison_codes), "include_progress": True}, comparison_codes)
    assert any(row["status"] == "PREPARE" and row["progress_state"] == "draft" for row in unfinished["rows"])
    assert any(row["status"] == "FINISHED" and row["progress_state"] == "available" for row in comparison["rows"])

    return {
        "mode": "same-origin-gateway" if via_gateway else "direct",
        "user": identity["userName"],
        "catalog": sorted(remote_names),
        "product_count": product["total"],
        "inventory_summary": {"item_code": "CELL-18650-2200", "groups": stock["groups"]},
        "unfinished": unfinished, "reading_lamp": reading_lamp, "comparison": comparison,
        "verification": "Compared tool responses with actual MES list and detail endpoints; no model call",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--via-gateway", action="store_true")
    parser.add_argument("--output", type=Path, help="Optional new evidence file inside the dedicated Agent runtime")
    args = parser.parse_args()
    result = asyncio.run(check(args.via_gateway))
    evidence = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        target = args.output.resolve()
        runtime = (ROOT / ".runtime/mes-demo/agent").resolve()
        if target.parent != runtime:
            raise RuntimeError("Evidence file must be inside the dedicated Agent runtime")
        with target.open("x", encoding="utf-8") as file:
            file.write(evidence + "\n")
        print(f"Verified {result['mode']}: {len(result['catalog'])} tools; evidence saved to {target}")
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
