import importlib
import socket

import pytest


def test_package_import_does_not_attempt_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_on_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("importing ktg_agent attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", fail_on_network)
    monkeypatch.setattr(socket.socket, "connect", fail_on_network)

    module = importlib.import_module("ktg_agent")

    assert module.__name__ == "ktg_agent"
