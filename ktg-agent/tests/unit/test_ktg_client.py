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
