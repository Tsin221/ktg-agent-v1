"""HTTP contract for durable run cancellation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Thread
from typing import cast

from ktg_agent.core.events import EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import JsonObject, Message
from ktg_agent.ktg import KTGClient
from ktg_agent.server import AgentHTTPServer, Settings, create_service
from ktg_agent.sessions.coordinator import RegisterAgent, RunCoordinator
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.sessions.service import SessionService
from test_session_http import FakeClient, request


@contextmanager
def served(repo: SessionRepository, coordinator: RunCoordinator) -> Iterator[AgentHTTPServer]:
    settings = Settings("key", "http://localhost", "model", "http://localhost")
    service = SessionService(repo, lambda token: cast(KTGClient, FakeClient(token)), coordinator)
    server = AgentHTTPServer(("127.0.0.1", 0), create_service(settings), service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_cancel_http_queued_idempotent_and_settled(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "run-1", "hash-1", {})
    calls: list[str] = []

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        calls.append(run_id)
        raise AssertionError("cancelled queued run must not execute")

    coordinator = RunCoordinator(repo, execute)
    release = Event()
    occupied = [Event() for _ in range(4)]

    def occupy(started: Event) -> None:
        started.set()
        release.wait()

    workers = [coordinator._workers.submit(occupy, started) for started in occupied]
    try:
        assert all(started.wait(5) for started in occupied)
        coordinator.schedule(run, "question", "Bearer alice")
        path = f"/agent-api/v1/runs/{run.id}/cancel"
        with served(repo, coordinator) as server:
            assert request(server, "POST", path, token="Bearer bob",
                           body={"request_id": "cancel-1"}) == (404, {
                "error": {"code": "RUN_NOT_FOUND", "message": "运行不存在。"}})
            assert request(server, "POST", path, token="Bearer expired",
                           body={"request_id": "cancel-1"})[0] == 401
            for body in ({}, {"request_id": ""}, {"request_id": "x" * 129},
                         {"request_id": "cancel-1", "extra": True}):
                assert request(server, "POST", path, body=body)[0] == 400
            first = request(server, "POST", path, body={"request_id": "cancel-1"})
            assert first == (202, {"run_id": run.id, "request_id": "cancel-1",
                                   "status": "cancelling", "cancel_status": "accepted"})
            assert request(server, "POST", path, body={"request_id": "cancel-1"}) == first
            assert request(server, "GET", f"/agent-api/v1/runs/{run.id}")[1]["status"] == "cancelling"
            assert request(server, "POST", path, body={"request_id": "cancel-2"})[0] == 202
            assert request(server, "POST", path, body={"request_id": "cancel-1", "extra": 1})[0] == 400
            assert [item.type for item in repo.list_events("id:7", run.id).items] == [
                "cancel_requested", "cancel_requested"]
            release.set()
            for worker in workers:
                worker.result(5)
            coordinator.close()
            assert calls == []
            assert repo.get_run("id:7", run.id).status == "cancelled"  # type: ignore[union-attr]
            before = repo.last_event_seq("id:7", run.id)
            settled = request(server, "POST", path, body={"request_id": "after"})
            assert settled == (200, {"run_id": run.id, "request_id": "after",
                                     "status": "cancelled", "cancel_status": "already_settled"})
            assert request(server, "POST", path, body={"request_id": "after"}) == settled
            assert request(server, "POST", path, body={"request_id": "cancel-1"}) == first
            assert repo.last_event_seq("id:7", run.id) == before
            assert repo.list_events("id:7", run.id).items[-1].type == "run_settled"
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", f"/agent-api/v1/runs/{run.id}/events?after_seq=0",
                               headers={"Authorization": "Bearer alice"})
            replay = connection.getresponse()
            assert replay.status == 200
            frames = replay.read().decode("utf-8")
            connection.close()
            assert "event: cancel_requested" in frames
            assert '"status": "cancelling"' in frames
            assert "event: run_settled" in frames
    finally:
        release.set()
        for worker in workers:
            worker.result(5)
