from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

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
    repository.settle_run("id:1", run.id, "completed", "completed", {}, ())
    with pytest.raises(ValueError, match="不匹配"):
        store.claim("id:1", session.id, plan["plan_id"], "wrong")
    claimed, won = store.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    assert won and claimed["status"] == "executing"
    assert claimed["steps"] == plan["steps"]


def test_concurrent_confirmation_has_one_winner(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    plan = store.create("id:1", run.id, [{"action": "create_draft"}])
    repository.settle_run("id:1", run.id, "completed", "completed", {}, ())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.claim("id:1", session.id, plan["plan_id"], plan["digest"])[1], range(4)))
    assert results.count(True) == 1


def test_interrupted_execution_retains_operation_id_and_never_reclaims(tmp_path: Path) -> None:
    repository, store, session, run = setup_store(tmp_path)
    plan = store.create("id:1", run.id, [{"action": "create_draft"}, {"action": "release"}])
    repository.settle_run("id:1", run.id, "completed", "completed", {}, ())
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
    repository.settle_run("id:1", run.id, "completed", "completed", {}, ())
    cancelled = store.cancel("id:1", session.id, plan["plan_id"], plan["digest"])
    assert cancelled["status"] == "cancelled"
    _, won = store.claim("id:1", session.id, plan["plan_id"], plan["digest"])
    assert not won
