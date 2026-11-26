"""Durable, owner-scoped operation plans; only the trusted confirmation path claims them."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    """Application-owned operation records in the unified SQLite database."""

    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS app_operation_plans (
                plan_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                app_session_id TEXT REFERENCES app_sessions(id),
                app_run_id TEXT REFERENCES app_runs(id),
                owner_user_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                results_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK ((app_session_id IS NULL AND app_run_id IS NULL) OR
                       (app_session_id IS NOT NULL AND app_run_id IS NOT NULL AND
                        app_session_id=session_id AND app_run_id=run_id))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS app_operation_plans_session "
                       "ON app_operation_plans(owner_user_id,session_id,created_at)")

    @property
    def path(self) -> Path:
        return self._repository.path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._repository.connect() as db:
            yield db

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
            "historical": row["app_session_id"] is None,
        }

    def _row(self, db: sqlite3.Connection, owner: str, session_id: str, plan_id: str) -> sqlite3.Row:
        db.execute("UPDATE app_operation_plans SET status='expired',updated_at=? "
                   "WHERE owner_user_id=? AND session_id=? AND plan_id=? AND status='pending' AND expires_at<=?",
                   (self._now(), owner, session_id, plan_id, self._now()))
        row = db.execute("SELECT * FROM app_operation_plans WHERE plan_id=? AND session_id=? AND owner_user_id=?",
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
            source = db.execute("SELECT status FROM app_runs WHERE id=? AND session_id=? AND owner_user_id=?",
                                (run_id, run.session_id, owner)).fetchone()
            if source is None or source["status"] != "running":
                raise ValueError("准备方案的当前运行已结束。")
            db.execute("UPDATE app_operation_plans SET status='superseded',updated_at=? "
                       "WHERE app_session_id=? AND owner_user_id=? AND status='pending'", (now, run.session_id, owner))
            db.execute("""INSERT INTO app_operation_plans(plan_id,session_id,run_id,app_session_id,app_run_id,
                       owner_user_id,digest,steps_json,results_json,status,created_at,expires_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
                       (plan_id, run.session_id, run_id, run.session_id, run_id, owner, digest, steps_json,
                        _safe_json({"results": results}), now, expires, now))
            return self._from_row(self._row(db, owner, run.session_id, plan_id))

    def get(self, owner: str, session_id: str, plan_id: str) -> Plan:
        with self._connect() as db:
            return self._from_row(self._row(db, owner, session_id, plan_id))

    def list_plans(self, owner: str, session_id: str, *, limit: int = 30) -> list[Plan]:
        with self._connect() as db:
            now = self._now()
            db.execute("UPDATE app_operation_plans SET status='expired',updated_at=? "
                       "WHERE owner_user_id=? AND session_id=? AND status='pending' AND expires_at<=?",
                       (now, owner, session_id, now))
            # A historical unknown receipt must stay discoverable even after many new plans.
            rows = db.execute("SELECT * FROM app_operation_plans WHERE owner_user_id=? AND session_id=? AND "
                              "(status IN ('pending','executing','reconciling','needs_reconciliation') OR plan_id IN "
                              "(SELECT plan_id FROM app_operation_plans WHERE owner_user_id=? AND session_id=? "
                              "ORDER BY created_at DESC LIMIT ?)) ORDER BY created_at DESC",
                              (owner, session_id, owner, session_id, limit)).fetchall()
            return [self._from_row(row) for row in rows]

    def is_historical_session(self, owner: str, session_id: str) -> bool:
        """Authorize historical operations without resurrecting old chat sessions."""
        with self._connect() as db:
            return db.execute("SELECT 1 FROM app_operation_plans WHERE owner_user_id=? "
                              "AND session_id=? AND app_session_id IS NULL LIMIT 1",
                              (owner, session_id)).fetchone() is not None

    def list_historical_plans(self, owner: str, session_id: str | None = None) -> list[Plan]:
        """Retain discoverability, including unknown receipts from retired sessions."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM app_operation_plans WHERE owner_user_id=? "
                              "AND app_session_id IS NULL AND (? IS NULL OR session_id=?) "
                              "ORDER BY created_at DESC,plan_id", (owner, session_id, session_id)).fetchall()
            return [self._from_row(row) for row in rows]

    def import_legacy(self, source: str | Path) -> int:
        """Import operation evidence only, from an explicit reviewed legacy DB backup.

        The old database is opened read-only and is never an online fallback.
        No chat sessions or runs are fabricated. Repeating the same import does
        not overwrite a locally recovered receipt or acquire execution rights.
        """
        path = Path(source)
        if not path.is_absolute() or not path.is_file() or path.resolve() == self._repository.path.resolve():
            raise ValueError("a separate absolute legacy database backup is required")
        columns = ("plan_id", "session_id", "run_id", "owner_user_id", "digest", "steps_json",
                   "results_json", "status", "created_at", "expires_at", "updated_at")
        source_db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        source_db.row_factory = sqlite3.Row
        try:
            source_db.execute("BEGIN")
            rows = source_db.execute("SELECT * FROM agent_operation_plans ORDER BY plan_id").fetchall()
            for row in rows:
                association = source_db.execute("SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id "
                    "WHERE r.id=? AND r.session_id=? AND r.owner_user_id=? AND s.owner_user_id=?",
                    (row["run_id"], row["session_id"], row["owner_user_id"], row["owner_user_id"])).fetchone()
                if association is None:
                    raise ValueError("legacy operation identity or source association does not match")
                steps = json.loads(row["steps_json"])["steps"]
                results = json.loads(row["results_json"])["results"]
                if (not isinstance(steps, list) or not isinstance(results, list) or not steps
                        or len(steps) != len(results)
                        or any(not isinstance(item, dict) or not isinstance(item.get("operation_id"), str)
                               or not item["operation_id"] for item in results)):
                    raise ValueError("invalid legacy operation evidence")
        finally:
            source_db.close()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = 0
            for row in rows:
                existing = db.execute("SELECT * FROM app_operation_plans WHERE plan_id=?",
                                      (row["plan_id"],)).fetchone()
                if existing is not None:
                    # Preserve later verification in the destination. Immutable
                    # identifiers, source, proposal and original request must match.
                    prior_results = json.loads(existing["results_json"])["results"]
                    source_results = json.loads(row["results_json"])["results"]
                    if (existing["app_session_id"] is not None or any(existing[key] != row[key] for key in
                            ("session_id", "run_id", "owner_user_id", "digest", "steps_json", "created_at", "expires_at"))
                            or any(prior.get(key) != original.get(key) for prior, original in
                                   zip(prior_results, source_results, strict=True) for key in
                                   ("operation_id", "request", "request_path", "request_order_id"))):
                        raise ValueError("legacy operation evidence conflicts with existing record")
                    continue
                db.execute("INSERT INTO app_operation_plans(" + ",".join(columns) + ") "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(row[key] for key in columns))
                count += 1
            return count

    def claim(self, owner: str, session_id: str, plan_id: str, digest: str) -> tuple[Plan, bool]:
        """Atomically grant one execution attempt to a confirmed, finished proposal."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, session_id, plan_id)
            if row["digest"] != digest:
                raise ValueError("确认方案与原方案不匹配。")
            if row["status"] != "pending" or row["app_run_id"] is None:
                return self._from_row(row), False
            source = db.execute("SELECT status FROM app_runs WHERE id=? AND owner_user_id=? AND session_id=?",
                                (row["app_run_id"], owner, row["app_session_id"])).fetchone()
            if source is None or source["status"] != "completed":
                raise ValueError("准备方案的会话运行尚未完成或未正常结束。")
            db.execute("UPDATE app_operation_plans SET status='executing',updated_at=? WHERE plan_id=?",
                       (self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id)), True

    def cancel(self, owner: str, session_id: str, plan_id: str, digest: str) -> Plan:
        with self._connect() as db:
            row = self._row(db, owner, session_id, plan_id)
            if row["digest"] != digest:
                raise ValueError("取消方案与原方案不匹配。")
            db.execute("UPDATE app_operation_plans SET status='cancelled',updated_at=? "
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
            transitions = {"pending": {"pending","started","succeeded","failed","skipped"},
                           "started": {"started","succeeded","failed","unknown"},
                           "unknown": {"unknown","succeeded","failed"},
                           "succeeded": {"succeeded"}, "failed": {"failed"}, "skipped": {"skipped"}}
            for previous, current in zip(prior["results"], results, strict=True):
                if current["status"] not in transitions[previous["status"]]:
                    raise ValueError("operation state cannot move backwards")
                if "request" in previous and any(current.get(key) != previous.get(key)
                    for key in ("request", "request_path", "request_order_id")):
                    raise ValueError("sent operation request is immutable")
            db.execute("UPDATE app_operation_plans SET status=?,results_json=?,updated_at=? WHERE plan_id=?",
                       (status, _safe_json({"results": results}), self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id))

    def claim_reconciliation(self, owner: str, session_id: str, plan_id: str) -> tuple[Plan, bool]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, session_id, plan_id)
            if row["status"] != "needs_reconciliation":
                return self._from_row(row), False
            db.execute("UPDATE app_operation_plans SET status='reconciling',updated_at=? WHERE plan_id=?",
                       (self._now(), plan_id))
            return self._from_row(self._row(db, owner, session_id, plan_id)), True

    def recover_interrupted(self) -> int:
        """Startup recovery cannot infer whether an outstanding remote request committed."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM app_operation_plans WHERE status IN ('executing','reconciling')").fetchall()
            for row in rows:
                self._interrupt_row(db, row)
            return len(rows)

    def _interrupt_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> None:
        results: list[Step] = self._from_row(row)["results"]
        for result in results:
            if result["status"] == "started":
                result.update(status="unknown", message="执行中断，须核对原操作回执。")
            elif result["status"] == "pending":
                result.update(status="skipped", message="方案中断，未发送此操作。")
        db.execute("UPDATE app_operation_plans SET status=?,results_json=?,updated_at=? WHERE plan_id=?",
                   (final_status(results), _safe_json({"results": results}), self._now(), row["plan_id"]))

    def interrupt(self, owner: str, session_id: str, plan_id: str) -> None:
        """Only the terminating executor may interrupt its own claimed plan."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, session_id, plan_id)
            if row["status"] in {"executing", "reconciling"}:
                self._interrupt_row(db, row)
