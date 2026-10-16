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
from time import monotonic, sleep
from typing import cast

from ktg_agent.core.messages import AssistantMessage, JsonObject, StopReason, TextBlock, UserMessage
from ktg_agent.core.events import EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import Message
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.server import AgentHTTPServer, Settings, create_service, create_session_service
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
                      page_context: JsonObject | None, authorization: str,
                      event_sink: EventSink) -> AgentRunResult:
        return await query_service.run_session(
            run_id=run_id, message=message, history=history,
            page_context=page_context, authorization=authorization,
            event_sink=event_sink,
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
    contextual_request = next(request for request in provider.requests
                              if request.messages[-1].content[0] == TextBlock("查库存"))
    other_request = next(request for request in provider.requests
                         if request.messages[-1].content[0] == TextBlock("other session"))
    first_block = contextual_request.messages[0].content[0]
    assert isinstance(first_block, TextBlock)
    first_prompt = first_block.text
    assert '"itemCode":"A"' in first_prompt
    assert '"filters"' in first_prompt
    assert "页面上下文仅供定位" in first_prompt
    assert '"itemCode":"A"' not in str(other_request.messages)


def test_http_restart_keeps_history_and_does_not_replay_orphaned_run(
    tmp_path: Path, monkeypatch: object,
) -> None:
    import ktg_agent.server as server_module
    from pytest import MonkeyPatch
    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.setattr(server_module, "KTGClient", lambda **kwargs: FakeClient(kwargs["authorization"]))
    path = tmp_path / "sessions.sqlite3"
    settings = Settings("key", "http://localhost", "model", "http://localhost",
                        session_db_path=str(path))

    @contextmanager
    def served(provider: FakeProvider) -> Iterator[AgentHTTPServer]:
        query_service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 128),
            ktg_client_factory=lambda token: cast(KTGClient, FakeClient(token)),
        )
        session_service = create_session_service(settings, query_service)
        server = AgentHTTPServer(("127.0.0.1", 0), query_service, session_service)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def submit_and_wait(server: AgentHTTPServer, sid: str, number: int,
                        page_context: JsonObject | None = None) -> str:
        request_body: JsonObject = {"request_id": f"r{number}", "message": f"question {number}"}
        if page_context is not None:
            request_body["page_context"] = page_context
        status, body = request(server, "POST", f"/agent-api/v1/sessions/{sid}/runs",
                               body=request_body)
        assert status == 202
        rid = cast(str, body["run_id"])
        deadline = monotonic() + 3
        while monotonic() < deadline:
            snapshot = request(server, "GET", f"/agent-api/v1/runs/{rid}")[1]
            if snapshot["status"] == "completed":
                assert snapshot["result_summary"] is not None
                return rid
            sleep(0.01)
        raise AssertionError("run did not complete")

    first_provider = FakeProvider((ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock(f"answer {number}"),)), StopReason.COMPLETED, Usage(1, 1),
    )),)) for number in (1, 2)))
    with served(first_provider) as server:
        sid = cast(str, request(server, "POST", "/agent-api/v1/sessions", body={})[1]["session_id"])
        completed_ids = [
            submit_and_wait(server, sid, 1, {"route_name": "WmStock",
                                            "filters": {"itemCode": "A"}}),
            submit_and_wait(server, sid, 2),
        ]
        assert '"itemCode":"A"' in str(first_provider.requests[0].messages[0])
        assert '"itemCode":"A"' not in str(first_provider.requests[1].messages)
        messages = request(server, "GET", f"/agent-api/v1/sessions/{sid}/messages")[1]
        assert [item["role"] for item in cast(list[dict[str, object]], messages["items"])] == [
            "user", "assistant", "user", "assistant",
        ]
        assert '"itemCode":"A"' not in str(messages)

    repo = SessionRepository(path)
    orphan, _ = repo.admit_run("id:7", sid, "interrupted", "interrupted", {})
    repo.update_run_status("id:7", orphan.id, "running")
    repo.append_event("id:7", orphan.id, 1, "tool_execution_start", {"tool_name": "write"})
    third_provider = FakeProvider((ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((TextBlock("answer 3"),)), StopReason.COMPLETED, Usage(1, 1),
    )),)),))
    with served(third_provider) as server:
        orphan_snapshot = request(server, "GET", f"/agent-api/v1/runs/{orphan.id}")[1]
        assert orphan_snapshot["status"] == "needs_reconciliation"
        assert orphan_snapshot["stop_reason"] == "SERVICE_RESTARTED"
        assert orphan_snapshot["last_seq"] == 2
        assert len(third_provider.requests) == 0
        for rid in completed_ids:
            assert request(server, "GET", f"/agent-api/v1/runs/{rid}")[1]["status"] == "completed"
        assert request(server, "GET", f"/agent-api/v1/sessions/{sid}")[1]["active_run_id"] is None
        third = submit_and_wait(server, sid, 3)
        assert third != orphan.id
        assert len(third_provider.requests) == 1
        assert [item.content[0].text for item in third_provider.requests[0].messages
                if isinstance(item.content[0], TextBlock)][-5:] == [
                    "question 1", "answer 1", "question 2", "answer 2", "question 3",
                ]
        assert request(server, "POST", f"/agent-api/v1/sessions/{sid}/runs",
                       body={"request_id": "interrupted", "message": "different"})[0] == 409
