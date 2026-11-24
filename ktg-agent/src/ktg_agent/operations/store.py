"""Durable, owner-scoped operation plans; only the trusted confirmation path claims them."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from ktg_agent.sessions.repository import SessionRepository, _safe_json

type Plan = dict[str, Any]
type Step = dict[str, Any]


def final_status(results: list[Step]) -> str:
    statuses = {result["status"] for result in results}
    if "unknown" in statuses or "started" in statuses:
        return "needs_reconciliation"
    if statuses == {"succeeded"}:
        return "succeeded"
    return "partial" if "succeeded" in statuses else "failed"


class OperationPlanStore:
    """An additive plan table alongside existing session storage, no MES data writes."""

    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS agent_operation_plans (
                plan_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                run_id TEXT NOT NULL REFERENCES runs(id),
                owner_user_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                results_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS agent_operation_plans_session "
                       "ON agent_operation_plans(owner_user_id,session_id,created_at)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._repository.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Plan:
        return {
            "kind": "operation_plan", "plan_id": row["plan_id"], "run_id": row["run_id"],
            "session_id": row["session_id"], "digest": row["digest"], "status": row["status"],
            "steps": json.loads(row["steps_json"])["steps"],
            "results": json.loads(row["results_json"])["results"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "updated_at": row["updated_at"],
        }

    def _row(self, db: sqlite3.Connection, owner: str, session_id: str, plan_id: str) -> sqlite3.Row:
        db.execute("UPDATE agent_operation_plans SET status='expired',updated_at=? "
                   "WHERE owner_user_id=? AND session_id=? AND plan_id=? AND status='pending' AND expires_at<=?",
                   (self._now(), owner, session_id, plan_id, self._now()))
        row = db.execute("SELECT * FROM agent_operation_plans WHERE plan_id=? AND session_id=? AND owner_user_id=?",
                         (plan_id, session_id, owner)).fetchone()
        if row is None:
            raise KeyError("operation plan not found")
        return cast(sqlite3.Row, row)

    def create(self, owner: str, run_id: str, steps: list[Step]) -> Plan:
        run = self._repository.get_run(owner, run_id)
        if run is None or run.status != "running":
            raise ValueError("只能在当前运行的会话中准备操作方案。")
        if not 1 <= len(steps) <= 20:
            raise ValueError("一个方案须包含 1–20 项操作。")
        steps_json = _safe_json({"steps": steps})
        digest = "sha256:" + hashlib.sha256(json.dumps(json.loads(steps_json), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        results = [{"operation_id": str(uuid4()), "status": "pending"} for _ in steps]
        plan_id, now = str(uuid4()), self._now()
        expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE agent_operation_plans SET status='superseded',updated_at=? "
                       "WHERE session_id=? AND owner_user_id=? AND status='pending'", (now, run.session_id, owner))
            db.execute("INSERT INTO agent_operation_plans VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
                       (plan_id, run.session_id, run_id, owner, digest, steps_json,
                        _safe_json({"results": results}), now, expires, now))
            return self._from_row(self._row(db, owner, run.session_id, plan_id))

    def get(self, owner: str, session_id: str, plan_id: str) -> Plan:
        with self._connect() as db:
            return self._from_row(self._row(db, owner, session_id, plan_id))

    def list_plans(self, owner: str, session_id: str, *, limit: int = 30) -> list[Plan]:
        with self._connect() as db:
            now = self._now()
            db.execute("UPDATE agent_operation_plans SET status='expired',updated_at=? "
                       "WHERE owner_user_id=? AND session_id=? AND status='pending' AND expires_at<=?",
                       (now, owner, session_id, now))
            rows = db.execute("SELECT * FROM agent_operation_plans WHERE owner_user_id=? AND session_id=? "
                              "ORDER BY created_at DESC LIMIT ?", (owner, session_id, limit)).fetchall()
            return [self._from_row(row) for row in rows]

    def claim(self, owner: str, session_id: str, plan_id: str, digest: str) -> tuple[Plan, bool]:
        """Atomically grant one execution attempt to a confirmed, finished proposal."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, session_id, plan_id)
            if row["digest"] != digest:
                raise ValueError("确认方案与原方案不匹配。")
            if row["status"] != "pending":
                return self._from_row(row), False
            source = db.execute("SELECT status FROM runs WHERE id=? AND owner_user_id=? AND session_id=?",
                                (row["run_id"], owner, session_id)).fetchone()
            if source is None or source["status"] != "completed":
                raise ValueError("准备方案的会话运行尚未完成或未正常结束。")
            db.execute("UPDATE agent_operation_plans SET status='executing',updated_at=? WHERE plan_id=?",
                       (self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id)), True

    def cancel(self, owner: str, session_id: str, plan_id: str, digest: str) -> Plan:
        with self._connect() as db:
            row = self._row(db, owner, session_id, plan_id)
            if row["digest"] != digest:
                raise ValueError("取消方案与原方案不匹配。")
            db.execute("UPDATE agent_operation_plans SET status='cancelled',updated_at=? "
                       "WHERE plan_id=? AND status='pending'", (self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id))

    def save_results(self, owner: str, session_id: str, plan_id: str,
                     results: list[Step], status: str) -> Plan:
        if status not in {"executing", "succeeded", "partial", "failed", "needs_reconciliation"}:
            raise ValueError("invalid operation-plan status")
        with self._connect() as db:
            row = self._row(db, owner, session_id, plan_id)
            prior = self._from_row(row)
            if row["status"] not in {"executing", "reconciling"}:
                raise ValueError("operation-plan is not executing")
            if [r["operation_id"] for r in results] != [r["operation_id"] for r in prior["results"]]:
                raise ValueError("operation IDs are immutable")
            db.execute("UPDATE agent_operation_plans SET status=?,results_json=?,updated_at=? WHERE plan_id=?",
                       (status, _safe_json({"results": results}), self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id))

    def claim_reconciliation(self, owner: str, session_id: str, plan_id: str) -> tuple[Plan, bool]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, session_id, plan_id)
            if row["status"] != "needs_reconciliation":
                return self._from_row(row), False
            db.execute("UPDATE agent_operation_plans SET status='reconciling',updated_at=? WHERE plan_id=?",
                       (self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id)), True

    def recover_interrupted(self) -> int:
        """Startup recovery cannot infer whether an outstanding remote request committed."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM agent_operation_plans WHERE status IN ('executing','reconciling')").fetchall()
            for row in rows:
                results: list[Step] = self._from_row(row)["results"]
                for result in results:
                    if result["status"] == "started":
                        result.update(status="unknown", message="执行中断，须核对原操作回执。")
                    elif result["status"] == "pending":
                        result.update(status="skipped", message="方案中断，未发送此操作。")
                db.execute("UPDATE agent_operation_plans SET status=?,results_json=?,updated_at=? WHERE plan_id=?",
                           (final_status(results), _safe_json({"results": results}), self._now(), row["plan_id"]))
            return len(rows)
