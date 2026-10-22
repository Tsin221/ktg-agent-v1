"""Transport failures that leave workorder write results uncertain."""

from http.client import RemoteDisconnected

import pytest

from ktg_agent.ktg.client import KTGClient, KTGError


def test_committed_write_with_dropped_response_is_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def drop_response(*_args: object, **_kwargs: object) -> object:
        raise RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr("ktg_agent.ktg.client.urlopen", drop_response)

    with pytest.raises(KTGError) as error:
        KTGClient._send_json(
            "POST",
            "http://127.0.0.1:18082/mes/pro/workorder",
            {"Authorization": "Bearer test-token"},
            {"workorderCode": "S2R-LOSS"},
            1.0,
        )

    assert error.value.code == "KTG_CONNECTION_ERROR"
import asyncio
from collections.abc import Mapping

from ktg_agent.ktg.client import KTGClient


def test_workorder_digest_matches_mes09_canonical_vector() -> None:
    draft = {
        "workorderCode": "WO-100", "workorderName": "Sample workorder",
        "workorderType": "SELF", "orderSource": "MANUAL", "productId": 12,
        "quantity": "10.000", "requestDate": "2026-10-23", "parentId": 0,
    }
    assert KTGClient.workorder_operation_digest("CREATE_DRAFT", draft) == (
        "sha256:877e68bb22edbf476576ac950402b13ff40e0dc1b7b385a50f37ce2e1cdcd206"
    )


def test_workorder_operation_unknown_queries_same_id_and_does_not_blind_retry() -> None:
    writes: list[tuple[str, str, Mapping[str, object]]] = []
    reads: list[str] = []

    async def read(url: str, _headers: Mapping[str, str], _params: Mapping[str, object], _timeout: float) -> object:
        reads.append(url)
        return {"code": 404, "msg": "not found"}

    async def write(method: str, url: str, _headers: Mapping[str, str], body: Mapping[str, object], _timeout: float) -> dict[str, object]:
        writes.append((method, url, body))
        return {"code": 202, "errorCode": "RESULT_UNKNOWN"}

    client = KTGClient(base_url="http://127.0.0.1:8080", authorization="Bearer token",
                       transport=read, write_transport=write)
    draft = {"workorderCode": "WO-100"}
    command: dict[str, object] = {
        "operationId": "8eec45ac-f6ea-4d07-8758-bec40a772dde",
        "action": "CREATE_DRAFT", "expectedVersion": None, "draft": draft,
        "requestDigest": KTGClient.workorder_operation_digest("CREATE_DRAFT", draft),
    }
    result = asyncio.run(client.execute_workorder_operation(command))
    assert result["outcome"] == "unknown"
    assert result["operationId"] == command["operationId"]
    assert len(writes) == 1 and writes[0][0] == "POST"
    assert writes[0][1].endswith("/mes/pro/workorder/operations")
    assert reads == [f"http://127.0.0.1:8080/mes/pro/workorder/operations/{command['operationId']}"] * 2
