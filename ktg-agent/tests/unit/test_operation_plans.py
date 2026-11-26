from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from ktg_agent.sessions.repository import SessionRepository, SessionRecord, RunRecord
from ktg_agent.operations.store import OperationPlanStore


def setup_store(tmp_path: Path) -> tuple[SessionRepository, OperationPlanStore, SessionRecord, RunRecord]:
    repository = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repository.create_session("id:1")
    run = repository.create_run("id:1", session.id, "req", "hash", {})
    repository.update_run_status("id:1", run.id, "running")
    return repository, OperationPlanStore(repository), session, run


def test_immutable_plan_is_owner_session_scoped_and_requires_completed_run(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    plan = store.create("id:1", run.id, [{"action": "create_draft", "draft": {"quantity": 50}}])
    assert plan["status"] == "pending"
    with pytest.raises(KeyError):
        store.get("id:2", session.id, plan["plan_id"])
    with pytest.raises(KeyError):
        store.get("id:1", "other", plan["plan_id"])
    with pytest.raises(ValueError, match="尚未完成"):
        store.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    repository.settle_native_run("id:1", run.id, "completed", "completed", {}, ())
    with pytest.raises(ValueError, match="不匹配"):
        store.claim("id:1", session.id, plan["plan_id"], "wrong")
    claimed, won = store.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    assert won and claimed["status"] == "executing"
    assert claimed["steps"] == plan["steps"]


def test_concurrent_confirmation_has_one_winner(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    plan = store.create("id:1", run.id, [{"action": "create_draft"}])
    repository.settle_native_run("id:1", run.id, "completed", "completed", {}, ())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.claim("id:1", session.id, plan["plan_id"], plan["digest"])[1], range(4)))
    assert results.count(True) == 1


def test_interrupted_execution_retains_operation_id_and_never_reclaims(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    plan = store.create("id:1", run.id, [{"action": "create_draft"}, {"action": "release"}])
    repository.settle_native_run("id:1", run.id, "completed", "completed", {}, ())
    plan, _ = store.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    first_id = plan["results"][0]["operation_id"]
    plan["results"][0].update({"status": "started", "request": {"operationId": first_id}})
    store.save_results("id:1", session.id, plan["plan_id"], plan["results"], "executing")
    restarted = OperationPlanStore(repository)
    assert restarted.recover_interrupted() == 1
    recovered = restarted.get("id:1", session.id, plan["plan_id"])
    assert recovered["status"] == "needs_reconciliation"
    assert recovered["results"][0]["operation_id"] == first_id
    assert recovered["results"][0]["status"] == "unknown"
    assert recovered["results"][1]["status"] == "skipped"
    _, won = restarted.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    assert not won


def test_cancel_before_confirmation_prevents_execution(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    plan = store.create("id:1", run.id, [{"action": "create_draft"}])
    repository.settle_native_run("id:1", run.id, "completed", "completed", {}, ())
    cancelled = store.cancel("id:1", session.id, plan["plan_id"], plan["digest"])
    assert cancelled["status"] == "cancelled"
    _, won = store.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    assert not won


def test_new_proposal_supersedes_previous_unconfirmed_same_session(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    earlier = store.create("id:1", run.id, [{"action": "create_draft", "quantity": 10}])
    later = store.create("id:1", run.id, [{"action": "create_draft", "quantity": 12}])
    assert store.get("id:1", session.id, earlier["plan_id"])["status"] == "superseded"
    assert store.get("id:1", session.id, later["plan_id"])["status"] == "pending"
    repository.settle_native_run("id:1", run.id, "completed", "completed", {}, ())
    assert not store.claim("id:1", session.id, earlier["plan_id"], earlier["digest"])[1]


def test_unknown_receipt_remains_listed_and_sent_request_is_immutable(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    old=store.create('id:1',run.id,[{'action':'create_draft'}])
    repository.settle_native_run('id:1',run.id,'completed','completed',{},())
    old,_=store.claim('id:1',session.id,old['plan_id'],old['digest'])
    old['results'][0].update(status='started',request={'value':1},request_path='/mes/pro/workorder/operations')
    store.save_results('id:1',session.id,old['plan_id'],old['results'],'executing')
    with pytest.raises(ValueError, match='immutable'):
        old['results'][0]['request']['value']=2
        store.save_results('id:1',session.id,old['plan_id'],old['results'],'executing')
    store.interrupt('id:1',session.id,old['plan_id'])
    for i in range(5):
        newer=repository.create_run('id:1', session.id, str(i), 'hash',{})
        repository.update_run_status('id:1',newer.id,'running')
        proposal=store.create('id:1',newer.id,[{'action':'create_draft'}])
        store.cancel('id:1',session.id,proposal['plan_id'],proposal['digest'])
        repository.settle_native_run('id:1',newer.id,'completed','completed',{},())
    assert any(p['plan_id']==old['plan_id'] and p['status']=='needs_reconciliation' for p in store.list_plans('id:1',session.id,limit=1))


def test_legacy_business_evidence_import_does_not_restore_chat_or_reexecute(tmp_path: Path) -> None:
    legacy = tmp_path / 'legacy.sqlite'
    old_steps = json.dumps({'steps': [{'action': 'create_draft'}]})
    old_results = json.dumps({'results': [{'operation_id': 'stable-op', 'status': 'started',
                                         'request': {'operationId': 'stable-op'}, 'request_path': '/receipt'}]})
    pending_results = json.dumps({'results': [{'operation_id': 'never-sent', 'status': 'pending'}]})
    with sqlite3.connect(legacy) as db:
        db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,owner_user_id TEXT);
          CREATE TABLE runs(id TEXT PRIMARY KEY,session_id TEXT,owner_user_id TEXT);
          CREATE TABLE agent_operation_plans(plan_id TEXT PRIMARY KEY,session_id TEXT,run_id TEXT,
          owner_user_id TEXT,digest TEXT,steps_json TEXT,results_json TEXT,status TEXT,
          created_at TEXT,expires_at TEXT,updated_at TEXT);
          INSERT INTO sessions VALUES('old-sid','id:1');
          INSERT INTO runs VALUES('old-rid','old-sid','id:1');''')
        db.executemany('INSERT INTO agent_operation_plans VALUES(?,?,?,?,?,?,?,?,?,?,?)', [
            ('old-plan', 'old-sid', 'old-rid', 'id:1', 'digest', old_steps, old_results,
             'executing', '2026-11-25', '2099-01-01', '2026-11-25'),
            ('old-pending', 'old-sid', 'old-rid', 'id:1', 'digest', old_steps, pending_results,
             'pending', '2026-11-25', '2099-01-01', '2026-11-25')])
    backup = legacy.read_bytes()
    repository = SessionRepository(tmp_path / 'fresh.sqlite')
    store = OperationPlanStore(repository)
    assert store.import_legacy(legacy) == 2
    assert store.recover_interrupted() == 1
    assert repository.get_session('id:1', 'old-sid') is None
    assert repository.get_run('id:1', 'old-rid') is None
    assert not store.is_historical_session('id:2', 'old-sid')
    assert store.list_historical_plans('id:2') == []
    assert store.is_historical_session('id:1', 'old-sid')
    assert {row['plan_id'] for row in store.list_historical_plans('id:1')} == {'old-plan','old-pending'}
    old = store.get('id:1', 'old-sid', 'old-plan')
    assert old['historical'] and old['status'] == 'needs_reconciliation'
    assert old['results'][0]['status'] == 'unknown'
    assert old['results'][0]['operation_id'] == 'stable-op'
    assert not store.claim('id:1', 'old-sid', 'old-plan', 'digest')[1]
    assert not store.claim('id:1', 'old-sid', 'old-pending', 'digest')[1]
    result, won = store.claim_reconciliation('id:1', 'old-sid', 'old-plan')
    assert won and result['results'][0]['request']['operationId'] == 'stable-op'
    result['results'][0].update(status='succeeded', message='verified original receipt')
    store.save_results('id:1', 'old-sid', 'old-plan', result['results'], 'succeeded')
    assert store.import_legacy(legacy) == 0
    assert store.get('id:1', 'old-sid', 'old-plan')['status'] == 'succeeded'
    assert legacy.read_bytes() == backup
