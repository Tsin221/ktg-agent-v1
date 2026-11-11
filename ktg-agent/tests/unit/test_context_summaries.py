"""Durability and transactional guarantees for persisted summaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ktg_agent.core.messages import TextBlock, UserMessage
from ktg_agent.sessions.repository import SessionRepository, SummaryRecord


def _v4_database(path: Path, request_json: str = '{}') -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL);
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
                request_summary_json TEXT NOT NULL, status TEXT NOT NULL,
                budget_json TEXT, usage_json TEXT
            );
            PRAGMA user_version=4;
        """)
        db.execute("INSERT INTO sessions VALUES('s','alice')")
        db.execute("INSERT INTO runs(id,session_id,owner_user_id,request_summary_json,status) "
                   "VALUES('r','s','alice',?,'completed')", (request_json,))


def test_v4_migration_backfills_kind_and_keeps_summary_table_empty(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    _v4_database(path, '{"kind":"compaction"}')
    SessionRepository(path)
    SessionRepository(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT kind,context_json FROM runs").fetchone() == ("compaction", None)
        assert db.execute("SELECT count(*) FROM context_summaries").fetchone()[0] == 0
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"one_active_summary_per_session", "one_summary_per_call"} <= names


def test_failed_v4_migration_rolls_back_every_schema_change(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite3"
    _v4_database(path, "not json")
    with pytest.raises(sqlite3.OperationalError):
        SessionRepository(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        assert "kind" not in columns and "context_json" not in columns
        assert db.execute("SELECT name FROM sqlite_master WHERE name='context_summaries'").fetchone() is None


def test_commit_is_idempotent_owner_scoped_and_survives_pruning(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("alice")
    source = repo.create_run("alice", session.id, "source", "one", {})
    repo.settle_run("alice", source.id, "completed", "completed", {},
                    (UserMessage((TextBlock("first"),)), UserMessage((TextBlock("second"),))))
    records = repo.completed_message_records("alice", session.id)
    first = repo.create_run("alice", session.id, "compact-1", "two", {}, kind="compaction")
    def commit(owner: str, run_id: str, call_id: str, *, count: int = 1,
               tokens: int = 100) -> SummaryRecord:
        return repo.commit_summary(
            owner, session.id, run_id=run_id, covers_start_message_id=records[0].id,
            covers_end_message_id=records[count - 1].id, source_message_count=count,
            source_token_estimate=tokens, summary_text="short", summary_token_estimate=2,
            model="fake", call_id=call_id, usage=None)

    saved = commit("alice", first.id, "call-1")
    assert commit("alice", first.id, "call-1") == saved
    assert repo.active_summary("bob", session.id) is None
    assert repo.list_summaries("bob", session.id) == ()
    with pytest.raises(KeyError):
        commit("bob", first.id, "call-1")
    repo.settle_run("alice", first.id, "completed", "completed", {}, ())
    second = repo.create_run("alice", session.id, "compact-2", "three", {}, kind="compaction")
    with pytest.raises(ValueError, match="SUMMARY_NOT_SMALLER"):
        commit("alice", second.id, "call-2", tokens=1)
    assert repo.active_summary("alice", session.id) == saved
    replacement = commit("alice", second.id, "call-2", count=2)
    chain = repo.list_summaries("alice", session.id)
    assert len(chain) == 2
    assert chain[0].status == "superseded" and chain[0].superseded_by == replacement.summary_id
    assert chain[1].status == "active"
    stats = repo.session_stats("alice", session.id)
    assert stats["summary_counts"] == {"active": 1, "superseded": 1}
    assert stats["compaction_count"] == 2
    active = stats["active_summary"]
    assert isinstance(active, dict)
    assert active["covers_end_message_id"] == replacement.covers_end_message_id
    assert active["summary_token_estimate"] == replacement.summary_token_estimate
    repo.prune_events()
    reopened = SessionRepository(path)
    assert reopened.recover_interrupted_runs() == 1
    assert reopened.get_run("alice", second.id).status == "cancelled"  # type: ignore[union-attr]
    assert reopened.active_summary("alice", session.id) == replacement
    assert len(reopened.completed_message_records("alice", session.id)) == 2
