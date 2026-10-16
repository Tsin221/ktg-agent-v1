from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ktg_agent.core.messages import AssistantMessage, TextBlock, ToolResultMessage, UserMessage
from ktg_agent.sessions import SessionRepository


def test_repository_reopens_with_ordered_history(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    with SessionRepository(path) as repo:
        session = repo.create_session("alice", title="库存")
        run = repo.create_run("alice", session.id, "request-1", "digest-1", {"message": "查库存"})
        first = repo.append_message("alice", run.id, UserMessage((TextBlock("查库存"),)))
        second = repo.append_message("alice", run.id, AssistantMessage((TextBlock("还有 12 件"),)))
        repo.append_event("alice", run.id, 1, "agent_start", {})
        repo.append_event("alice", run.id, 2, "business_result", {"quantity": 12})
        repo.update_run_status("alice", run.id, "completed", stop_reason="completed")

    with SessionRepository(path) as repo:
        reopened = repo.get_session("alice", session.id)
        assert reopened is not None and reopened.id == session.id
        assert reopened.updated_at >= session.updated_at
        saved_run = repo.get_run("alice", run.id)
        assert saved_run is not None and saved_run.status == "completed"
        page = repo.list_messages("alice", session.id, limit=1)
        assert [item.id for item in page.items] == [first.id]
        assert [item.id for item in repo.list_messages("alice", session.id, after=page.next_cursor).items] == [second.id]
        assert [item.seq for item in repo.list_events("alice", run.id).items] == [1, 2]
        assert repo.list_events("alice", run.id, after=1).items[0].payload == {"quantity": 12}


def test_constraints_are_durable_and_failed_writes_roll_back(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    with SessionRepository(path) as repo:
        session = repo.create_session("alice")
        run = repo.create_run("alice", session.id, "same", "digest", {"message": "hi"})
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_run("alice", session.id, "other", "digest", {"message": "other"})
        repo.update_run_status("alice", run.id, "completed", stop_reason="completed")
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_run("alice", session.id, "same", "digest", {"message": "hi"})
        next_run = repo.create_run("alice", session.id, "other", "digest", {"message": "other"})
        repo.append_event("alice", next_run.id, 1, "agent_start", {})
        with pytest.raises(sqlite3.IntegrityError):
            repo.append_event("alice", next_run.id, 1, "agent_end", {})
        assert [item.id for item in repo.list_runs("alice", session.id).items] == [run.id, next_run.id]
        assert [item.type for item in repo.list_events("alice", next_run.id).items] == ["agent_start"]
        with pytest.raises(sqlite3.IntegrityError):
            repo.update_run_status("alice", run.id, "running")
        saved = repo.get_run("alice", run.id)
        assert saved is not None and saved.status == "completed"


def test_session_and_run_pages_use_stable_cursors(tmp_path: Path) -> None:
    with SessionRepository(tmp_path / "sessions.sqlite3") as repo:
        sessions = [repo.create_session("alice") for _ in range(3)]
        repo.create_session("bob")
        first_page = repo.list_sessions("alice", limit=2)
        assert [item.id for item in first_page.items] == [sessions[2].id, sessions[1].id]
        assert first_page.next_cursor is not None
        assert [item.id for item in repo.list_sessions("alice", after=first_page.next_cursor).items] == [sessions[0].id]
        first_run = repo.create_run("alice", sessions[0].id, "r1", "h1", {})
        repo.update_run_status("alice", first_run.id, "completed")
        second_run = repo.create_run("alice", sessions[0].id, "r2", "h2", {})
        page = repo.list_runs("alice", sessions[0].id, limit=1)
        assert [item.id for item in page.items] == [first_run.id]
        assert [item.id for item in repo.list_runs("alice", sessions[0].id, after=page.next_cursor).items] == [second_run.id]
        assert repo.list_runs("bob", sessions[0].id).items == ()


def test_owner_scoping_and_secret_rejection(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    with SessionRepository(path) as repo:
        session = repo.create_session("alice")
        run = repo.create_run("alice", session.id, "request-1", "digest", {"message": "hello"})
        assert repo.get_session("bob", session.id) is None
        assert repo.get_run("bob", run.id) is None
        assert repo.list_messages("bob", session.id).items == ()
        with pytest.raises(ValueError, match="sensitive"):
            repo.append_message("alice", run.id, UserMessage((TextBlock("Bearer abc123"),)))
        with pytest.raises(ValueError, match="sensitive"):
            repo.append_event("alice", run.id, 1, "business_result", {"api_key": "private"})
        with pytest.raises(ValueError, match="sensitive"):
            repo.append_message("alice", run.id, ToolResultMessage("call", (TextBlock("ok"),), details={"Authorization": "Bearer secret"}))
        with pytest.raises(ValueError, match="sensitive"):
            repo.create_run("alice", session.id, "request-2", "digest", {"headers": {"x-api-key": "private"}})
        with pytest.raises(ValueError, match="sensitive"):
            repo.create_session("Bearer unsafe")
        with pytest.raises(ValueError, match="sensitive"):
            repo.append_message("alice", run.id, UserMessage((TextBlock("api key: private"),)))
    assert b"Bearer abc123" not in path.read_bytes()
    assert b"private" not in path.read_bytes()


def test_migration_rejects_unrelated_database_and_future_version(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE inventory_previews (id TEXT)")
    with pytest.raises(RuntimeError, match="unrelated"):
        SessionRepository(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone() is None

    future = tmp_path / "future.sqlite3"
    with sqlite3.connect(future) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        SessionRepository(future)


def test_multi_statement_write_rolls_back_on_later_failure(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    with SessionRepository(path) as repo:
        session = repo.create_session("alice")
        run = repo.create_run("alice", session.id, "r1", "h1", {})
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TRIGGER fail_session_touch BEFORE UPDATE ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'touch failed'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="touch failed"):
            repo.append_message("alice", run.id, UserMessage((TextBlock("hello"),)))
        assert repo.list_messages("alice", session.id).items == ()
