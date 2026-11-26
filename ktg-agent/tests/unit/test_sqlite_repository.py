from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from ktg_agent.sessions.conversation_store import SQLiteConversationStore
from ktg_agent.sessions.repository import RunBusyError, RunConflictError, SessionRepository


def _repo(tmp_path: Path) -> SessionRepository:
    repo = SessionRepository(tmp_path / 'unified.sqlite3')
    asyncio.run(repo.initialize_framework_stores())
    return repo


def test_empty_same_database_and_incompatible_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with repo.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'app_sessions', 'app_runs', 'app_control_commands', 'app_conversation_entries', 'runs', 'events',
                'snapshots', 'snapshot_idempotency_keys', 'tool_effects', 'snapshot_messages', 'snapshot_message_refs',
                'agent_native_plan_items'} <= tables
        assert not tables & {'sessions', 'messages', 'model_requests', 'context_summaries', 'tasks', 'task_versions'}
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    sess = repo.create_session('owner')
    reopened = SessionRepository(repo.path)
    asyncio.run(reopened.initialize_framework_stores())
    assert reopened.get_session('owner', sess.id) == sess

    old = tmp_path / 'old.sqlite3'
    with sqlite3.connect(old) as db:
        db.execute('CREATE TABLE runs(id TEXT)')
        db.execute('PRAGMA user_version=6')
    before = old.read_bytes()
    with pytest.raises(RuntimeError, match='incompatible'):
        SessionRepository(old)
    assert old.read_bytes() == before


def test_run_atomic_admission_and_continuation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sid = repo.create_session('owner').id

    def same_request() -> tuple[str, bool]:
        run, created = repo.admit_run('owner', sid, 'request', 'hash', {'message': 'x'})
        return run.id, created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: same_request(), range(8)))
    assert len({run_id for run_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    rid = results[0][0]
    with pytest.raises(RunConflictError):
        repo.admit_run('owner', sid, 'request', 'other', {'message': 'new'})
    with pytest.raises(RunBusyError):
        repo.admit_run('owner', sid, 'another', 'hash', {})
    with pytest.raises(KeyError):
        repo.admit_run('other', sid, 'other', 'hash', {})
    repo.update_run_status('owner', rid, 'running')
    done = repo.settle_native_run('owner', rid, 'completed', 'completed', {}, (), usage=RunUsage())
    assert done.task_id == rid
    next_run, _ = repo.admit_run('owner', sid, 'next', 'digest', {'task_id': rid, 'message': '继续'})
    assert next_run.id != rid and next_run.task_id == rid
    assert repo.get_task_prior_run('owner', sid, rid, next_run.id) == done
    with pytest.raises(KeyError):
        repo.get_run_context('other', rid)


def test_atomic_native_settlement_rollback_and_usage(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sid = repo.create_session('owner').id
    run = repo.create_run('owner', sid, 'request', 'hash', {'message': 'x'})
    repo.update_run_status('owner', run.id, 'running')
    first = ModelRequest(parts=[UserPromptPart('原始请求')])
    last = ModelResponse(parts=[TextPart('最终回复')])
    compact = ModelRequest(parts=[UserPromptPart('压缩摘要')])
    repo.archive_native_run('owner', run.id, (first,))
    repo.update_run_usage('owner', run.id, RunUsage(requests=1, input_tokens=10, output_tokens=6))
    repo.admit_control('owner', run.id, 'next', 'follow_up', {'message': '继续'}, accepting=True)
    repo.admit_control('owner', run.id, 'reject', 'steer', {'message': '不要'}, accepting=True)
    repo.mark_input_rejected('owner', run.id, 'reject')

    with repo.connect() as db:
        db.execute("CREATE TRIGGER reject_settlement BEFORE UPDATE OF status ON app_runs "
                   "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.settle_native_run('owner', run.id, 'completed', 'completed', {}, (last,),
                               usage=RunUsage(requests=1, input_tokens=10, output_tokens=6), context=(compact, last))
    assert repo.load_conversation('owner', sid) == (first,)
    before = asyncio.run(SQLiteConversationStore(repo.path).load(sid, scope='owner'))
    assert before is not None and before.usage.requests == 0
    assert repo.get_run('owner', run.id).status == 'running'  # type: ignore[union-attr]
    assert repo.list_inputs('owner', run.id).items[0].status == 'queued'

    with repo.connect() as db:
        db.execute('DROP TRIGGER reject_settlement')
    done = repo.settle_native_run('owner', run.id, 'completed', 'completed', {}, (last,),
                                 usage=RunUsage(requests=1, input_tokens=10, output_tokens=6, cache_read_tokens=2),
                                 context=(compact, last))
    assert done.usage is not None and done.usage['complete'] is True and done.usage['cache_read_tokens'] == 2
    after = asyncio.run(SQLiteConversationStore(repo.path).load(sid, scope='owner'))
    assert after is not None and after.usage.requests == 1 and after.usage.total_tokens == 16
    assert repo.load_conversation('owner', sid) == (first, last)
    assert repo.get_run_context('owner', run.id) == (compact, last)
    assert repo.load_run_messages('owner', run.id) == (first, last)
    messages = repo.list_messages('owner', sid, limit=1)
    assert messages.next_cursor is not None
    assert messages.items[0].message == first
    assert repo.list_messages('owner', sid, after=messages.next_cursor).items[0].message == last
    assert [item.reason for item in repo.list_inputs('owner', run.id).items] == ['run_completed', 'native_not_accepting']
    assert repo.session_stats('owner', sid)['message_count'] == 2
    second = repo.create_run('owner', sid, 'second', 'hash', {})
    repo.settle_native_run('owner', second.id, 'completed', 'completed', {}, (), context=(first, last))
    assert repo.get_run_context('owner', second.id) is None


def test_recovery_preserves_usage_and_allows_new_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sid = repo.create_session('owner').id
    run = repo.create_run('owner', sid, 'request', 'hash', {'message': 'work'})
    repo.update_run_status('owner', run.id, 'running')
    repo.update_run_usage('owner', run.id, RunUsage(requests=1, unmeasured_requests=1))
    repo.admit_control('owner', run.id, 'followup', 'follow_up', {'message': '继续'}, accepting=True)
    reopened = SessionRepository(repo.path)
    assert reopened.recover_interrupted_runs() == 1
    assert reopened.recover_interrupted_runs() == 0
    interrupted = reopened.get_run('owner', run.id)
    assert interrupted is not None and interrupted.usage is not None
    assert interrupted.status == 'needs_reconciliation'
    assert interrupted.usage['input_tokens'] is None and interrupted.usage['complete'] is False
    assert reopened.list_inputs('owner', run.id).items[0].reason == 'process_interrupted'
    next_run = reopened.create_run('owner', sid, 'continue', 'hash', {'task_id': run.task_id})
    assert next_run.task_id == run.task_id and next_run.id != run.id
    recovered = ModelResponse(parts=[TextPart('checkpoint')])
    reopened.recover_native_messages('owner', next_run.id, run.id, (recovered,))
    assert reopened.load_run_messages('owner', run.id) == (recovered,)
    with pytest.raises(KeyError):
        reopened.recover_native_messages('other', next_run.id, run.id, (recovered,))


def test_cancelled_settlement_has_no_completed_answer_or_usage_copy(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session = repo.create_session('owner')
    run = repo.create_run('owner', session.id, 'run', 'hash', {})
    repo.update_run_status('owner', run.id, 'running')
    repo.admit_control('owner', run.id, 'stop', 'cancel', {}, accepting=True)
    answer = ModelResponse(parts=[TextPart('这段事实仍保留在正式记录')])
    result = repo.settle_native_run('owner', run.id, 'completed', 'completed',
                                    {'answer': '这段事实仍保留在正式记录', 'usage': {'complete': True}}, (answer,),
                                    usage=RunUsage(requests=1, input_tokens=2, output_tokens=3))
    assert result.status == 'cancelled' and result.stop_reason == 'cancelled'
    assert result.result_summary == {'stop_reason': 'cancelled'}
    assert result.usage is not None and result.usage['complete'] is False
    assert repo.load_run_messages('owner', run.id) == (answer,)


def test_recovered_messages_cannot_be_appended_after_newer_formal_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sid = repo.create_session('owner').id
    old = repo.create_run('owner', sid, 'old', 'hash', {})
    repo.update_run_status('owner', old.id, 'running')
    repo.recover_interrupted_runs()
    other = repo.create_run('owner', sid, 'other', 'hash', {})
    reply = ModelResponse(parts=[TextPart('new history')])
    repo.settle_native_run('owner', other.id, 'completed', 'completed', {}, (reply,))
    continuation = repo.create_run('owner', sid, 'continuation', 'hash', {'task_id': old.id})
    with pytest.raises(ValueError, match='newer formal history'):
        repo.recover_native_messages('owner', continuation.id, old.id,
                                     (ModelResponse(parts=[TextPart('older checkpoint')]),))
    assert repo.load_conversation('owner', sid) == (reply,)
