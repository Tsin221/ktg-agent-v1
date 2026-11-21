import asyncio
import json
from collections.abc import Mapping
from typing import Any

from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.exploration.runtime import ExplorationRuntime
from ktg_agent.ktg.client import KTGClient
from ktg_agent.tools.registry import ToolContext


def test_runtime_queries_and_cached_pages_stay_inside_account_and_showcase_scope() -> None:
    async def scenario() -> None:
        permissions = ["mes:dv:machinery:list", "mes:dv:machinery:query"]
        calls: list[str] = []

        async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 18}, "permissions": permissions}
            calls.append(url)
            assert url.endswith("/mes/dv/machinery/list")
            return {"code": 200, "total": 2, "rows": [
                {"machineryId": 1, "machineryCode": "R01", "machineryName": "装配机"},
                {"machineryId": 2, "machineryCode": "XR02", "machineryName": "切割机"}]}

        client = KTGClient(base_url="http://test", authorization="Bearer test", transport=transport)
        context = ToolContext("run", "call", identity={"userId": 18}, ktg_client=client)
        store = ReferenceStore()
        runtime = ExplorationRuntime(store)
        catalog = await runtime.invoke("ktg_describe_business", {}, context)
        assert "qc.ipqc" not in catalog.content and "md.client" not in catalog.content
        assert "dv.machinery" in catalog.content
        query = await runtime.invoke("ktg_query_business", {"entity": "dv.machinery", "filters": {
            "field": "code", "op": "starts_with", "value": "R"}}, context)
        assert query.error_code is None
        result = json.loads(query.content)
        assert result["total"] == 1 and result["records"][0]["code"] == "R01"
        reference = result["result_ref"]
        permissions.clear()
        revoked = await runtime.invoke("ktg_inspect_result", {"result_ref": reference}, context)
        assert revoked.error_code == "PERMISSION_DENIED"
        assert len(calls) == 1
        store.close()

    asyncio.run(scenario())


def test_runtime_never_routes_unknown_api_or_nonshowcase_entities() -> None:
    async def scenario() -> None:
        async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            assert url.endswith("/getInfo")
            return {"code": 200, "user": {"userId": 18}, "permissions": ["*:*:*"]}

        client = KTGClient(base_url="http://test", authorization="Bearer test", transport=transport)
        context = ToolContext("run", "call", identity={"userId": 18}, ktg_client=client)
        store = ReferenceStore()
        runtime = ExplorationRuntime(store)
        cases: list[tuple[str, Mapping[str, Any]]] = [
            ("ktg_read_record", {"entity": "qc.ipqc", "record_id": 1}),
            ("ktg_call_read_api", {"endpoint_id": "system.autocode.get"}),
        ]
        for name, arguments in cases:
            result = await runtime.invoke(name, arguments, context)
            assert result.error_code in {"OUT_OF_SCOPE", "API_UNAVAILABLE"}
        store.close()

    asyncio.run(scenario())
