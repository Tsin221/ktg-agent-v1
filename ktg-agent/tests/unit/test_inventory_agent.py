"""Fake KTG contract for durable inventory preview and confirmation."""

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore, build_inventory_result_tool, build_inventory_tools, confirm_inventory, inventory_result
from ktg_agent.tools import RetryPolicy, ToolContext, ToolEffect, ToolRegistry, ToolRunner
from ktg_agent.tools.registry import ToolChannel


def test_preview_confirm_replay_and_restart() -> None:
    asyncio.run(_scenario(stale=False, action=RECEIPT))


def test_production_issue_preview_confirm_and_restart() -> None:
    asyncio.run(_scenario(stale=False, action=ISSUE))


def test_operation_result_tool_is_read_only_and_separate_for_mes12_catalog_wiring(tmp_path: Path) -> None:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    tool = build_inventory_result_tool(store)
    assert tool.name == "ktg_wm_operation_result"
    assert tool.effect is ToolEffect.READ
    assert tool.retry_policy is RetryPolicy.BOUNDED_READ
    assert tool.expose_to_model is True
    assert tool.required_permissions == ("mes:wm:wmstock:list",)


def test_changed_lines_block_write_and_replay() -> None:
    asyncio.run(_scenario(stale=True, action=RECEIPT))


def test_ambiguous_code_and_missing_read_permission_do_not_prepare_write() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as folder:
            async def read(url: str, _headers: Mapping[str, str],
                           _params: Mapping[str, object], _timeout: float) -> object:
                if url.endswith("/mes/wm/itemrecpt/list"):
                    return {"code": 200, "total": 2, "rows": [
                        {"recptId": 12, "recptCode": "IR-12"},
                        {"recptId": 13, "recptCode": "IR-12"}]}
                if url.endswith("/mes/wm/inventory-operations/documents/ITEM_RECEIPT_EXECUTE/12"):
                    raise KTGError("KTG_HTTP_ERROR", "KTG 接口返回 HTTP 403")
                raise AssertionError(url)

            client = KTGClient(base_url="http://localhost", authorization="Bearer test",
                               transport=read)
            store = InventoryStore(str(Path(folder) / "operations.sqlite3"))
            runner = ToolRunner(ToolRegistry(build_inventory_tools(store, frozenset({RECEIPT}))))
            context = ToolContext(run_id="run", tool_call_id="preview",
                                  identity={"userId": 7}, ktg_client=client)
            ambiguous = await runner.run(
                ToolCall("preview", "ktg_preview_item_receipt", {"document_code": "IR-12"}), context)
            assert ambiguous.details is None
            assert "未唯一匹配" in ambiguous.content
            denied = await runner.run(
                ToolCall("preview", "ktg_preview_item_receipt", {"document_id": 12}), context)
            assert denied.details is None
            assert "403" in denied.content
            with store._connect() as db:
                assert db.execute("SELECT COUNT(*) FROM inventory_previews").fetchone()[0] == 0

    asyncio.run(scenario())


def test_result_rechecks_document_read_permission_before_returning_cached_data() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as folder:
            proposal = {"documentId": 12, "documentCode": "IR-12", "documentStatus": "APPROVED",
                        "documentVersion": 4, "additions": [{"sourceDocLineId": 1, "quantity": 2}]}
            denied_urls: list[str] = []

            async def read(url: str, _headers: Mapping[str, str],
                           _params: Mapping[str, object], _timeout: float) -> object:
                denied_urls.append(url)
                raise KTGError("KTG_HTTP_ERROR", "forbidden")

            store = InventoryStore(str(Path(folder) / "operations.sqlite3"))
            operation_id = store.create("id:7", RECEIPT, proposal)
            context = ToolContext(run_id="run", tool_call_id="result", identity={"userId": 7},
                                  ktg_client=KTGClient(base_url="http://localhost", authorization="Bearer test",
                                                       transport=read))
            pending = await inventory_result(store, context, operation_id)
            assert pending["error_code"] == "OPERATION_NOT_FOUND"
            assert "documentCode" not in pending
            with store._connect() as db:
                db.execute("UPDATE inventory_previews SET state='finished', result_json=? WHERE operation_id=?",
                           ('{"kind":"inventory_result","operation_id":"' + operation_id + '","outcome":"succeeded","label":"ok","receipt":{"secret":true}}', operation_id))
            cached = await inventory_result(store, context, operation_id)
            assert cached["error_code"] == "OPERATION_NOT_FOUND"
            assert "receipt" not in cached
            assert len(denied_urls) == 2
            assert all("/documents/ITEM_RECEIPT_EXECUTE/12" in url for url in denied_urls)

    asyncio.run(scenario())


def test_expired_preview_cannot_be_confirmed_or_returned_as_prepared() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as folder:
            proposal = {"documentId": 12, "documentCode": "IR-12", "documentStatus": "APPROVED",
                        "documentVersion": 4, "additions": [{"sourceDocLineId": 1, "quantity": 2}]}
            writes = 0

            async def read(url: str, _headers: Mapping[str, str],
                           _params: Mapping[str, object], _timeout: float) -> object:
                if url.endswith("/documents/ITEM_RECEIPT_EXECUTE/12"):
                    return {"code": 200, "data": {"readable": True}}
                raise AssertionError(url)

            async def write(_method: str, _url: str, _headers: Mapping[str, str],
                            _body: Mapping[str, object], _timeout: float) -> dict[str, object]:
                nonlocal writes
                writes += 1
                raise AssertionError("expired proposal must not execute")

            store = InventoryStore(str(Path(folder) / "operations.sqlite3"))
            operation_id = store.create("id:7", RECEIPT, proposal)
            with store._connect() as db:
                db.execute("UPDATE inventory_previews SET expires_at=? WHERE operation_id=?", (time.time() - 1, operation_id))
            context = ToolContext(run_id="run", tool_call_id="confirm", identity={"userId": 7},
                                  ktg_client=KTGClient(base_url="http://localhost", authorization="Bearer test",
                                                       transport=read, write_transport=write))
            expired = await inventory_result(store, context, operation_id)
            assert expired["error_code"] == "PREVIEW_EXPIRED"
            assert "documentCode" not in expired
            confirmed = await confirm_inventory(store, context, operation_id, RECEIPT)
            assert confirmed["error_code"] == "PREVIEW_EXPIRED"
            assert writes == 0

    asyncio.run(scenario())


async def _scenario(*, stale: bool, action: str) -> None:
    with TemporaryDirectory() as folder:
        state = {"quantity": 5, "writes": 0, "reads": 0}
        resource = "itemrecpt" if action == RECEIPT else "issueheader"
        code_key = "recptCode" if action == RECEIPT else "issueCode"
        id_key = "recptId" if action == RECEIPT else "issueId"
        document_code = "IR-12" if action == RECEIPT else "PI-12"

        async def read(url: str, _headers: Mapping[str, str],
                       params: Mapping[str, object], _timeout: float) -> object:
            state["reads"] += 1
            if url.endswith(f"/mes/wm/{resource}/list"):
                assert params == {code_key: document_code, "pageNum": 1, "pageSize": 100}
                return {"code": 200, "total": 1, "rows": [{id_key: 12, code_key: document_code}]}
            if url.endswith(f"/mes/wm/inventory-operations/documents/{action}/12"):
                return {"code": 200, "data": {"operationStatus": "unknown"}}
            if url.endswith(f"/mes/wm/{resource}/12/execution-preview"):
                lines = {"additions": [{"sourceDocLineId": 1, "itemCode": "A",
                                         "quantity": state["quantity"], "warehouseName": "Main"}]} if action == RECEIPT else {
                    "movements": [{"sourceDocLineId": 1, "itemCode": "A",
                                   "quantity": state["quantity"], "sourceWarehouseName": "Main",
                                   "target": {"warehouseName": "Line"}}]}
                return {"code": 200, "data": {"documentId": 12, "documentCode": document_code,
                        "documentStatus": "APPROVED", "documentVersion": 4,
                        **lines}}
            if "/mes/wm/inventory-operations/" in url:
                if not state["writes"]:
                    raise KTGError("KTG_HTTP_ERROR", "not found")
                return {"code": 200, "data": {"operationId": url.rsplit("/", 1)[-1],
                        "operationStatus": "succeeded", "actionType": action,
                        "documentId": 12}}
            raise AssertionError(url)

        async def write(_method: str, _url: str, _headers: Mapping[str, str],
                        body: Mapping[str, object], _timeout: float) -> dict[str, object]:
            assert body["documentId"] == 12
            assert body["expectedVersion"] == 4
            state["writes"] += 1
            raise KTGError("KTG_CONNECTION_ERROR", "response lost")

        client = KTGClient(base_url="http://localhost", authorization="Bearer test",
                           transport=read, write_transport=write)
        store = InventoryStore(str(Path(folder) / "operations.sqlite3"))
        registry = ToolRegistry((*build_inventory_tools(store, frozenset({action})),
                                 build_inventory_result_tool(store)))
        runner = ToolRunner(registry)
        identity = {"userId": 7, "userName": "alice"}
        context = ToolContext(run_id="run", tool_call_id="preview", identity=identity,
                              ktg_client=client)
        preview_name = "ktg_preview_item_receipt" if action == RECEIPT else "ktg_preview_production_issue"
        invalid: tuple[JsonObject, ...] = (
            {}, {"document_id": 12, "document_code": document_code}, {"document_id": "12"},
            {"document_code": document_code, "quantity": 5}, {"documentId": 12},
        )
        for arguments in invalid:
            result = await runner.run(ToolCall("preview", preview_name, arguments), context)
            assert result.error_code == "VALIDATION_ERROR"
        assert state["reads"] == 0
        selector: JsonObject = {"document_id": 12} if action == RECEIPT else {"document_code": document_code}
        preview = await runner.run(ToolCall("preview", preview_name, selector), context)
        assert preview.details is not None
        operation_id = preview.details["operation_id"]
        assert isinstance(operation_id, str)
        assert operation_id in preview.content
        pending = await runner.run(ToolCall("preview", "ktg_wm_operation_result", {"operation_id": operation_id}), context)
        assert pending.error_code == "OPERATION_PENDING"
        invalid_result = await runner.run(ToolCall("preview", "ktg_wm_operation_result", {"operation_id": "x" * 36}), context)
        assert invalid_result.error_code == "VALIDATION_ERROR"
        other_user = ToolContext(run_id="run", tool_call_id="result", identity={"userId": 8}, ktg_client=client)
        unauthorized = await runner.run(
            ToolCall("result", "ktg_wm_operation_result", {"operation_id": operation_id}), other_user)
        assert unauthorized.error_code == "OPERATION_NOT_FOUND"
        assert unauthorized.details is not None and "documentCode" not in unauthorized.details
        assert state["writes"] == 0
        if stale:
            state["quantity"] = 6
        model_attempt = await runner.run(
            ToolCall("model-write", "ktg_execute_item_receipt" if action == RECEIPT else "ktg_execute_production_issue", {"operation_id": operation_id}),
            ToolContext(run_id="run", tool_call_id="model-write", operation_id=operation_id,
                        identity=identity, ktg_client=client))
        assert model_attempt.error_code == "TOOL_NOT_FOUND"
        assert state["writes"] == 0
        confirmed = await runner.run(
            ToolCall("confirm", "ktg_execute_item_receipt" if action == RECEIPT else "ktg_execute_production_issue", {"operation_id": operation_id}),
            ToolContext(run_id="run", tool_call_id="confirm", operation_id=operation_id,
                        channel=ToolChannel.TRUSTED_CONFIRMATION, confirmation_id=operation_id,
                        identity=identity, ktg_client=client))
        assert confirmed.details is not None
        assert confirmed.details["outcome"] == ("failed" if stale else "succeeded")
        assert state["writes"] == (0 if stale else 1)
        restarted = InventoryStore(str(Path(folder) / "operations.sqlite3"))
        recovered = await inventory_result(restarted, context, operation_id)
        assert recovered["outcome"] == confirmed.details["outcome"]
        replay = await runner.run(
            ToolCall("repeat", "ktg_execute_item_receipt" if action == RECEIPT else "ktg_execute_production_issue", {"operation_id": operation_id}),
            ToolContext(run_id="run", tool_call_id="repeat", operation_id=operation_id,
                        channel=ToolChannel.TRUSTED_CONFIRMATION, confirmation_id=operation_id,
                        identity=identity, ktg_client=client))
        assert replay.details is not None
        assert replay.details["outcome"] == confirmed.details["outcome"]
        assert state["writes"] == (0 if stale else 1)
        denied = await inventory_result(restarted, ToolContext(
            run_id="run", tool_call_id="other", identity={"userId": 8}, ktg_client=client), operation_id)
        assert denied["error_code"] == "OPERATION_NOT_FOUND"
