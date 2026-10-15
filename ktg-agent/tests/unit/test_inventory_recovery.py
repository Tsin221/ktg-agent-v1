"""A dropped write response must be resolved through the original ID."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.inventory import RECEIPT, InventoryStore, confirm_inventory, inventory_result
from ktg_agent.tools import ToolContext


OPERATION_ID = "77777777-7777-4777-8777-777777777777"
COMMAND: dict[str, object] = {
    "operationId": OPERATION_ID,
    "actionType": "ITEM_RECEIPT_EXECUTE",
    "documentId": 12,
    "expectedVersion": 4,
}


def test_dropped_write_response_reads_committed_result_once() -> None:
    calls: list[str] = []

    async def write(method: str, url: str, headers: Mapping[str, str],
                    body: Mapping[str, object], timeout: float) -> dict[str, object]:
        calls.append(method)
        assert body == COMMAND
        raise KTGError("KTG_CONNECTION_ERROR", "disconnected")

    async def read(url: str, headers: Mapping[str, str],
                   params: Mapping[str, object], timeout: float) -> dict[str, object]:
        calls.append("GET")
        assert url.endswith("/mes/wm/inventory-operations/" + OPERATION_ID)
        return {"code": 200, "data": {"operationId": OPERATION_ID,
                "operationStatus": "succeeded", "actionType": COMMAND["actionType"],
                "documentId": 12, "safeToRetry": False}}

    client = KTGClient(base_url="http://localhost", authorization="Bearer example",
                       transport=read, write_transport=write)
    result = asyncio.run(client.execute_inventory_operation(COMMAND))
    assert result["operationStatus"] == "succeeded"
    assert calls == ["POST", "GET"]


def test_both_requests_disconnect_stays_unknown() -> None:
    calls: list[str] = []

    async def write(method: str, url: str, headers: Mapping[str, str],
                    body: Mapping[str, object], timeout: float) -> dict[str, object]:
        calls.append(method)
        raise KTGError("KTG_CONNECTION_ERROR", "disconnected")

    async def read(url: str, headers: Mapping[str, str],
                   params: Mapping[str, object], timeout: float) -> dict[str, object]:
        calls.append("GET")
        raise KTGError("KTG_CONNECTION_ERROR", "disconnected")

    client = KTGClient(base_url="http://localhost", authorization="Bearer example",
                       transport=read, write_transport=write)
    result = asyncio.run(client.execute_inventory_operation(COMMAND))
    assert result == {"operationId": OPERATION_ID, "operationStatus": "unknown",
                      "safeToRetry": False}
    assert calls == ["POST", "GET"]


def test_restart_recovers_same_id_after_both_connections_fail() -> None:
    asyncio.run(_restart_recovers_same_id_after_both_connections_fail())


async def _restart_recovers_same_id_after_both_connections_fail() -> None:
    with TemporaryDirectory() as folder:
        state = {"writes": 0, "query_online": False, "balance": 10}
        proposal: JsonObject = {
            "documentId": 12, "documentCode": "IR-12", "documentStatus": "APPROVED",
            "documentVersion": 4, "additions": [{"sourceDocLineId": 1, "quantity": 2}],
        }

        async def read(url: str, headers: Mapping[str, str],
                       params: Mapping[str, object], timeout: float) -> dict[str, object]:
            if url.endswith(f"/mes/wm/inventory-operations/documents/{RECEIPT}/12"):
                return {"code": 200, "data": {"operationStatus": "unknown"}}
            if url.endswith("/mes/wm/itemrecpt/12/execution-preview"):
                return {"code": 200, "data": proposal}
            if not state["query_online"]:
                raise KTGError("KTG_CONNECTION_ERROR", "query disconnected")
            return {"code": 200, "data": {
                "operationId": url.rsplit("/", 1)[-1], "operationStatus": "succeeded",
                "actionType": RECEIPT, "documentId": 12, "documentStatus": "FINISHED",
                "transactions": [{"transactionId": 31}], "safeToRetry": False,
            }}

        async def write(method: str, url: str, headers: Mapping[str, str],
                        body: Mapping[str, object], timeout: float) -> dict[str, object]:
            assert body["operationId"] == operation_id
            state["writes"] += 1
            state["balance"] += 2
            raise KTGError("KTG_CONNECTION_ERROR", "write response disconnected")

        client = KTGClient(base_url="http://localhost", authorization="Bearer example",
                           transport=read, write_transport=write)
        db_path = str(Path(folder) / "operations.sqlite3")
        store = InventoryStore(db_path)
        operation_id = store.create("id:7", RECEIPT, proposal)
        context = ToolContext(run_id="run", tool_call_id="confirm", identity={"userId": 7},
                              ktg_client=client)
        unknown = await confirm_inventory(store, context, operation_id, RECEIPT)
        assert unknown["outcome"] == "unknown"
        assert state == {"writes": 1, "query_online": False, "balance": 12}

        restarted = InventoryStore(db_path)
        state["query_online"] = True
        recovered = await inventory_result(restarted, context, operation_id)
        assert recovered["outcome"] == "succeeded"
        receipt = recovered["receipt"]
        assert isinstance(receipt, dict)
        assert receipt["transactions"] == [{"transactionId": 31}]
        repeated = await confirm_inventory(restarted, context, operation_id, RECEIPT)
        assert repeated["outcome"] == "succeeded"
        assert state["writes"] == 1
        assert state["balance"] == 12
