"""Owner-scoped event replay, retention and public envelope."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

from ktg_agent.core.messages import JsonObject, TextBlock, UserMessage
from ktg_agent.ktg import KTGClient
from ktg_agent.sessions.repository import EventHistoryExpiredError, SessionRepository
from ktg_agent.sessions.service import SessionService


class FakeClient:
    def __init__(self, authorization: str) -> None:
        self.authorization = authorization

    async def get_current_user(self) -> dict[str, object]:
        return {"userId": 7 if self.authorization == "alice" else 8}


def _service(repo: SessionRepository) -> SessionService:
    return SessionService(repo, lambda token: cast(KTGClient, FakeClient(token)))


def test_event_pages_are_owned_ordered_and_include_a_public_envelope(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    for seq in range(1, 5):
        repo.append_event("id:7", run.id, seq, "message_update", {"text": f"part {seq}"})
    service = _service(repo)

    first = asyncio.run(service.events("alice", run.id, after_seq=0, limit=2))
    assert [item["seq"] for item in cast(list[JsonObject], first["items"])] == [1, 2]
    assert first["next_cursor"] == 2
    second = asyncio.run(service.events("alice", run.id, after_seq=2, limit=2))
    assert [item["seq"] for item in cast(list[JsonObject], second["items"])] == [3, 4]
    assert second["next_cursor"] is None
    assert cast(list[JsonObject], first["items"])[0] == {
        "schema_version": 1, "session_id": session.id, "run_id": run.id,
        "seq": 1, "type": "message_update",
        "timestamp": repo.list_events("id:7", run.id, after=0).items[0].created_at,
        "payload": {"text": "part 1"},
    }
    assert cast(str, cast(list[JsonObject], first["items"])[0]["timestamp"]).endswith("+00:00")
    with pytest.raises(KeyError):
        asyncio.run(service.events("bob", run.id))
    with pytest.raises(KeyError):
        asyncio.run(service.events("alice", "missing"))
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError):
            repo.list_events("id:7", run.id, after=invalid)  # type: ignore[arg-type]


def test_concurrent_event_writes_cannot_skip_or_duplicate_sequence(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    repo.append_event("id:7", run.id, 2, "agent_start", {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(repo.append_event, "id:7", run.id, 1, "agent_start", {})
                   for _ in range(2)]
        outcomes = [future.exception() for future in futures]
    assert all(outcome is None for outcome in outcomes)
    repo.append_event("id:7", run.id, 2, "agent_end", {})
    assert [event.seq for event in repo.list_events("id:7", run.id).items] == [1, 2, 3, 4]
    repo.settle_run("id:7", run.id, "completed", "completed", {}, ())
    with pytest.raises(KeyError):
        repo.append_event("id:7", run.id, 4, "message_update", {})
    assert repo.list_events("id:7", run.id).items[-1].type == "run_settled"


def test_expiry_only_after_cleanup_preserves_snapshot_and_cursor_boundary(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    repo.append_event("id:7", run.id, 1, "agent_start", {})
    repo.settle_run("id:7", run.id, "completed", "completed", {"answer": "done"},
                    (UserMessage((TextBlock("hello"),)),))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="microseconds")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE runs SET ended_at=? WHERE id=?", (old, run.id))
    assert [event.seq for event in repo.list_events("id:7", run.id, after=0).items] == [1, 2]
    assert repo.prune_events() == 2
    assert repo.prune_events() == 0
    with pytest.raises(EventHistoryExpiredError):
        repo.list_events("id:7", run.id, after=0)
    with pytest.raises(EventHistoryExpiredError):
        repo.list_events("id:7", run.id, after=1)
    assert repo.list_events("id:7", run.id, after=2).items == ()
    assert repo.last_event_seq("id:7", run.id) == 2
    assert repo.get_run("id:7", run.id).result_summary == {"answer": "done"}  # type: ignore[union-attr]
    assert len(repo.list_messages("id:7", session.id).items) == 1


def test_legacy_payload_is_filtered_and_new_unsafe_events_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    unsafe_payloads: list[JsonObject] = [
        {"headers": {"Authorization": "Bearer abc"}},
        {"message": "Traceback (most recent call last):\nFile \"x.py\", line 1"},
    ]
    for payload in unsafe_payloads:
        with pytest.raises(ValueError, match="sensitive"):
            repo.append_event("id:7", run.id, 1, "tool_execution_end", payload)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (run.id, 1, "tool_execution_end",
                   '{"headers":{"Authorization":"Bearer abc"},"details":{"ok":true,"stack_trace":"private"},"message":"Bearer abc"}',
                   datetime.now(timezone.utc).isoformat()))
    page = asyncio.run(_service(repo).events("alice", run.id))
    public_payload = cast(JsonObject, cast(list[JsonObject], page["items"])[0]["payload"])
    assert public_payload == {"details": {"ok": True}, "message": "[redacted]"}
    assert "Bearer abc" not in str(page)


def test_failed_cleanup_rolls_back_replay_boundary(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("id:7")
    run = repo.create_run("id:7", session.id, "r1", "h1", {})
    repo.settle_run("id:7", run.id, "completed", "completed", {}, ())
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="microseconds")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE runs SET ended_at=? WHERE id=?", (old, run.id))
        db.execute("CREATE TRIGGER reject_event_cleanup BEFORE DELETE ON events "
                   "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.prune_events()
    assert [item.seq for item in repo.list_events("id:7", run.id, after=0).items] == [1]


def test_v1_database_migrates_replay_boundary(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    SessionRepository(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE model_requests")
        db.execute("DROP TABLE control_commands")
        db.execute("DROP TABLE context_summaries")
        db.execute("ALTER TABLE runs DROP COLUMN budget_json")
        db.execute("ALTER TABLE runs DROP COLUMN usage_json")
        db.execute("ALTER TABLE runs DROP COLUMN kind")
        db.execute("ALTER TABLE runs DROP COLUMN context_json")
        db.execute("ALTER TABLE runs DROP COLUMN replay_floor_seq")
        db.execute("PRAGMA user_version=1")
    SessionRepository(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert any(row[1] == "replay_floor_seq" for row in db.execute("PRAGMA table_info(runs)"))
        assert db.execute("SELECT name FROM sqlite_master WHERE name='control_commands'").fetchone() is not None
