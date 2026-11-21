"""Scoped candidate retrieval against fake authorized MES transports."""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone, timedelta
import json
from unittest.mock import patch
from urllib.parse import urlsplit
from typing import Any

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.exploration.catalog import get_entity
from ktg_agent.exploration.engine import ReadEngine
from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.exploration.retrieval import search_business
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools.registry import ToolContext


def fixture_engine(rows: Mapping[str, list[JsonObject]], *,
                   permissions: frozenset[str] = frozenset({"*:*:*"})) -> tuple[ReadEngine, list[str]]:
    calls: list[str] = []
    paths = {get_entity(key).path + "/list": values for key, values in rows.items()}

    async def transport(url: str, _headers: Mapping[str, str], _params: Mapping[str, object],
                        _timeout: float) -> object:
        path = urlsplit(url).path
        calls.append(path)
        assert path in paths, path
        values = paths[path]
        return {"code": 200, "rows": values, "total": len(values)}

    client = KTGClient(base_url="http://mes.test", authorization="Bearer fixture", transport=transport)
    identity: JsonObject = {"userId": 7, "_agent_session_id": "search-session"}
    context = ToolContext("search-run", "search-call", identity=identity, ktg_client=client)
    return ReadEngine(context, ReferenceStore(), client, identity, permissions), calls


def fault_rows() -> dict[str, list[JsonObject]]:
    return {
        "dv.machinery": [
            {"machineryId": 1, "machineryCode": "R-001", "machineryName": "装配机"},
            {"machineryId": 2, "machineryCode": "AR-002", "machineryName": "包装机"},
            {"machineryId": 3, "machineryCode": "R-003", "machineryName": "冲压机"},
        ],
        "dv.repair": [
            {"repairId": 11, "repairCode": "REP-11", "machineryId": 1, "requireDate": "2020-11-18"},
            {"repairId": 22, "repairCode": "REP-22", "machineryId": 2, "requireDate": "2020-11-19"},
            {"repairId": 33, "repairCode": "REP-33", "machineryId": 3, "requireDate": "2020-11-20"},
        ],
        "dv.repair_line": [
            {"lineId": 111, "repairId": 11, "malfunction": "液压管路渗漏"},
            {"lineId": 222, "repairId": 22, "malfunction": "漏油"},
            {"lineId": 333, "repairId": 33, "malfunction": "密封件损坏"},
        ],
    }


def test_fault_join_uses_three_bulk_scans_preserves_evidence_and_strict_prefix() -> None:
    async def scenario() -> None:
        engine, calls = fixture_engine(fault_rows())
        result = await search_business(engine, {"query": "R开头，之前漏过油的机器"})
        records = result["records"]
        assert isinstance(records, list)
        assert {row["id"] for row in records if isinstance(row, dict)} == {1, 3}
        assert len(calls) == len(set(calls)) == 3
        assert result["coverage"] == "complete"
        assert result["searched_entities"] == ["dv.machinery"]
        encoded = json.dumps(records, ensure_ascii=False)
        assert "液压管路渗漏" in encoded and "密封件损坏" in encoded
        assert all(isinstance(row, dict) and row["requires_confirmation"] is True for row in records)
        semantic = result["semantic_status"]
        assert isinstance(semantic, dict) and semantic["pretrained_semantic_configured"] is False
        assert engine.inspect(str(result["result_ref"]))["records"] == records
        engine.store.close()

    asyncio.run(scenario())


def test_missing_repair_query_permission_never_fetches_or_exposes_fault_lines() -> None:
    async def scenario() -> None:
        rows = fault_rows()
        rows["dv.repair_line"][0]["malfunction"] = "隐秘故障原文"
        grants = frozenset({"mes:dv:machinery:list", "mes:dv:repair:list", "mes:dv:repairline:list"})
        engine, calls = fixture_engine(rows, permissions=grants)
        result = await search_business(engine, {"query": "R开头漏油的机器"})
        assert result["coverage"] == "partial"
        assert not any("repairline" in path for path in calls)
        assert "隐秘故障原文" not in json.dumps(result, ensure_ascii=False)
        assert result["warnings"]
        engine.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments,code", [
    ({"query": "R", "entity_types": ["dv.machinery", "md.client"]}, "OUT_OF_SCOPE"),
    ({"query": "R", "entity_types": ["dv.machinery"], "filters": {
        "all": [{"related": {"relation": "machinery.repairs", "where": {
            "field": "status", "op": "eq", "value": "FINISHED"}}}]}}, "UNSUPPORTED_FILTER"),
    ({"query": "R", "entity_types": ["dv.machinery"], "filters": {
        "field": "unknown", "op": "eq", "value": "x"}}, "UNSUPPORTED_FILTER"),
])
def test_invalid_scope_or_filter_is_rejected_before_any_business_request(arguments: Mapping[str, Any], code: str) -> None:
    async def scenario() -> None:
        engine, calls = fixture_engine({})
        with pytest.raises(KTGError) as failure:
            await search_business(engine, arguments)
        assert failure.value.code == code
        assert calls == []
        engine.store.close()

    asyncio.run(scenario())


def test_time_only_search_uses_business_report_date_and_server_timezone() -> None:
    async def scenario() -> None:
        rows: list[JsonObject] = [
            {"repairId": 1, "repairCode": "OLD", "requireDate": "2026-11-10", "updateTime": "2026-11-21"},
            {"repairId": 3, "repairCode": "NEW3", "requireDate": "2026-11-20"},
            {"repairId": 2, "repairCode": "NEW2", "requireDate": "2026-11-20"},
        ]
        engine, _ = fixture_engine({"dv.repair": rows})
        with patch("ktg_agent.exploration.retrieval._now", return_value=datetime(
                2026, 11, 21, 9, tzinfo=timezone(timedelta(hours=8)))):
            result = await search_business(engine, {"query": "最近那次维修", "entity_types": ["dv.repair"]})
            records = result["records"]
            assert isinstance(records, list)
            assert [row["id"] for row in records if isinstance(row, dict)] == [2, 3, 1]
            recent = await search_business(engine, {"query": "近七天的维修", "entity_types": ["dv.repair"]})
            recent_records = recent["records"]
            assert isinstance(recent_records, list)
            assert {row["id"] for row in recent_records if isinstance(row, dict)} == {2, 3}
            assert "2026-11-15" in json.dumps(recent["applied_query"])
            assert "Asia/Shanghai" in json.dumps(recent["applied_query"])
        engine.store.close()

    asyncio.run(scenario())


def test_authorized_filter_executes_and_scan_budget_does_not_become_full_total() -> None:
    async def scenario() -> None:
        rows: list[JsonObject] = [
            {"machineryId": i, "machineryCode": f"R-{i}", "machineryName": "装配机", "status": status}
            for i, status in [(1, "WORKING"), (2, "REPAIR"), (3, "REPAIR")]
        ]
        engine, _ = fixture_engine({"dv.machinery": rows}, permissions=frozenset({"mes:dv:machinery:list"}))
        with patch("ktg_agent.exploration.retrieval.MAX_SEARCH_ROWS", 2):
            result = await search_business(engine, {"query": "R开头的机器", "filters": {
                "field": "status", "op": "eq", "value": "REPAIR"}})
        records = result["records"]
        assert isinstance(records, list) and len(records) == 1
        assert result["coverage"] == "partial" and result["total"] is None
        assert result["warnings"]
        engine.store.close()

    asyncio.run(scenario())


def test_fault_and_time_must_refer_to_the_same_repair_event() -> None:
    async def scenario() -> None:
        rows = fault_rows()
        rows["dv.repair"].append({"repairId": 44, "repairCode": "REP-44", "machineryId": 1,
                                  "requireDate": "2026-11-20"})
        rows["dv.repair_line"].append({"lineId": 444, "repairId": 44, "malfunction": "更换按钮"})
        engine, _ = fixture_engine(rows)
        with patch("ktg_agent.exploration.retrieval._now", return_value=datetime(
                2026, 11, 21, 9, tzinfo=timezone(timedelta(hours=8)))):
            result = await search_business(engine, {"query": "近七天R开头漏油的机器"})
        assert result["records"] == []  # Old leak + unrelated recent repair is not a match.
        engine.store.close()

    asyncio.run(scenario())


def test_repair_line_does_not_inherit_a_siblings_fault_match() -> None:
    async def scenario() -> None:
        rows = fault_rows()
        rows["dv.repair_line"].append({"lineId": 112, "repairId": 11, "malfunction": "更换按钮"})
        engine, _ = fixture_engine(rows)
        result = await search_business(engine, {"query": "漏油", "entity_types": ["dv.repair_line"]})
        records = result["records"]
        assert isinstance(records, list)
        assert 112 not in {row["id"] for row in records if isinstance(row, dict)}
        engine.store.close()

    asyncio.run(scenario())


def test_datetime_period_uses_current_instant_not_future_end_of_day() -> None:
    async def scenario() -> None:
        rows: list[JsonObject] = [
            {"taskId": 1, "taskName": "生产任务", "startTime": "2026-11-14T10:00:00+08:00"},
            {"taskId": 2, "taskName": "生产任务", "startTime": "2026-11-21T10:00:00+08:00"},
            {"taskId": 3, "taskName": "生产任务", "startTime": "2026-11-20T10:00:00+08:00"},
        ]
        engine, _ = fixture_engine({"pro.task": rows})
        with patch("ktg_agent.exploration.retrieval._now", return_value=datetime(
                2026, 11, 21, 9, tzinfo=timezone(timedelta(hours=8)))):
            result = await search_business(engine, {"query": "近七天生产任务", "entity_types": ["pro.task"]})
        records = result["records"]
        assert isinstance(records, list)
        assert {row["id"] for row in records if isinstance(row, dict)} == {1, 3}
        engine.store.close()

    asyncio.run(scenario())
