import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast

import pytest

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.exploration.catalog import ENTITIES, get_entity
from ktg_agent.exploration.engine import ReadEngine
from ktg_agent.exploration.flows import business_flow
from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.exploration.relations import follow
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools.registry import ToolContext


async def make_engine(
    data: Mapping[str, list[JsonObject]], permissions: list[str] | None = None,
    *, partial_entities: set[str] | None = None, cancellation: object | None = None,
    denied_details: set[tuple[str, int]] | None = None,
) -> tuple[ReadEngine, ReferenceStore, list[str]]:
    calls: list[str] = []

    async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        calls.append(url)
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": 7}, "permissions": permissions if permissions is not None else ["*:*:*"]}
        for spec in ENTITIES.values():
            if url.endswith(spec.path + "/list"):
                # Deliberately ignore backend filters: relation code must check.
                rows = data.get(spec.key, [])
                total = 10000 if spec.key in (partial_entities or set()) else len(rows)
                return {"code": 200, "rows": rows, "total": total}
            if url.startswith("http://test" + spec.path + "/"):
                suffix = url.rsplit("/", 1)[-1]
                if suffix.isdecimal():
                    if (spec.key, int(suffix)) in (denied_details or set()):
                        return {"code": 403, "msg": "Detail access denied"}
                    row = next((r for r in data.get(spec.key, []) if r.get(spec.id_field) == int(suffix)), None)
                    return {"code": 200, "data": row}
        raise AssertionError(url)

    client = KTGClient(base_url="http://test", authorization="Bearer test", transport=transport)
    store = ReferenceStore()
    context = ToolContext("run", "call", identity={"userId": 7}, ktg_client=client, cancellation=cancellation)
    return await ReadEngine.create(context, store), store, calls


def test_forward_relation_verifies_foreign_key_and_deduplicates_roots() -> None:
    async def scenario() -> None:
        engine, store, calls = await make_engine({
            "dv.repair": [{"repairId": 1, "repairCode": "REP-1"}],
            "dv.repair_line": [{"lineId": 10, "repairId": 1, "malfunction": "漏油"},
                               {"lineId": 11, "repairId": 999, "malfunction": "其他设备故障"}],
        })
        root = await engine.read("dv.repair", 1)
        result = await follow(engine, [str(root["record_ref"])] * 2, relations=("repair.lines",))
        nodes = cast(list[JsonObject], result["nodes"])
        assert {n["id"] for n in nodes} == {"dv.repair:1", "dv.repair_line:10"}
        assert "其他设备故障" not in str(result)
        assert len([call for call in calls if call.endswith("/repairline/list")]) == 1
        store.close()

    asyncio.run(scenario())


def test_reverse_path_and_cycle_budget_do_not_duplicate_nodes() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({
            "dv.repair_line": [{"lineId": 10, "repairId": 1}],
            "dv.repair": [{"repairId": 1, "machineryId": 5}],
            "dv.machinery": [{"machineryId": 5, "machineryName": "R机"}],
        })
        row = await engine.read("dv.repair_line", 10)
        result = await follow(engine, [str(row["record_ref"])],
                              relations=("line.repair", "repair.machinery", "repair.lines"), max_depth=3)
        nodes = cast(list[JsonObject], result["nodes"])
        assert len(nodes) == 3
        assert len({str(node["id"]) for node in nodes}) == 3
        store.close()

    asyncio.run(scenario())


def test_unauthorized_relations_reveal_no_target_identifiers_or_counts() -> None:
    async def scenario() -> None:
        engine, store, calls = await make_engine({
            "dv.machinery": [{"machineryId": 1, "machineryName": "R机"}],
            "dv.repair": [{"repairId": 987654, "machineryId": 1, "repairCode": "SECRET"}],
        }, ["mes:dv:machinery:query", "mes:dv:machinery:list"])
        row = await engine.read("dv.machinery", 1)
        result = await follow(engine, [str(row["record_ref"])], relations=("machinery.repairs",))
        assert result["coverage"] == "partial"
        assert result["gaps"]
        assert "SECRET" not in str(result) and "987654" not in str(result)
        assert not any("/repair/" in call for call in calls)
        store.close()

    asyncio.run(scenario())


def test_node_limit_is_explicitly_partial() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({
            "dv.repair": [{"repairId": 1}],
            "dv.repair_line": [{"lineId": i, "repairId": 1} for i in range(10, 20)],
        })
        row = await engine.read("dv.repair", 1)
        result = await follow(engine, [str(row["record_ref"])], relations=("repair.lines",), max_nodes=2)
        assert len(cast(list[JsonValue], result["nodes"])) == 2
        assert result["coverage"] == "partial"
        store.close()

    asyncio.run(scenario())


def test_repair_flow_records_acceptance_date_without_inventing_pass_result() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({"dv.repair": [{
            "repairId": 1, "repairCode": "REP-1", "status": "FINISHED", "repairResult": "SUCCESS",
            "requireDate": "2026-11-01", "finishDate": "2026-11-02", "confirmDate": "2026-11-03",
        }]})
        row = await engine.read("dv.repair", 1)
        result = await business_flow(engine, str(row["record_ref"]))
        assert result["kind"] == "exploration_flow"
        nodes = cast(list[JsonObject], result["nodes"])
        assert len(nodes) == 3
        assert "验收通过" not in str(result)
        assert "2026-11-03" in str(result)
        assert all(set(("id", "label", "state", "record_refs", "summary", "evidence", "warnings")) <= set(n) for n in nodes)
        store.close()

    asyncio.run(scenario())


def test_production_flow_keeps_missing_nodes_and_uses_workorder_quantity() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({
            "pro.workorder": [{"workorderId": 1, "workorderCode": "WO-1", "quantity": "100", "quantityProduced": "50", "status": "CONFIRMED"}],
            "pro.task": [{"taskId": 2, "workorderId": 1}],
            "pro.feedback": [{"recordId": 3, "workorderId": 1, "taskId": 2, "quantityFeedback": "150"}],
            "wm.product_recpt": [{"recptId": 888, "workorderId": 999, "recptCode": "UNRELATED"}],
        })
        row = await engine.read("pro.workorder", 1)
        result = await business_flow(engine, str(row["record_ref"]))
        assert "50" in str(result["headline"]) and "150" not in str(result["headline"])
        assert "UNRELATED" not in str(result)
        nodes = cast(list[JsonObject], result["nodes"])
        assert len(nodes) == 8
        assert next(n for n in nodes if n["id"] == "ipqc")["state"] == "本演示未接入"
        assert next(n for n in nodes if n["id"] == "receipt")["state"] == "本演示未接入"
        store.close()

    asyncio.run(scenario())


def test_employee_flow_never_queries_missing_permissions_or_out_of_scope_nodes() -> None:
    async def scenario() -> None:
        engine, store, calls = await make_engine({
            "pro.workorder": [{"workorderId": 1, "workorderCode": "WO-1", "status": "CONFIRMED"}],
            "pro.task": [{"taskId": 777, "workorderId": 1, "taskCode": "PRIVATE"}],
        }, ["mes:pro:workorder:query", "mes:pro:workorder:list"])
        row = await engine.read("pro.workorder", 1)
        result = await business_flow(engine, str(row["record_ref"]))
        nodes = cast(list[JsonObject], result["nodes"])
        assert next(n for n in nodes if n["id"] == "tasks")["state"] == "无权限"
        assert "PRIVATE" not in str(result) and "777" not in str(result)
        assert not any(any(part in call for part in ("/protask/", "/feedback/", "/wm/", "/qc/")) for call in calls)
        store.close()

    asyncio.run(scenario())


def test_list_only_reference_cannot_bypass_source_query_permission() -> None:
    async def scenario() -> None:
        engine, store, calls = await make_engine({}, ["mes:wm:issueline:list"])
        row = engine.record(get_entity("wm.issue_line"), {"id": 12, "issue_id": 1})
        with pytest.raises(KTGError) as denied:
            await follow(engine, [str(row["record_ref"])])
        assert denied.value.code == "PERMISSION_DENIED"
        assert calls == ["http://test/getInfo"]
        store.close()

    asyncio.run(scenario())


def test_partial_repair_history_remains_partial_in_persisted_flow() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({
            "dv.machinery": [{"machineryId": 2}],
            "dv.repair": [{"repairId": 1, "machineryId": 2, "requireDate": "2026-11-01",
                            "finishDate": "2026-11-02", "status": "FINISHED", "repairResult": "SUCCESS"}],
        }, partial_entities={"dv.repair"})
        row = await engine.read("dv.machinery", 2)
        result = await business_flow(engine, str(row["record_ref"]))
        assert result["coverage"] == "partial"
        assert "最近一次尚未核实" in str(result["headline"])
        assert engine.inspect(str(result["result_ref"]))["coverage"] == "partial"
        store.close()

    asyncio.run(scenario())


def test_date_conflicts_are_not_presented_as_completed_success() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({"dv.repair": [{
            "repairId": 1, "status": "PREPARE", "finishDate": "2026-11-05", "confirmDate": "2026-11-01",
            "repairResult": "SUCCESS",
        }]})
        row = await engine.read("dv.repair", 1)
        result = await business_flow(engine, str(row["record_ref"]))
        assert "待核实" in str(result["headline"])
        nodes = cast(list[JsonObject], result["nodes"])
        assert next(n for n in nodes if n["id"] == "acceptance")["state"] == "未核实"
        store.close()

    asyncio.run(scenario())


def test_cancellation_prevents_further_relation_or_flow_io() -> None:
    async def scenario() -> None:
        cancellation = SimpleNamespace(is_cancelled=False)
        engine, store, calls = await make_engine({"dv.repair": [{"repairId": 1}]}, cancellation=cancellation)
        row = await engine.read("dv.repair", 1)
        before = len(calls)
        cancellation.is_cancelled = True
        for operation in (follow(engine, [str(row["record_ref"])]), business_flow(engine, str(row["record_ref"]))):
            with pytest.raises(KTGError) as error:
                await operation
            assert error.value.code == "RUN_CANCELLED"
        assert len(calls) == before
        store.close()

    asyncio.run(scenario())


def test_list_summary_requires_actual_detail_authorization_before_expansion() -> None:
    async def scenario() -> None:
        engine, store, calls = await make_engine({
            "dv.repair": [{"repairId": 1}],
            "dv.repair_line": [{"lineId": 10, "repairId": 1}],
        }, denied_details={("dv.repair_line", 10)})
        row = await engine.read("dv.repair", 1)
        result = await follow(engine, [str(row["record_ref"])], relations=("repair.lines", "line.repair"))
        assert result["coverage"] == "partial"
        assert len(cast(list[JsonValue], result["edges"])) == 1
        assert any(call.endswith("/repairline/10") for call in calls)
        assert result["gaps"]
        store.close()

    asyncio.run(scenario())


def test_unknown_relation_is_a_validation_error() -> None:
    async def scenario() -> None:
        engine, store, _ = await make_engine({})
        with pytest.raises(KTGError):
            await follow(engine, [], relations=("invented.relation",))
        store.close()

    asyncio.run(scenario())
