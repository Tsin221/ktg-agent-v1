from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Annotated
from unittest.mock import patch

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from ktg_agent.http_app import MAX_BODY_BYTES, PublicError, create_app, get_resources
from ktg_agent.http_candidate import run
from ktg_agent.server import Settings


def config(tmp_path: Path) -> Settings:
    return Settings("fake", "http://model.invalid", "fake", "http://mes.invalid",
                    session_db_path=str(tmp_path / "sessions.sqlite"))


def test_no_construct_side_effects_and_health(tmp_path: Path) -> None:
    settings = config(tmp_path)
    app = create_app(settings)
    assert not Path(settings.session_db_path).exists()
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/health").headers["cache-control"] == "no-store"
        assert client.get("/openapi.json").status_code == 200
        assert Path(settings.session_db_path).exists()
    assert app.state.resources is None
    assert not app.state.ready
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


def test_lifecycle_order_and_cleanup(tmp_path: Path) -> None:
    events: list[str] = []
    resource = object()

    def factory(settings: Settings, stack: AsyncExitStack) -> object:
        events.append("open")
        stack.callback(events.append, "close")
        events.extend(["recover_runs", "recover_operations"])
        return resource

    app = create_app(config(tmp_path), resource_factory=factory)
    assert events == []
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.resources is resource
        assert events == ["open", "recover_runs", "recover_operations"]
    assert events == ["open", "recover_runs", "recover_operations", "close"]

    def broken(settings: Settings, stack: AsyncExitStack) -> object:
        stack.callback(events.append, "partial_close")
        raise RuntimeError("startup failed")

    app = create_app(config(tmp_path), resource_factory=broken)
    with pytest.raises(RuntimeError, match="startup failed"), TestClient(app):
        pass
    assert events[-1] == "partial_close"
    assert not app.state.ready
    assert app.state.resources is None


def test_existing_recovery_once(tmp_path: Path) -> None:
    with patch("ktg_agent.http_app.SessionRepository.recover_interrupted_runs", return_value=1) as runs, patch(
        "ktg_agent.http_app.OperationPlanStore.recover_interrupted", return_value=1
    ) as operations:
        app = create_app(config(tmp_path))
        runs.assert_not_called()
        with TestClient(app) as client:
            client.get("/health")
            client.get("/health")
            runs.assert_called_once_with()
            operations.assert_called_once_with()
        runs.assert_called_once_with()


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    count: int


def test_http_boundary(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))

    @app.post("/test")
    def endpoint(body: Input, resources: Annotated[object, Depends(get_resources)]) -> Input:
        return body

    @app.get("/business")
    def business() -> None:
        raise PublicError(409, "RUN_BUSY", "Run is busy.")

    @app.get("/crash")
    def crash() -> None:
        raise ValueError("SECRET bearer credential")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/test", json={"count": 2}).json() == {"count": 2}
        for payload in [{"count": "2"}, {"count": 1, "token": "SECRET"}, [], {}]:
            response = client.post("/test", json=payload)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"
            assert "SECRET" not in response.text
        response = client.post("/test", content="{", headers={"content-type": "application/json"})
        assert response.json()["error"]["code"] == "INVALID_JSON"
        for body in ["", "x" * (MAX_BODY_BYTES + 1)]:
            assert client.post("/test", content=body).json()["error"]["code"] == "INVALID_REQUEST"
        for length in ["-1", "broken", str(MAX_BODY_BYTES + 1)]:
            assert client.post("/test", content="{}", headers={"content-length": length}).status_code == 400
        response = client.get("/business")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RUN_BUSY"
        assert client.get("/missing").json() == {"error": "NOT_FOUND"}
        response = client.get("/crash")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "SECRET" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/test").status_code == 405


def test_uvicorn_parameters(tmp_path: Path) -> None:
    settings = replace(config(tmp_path), host="127.0.0.1", port=18992)
    with patch("ktg_agent.http_candidate.uvicorn.run") as start:
        run(settings)
    assert start.call_args.kwargs == dict(host="127.0.0.1", port=18992, workers=1,
                                         reload=False, lifespan="on", access_log=False)
    assert not Path(settings.session_db_path).exists()


def test_invalid_config_and_not_ready(tmp_path: Path) -> None:
    app = create_app(replace(config(tmp_path), session_db_path=""))
    with pytest.raises(RuntimeError, match="absolute isolated"), TestClient(app):
        pass
    assert not app.state.ready
    client = TestClient(create_app(config(tmp_path)))
    assert client.get("/health").status_code == 503


def test_safe_upstream_errors_and_chunked_limit(tmp_path: Path) -> None:
    from ktg_agent.ktg.client import KTGError
    from ktg_agent.providers.base import ProviderError
    app = create_app(config(tmp_path))

    @app.get("/mes")
    def mes() -> None:
        raise KTGError("AUTH_INVALID", "SECRET", status=401)

    @app.get("/model")
    def model() -> None:
        raise ProviderError("FAILED", "SECRET")

    @app.post("/body")
    def body(value: Input) -> Input:
        return value

    with TestClient(app, raise_server_exceptions=False) as client:
        for path, status in [("/mes", 401), ("/model", 502)]:
            response = client.get(path)
            assert response.status_code == status
            assert "SECRET" not in response.text
            assert response.headers["x-content-type-options"] == "nosniff"
        response = client.post("/body", content=iter([b"x" * MAX_BODY_BYTES, b"x"]))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_async_shutdown_hook_on_request_failure(tmp_path: Path) -> None:
    events: list[str] = []

    async def close() -> None:
        events.append("async_close")

    def factory(settings: Settings, stack: AsyncExitStack) -> object:
        stack.push_async_callback(close)
        return object()

    with TestClient(create_app(config(tmp_path), resource_factory=factory)) as client:
        assert client.get("/health").status_code == 200
    assert events == ["async_close"]
