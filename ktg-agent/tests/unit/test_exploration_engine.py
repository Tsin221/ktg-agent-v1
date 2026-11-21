import asyncio
from collections.abc import Mapping

import pytest

from ktg_agent.exploration.engine import ReadEngine
from ktg_agent.exploration.engine import CATALOG_VERSION
from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools.registry import ToolContext


def test_scoped_reads_project_only_audited_fields_and_recheck_permissions() -> None:
    async def scenario() -> None:
        permissions = ["*:*:*"]
        calls: list[str] = []

        async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            calls.append(url)
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7}, "permissions": permissions}
            return {"code": 200, "data": {"machineryId": 1, "machineryCode": "R-01",
                                          "machineryName": "装配机", "password": "never-return"}}

        client = KTGClient(base_url="http://test", authorization="Bearer test", transport=transport)
        store = ReferenceStore()
        context = ToolContext("run-1", "call-1", identity={"userId": 7, "_agent_session_id": "s1"}, ktg_client=client)
        engine = await ReadEngine.create(context, store)
        row = await engine.read("dv.machinery", 1)
        assert row["code"] == "R-01" and "password" not in row
        reference = str(row["record_ref"])
        next_context = ToolContext("run-2", "call-2", identity={"userId": 7, "_agent_session_id": "s1"}, ktg_client=client)
        next_engine = await ReadEngine.create(next_context, store)
        entity, key = next_engine.resolve_record(reference)
        assert (entity.key, key) == ("dv.machinery", 1)
        other = ToolContext("run-3", "call-3", identity={"userId": 7, "_agent_session_id": "s2"}, ktg_client=client)
        with pytest.raises(KTGError) as denied:
            (await ReadEngine.create(other, store)).resolve_record(reference)
        assert denied.value.code == "REFERENCE_UNAVAILABLE"
        permissions.clear()
        with pytest.raises(KTGError) as revoked:
            await (await ReadEngine.create(next_context, store)).read("dv.machinery", 1)
        assert revoked.value.code == "PERMISSION_DENIED"
        assert len([url for url in calls if url.endswith("/machinery/1")]) == 1
        store.close()

    asyncio.run(scenario())


def test_showcase_scope_blocks_direct_ids_and_cached_results_even_for_admin() -> None:
    async def scenario() -> None:
        business_calls: list[str] = []

        async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7}, "permissions": ["*:*:*"]}
            business_calls.append(url)
            raise AssertionError("Out-of-scope requests must never reach the MES")

        client = KTGClient(base_url="http://test", authorization="Bearer test", transport=transport)
        store = ReferenceStore()
        context = ToolContext("run", "call", identity={"userId": 7}, ktg_client=client)
        engine = await ReadEngine.create(context, store)
        for entity in ("qc.ipqc", "cal.team", "md.client", "report.definition"):
            with pytest.raises(KTGError) as denied:
                await engine.read(entity, 1)
            assert denied.value.code == "OUT_OF_SCOPE"
        reference = store.put("result", owner=engine.owner, scope=engine.scope, payload={
            "catalog_version": CATALOG_VERSION, "requirements": [], "entities": ["qc.ipqc"],
            "result": {"records": [{"id": 1}]},
        })
        with pytest.raises(KTGError) as cached:
            engine.inspect(reference)
        assert cached.value.code == "OUT_OF_SCOPE"
        assert business_calls == []
        store.close()

    asyncio.run(scenario())


def test_scan_marks_limits_and_duplicate_pages_as_partial() -> None:
    async def scenario() -> None:
        async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7}, "permissions": ["*:*:*"]}
            return {"code": 200, "total": 200, "rows": [{"machineryId": 1, "machineryCode": "R-01"}]}

        client = KTGClient(base_url="http://test", authorization="Bearer test", transport=transport)
        store = ReferenceStore()
        context = ToolContext("run", "call", identity={"userId": 7}, ktg_client=client)
        engine = await ReadEngine.create(context, store)
        page = await engine.scan("dv.machinery")
        assert not page.complete and page.total == 200 and len(page.rows) == 1
        assert page.warnings
        store.close()

    asyncio.run(scenario())
