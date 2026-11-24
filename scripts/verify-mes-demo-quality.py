"""Independently verify formal MES quality facts, Agent projections and natural turns."""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ktg_agent.ktg.client import KTGClient

# Use the current dedicated demo account/runtime and trusted read-only helpers.
_DEMO = runpy.run_path(str(Path(__file__).with_name("verify-mes-demo-materials.py")))
RUNTIME = _DEMO["RUNTIME"]
DIRECT_MES = _DEMO["DIRECT_MES"]
DIRECT_AGENT = _DEMO["DIRECT_AGENT"]
GATEWAY = _DEMO["GATEWAY"]
login = _DEMO["login"]
request_json = _DEMO["request_json"]
all_rows = _DEMO["all_rows"]
sse_events = _DEMO["sse_events"]
TOOL = "ktg_query_workorder_quality"
EXPECTED_TOOLS = set(_DEMO["EXPECTED_TOOLS"]) | {TOOL}
WINTER, HOME, OFFICE, RETAIL, STOCK = (_DEMO[k] for k in ("WINTER", "HOME", "OFFICE", "RETAIL", "STOCK"))
CODES = [WINTER, HOME, OFFICE, RETAIL, STOCK]
REJECTED = "LAMPBAT-00000023"
PASSED = "LAMPBAT-00000021"
UNINSPECTED = "LAMPBAT-00000011"
SUMMARY_KEYS = (
    "planned", "allocated", "waitingAssembly", "waitingInspection", "pendingDisposition",
    "reworkInProgress", "waitingReinspection", "qualified", "scrapped",
    "firstInspectionRejected", "reinspectionCount",
)


async def reference(client: KTGClient) -> dict[str, Any]:
    """Use MES source endpoints, not the Agent's own parsing or helpers."""
    orders = await all_rows(client, "/mes/pro/workorder/list")
    by_code = {row["workorderCode"]: row for row in orders}
    assert set(by_code) == set(CODES) and len(orders) == len(CODES)
    result: dict[str, Any] = {}
    for code in CODES:
        row = by_code[code]
        fact: dict[str, Any] = {"workorderId": str(row["workorderId"]), "status": row["status"]}
        result[code] = fact
        if code == WINTER:
            assert row["status"] == "PREPARE"
            fact.update(summary=None, units=[])
            continue
        body = await client.get(f"/mes/pro/execution/workorders/{row['workorderId']}")
        assert body["code"] == 200
        progress = body["data"]["progress"]
        assert str(progress["workorderId"]) == fact["workorderId"]
        fact["summary"] = {key: progress[key] for key in SUMMARY_KEYS}
        tasks = [task for task in progress["tasks"] if task["stepKind"] == "INSPECTION" and task["taskKind"] == "NORMAL"]
        assert len(tasks) == 1
        task_id = tasks[0]["taskId"]
        unit_rows = await all_rows(client, f"/mes/pro/execution/tasks/{task_id}/units", {"includeHistory": "true"})
        assert len({unit["snId"] for unit in unit_rows}) == len(unit_rows)
        units = []
        for unit in unit_rows:
            body = await client.get(f"/mes/pro/execution/units/{unit['snId']}/inspections")
            assert body["code"] == 200 and isinstance(body["data"], list)
            history = body["data"]
            assert [entry["roundNo"] for entry in history] == list(range(1, len(history) + 1))
            for entry in history:
                assert entry["snId"] == unit["snId"] and entry["taskId"] == task_id
            units.append({**unit, "inspections": history})
        fact["units"] = units
        assert len(units) == progress["allocated"] - progress["waitingAssembly"]
        assert sum(bool(unit["inspections"]) and unit["inspections"][0]["checkResult"] == "REJECT" for unit in units) == progress["firstInspectionRejected"]
        assert sum(max(0, len(unit["inspections"]) - 1) for unit in units) == progress["reinspectionCount"]
    # Independently confirm the known baseline from the formal demo. Do not alter it
    # to create a convenient rework or scrap case.
    assert result[HOME]["summary"]["qualified"] == 10
    assert result[OFFICE]["summary"]["waitingInspection"] == 4 and not any(unit["inspections"] for unit in result[OFFICE]["units"])
    assert result[STOCK]["summary"]["waitingAssembly"] == 10 and not result[STOCK]["units"]
    assert result[RETAIL]["summary"]["qualified"] == 9
    assert result[RETAIL]["summary"]["pendingDisposition"] == 1
    anomalies = [unit for unit in result[RETAIL]["units"] if any(entry["checkResult"] == "REJECT" for entry in unit["inspections"])]
    assert len(anomalies) == 1 and anomalies[0]["snCode"] == REJECTED and anomalies[0]["unitState"] == "WAIT_DISPOSITION"
    inspection = anomalies[0]["inspections"][0]
    line = inspection["lines"][0]
    assert (line["indexCode"], line["indexName"], line["unitOfMeasure"], float(line["thresholdMin"]), float(line["thresholdMax"])) == ("IDX-LAMP-VOLT", "开路电压", "V", 3.6, 4.2)
    assert float(json.loads(line["remark"])["value"]) == 3.2
    assert inspection["defects"][0]["lineId"] == line["lineId"] and inspection["defects"][0]["defectLevel"] == "MAJ"
    return result


def assert_projection(details: dict[str, Any], source: dict[str, Any], codes: list[str],
                      sn_codes: list[str] | None = None) -> None:
    assert details["kind"] == "workorder_quality"
    assert details["coverage"] == "complete" and details.get("missing_codes", []) == []
    assert [order["workorderCode"] for order in details["orders"]] == codes
    for order in details["orders"]:
        code = order["workorderCode"]
        fact = source[code]
        assert str(order["workorderId"]) == fact["workorderId"] and order["status"] == fact["status"]
        if code == WINTER:
            assert order["history_coverage"] == "not_applicable" and order["summary"] is None and not order["units"]
            continue
        assert order["history_coverage"] == "complete" and order["summary"] == fact["summary"]
        units = {unit["snCode"]: unit for unit in fact["units"]}
        if sn_codes is None:
            expected = {code for code, unit in units.items() if any(entry["checkResult"] == "REJECT" for entry in unit["inspections"])}
            assert order["total_units"] == order["scanned_units"] == len(fact["units"])
        else:
            expected = set(sn_codes) & set(units)
            assert set(order.get("missing_sn_codes", [])) == set(sn_codes) - set(units)
            assert order["total_units"] == order["scanned_units"] == len(expected)
        assert {unit["snCode"] for unit in order["units"]} == expected
        for unit in order["units"]:
            real = units[unit["snCode"]]
            history = real["inspections"]
            assert str(unit["snId"]) == str(real["snId"]) and unit["unitState"] == real["unitState"]
            assert unit["history_state"] == "verified"
            assert unit["inspection_count"] == len(history)
            assert unit["first_result"] == (history[0]["checkResult"] if history else None)
            assert unit["latest_result"] == (history[-1]["checkResult"] if history else None)
            assert unit["ever_rejected"] == any(entry["checkResult"] == "REJECT" for entry in history)
            assert len(unit["inspections"]) == len(history)
            for projected, entry in zip(unit["inspections"], history):
                assert str(projected["inspectionId"]) == str(entry["inspectionId"])
                assert projected["round_no"] == entry["roundNo"] and projected["result"] == entry["checkResult"]
                assert projected["inspect_date"] == entry["inspectDate"]
                if entry["checkResult"] == "ACCEPT":
                    assert not projected["failed_indices"]
                    continue
                assert len(projected["failed_indices"]) == 1
                measured = projected["failed_indices"][0]
                line, defect = entry["lines"][0], entry["defects"][0]
                for key in ("indexCode", "indexName", "indexType", "unitOfMeasure"):
                    assert measured[key] == line[key]
                for key in ("thresholdMin", "thresholdMax"):
                    assert float(measured[key]) == float(line[key])
                assert float(measured["value"]) == float(json.loads(line["remark"])["value"])
                assert measured["defectName"] == defect["defectName"] and measured["defectLevel"] == defect["defectLevel"]
                assert measured["reason"] == defect["remark"]


async def natural_conversation(authorization: str) -> dict[str, Any]:
    """One opted-in real conversation. No submission retries, fixed tool calls or DB changes."""
    session = request_json(GATEWAY + "/agent-api/v1/sessions", authorization, {"title": "正式演示验收：质量异常多轮"})
    session_id = session["session_id"]
    prompts = (("床头阅读灯怎么还没全部合格？", RETAIL),
               ("具体哪件，哪项不合格，差了多少？", RETAIL),
               ("办公台灯也有这种问题吗？", OFFICE))
    outcomes: list[dict[str, Any]] = []
    for prompt, code in prompts:
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
                return {"session_id": session_id, "turns": outcomes, "object_context_passed": False}
            await asyncio.sleep(1)
        events = sse_events(GATEWAY, authorization, run_id)
        messages = request_json(GATEWAY + f"/agent-api/v1/sessions/{session_id}/messages?limit=100", authorization)
        assistant = [m for m in messages["items"] if m["run_id"] == run_id and m["role"] == "assistant"]
        calls = [block for message in assistant for block in message["content"] if block["type"] == "tool_call"]
        end_events = [event["payload"] for event in events if event["type"] == "tool_execution_end"]
        successful = {entry["tool_call_id"] for entry in end_events if entry["status"] == "succeeded"}
        selected = [call for call in calls if call["name"] == TOOL and call["id"] in successful]
        match = any(call["arguments"].get("workorder_codes") == [code] for call in selected)
        answer = "\n".join(block["text"] for block in assistant[-1]["content"] if block["type"] == "text") if assistant else ""
        outcomes.append({"question": prompt, "run_id": run_id, "status": snapshot["status"],
                         "stop_reason": snapshot["stop_reason"], "model_requests": len(snapshot.get("model_requests", [])),
                         "calls": calls, "tool_results": end_events, "answer": answer,
                         "expected_workorder_code": code, "object_context_match": match, "event_count": len(events)})
        if snapshot["status"] != "completed":
            break
    return {"session_id": session_id, "turns": outcomes,
            "object_context_passed": len(outcomes) == len(prompts) and all(turn["status"] == "completed" and turn.get("object_context_match") for turn in outcomes)}


async def main_async(via_gateway: bool, reference_only: bool, model_conversation: bool, source_only: bool = False) -> dict[str, Any]:
    mes_base = GATEWAY + "/prod-api" if via_gateway else DIRECT_MES
    agent_base = GATEWAY if via_gateway else DIRECT_AGENT
    authorization = login(mes_base)
    client = KTGClient(base_url=mes_base, authorization=authorization)
    identity = await client.get_current_user()
    assert identity.get("userName") == "lamp_leader"
    permissions = frozenset((await client.get("/getInfo"))["permissions"])
    assert "*:*:*" not in permissions and {"mes:pro:workorder:list", "mes:pro:execution:query"} <= permissions
    facts = await reference(client)
    evidence: dict[str, Any] = {"mode": "same-origin-gateway" if via_gateway else "direct", "user": identity["userName"], "reference": facts}
    if reference_only:
        return evidence
    from ktg_agent.core.messages import ToolCall
    from ktg_agent.providers.base import ModelConfig
    from ktg_agent.providers.fake import FakeProvider
    from ktg_agent.service import QueryService
    from ktg_agent.tools.catalog import CatalogIdentity
    from ktg_agent.tools.registry import ToolContext

    if not source_only:
        remote = request_json(agent_base + "/agent-api/v1/tools", authorization)
        assert {item["name"] for item in remote["items"]} == EXPECTED_TOOLS
    else:
        evidence["mode"] += "-source-only"
    service = QueryService(provider=FakeProvider(()), model_config=ModelConfig("offline-verification", 128),
                           ktg_client_factory=lambda _: client)
    run_id = str(uuid4())
    candidate = service._catalog.candidate_snapshot(CatalogIdentity(permissions, run_id), service._switches_provider())
    assert set(candidate.candidates) == EXPECTED_TOOLS
    runner = service._tool_runner(candidate)
    evidence["catalog"] = sorted(EXPECTED_TOOLS)
    evidence["observations"] = observations = []
    for codes, sn_codes in (([RETAIL], None), (CODES, None), ([RETAIL], [REJECTED, PASSED]), ([OFFICE], [UNINSPECTED])):
        args = {"workorder_codes": codes}
        if sn_codes is not None:
            args["sn_codes"] = sn_codes
        call = ToolCall(str(uuid4()), TOOL, args)
        result = await runner.run(call, ToolContext(run_id, call.id, identity=identity, ktg_client=client))
        assert not result.is_error and isinstance(result.details, dict), result.error_code
        assert_projection(result.details, facts, codes, sn_codes)
        observations.append({"args": args, "details": result.details})
    if model_conversation:
        evidence["natural_conversation"] = await natural_conversation(authorization)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--source-only", action="store_true", help="Verify local Agent source before the service restart")
    parser.add_argument("--via-gateway", action="store_true")
    parser.add_argument("--model-conversation", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.model_conversation and (args.reference_only or args.source_only or not args.via_gateway):
        parser.error("--model-conversation requires --via-gateway and the full tool verification")
    results = asyncio.run(main_async(args.via_gateway, args.reference_only, args.model_conversation, args.source_only))
    if args.output is not None:
        output = args.output.resolve()
        assert output.parent == RUNTIME.resolve(), "Evidence must stay inside dedicated demo Agent runtime"
        with output.open("x", encoding="utf-8") as file:
            json.dump(results, file, ensure_ascii=False, indent=2, default=_DEMO["json_decimal"])
            file.write("\n")
        print(f"Quality evidence saved to {output}")
    else:
        print(json.dumps(results, ensure_ascii=True, indent=2, default=_DEMO["json_decimal"]))


if __name__ == "__main__":
    main()
