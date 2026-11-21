"""Independent runtime contract regressions, using only fake MES responses."""

import asyncio
from collections.abc import Mapping
import json

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.exploration.catalog import get_entity
from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.exploration.runtime import ExplorationRuntime
from ktg_agent.ktg.client import KTGClient
from ktg_agent.tools.registry import ToolContext


def runtime_fixture(entity: str, rows: list[JsonObject]) -> tuple[ExplorationRuntime, ToolContext, ReferenceStore]:
    spec = get_entity(entity)

    async def transport(url: str, _headers: Mapping[str, str], _params: Mapping[str, object],
                        _timeout: float) -> object:
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": 18}, "permissions": list(spec.permissions["list"])}
        assert url.endswith(spec.path + "/list"), url
        return {"code": 200, "total": len(rows), "rows": rows}

    client = KTGClient(base_url="http://mes.test", authorization="Bearer fixture", transport=transport)
    context = ToolContext("review-run", "review-call", identity={"userId": 18}, ktg_client=client)
    store = ReferenceStore()
    return ExplorationRuntime(store), context, store


def test_group_by_is_executed_or_explicitly_rejected_without_aggregates() -> None:
    async def scenario() -> None:
        runtime, context, store = runtime_fixture("pro.workorder", [
            {"workorderId": 1, "productName": "同一产品"},
            {"workorderId": 2, "productName": "同一产品"},
        ])
        result = await runtime.invoke("ktg_query_business", {
            "entity": "pro.workorder", "group_by": ["product_name"],
        }, context)
        if result.error_code is None:
            records = json.loads(result.content)["records"]
            assert len(records) == 1 and records[0]["product_name"] == "同一产品"
            assert "record_ref" not in records[0]  # Group is not a single business record.
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [
    {"aggregates": [{"op": "sum", "field": "quantity", "as": "total_quantity"}]},
    {"group_by": ["client_name"], "aggregates": [{"op": "count_records", "as": "count"}]},
])
def test_aggregate_missing_source_fields_are_not_silently_treated_as_null(arguments: JsonObject) -> None:
    async def scenario() -> None:
        runtime, context, store = runtime_fixture("pro.workorder", [{"workorderId": 1, "workorderCode": "WO-1"}])
        result = await runtime.invoke("ktg_query_business", {"entity": "pro.workorder", **arguments}, context)
        assert result.error_code == "FIELD_UNAVAILABLE"
        store.close()

    asyncio.run(scenario())


def test_empty_source_does_not_silently_accept_an_unknown_projection() -> None:
    async def scenario() -> None:
        runtime, context, store = runtime_fixture("pro.workorder", [])
        result = await runtime.invoke("ktg_query_business", {
            "entity": "pro.workorder", "fields": ["password"],
        }, context)
        assert result.error_code is not None
        store.close()

    asyncio.run(scenario())


def test_describe_business_finds_colloquial_machine_alias_with_only_actor_permissions() -> None:
    async def scenario() -> None:
        runtime, context, store = runtime_fixture("dv.machinery", [])
        result = await runtime.invoke("ktg_describe_business", {"query": "机器"}, context)
        assert result.error_code is None
        assert "dv.machinery" in {item["entity"] for item in json.loads(result.content)["items"]}
        store.close()

    asyncio.run(scenario())


def test_describe_include_is_fulfilled_or_reports_unsupported_metadata() -> None:
    async def scenario() -> None:
        runtime, context, store = runtime_fixture("dv.machinery", [])
        result = await runtime.invoke("ktg_describe_business", {
            "entity_types": ["dv.machinery"], "include": ["dictionaries"],
        }, context)
        if result.error_code is None:
            data = json.loads(result.content)
            assert (any("dictionaries" in item for item in data["items"])
                    or "dictionaries" in data.get("unsupported_includes", []))
        store.close()

    asyncio.run(scenario())
