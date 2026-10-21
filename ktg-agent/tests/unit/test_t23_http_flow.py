"""A model HTTP stream must reach durable run events before its tail arrives."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

from ktg_agent.core.events import EventSink
from ktg_agent.core.loop import AgentRunResult, RunLimits
from ktg_agent.core.messages import JsonObject, Message
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import ModelConfig
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider
from ktg_agent.server import AgentHTTPServer
from ktg_agent.service import QueryService
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.coordinator import RegisterAgent, RunCoordinator
from ktg_agent.sessions.service import SessionService
from ktg_agent.core.agent import Agent


def _frame(value: object) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


def _read_event(response: HTTPResponse) -> JsonObject:
    data = bytearray()
    while not data.endswith(b"\n\n"):
        part = response.read(1)
        if not part:
            raise AssertionError("SSE closed before run settled")
        data.extend(part)
    return cast(JsonObject, json.loads("\n".join(
        line.removeprefix("data: ") for line in data.decode().splitlines()
        if line.startswith("data: ")
    )))


def _request(server: AgentHTTPServer, method: str, path: str,
             body: JsonObject | None = None) -> tuple[int, JsonObject]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    payload = None if body is None else json.dumps(body).encode()
    connection.request(method, path, body=payload, headers={
        "Authorization": "Bearer alice", "Content-Type": "application/json",
    })
    response = connection.getresponse()
    result = cast(JsonObject, json.loads(response.read()))
    status = response.status
    connection.close()
    return status, result


class _Client:
    async def get_current_user(self) -> JsonObject:
        return {"userId": 7}


@contextmanager
def _served(repo: SessionRepository, model_port: int) -> Iterator[AgentHTTPServer]:
    service = QueryService(
        provider=OpenAICompatibleProvider(
            api_key="local-test", base_url=f"http://127.0.0.1:{model_port}/v1",
        ),
        model_config=ModelConfig("controlled-model", 32),
        ktg_client_factory=lambda _: cast(KTGClient, _Client()),
        limits=RunLimits(max_total_tokens=20),
    )

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      event_sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_session(
            run_id=run_id, message=message, history=history,
            page_context=page_context, authorization=authorization,
            event_sink=event_sink, on_agent_ready=register_agent,
        )

    sessions = SessionService(
        repo, lambda _: cast(KTGClient, _Client()),
        RunCoordinator(repo, execute, service.budget_snapshot),
    )
    server = AgentHTTPServer(("127.0.0.1", 0), service, sessions)
    server.sse_poll_seconds = 0.02
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


def test_real_http_model_delta_precedes_tail_and_reconnect_does_not_rebill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    release_tail = Event()
    first_sent = Event()
    model_calls: list[JsonObject] = []

    class ModelHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            assert self.path == "/v1/chat/completions"
            length = int(self.headers["Content-Length"])
            model_calls.append(cast(JsonObject, json.loads(self.rfile.read(length))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send(data: bytes) -> None:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            send(_frame({"choices": [{"index": 0, "delta": {"content": "第一段"},
                                      "finish_reason": None}], "usage": None}))
            first_sent.set()
            if not release_tail.wait(10):
                return
            send(_frame({"choices": [{"index": 0, "delta": {"content": "第二段"},
                                      "finish_reason": None}], "usage": None}))
            send(_frame({"choices": [{"index": 0, "delta": {},
                                      "finish_reason": "stop"}], "usage": None}))
            send(_frame({"choices": [], "usage": {
                "prompt_tokens": 7, "completion_tokens": 3,
            }}))
            send(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *_args: object) -> None:
            pass

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model.daemon_threads = True
    model_thread = Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    try:
        with _served(repo, model.server_port) as server:
            sid = cast(str, _request(server, "POST", "/agent-api/v1/sessions", {})[1]["session_id"])
            status, admitted = _request(server, "POST", f"/agent-api/v1/sessions/{sid}/runs",
                                        {"request_id": "once", "message": "测试增量"})
            assert status == 202
            rid = cast(str, admitted["run_id"])
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            connection.request("GET", f"/agent-api/v1/runs/{rid}/events",
                               headers={"Authorization": "Bearer alice"})
            stream = connection.getresponse()
            assert stream.status == 200 and first_sent.wait(10)
            while True:
                first = _read_event(stream)
                if first["type"] == "message_update":
                    break
            assert cast(JsonObject, first["payload"])["text"] == "第一段"
            assert not release_tail.is_set()
            _, running = _request(server, "GET", f"/agent-api/v1/runs/{rid}")
            assert running["status"] == "running"
            assert cast(JsonObject, running["usage"])["known_total_tokens"] == 0
            connection.close()

            release_tail.set()
            reconnect = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            reconnect.request("GET", f"/agent-api/v1/runs/{rid}/events?after_seq={first['seq']}",
                              headers={"Authorization": "Bearer alice"})
            replay = reconnect.getresponse()
            while True:
                item = _read_event(replay)
                if item["type"] == "run_settled":
                    break
            reconnect.close()
            assert cast(JsonObject, item["payload"])["status"] == "completed"
            _, settled = _request(server, "GET", f"/agent-api/v1/runs/{rid}")
            assert cast(JsonObject, settled["usage"])["known_total_tokens"] == 10
            assert settled["usage"] == cast(JsonObject, item["payload"])["usage"]
            assert len(cast(list[object], settled["model_requests"])) == 1
            assert len(model_calls) == 1
        reopened = SessionRepository(tmp_path / "sessions.sqlite3")
        saved = reopened.get_run("id:7", rid)
        assert saved is not None and saved.usage is not None
        assert saved.usage["known_total_tokens"] == 10
    finally:
        release_tail.set()
        model.shutdown()
        model_thread.join(timeout=10)
        model.server_close()
