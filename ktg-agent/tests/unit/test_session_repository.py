from __future__ import annotations

import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from ktg_agent.core.messages import AssistantMessage, JsonObject, TextBlock, ToolResultMessage, UserMessage
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.repository import ControlConflictError, ControlRecord, ControlStateError


def test_session_stats_only_counts_reported_usage_and_checks_owner(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "stats.sqlite3")
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "one", "one", {})
    repo.append_event("alice", run.id, 1, "model_request_started",
                      {"call_id": "known", "turn": 1, "model": "fake"})
    repo.append_event("alice", run.id, 2, "model_usage_reported",
                      {"call_id": "known", "turn": 1, "input_tokens": 5,
                       "output_tokens": 2, "cached_input_tokens": 1})
    repo.append_event("alice", run.id, 3, "model_request_started",
                      {"call_id": "unknown", "turn": 2, "model": "fake"})
    repo.settle_run("alice", run.id, "completed", "completed", {},
                    (UserMessage((TextBlock("hello"),)),))
    stats = repo.session_stats("alice", session.id)
    usage = stats["known_usage"]
    assert isinstance(usage, dict)
    assert usage["known_total_tokens"] == 7
    assert usage["unknown_request_count"] == 1
    assert usage["complete"] is False
    assert stats["message_count"] == 1
    assert stats["summary_counts"] == {}
    with pytest.raises(KeyError):
        repo.session_stats("bob", session.id)
    with pytest.raises(KeyError):
        repo.session_stats("alice", "missing")


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
        repo.append_event("alice", next_run.id, 1, "agent_end", {})
        assert [item.id for item in repo.list_runs("alice", session.id).items] == [run.id, next_run.id]
        assert [item.type for item in repo.list_events("alice", next_run.id).items] == ["agent_start", "agent_end"]
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


def test_restart_settles_only_orphaned_runs_once(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    runs = []
    for status in ("queued", "running", "cancelling"):
        session = repo.create_session("alice")
        run = repo.create_run("alice", session.id, status, status, {})
        if status != "queued":
            repo.update_run_status("alice", run.id, status)
            repo.append_event("alice", run.id, 1, "tool_execution_start", {"tool_name": "write"})
        runs.append(run)
    completed_session = repo.create_session("alice")
    completed = repo.create_run("alice", completed_session.id, "done", "done", {})
    repo.settle_run("alice", completed.id, "completed", "completed", {"answer": "saved"},
                    (UserMessage((TextBlock("hello"),)),
                     AssistantMessage((TextBlock("saved"),))))

    reopened = SessionRepository(path)
    assert reopened.recover_interrupted_runs() == 3
    assert reopened.recover_interrupted_runs() == 0
    for run, status in zip(runs, ("cancelled", "needs_reconciliation", "needs_reconciliation")):
        saved = reopened.get_run("alice", run.id)
        assert saved is not None and saved.status == status
        assert saved.stop_reason == "SERVICE_RESTARTED"
        assert saved.result_summary is not None
        assert saved.result_summary["error"] == "SERVICE_RESTARTED"
        events = reopened.list_events("alice", run.id).items
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert events[-1].type == "run_settled"
        assert events[-1].payload["status"] == status
        assert reopened.get_active_run("alice", run.session_id) is None
    assert reopened.get_run("alice", completed.id).status == "completed"  # type: ignore[union-attr]
    assert len(reopened.list_messages("alice", completed_session.id).items) == 2


def test_control_admission_is_idempotent_and_events_share_durable_sequence(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "run", "hash", {})
    repo.update_run_status("alice", run.id, "running")
    repo.append_event("alice", run.id, 1, "agent_start", {})
    request: JsonObject = {"mode": "follow_up", "message": "second"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.admit_control(
            "alice", run.id, "input-1", "follow_up", request, accepting=True), range(2)))
    assert [created for _, created in results].count(True) == 1
    assert results[0][0] == results[1][0]
    with pytest.raises(ControlConflictError):
        repo.admit_control("alice", run.id, "input-1", "steer", request, accepting=True)
    with pytest.raises(ControlConflictError):
        repo.admit_control("alice", run.id, "input-1", "follow_up",
                           {"mode": "follow_up", "message": "changed"}, accepting=True)
    repo.append_event("alice", run.id, 1, "turn_end", {})
    repo.mark_input_consumed("alice", run.id, "input-1")
    repo.settle_run("alice", run.id, "completed", "completed", {}, ())
    repeated, created = repo.admit_control("alice", run.id, "input-1", "follow_up", request,
                                           accepting=False)
    assert not created and repeated.status == "consumed"
    assert [item.seq for item in repo.list_events("alice", run.id).items] == [1, 2, 3, 4, 5]
    assert [item.type for item in repo.list_events("alice", run.id).items] == [
        "agent_start", "input_queued", "turn_end", "input_consumed", "run_settled"]
    assert repo.list_inputs("bob", run.id).items == ()
    with pytest.raises(KeyError):
        repo.admit_control("bob", run.id, "foreign", "steer",
                           {"mode": "steer", "message": "foreign"}, accepting=True)


def test_control_and_core_events_commit_concurrently_before_settlement(tmp_path: Path) -> None:
    from threading import Barrier

    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "run", "hash", {})
    repo.update_run_status("alice", run.id, "running")
    gate = Barrier(3)

    def core_event() -> None:
        gate.wait(timeout=5)
        repo.append_event("alice", run.id, 1, "turn_start", {})

    def control_event() -> tuple[ControlRecord, bool]:
        gate.wait(timeout=5)
        return repo.admit_control("alice", run.id, "input-1", "follow_up",
                                  {"mode": "follow_up", "message": "next"}, accepting=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        core = pool.submit(core_event)
        control = pool.submit(control_event)
        gate.wait(timeout=5)
        core.result(5)
        receipt, created = control.result(5)
    assert created and receipt.status == "queued"
    repo.settle_run("alice", run.id, "completed", "completed", {}, ())
    events = repo.list_events("alice", run.id).items
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert set(event.type for event in events[:2]) == {"turn_start", "input_queued"}
    assert [event.type for event in events[-2:]] == ["input_not_consumed", "run_settled"]


def test_pending_inputs_are_closed_before_settled_and_recovery_never_replays(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "run", "hash", {})
    with pytest.raises(ControlStateError) as exc:
        repo.admit_control("alice", run.id, "early", "steer",
                           {"mode": "steer", "message": "early"}, accepting=False)
    assert exc.value.code == "RUN_NOT_RUNNING"
    repo.update_run_status("alice", run.id, "running")
    first, _ = repo.admit_control("alice", run.id, "one", "steer",
                                  {"mode": "steer", "message": "one"}, accepting=True)
    second, _ = repo.admit_control("alice", run.id, "two", "follow_up",
                                   {"mode": "follow_up", "message": "two"}, accepting=True)
    page = repo.list_inputs("alice", run.id, limit=1)
    assert [item.request_id for item in page.items] == ["one"]
    assert [item.request_id for item in repo.list_inputs("alice", run.id,
                                                          after=page.next_cursor).items] == ["two"]
    assert first.position < second.position
    repo.mark_input_consumed("alice", run.id, "one")
    reopened = SessionRepository(path)
    assert reopened.recover_interrupted_runs() == 1
    assert reopened.recover_interrupted_runs() == 0
    assert [(item.status, item.reason) for item in reopened.list_inputs("alice", run.id).items] == [
        ("consumed", None), ("not_consumed", "process_interrupted")]
    assert [item.type for item in reopened.list_events("alice", run.id).items][-2:] == [
        "input_not_consumed", "run_settled"]
    repeated, created = reopened.admit_control("alice", run.id, "one", "steer",
                                               {"mode": "steer", "message": "one"}, accepting=False)
    assert not created and repeated.status == "consumed"


def test_v2_database_adds_control_table_without_changing_existing_run(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "run", "hash", {})
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE task_versions")
        db.execute("DROP TABLE tasks")
        db.execute("DROP TABLE model_requests")
        db.execute("DROP TABLE control_commands")
        db.execute("DROP TABLE context_summaries")
        db.execute("ALTER TABLE runs DROP COLUMN budget_json")
        db.execute("ALTER TABLE runs DROP COLUMN usage_json")
        db.execute("ALTER TABLE runs DROP COLUMN kind")
        db.execute("ALTER TABLE runs DROP COLUMN context_json")
        db.execute("PRAGMA user_version=2")
    migrated = SessionRepository(path)
    restored = migrated.get_run("alice", run.id)
    assert restored is not None and restored.id == run.id
    assert restored.usage == {"source": "legacy_unknown", "complete": False}
    assert restored.budget is None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT name FROM sqlite_master WHERE name='control_commands'").fetchone() is not None


def test_usage_survives_restart_recovery_and_event_pruning(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "r1", "hash", {})
    repo.set_run_budget("alice", run.id, {"max_total_tokens": 20})
    repo.update_run_status("alice", run.id, "running")
    repo.append_event("alice", run.id, 1, "model_request_started",
                      {"call_id": "call-1", "turn": 1, "model": "fake"})
    repo.append_event("alice", run.id, 2, "model_usage_reported",
                      {"call_id": "call-1", "turn": 1, "input_tokens": 7,
                       "output_tokens": 3, "cached_input_tokens": 2})
    repo.append_event("alice", run.id, 3, "model_request_started",
                      {"call_id": "call-2", "turn": 2, "model": "fake"})
    reopened = SessionRepository(path)
    assert reopened.get_run("bob", run.id) is None
    assert reopened.recover_interrupted_runs() == 1
    saved = reopened.get_run("alice", run.id)
    assert saved is not None and saved.status == "needs_reconciliation"
    assert saved.usage == {"known_input_tokens": 7, "known_output_tokens": 3,
                           "known_cached_input_tokens": 2, "known_total_tokens": 10,
                           "unknown_request_count": 1, "complete": False}
    assert [item["usage_source"] for item in saved.model_requests] == [
        "provider_reported", "unknown"]
    assert reopened.list_events("alice", run.id).items[-1].payload["usage"] == saved.usage
    from datetime import datetime, timedelta, timezone
    reopened.prune_events(now=datetime.now(timezone.utc) + timedelta(days=31))
    assert SessionRepository(path).get_run("alice", run.id).usage == saved.usage  # type: ignore[union-attr]


def test_usage_event_and_snapshot_roll_back_together(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "r1", "hash", {})
    repo.update_run_status("alice", run.id, "running")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_usage BEFORE INSERT ON model_requests "
                   "BEGIN SELECT RAISE(ABORT, 'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.append_event("alice", run.id, 1, "model_request_started",
                          {"call_id": "call-1", "turn": 1, "model": "fake"})
    assert repo.list_events("alice", run.id).items == ()
    assert repo.get_run("alice", run.id).usage == {  # type: ignore[union-attr]
        "known_input_tokens": 0, "known_output_tokens": 0,
        "known_cached_input_tokens": 0, "known_total_tokens": 0,
        "unknown_request_count": 0, "complete": True}


@pytest.mark.parametrize("status,stop_reason,expected_reason", [
    ("cancelled", "cancelled", "cancelled"),
    ("failed", "limit", "limit"),
    ("failed", "error", "run_failed"),
])
def test_settlement_closes_pending_inputs(tmp_path: Path, status: str,
                                          stop_reason: str, expected_reason: str) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("alice")
    run = repo.create_run("alice", session.id, "run", "hash", {})
    repo.update_run_status("alice", run.id, "running")
    repo.admit_control("alice", run.id, "input", "follow_up",
                       {"mode": "follow_up", "message": "later"}, accepting=True)
    repo.settle_run("alice", run.id, status, stop_reason, {}, ())
    item = repo.list_inputs("alice", run.id).items[0]
    assert (item.status, item.reason) == ("not_consumed", expected_reason)
    assert [event.type for event in repo.list_events("alice", run.id).items][-2:] == [
        "input_not_consumed", "run_settled"]
