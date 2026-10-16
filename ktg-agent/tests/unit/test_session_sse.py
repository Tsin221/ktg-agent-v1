"""HTTP checks for durable SSE replay and continuation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from threading import BoundedSemaphore, Thread
from time import monotonic, sleep
from typing import Iterator, cast

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.server import AgentHTTPServer, _sse_event
from ktg_agent.service import QueryService
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService


class FakeClient:
    def __init__(self, authorization: str) -> None:
        self.authorization = authorization

    async def get_current_user(self) -> dict[str, object]:
        if self.authorization == "Bearer expired":
            raise KTGError("AUTH_EXPIRED", "expired", status=401)
        users = {"Bearer alice": 7, "Bearer bob": 8}
        if self.authorization not in users:
            raise KTGError("AUTH_REQUIRED", "missing", status=401)
        return {"userId": users[self.authorization]}


@contextmanager
def served(repo: SessionRepository) -> Iterator[AgentHTTPServer]:
    session_service = SessionService(repo, lambda token: cast(KTGClient, FakeClient(token)))
    server = AgentHTTPServer(("127.0.0.1", 0), cast(QueryService, object()), session_service)
    server.sse_poll_seconds = 0.02
    server.sse_heartbeat_seconds = 0.1
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def subscribe(server: AgentHTTPServer, path: str, token: str = "Bearer alice"
              ) -> tuple[HTTPConnection, HTTPResponse]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.request("GET", path, headers={"Authorization": token})
    return connection, connection.getresponse()


def frames(response: HTTPResponse) -> list[str]:
    # Read one byte at a time to simulate arbitrary network chunk boundaries.
    return [frame for frame in response.read().decode("utf-8").split("\n\n") if frame]


def next_frame(response: HTTPResponse) -> str:
    data = bytearray()
    while not data.endswith(b"\n\n"):
        chunk = response.read(1)
        if not chunk:
            raise AssertionError("SSE connection closed before the next frame")
        data.extend(chunk)
    return data.decode("utf-8")


def event(frame: str) -> JsonObject:
    return cast(JsonObject, json.loads("\n".join(
        line.removeprefix("data: ") for line in frame.splitlines()
        if line.startswith("data: ")
    )))


def test_completed_run_replays_all_pages_with_multiline_frames(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    for seq in range(1, 102):
        repo.append_event("id:7", run.id, seq, "message_update", {"text": f"part {seq}"})
    repo.settle_run("id:7", run.id, "completed", "completed", {"answer": "done"}, ())
    with served(repo) as server:
        path = f"/agent-api/v1/runs/{run.id}/events?after_seq=0"
        connection, response = subscribe(server, path)
        try:
            assert response.status == 200
            assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
            assert response.getheader("X-Accel-Buffering") == "no"
            assert response.getheader("Cache-Control") == "no-cache, no-store, no-transform"
            replay = frames(response)
        finally:
            connection.close()
        assert len(replay) == 102
        assert [event(frame)["seq"] for frame in replay] == list(range(1, 103))
        assert replay[0].startswith("id: 1\nevent: message_update\ndata: {\n")
        assert replay[-1].startswith("id: 102\nevent: run_settled\n")
        assert cast(JsonObject, event(replay[-1])["payload"])["status"] == "completed"


def test_live_subscription_reconnects_without_reexecuting_run(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    repo.append_event("id:7", run.id, 1, "agent_start", {})
    with served(repo) as server:
        path = f"/agent-api/v1/runs/{run.id}/events"
        first_connection, first = subscribe(server, path)
        assert first.status == 200
        assert event(next_frame(first))["seq"] == 1
        repo.append_event("id:7", run.id, 2, "message_update", {"text": "live"})
        assert event(next_frame(first))["seq"] == 2
        first_connection.close()
        repo.settle_run("id:7", run.id, "completed", "completed", {}, ())
        second_connection, second = subscribe(server, path + "?after_seq=2")
        try:
            assert [event(frame)["type"] for frame in frames(second)] == ["run_settled"]
        finally:
            second_connection.close()
        assert len(repo.list_runs("id:7", session.id).items) == 1


def test_sse_auth_cursor_expiry_heartbeat_and_shutdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    route = f"/agent-api/v1/runs/{run.id}/events"
    with served(repo) as server:
        for suffix, token, expected in (
            ("", "Bearer bob", 404), ("", "Bearer expired", 401),
            ("?after_seq=-1", "Bearer alice", 400),
            ("?after_seq=1&after_seq=2", "Bearer alice", 400),
            ("?token=secret", "Bearer alice", 400),
        ):
            connection, response = subscribe(server, route + suffix, token)
            assert response.status == expected
            assert response.getheader("Content-Type") == "application/json; charset=utf-8"
            connection.close()
        connection, response = subscribe(server, route)
        assert response.status == 200
        assert next_frame(response) == ": heartbeat\n\n"
        assert repo.last_event_seq("id:7", run.id) == 0
        server.shutdown()
        assert frames(response) == []
        connection.close()
    repo.append_event("id:7", run.id, 1, "agent_start", {})
    repo.settle_run("id:7", run.id, "completed", "completed", {}, ())
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE runs SET ended_at=? WHERE id=?", (old, run.id))
    with served(repo) as server:
        connection, response = subscribe(server, route)
        assert response.status == 410
        assert json.loads(response.read())["error"]["code"] == "EVENT_HISTORY_EXPIRED"
        connection.close()
        connection, response = subscribe(server, route + "?after_seq=2")
        assert response.status == 200 and frames(response) == []
        connection.close()
    assert "secret" not in capsys.readouterr().err


def test_frame_formatter_preserves_multiline_json_and_event_id() -> None:
    frame = _sse_event({"seq": 3, "type": "message_update", "payload": {"text": "a\nb"}})
    assert frame.endswith(b"\n\n")
    assert event(frame.decode()) == {
        "seq": 3, "type": "message_update", "payload": {"text": "a\nb"},
    }


def test_subscription_limit_releases_slot_after_terminal_event(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    route = f"/agent-api/v1/runs/{run.id}/events"
    with served(repo) as server:
        server.sse_slots = BoundedSemaphore(1)
        first_connection, first = subscribe(server, route)
        assert first.status == 200
        second_connection, second = subscribe(server, route)
        assert second.status == 503
        assert json.loads(second.read())["error"]["code"] == "SSE_CAPACITY_EXCEEDED"
        second_connection.close()
        repo.settle_run("id:7", run.id, "completed", "completed", {}, ())
        assert event(next_frame(first))["type"] == "run_settled"
        assert first.read() == b""
        first_connection.close()
        third_connection, third = subscribe(server, route)
        assert third.status == 200
        assert [event(frame)["type"] for frame in frames(third)] == ["run_settled"]
        third_connection.close()


def test_client_disconnect_releases_subscription_without_cancelling_run(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    with served(repo) as server:
        server.sse_slots = BoundedSemaphore(1)
        connection, response = subscribe(server, f"/agent-api/v1/runs/{run.id}/events")
        assert response.status == 200
        response.close()
        connection.close()
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if server.sse_slots.acquire(blocking=False):
                server.sse_slots.release()
                break
            sleep(0.02)
        else:
            raise AssertionError("subscriber slot was not released")
        assert repo.get_run("id:7", run.id).status == "queued"  # type: ignore[union-attr]
