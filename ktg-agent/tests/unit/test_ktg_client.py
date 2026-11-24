"""Authenticated fixed-origin transport behavior and failure handling."""

import asyncio
from collections.abc import Mapping
from http.client import RemoteDisconnected
from urllib.error import HTTPError
from io import BytesIO
from email.message import Message

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
            "http://127.0.0.1:18082/mes/example/resource",
            {"Authorization": "Bearer test-token"},
            {"name": "sample"},
            1.0,
        )

    assert error.value.code == "KTG_CONNECTION_ERROR"


def test_delete_uses_configured_origin_and_propagates_permission_errors() -> None:
    calls: list[tuple[str, str, Mapping[str, str], Mapping[str, object]]] = []

    async def write(method: str, url: str, headers: Mapping[str, str],
                    body: Mapping[str, object], _timeout: float) -> dict[str, object]:
        calls.append((method, url, headers, body))
        return {"code": 403, "msg": "denied"}

    client = KTGClient(base_url="http://127.0.0.1:8080", authorization="Bearer token",
                       write_transport=write)
    with pytest.raises(KTGError) as error:
        asyncio.run(client.delete("/mes/example/resource/17"))
    assert error.value.code == "PERMISSION_DENIED"
    assert calls == [("DELETE", "http://127.0.0.1:8080/mes/example/resource/17",
                      {"Authorization": "Bearer token"}, {})]


@pytest.mark.parametrize("path", ["https://example.invalid/", "//example.invalid/path", "relative"])
def test_delete_rejects_non_local_paths(path: str) -> None:
    client = KTGClient(base_url="http://127.0.0.1:8080", authorization="Bearer token")
    with pytest.raises(ValueError):
        asyncio.run(client.delete(path))


def test_operation_transport_preserves_structured_rejection_without_claiming_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(*_args: object, **_kwargs: object) -> object:
        raise HTTPError("http://127.0.0.1:8080/mes/pro/workorder/operations", 409, "Conflict", Message(),
                        BytesIO(b'{"code":409,"errorCode":"WORKORDER_CONFLICT","msg":"stale"}'))

    monkeypatch.setattr("ktg_agent.ktg.client.urlopen", reject)
    client = KTGClient(base_url="http://127.0.0.1:8080", authorization="Bearer token")
    result = asyncio.run(client.post_operation("/mes/pro/workorder/operations", {}))
    assert result == {"code": 409, "errorCode": "WORKORDER_CONFLICT", "msg": "stale"}
    with pytest.raises(KTGError):
        asyncio.run(client.post("/mes/pro/workorder/operations", {}))
