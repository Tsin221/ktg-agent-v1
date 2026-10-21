"""Session statistics use persisted SQLite records and owner-scoped HTTP reads."""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import Iterator, cast

from ktg_agent.context.window import ContextPolicy
from ktg_agent.core.messages import TextBlock, UserMessage
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.server import AgentHTTPServer, Settings, create_service
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.sessions.service import SessionService


class FakeClient:
    def __init__(self, token: str) -> None:
        self.token = token

    async def get_current_user(self) -> dict[str, object]:
        if self.token not in {"Bearer alice", "Bearer bob"}:
            raise KTGError("AUTH_REQUIRED", "missing", status=401)
        return {"userId": 7 if self.token == "Bearer alice" else 8}


@contextmanager
def served(repo: SessionRepository, *, configured: bool) -> Iterator[AgentHTTPServer]:
    settings = Settings("key", "http://localhost", "model", "http://localhost")
    policy = ContextPolicy(8192, 1024) if configured else None
    service = SessionService(
        repo, lambda token: cast(KTGClient, FakeClient(token)),
        context_policy=policy, system_prompt_builder=lambda _: "system",
    )
    server = AgentHTTPServer(("127.0.0.1", 0), create_service(settings), service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def get(server: AgentHTTPServer, path: str, token: str = "Bearer alice") -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", server.server_port)
    connection.request("GET", path, headers={"Authorization": token})
    response = connection.getresponse()
    body = cast(dict[str, object], json.loads(response.read()))
    status = response.status
    connection.close()
    return status, body


def test_stats_http_counts_usage_pressure_and_access(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "one", "h1", {})
    repo.append_event("id:7", run.id, 1, "model_request_started",
                      {"call_id": "c1", "turn": 1, "model": "fake"})
    repo.append_event("id:7", run.id, 2, "model_usage_reported",
                      {"call_id": "c1", "turn": 1, "input_tokens": 10,
                       "output_tokens": 2, "cached_input_tokens": 3})
    repo.settle_run("id:7", run.id, "completed", "completed", {},
                    (UserMessage((TextBlock("hello"),)),))
    compact = repo.create_run("id:7", session.id, "two", "h2", {}, kind="compaction")
    repo.settle_run("id:7", compact.id, "failed", "failed", {}, ())
    with served(repo, configured=True) as server:
        path = f"/agent-api/v1/sessions/{session.id}/stats"
        status, body = get(server, path)
        assert status == 200
        assert body["message_count"] == 1
        assert body["messages_by_role"] == {"user": 1}
        assert body["run_count"] == 2
        assert body["runs_by_status"] == {"completed": 1, "failed": 1}
        assert body["runs_by_kind"] == {"user": 1, "compaction": 1}
        assert body["compaction_count"] == 1
        usage = cast(dict[str, object], body["known_usage"])
        assert usage["known_total_tokens"] == 12
        assert usage["known_cached_input_tokens"] == 3
        assert cast(dict[str, dict[str, object]], body["known_usage_by_kind"])["compaction"]["known_total_tokens"] == 0
        context = cast(dict[str, object], body["context"])
        assert context["window_tokens"] == 8192
        assert context["threshold_tokens"] == 6553
        assert context["estimate_kind"] == "estimated"
        assert cast(int, context["estimated_tokens"]) > 0
        assert context["retained_message_count"] == 1
        assert get(server, path, "Bearer bob") == (404, {"error": {"code": "SESSION_NOT_FOUND", "message": "会话不存在。"}})
        assert get(server, path, "")[0] == 401
        assert get(server, path + "?limit=1")[0] == 400
        assert get(server, "/agent-api/v1/sessions/missing/stats")[0] == 404


def test_stats_spill_count_ignores_compaction_run_context(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    user = repo.create_run("id:7", session.id, "one", "h1", {})
    repo.settle_run("id:7", user.id, "completed", "completed", {}, (),
                    context={"estimated_tokens": 100, "spill_count": 3, "trimmed": False})
    compact = repo.create_run("id:7", session.id, "two", "h2", {}, kind="compaction")
    repo.settle_run("id:7", compact.id, "completed", "completed", {}, (),
                    context={"state": "summary_ready", "spill_count": 0})
    with served(repo, configured=True) as server:
        status, body = get(server, f"/agent-api/v1/sessions/{session.id}/stats")
    assert status == 200
    context = cast(dict[str, object], body["context"])
    # The later compaction run carries no spill information and must not mask
    # the last user-run assembly.
    assert context["spill_count"] == 3


def test_stats_without_window_has_null_estimates(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    with served(repo, configured=False) as server:
        status, body = get(server, f"/agent-api/v1/sessions/{session.id}/stats")
    assert status == 200
    context = cast(dict[str, object], body["context"])
    assert context["configured"] is False
    assert context["window_tokens"] is None
    assert context["threshold_tokens"] is None
    assert context["estimated_tokens"] is None
    assert context["estimate_kind"] is None
