"""HTTP-level authorization and pagination checks for the session API."""

from __future__ import annotations

import json
import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Lock, Thread
from typing import cast

from ktg_agent.core.messages import AssistantMessage, JsonObject, StopReason, TextBlock, UserMessage
from ktg_agent.core.events import EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import Message
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.server import AgentHTTPServer, Settings, create_service
from ktg_agent.service import QueryService
from ktg_agent.providers.base import FinalResponse, ModelConfig, ModelEvent, ProviderRequest, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.coordinator import RunCoordinator
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


class FakeLegacyService:
    async def query(self, *, message: str, authorization: str) -> dict[str, object]:
        return {"kind": "query", "message": message}

    async def confirm(self, *, preview_id: str, authorization: str) -> dict[str, object]:
        return {"kind": "workorder", "preview_id": preview_id}

    async def confirm_inventory(self, *, operation_id: str,
                                authorization: str) -> dict[str, object]:
        return {"kind": "inventory", "operation_id": operation_id}


@contextmanager
def running_server(repo: SessionRepository) -> Iterator[AgentHTTPServer]:
    settings = Settings("key", "http://localhost", "model", "http://localhost")
    session_service = SessionService(repo, lambda authorization: cast(KTGClient, FakeClient(authorization)))
    server = AgentHTTPServer(("127.0.0.1", 0), create_service(settings), session_service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def request(server: AgentHTTPServer, method: str, path: str, *,
            token: str = "Bearer alice", body: Mapping[str, object] | None = None
            ) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", server.server_port)
    headers = {"Authorization": token}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    return status, payload


def test_session_http_owner_isolation_and_messages(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    with running_server(repo) as server:
        status, created = request(server, "POST", "/agent-api/v1/sessions",
                                  body={"title": "first", "userId": 8})
        assert status == 201
        sid = cast(str, created["session_id"])
        assert cast(str, created["created_at"]).endswith("+00:00")
        run = repo.create_run("id:7", sid, "r1", "h1", {})
        repo.append_message("id:7", run.id, UserMessage((TextBlock("hello"),)))
        repo.append_message("id:7", run.id, UserMessage((TextBlock("again"),)))

        status, session = request(server, "GET", f"/agent-api/v1/sessions/{sid}")
        assert status == 200 and session["active_run_id"] == run.id
        status, messages = request(server, "GET", f"/agent-api/v1/sessions/{sid}/messages?limit=1")
        assert status == 200
        assert cast(list[dict[str, object]], messages["items"])[0]["role"] == "user"
        next_page = request(server, "GET", f"/agent-api/v1/sessions/{sid}/messages?cursor={messages['next_cursor']}")[1]
        assert len(cast(list[object], next_page["items"])) == 1
        assert request(server, "GET", f"/agent-api/v1/sessions/{sid}", token="Bearer bob")[0] == 404
        assert request(server, "GET", f"/agent-api/v1/sessions/{sid}/messages", token="Bearer bob")[0] == 404
        assert request(server, "GET", "/agent-api/v1/sessions", token="Bearer bob")[1]["items"] == []
        assert request(server, "GET", "/agent-api/v1/sessions/no-such-session")[0] == 404


def test_session_http_pagination_and_auth(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    with running_server(repo) as server:
        ids = [request(server, "POST", "/agent-api/v1/sessions", body={})[1]["session_id"]
               for _ in range(3)]
        status, page = request(server, "GET", "/agent-api/v1/sessions?limit=2")
        assert status == 200
        assert [item["session_id"] for item in cast(list[dict[str, object]], page["items"])] == ids[2:0:-1]
        cursor = page["next_cursor"]
        second = request(server, "GET", f"/agent-api/v1/sessions?cursor={cursor}&limit=2")[1]
        assert [item["session_id"] for item in cast(list[dict[str, object]], second["items"])] == ids[:1]
        for _ in range(99):
            repo.create_session("id:7")
        capped = request(server, "GET", "/agent-api/v1/sessions?limit=999")[1]
        assert len(cast(list[object], capped["items"])) == 100
        assert capped["next_cursor"] is not None
        assert request(server, "GET", "/agent-api/v1/sessions?limit=0")[0] == 400
        for token in ("", "Bearer expired", "Bearer invalid"):
            assert request(server, "POST", "/agent-api/v1/sessions", token=token, body={})[0] == 401
            assert request(server, "GET", "/agent-api/v1/sessions", token=token)[0] == 401
            assert request(server, "GET", f"/agent-api/v1/sessions/{ids[0]}", token=token)[0] == 401


def test_existing_post_routes_still_dispatch(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    with running_server(repo) as server:
        server.query_service = cast(QueryService, FakeLegacyService())
        for path, body, kind in (
            ("/agent-api/v1/query", {"message": "hello"}, "query"),
            ("/agent-api/v1/workorder-drafts/confirm", {"preview_id": "p1"}, "workorder"),
            ("/agent-api/v1/inventory-operations/confirm", {"operation_id": "i1"}, "inventory"),
        ):
            status, response = request(server, "POST", path, body=body)
            assert status == 200 and response["kind"] == kind


class BlockingFakeProvider(FakeProvider):
    def __init__(self) -> None:
        turn = ScriptedTurn((ResponseCompleted(FinalResponse(
            message=AssistantMessage((TextBlock("完成"),)),
            stop_reason=StopReason.COMPLETED, usage=Usage(1, 1),
        )),))
        super().__init__([turn, turn])
        self.started = Event()
        self.release = Event()
        self.lock = Lock()
        self.calls = 0
        self.two_started = Event()

    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        async def blocked() -> AsyncIterator[ModelEvent]:
            with self.lock:
                self.calls += 1
                self.started.set()
                if self.calls == 2:
                    self.two_started.set()
            await asyncio.to_thread(self.release.wait)
            async for event in super(BlockingFakeProvider, self).stream(request):
                yield event
        return blocked()


def test_run_http_atomic_admission_and_owner_isolation(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    provider = BlockingFakeProvider()
    query_service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda token: cast(KTGClient, FakeClient(token)),
    )
    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      authorization: str, event_sink: EventSink) -> AgentRunResult:
        return await query_service.run_session(
            run_id=run_id, message=message, history=history,
            authorization=authorization, event_sink=event_sink,
        )

    session_service = SessionService(
        repo, lambda token: cast(KTGClient, FakeClient(token)),
        RunCoordinator(repo, execute),
    )
    settings = Settings("key", "http://localhost", "model", "http://localhost")
    server = AgentHTTPServer(("127.0.0.1", 0), create_service(settings), session_service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sid = cast(str, request(server, "POST", "/agent-api/v1/sessions", body={})[1]["session_id"])
        path = f"/agent-api/v1/sessions/{sid}/runs"
        body = {"request_id": "click-1", "message": " 查库存 ", "page_context": {"filters": {"itemCode": "A"}}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, server, "POST", path, body=body) for _ in range(2)]
            replies = [future.result() for future in futures]
        assert [status for status, _ in replies] == [202, 202]
        rid = cast(str, replies[0][1]["run_id"])
        assert replies[1][1]["run_id"] == rid
        assert "events_url" not in replies[0][1]
        assert len(repo.list_runs("id:7", sid).items) == 1
        assert provider.started.wait(3)
        assert provider.calls == 1
        conflict_status, conflict_body = request(server, "POST", path, body={
            "request_id": "click-1", "message": "different",
        })
        assert conflict_status == 409 and conflict_body["error"] == {
            "code": "REQUEST_ID_CONFLICT", "message": "request_id 已用于不同请求。",
        }
        busy_status, busy_body = request(server, "POST", path, body={
            "request_id": "click-2", "message": "next",
        })
        assert busy_status == 409
        assert cast(dict[str, object], busy_body["error"])["code"] == "RUN_BUSY"
        assert request(server, "GET", f"/agent-api/v1/runs/{rid}", token="Bearer bob")[0] == 404
        assert request(server, "GET", f"/agent-api/v1/runs/{rid}", token="Bearer expired")[0] == 401
        snapshot = request(server, "GET", f"/agent-api/v1/runs/{rid}")[1]
        assert snapshot["status"] == "running" and cast(int, snapshot["last_seq"]) >= 1
        assert snapshot["stop_reason"] is None and snapshot["result_summary"] is None
        other_sid = cast(str, request(server, "POST", "/agent-api/v1/sessions", body={})[1]["session_id"])
        assert request(server, "POST", f"/agent-api/v1/sessions/{other_sid}/runs",
                       body={"request_id": "click-2", "message": "other session"})[0] == 202
        assert provider.two_started.wait(3)
        assert request(server, "POST", path, token="Bearer bob", body=body)[0] == 404
        assert request(server, "POST", path, body={"request_id": "bad", "message": "hi",
                       "page_context": {"Authorization": "secret"}})[0] == 400
        provider.release.set()
    finally:
        provider.release.set()
        server.shutdown()
        thread.join()
        server.server_close()
    saved = repo.get_run("id:7", rid)
    assert saved is not None and saved.status == "completed" and saved.stop_reason == "completed"
    assert saved.result_summary is not None and saved.result_summary["answer"] == "完成"
