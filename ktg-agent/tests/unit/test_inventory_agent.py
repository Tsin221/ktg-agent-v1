"""Fake KTG contract for durable inventory preview and confirmation."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from ktg_agent.core.messages import ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore, build_inventory_tools, inventory_result
from ktg_agent.tools import ToolContext, ToolRegistry, ToolRunner
from ktg_agent.tools.registry import ToolChannel


def test_preview_confirm_replay_and_restart() -> None:
    asyncio.run(_scenario(stale=False, action=RECEIPT))


def test_production_issue_preview_confirm_and_restart() -> None:
    asyncio.run(_scenario(stale=False, action=ISSUE))


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


async def _scenario(*, stale: bool, action: str) -> None:
    with TemporaryDirectory() as folder:
        state = {"quantity": 5, "writes": 0}

        async def read(url: str, _headers: Mapping[str, str],
                       _params: Mapping[str, object], _timeout: float) -> object:
            if url.endswith(f"/mes/wm/inventory-operations/documents/{action}/12"):
                return {"code": 200, "data": {"operationStatus": "unknown"}}
            resource = "itemrecpt" if action == RECEIPT else "issueheader"
            if url.endswith(f"/mes/wm/{resource}/12/execution-preview"):
                lines = {"additions": [{"sourceDocLineId": 1, "itemCode": "A",
                                         "quantity": state["quantity"], "warehouseName": "Main"}]} if action == RECEIPT else {
                    "movements": [{"sourceDocLineId": 1, "itemCode": "A",
                                   "quantity": state["quantity"], "sourceWarehouseName": "Main",
                                   "target": {"warehouseName": "Line"}}]}
                return {"code": 200, "data": {"documentId": 12, "documentCode": "IR-12",
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
        registry = ToolRegistry(build_inventory_tools(store, frozenset({action})))
        runner = ToolRunner(registry)
        identity = {"userId": 7, "userName": "alice"}
        context = ToolContext(run_id="run", tool_call_id="preview", identity=identity,
                              ktg_client=client)
        preview = await runner.run(
            ToolCall("preview", "ktg_preview_item_receipt" if action == RECEIPT else "ktg_preview_production_issue", {"document_id": 12}), context)
        assert preview.details is not None
        operation_id = preview.details["operation_id"]
        assert isinstance(operation_id, str)
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
