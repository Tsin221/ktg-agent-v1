"""Versioned SQLite repository for linear Agent sessions.

Every public operation opens its own transaction. This permits one repository
instance to be shared by the threaded HTTP server without sharing connections.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generic, TypeVar, cast
from uuid import uuid4

from ktg_agent.core.messages import (
    JsonObject, Message, _json_object, _required_text, message_from_dict,
    message_to_dict,
)

T = TypeVar("T")
ACTIVE = frozenset({"queued", "running", "cancelling"})
TERMINAL = frozenset({
    "completed", "needs_input", "failed", "cancelled", "needs_reconciliation",
})
EVENT_RETENTION_DAYS = 30
STATUSES = ACTIVE | TERMINAL
_SECRET_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|bearer[_-]?token|secret|"
    r"headers?|cookies?|traceback|stack[_-]?trace|exception[_-]?stack)", re.I,
)
_SECRET_VALUE = re.compile(
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}\b|\bapi[_ -]?key\s*[:=]\s*\S+|"
    r"Traceback \(most recent call last\):|\bFile \"[^\"]+\", line \d+",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    owner_user_id: str
    title: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    session_id: str
    owner_user_id: str
    request_id: str
    request_hash: str
    request_summary: JsonObject
    status: str
    created_at: str
    started_at: str | None
    ended_at: str | None
    stop_reason: str | None
    result_summary: JsonObject | None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    session_id: str
    run_id: str
    message: Message
    created_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    run_id: str
    seq: int
    type: str
    payload: JsonObject
    created_at: str


@dataclass(frozen=True, slots=True)
class ControlRecord:
    position: int
    run_id: str
    owner_user_id: str
    request_id: str
    kind: str
    request: JsonObject
    status: str
    reason: str | None
    created_at: str
    updated_at: str


class RunConflictError(ValueError):
    code = "REQUEST_ID_CONFLICT"


class RunBusyError(ValueError):
    code = "RUN_BUSY"


class EventHistoryExpiredError(ValueError):
    code = "EVENT_HISTORY_EXPIRED"


class ControlConflictError(ValueError):
    code = "REQUEST_ID_CONFLICT"


class ControlStateError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_json(value: object) -> str:
    data = _json_object(value, "stored JSON")

    def check(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if _SECRET_KEY.search(key):
                    raise ValueError("sensitive data must not be stored")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, str) and _SECRET_VALUE.search(item):
            raise ValueError("sensitive data must not be stored")

    check(data)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _read_json(raw: str) -> JsonObject:
    return _json_object(json.loads(raw), "stored JSON")


class SessionRepository:
    """Owner-scoped persistence; callers must supply a verified owner ID."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("session database path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def __enter__(self) -> SessionRepository:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        with self._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > 3:
                raise RuntimeError("session database has a newer schema version")
            if version == 3:
                return
            if version == 1:
                db.execute("ALTER TABLE runs ADD COLUMN replay_floor_seq INTEGER NOT NULL DEFAULT 1")
                db.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                db.executescript("""BEGIN IMMEDIATE;
                    CREATE TABLE control_commands (
                        position INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(id),
                        owner_user_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('cancel','steer','follow_up')),
                        request_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('queued','consumed','not_consumed','accepted')),
                        reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(owner_user_id,run_id,request_id)
                    );
                    CREATE INDEX control_commands_page ON control_commands(run_id,position);
                    PRAGMA user_version=3;
                    COMMIT;
                """)
                return
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise RuntimeError("refusing to migrate an unrelated database")
            db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE sessions (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    owner_user_id TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX sessions_owner_page ON sessions(owner_user_id, position DESC);
                CREATE TABLE runs (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    owner_user_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    request_summary_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'queued','running','cancelling','completed','needs_input',
                        'failed','cancelled','needs_reconciliation')),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    stop_reason TEXT,
                    result_summary_json TEXT,
                    replay_floor_seq INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(owner_user_id, session_id, request_id)
                );
                CREATE UNIQUE INDEX runs_one_active_per_session ON runs(session_id)
                    WHERE status IN ('queued','running','cancelling');
                CREATE INDEX runs_session_page ON runs(session_id, position);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX messages_session_page ON messages(session_id, id);
                CREATE TABLE events (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    seq INTEGER NOT NULL CHECK (seq > 0),
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq)
                );
                PRAGMA user_version=2;
                COMMIT;
            """)
        self._migrate()

    def create_session(self, owner_user_id: str, *, title: str | None = None) -> SessionRecord:
        owner_user_id = _required_text(owner_user_id, "owner user id")
        _safe_json({"owner_user_id": owner_user_id})
        if title is not None:
            title = _required_text(title, "title")
            _safe_json({"title": title})
        session_id, now = str(uuid4()), _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id,owner_user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (session_id, owner_user_id, title, now, now),
            )
        return SessionRecord(session_id, owner_user_id, title, now, now)

    def get_session(self, owner_user_id: str, session_id: str) -> SessionRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id=? AND owner_user_id=?",
                (session_id, owner_user_id),
            ).fetchone()
        return _session(row) if row else None

    def list_sessions(self, owner_user_id: str, *, after: int | None = None, limit: int = 50) -> Page[SessionRecord]:
        _page_args(after, limit)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions WHERE owner_user_id=? AND (? IS NULL OR position < ?) "
                "ORDER BY position DESC LIMIT ?",
                (owner_user_id, after, after, limit + 1),
            ).fetchall()
        items = tuple(_session(row) for row in rows[:limit])
        return Page(items, int(rows[limit - 1]["position"]) if len(rows) > limit else None)

    def create_run(
        self, owner_user_id: str, session_id: str, request_id: str,
        request_hash: str, request_summary: JsonObject,
    ) -> RunRecord:
        _required_text(request_id, "request id")
        _required_text(request_hash, "request hash")
        _safe_json({"request_id": request_id, "request_hash": request_hash})
        summary = _safe_json(request_summary)
        run_id, now = str(uuid4()), _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO runs(id,session_id,owner_user_id,request_id,request_hash,"
                "request_summary_json,status,created_at) "
                "SELECT ?,s.id,s.owner_user_id,?,?,?,'queued',? FROM sessions s "
                "WHERE s.id=? AND s.owner_user_id=?",
                (run_id, request_id, request_hash, summary, now, session_id, owner_user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("session not found")
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        return RunRecord(run_id, session_id, owner_user_id, request_id, request_hash,
                         _read_json(summary), "queued", now, None, None, None, None)

    def admit_run(
        self, owner_user_id: str, session_id: str, request_id: str,
        request_hash: str, request_summary: JsonObject,
    ) -> tuple[RunRecord, bool]:
        """Atomically return an idempotent run or reserve the session for one new run."""
        _required_text(request_id, "request id")
        _required_text(request_hash, "request hash")
        _safe_json({"request_id": request_id, "request_hash": request_hash})
        summary = _safe_json(request_summary)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute(
                "SELECT id FROM sessions WHERE id=? AND owner_user_id=?",
                (session_id, owner_user_id),
            ).fetchone()
            if session is None:
                raise KeyError("session not found")
            existing = db.execute(
                "SELECT * FROM runs WHERE owner_user_id=? AND session_id=? AND request_id=?",
                (owner_user_id, session_id, request_id),
            ).fetchone()
            if existing is not None:
                run = _run(existing)
                if run.request_hash != request_hash:
                    raise RunConflictError("request_id 已用于不同请求。")
                return run, False
            busy = db.execute(
                "SELECT 1 FROM runs WHERE session_id=? AND status IN ('queued','running','cancelling')",
                (session_id,),
            ).fetchone()
            if busy is not None:
                raise RunBusyError("会话已有活动运行。")
            run_id, now = str(uuid4()), _now()
            db.execute(
                "INSERT INTO runs(id,session_id,owner_user_id,request_id,request_hash,"
                "request_summary_json,status,created_at) VALUES(?,?,?,?,?,?,'queued',?)",
                (run_id, session_id, owner_user_id, request_id, request_hash, summary, now),
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        return RunRecord(run_id, session_id, owner_user_id, request_id, request_hash,
                         _read_json(summary), "queued", now, None, None, None, None), True

    def get_run(self, owner_user_id: str, run_id: str) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=?", (run_id, owner_user_id),
            ).fetchone()
        return _run(row) if row else None

    @staticmethod
    def _next_seq(db: sqlite3.Connection, run_id: str) -> int:
        return int(db.execute(
            "SELECT MAX(COALESCE(MAX(e.seq),0),r.replay_floor_seq-1)+1 "
            "FROM runs r LEFT JOIN events e ON e.run_id=r.id WHERE r.id=? GROUP BY r.id",
            (run_id,),
        ).fetchone()[0])

    def admit_control(self, owner: str, run_id: str, request_id: str,
                      kind: str, request: JsonObject, *, accepting: bool,
                      ready: bool = True, closed: bool = False) -> tuple[ControlRecord, bool]:
        """Resolve idempotency before state, then commit command and event together."""
        encoded = _safe_json(request)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT r.status FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=?", (run_id, owner),
            ).fetchone()
            if run is None:
                raise KeyError("run not found")
            existing = db.execute(
                "SELECT * FROM control_commands WHERE owner_user_id=? AND run_id=? AND request_id=?",
                (owner, run_id, request_id),
            ).fetchone()
            if existing is not None:
                item = _control(existing)
                if item.kind != kind or item.request != request:
                    raise ControlConflictError("request_id 已用于不同控制命令。")
                return item, False
            status = str(run["status"])
            if kind == "cancel":
                if status in TERMINAL:
                    raise ControlStateError("RUN_ALREADY_SETTLED")
                command_status = "accepted"
                event_type = "cancel_requested"
            else:
                if status != "running":
                    raise ControlStateError("RUN_NOT_RUNNING" if status == "queued" else "RUN_NOT_ACTIVE")
                if closed:
                    raise ControlStateError("RUN_NOT_ACTIVE")
                if not ready:
                    raise ControlStateError("RUN_NOT_RUNNING")
                if not accepting:
                    raise ControlStateError("RUN_NOT_ACTIVE")
                command_status = "queued"
                event_type = "input_queued"
            now = _now()
            cursor = db.execute(
                "INSERT INTO control_commands(run_id,owner_user_id,request_id,kind,request_json,"
                "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, owner, request_id, kind, encoded, command_status, now, now),
            )
            if kind == "cancel":
                db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run_id,))
            payload: JsonObject = {"request_id": request_id}
            if kind != "cancel":
                payload["mode"] = kind
            db.execute(
                "INSERT INTO events(run_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, self._next_seq(db, run_id), event_type, _safe_json(payload), now),
            )
            row = db.execute("SELECT * FROM control_commands WHERE position=?", (cursor.lastrowid,)).fetchone()
            assert row is not None
            return _control(row), True

    def mark_input_consumed(self, owner: str, run_id: str, request_id: str) -> None:
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE control_commands SET status='consumed',updated_at=? WHERE owner_user_id=? "
                "AND run_id=? AND request_id=? AND status='queued' AND kind IN ('steer','follow_up')",
                (now, owner, run_id, request_id),
            )
            if cursor.rowcount != 1:
                raise ControlStateError("INPUT_NOT_QUEUED")
            db.execute("INSERT INTO events(run_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                       (run_id, self._next_seq(db, run_id), "input_consumed",
                        _safe_json({"request_id": request_id, "status": "consumed"}), now))

    def list_inputs(self, owner: str, run_id: str, *, after: int | None = None,
                    limit: int = 50) -> Page[ControlRecord]:
        _page_args(after, limit)
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.* FROM control_commands c JOIN runs r ON r.id=c.run_id "
                "JOIN sessions s ON s.id=r.session_id WHERE c.run_id=? AND s.owner_user_id=? "
                "AND c.kind IN ('steer','follow_up') AND c.position>? "
                "ORDER BY c.position LIMIT ?", (run_id, owner, after or 0, limit + 1),
            ).fetchall()
        return Page(tuple(_control(row) for row in rows[:limit]),
                    int(rows[limit - 1]["position"]) if len(rows) > limit else None)

    def last_event_seq(self, owner_user_id: str, run_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(COALESCE(MAX(e.seq),0),r.replay_floor_seq-1) AS last_seq FROM runs r "
                "JOIN sessions s ON s.id=r.session_id "
                "LEFT JOIN events e ON e.run_id=r.id "
                "WHERE r.id=? AND s.owner_user_id=? GROUP BY r.id",
                (run_id, owner_user_id),
            ).fetchone()
        if row is None:
            raise KeyError("run not found")
        return int(row["last_seq"])

    def list_runs(self, owner_user_id: str, session_id: str, *, after: int | None = None, limit: int = 50) -> Page[RunRecord]:
        _page_args(after, limit)
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE s.owner_user_id=? AND s.id=? AND (? IS NULL OR r.position>?) "
                "ORDER BY r.position LIMIT ?",
                (owner_user_id, session_id, after, after, limit + 1),
            ).fetchall()
        return Page(tuple(_run(row) for row in rows[:limit]),
                    int(rows[limit - 1]["position"]) if len(rows) > limit else None)

    def get_active_run(self, owner_user_id: str, session_id: str) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE s.id=? AND s.owner_user_id=? AND r.status IN "
                "('queued','running','cancelling')",
                (session_id, owner_user_id),
            ).fetchone()
        return _run(row) if row else None

    def recover_interrupted_runs(self) -> int:
        """Settle runs orphaned by a previous process before accepting requests.

        A running tool may already have written externally, so its outcome stays
        unknown. This method is deliberately never called by a live coordinator.
        """
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,session_id,status FROM runs "
                "WHERE status IN ('queued','running','cancelling')"
            ).fetchall()
            for row in rows:
                status = "cancelled" if row["status"] == "queued" else "needs_reconciliation"
                summary: JsonObject = {
                    "error": "SERVICE_RESTARTED",
                    "message": "服务重启中断运行；请核验已发出的业务操作。" if status == "needs_reconciliation"
                               else "服务重启前运行尚未开始，请重新提交。",
                    "outcome_unknown": status == "needs_reconciliation",
                }
                db.execute(
                    "UPDATE runs SET status=?,ended_at=?,stop_reason=?,result_summary_json=? WHERE id=?",
                    (status, now, "SERVICE_RESTARTED", _safe_json(summary), row["id"]),
                )
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, row["session_id"]))
                self._finish_queued_inputs(db, str(row["id"]), "process_interrupted", now)
                seq = self._next_seq(db, str(row["id"]))
                payload = _safe_json({"status": status, "stop_reason": "SERVICE_RESTARTED",
                                      "result_summary": summary})
                db.execute(
                    "INSERT INTO events(run_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (row["id"], seq, "run_settled", payload, now),
                )
        return len(rows)

    def update_run_status(
        self, owner_user_id: str, run_id: str, status: str, *,
        stop_reason: str | None = None, result_summary: JsonObject | None = None,
    ) -> RunRecord:
        if status not in STATUSES:
            raise ValueError("unknown run status")
        if stop_reason is not None:
            _required_text(stop_reason, "stop reason")
            _safe_json({"stop_reason": stop_reason})
        summary = _safe_json(result_summary) if result_summary is not None else None
        now = _now()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE runs SET status=?,started_at=CASE WHEN ?='running' "
                "THEN COALESCE(started_at,?) ELSE started_at END, "
                "ended_at=CASE WHEN ? IN ('completed','needs_input','failed','cancelled',"
                "'needs_reconciliation') THEN ? ELSE NULL END, "
                "stop_reason=?,result_summary_json=? WHERE id=? AND EXISTS "
                "(SELECT 1 FROM sessions s WHERE s.id=runs.session_id AND s.owner_user_id=?)",
                (status, status, now, status, now, stop_reason, summary, run_id, owner_user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("run not found")
        result = self.get_run(owner_user_id, run_id)
        assert result is not None
        return result

    def append_message(self, owner_user_id: str, run_id: str, message: Message) -> MessageRecord:
        payload = message_to_dict(message)
        encoded, now = _safe_json(payload), _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(session_id,run_id,role,content_json,created_at) "
                "SELECT r.session_id,r.id,?,?,? FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=?",
                (message.role.value, encoded, now, run_id, owner_user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("run not found")
            message_id = cursor.lastrowid
            db.execute("UPDATE sessions SET updated_at=? WHERE id=(SELECT session_id FROM runs WHERE id=?)", (now, run_id))
        assert message_id is not None
        return MessageRecord(message_id, self._session_id(run_id), run_id, message, now)

    def list_messages(self, owner_user_id: str, session_id: str, *, after: int | None = None, limit: int = 50) -> Page[MessageRecord]:
        _page_args(after, limit)
        with self._connect() as db:
            rows = db.execute(
                "SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE s.id=? AND s.owner_user_id=? AND m.id>? ORDER BY m.id LIMIT ?",
                (session_id, owner_user_id, after or 0, limit + 1),
            ).fetchall()
        return Page(tuple(_message(row) for row in rows[:limit]),
                    int(rows[limit - 1]["id"]) if len(rows) > limit else None)

    def completed_history(self, owner_user_id: str, session_id: str) -> tuple[Message, ...]:
        """Load canonical messages from completed runs in session order."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT m.* FROM messages m JOIN runs r ON r.id=m.run_id "
                "JOIN sessions s ON s.id=m.session_id WHERE s.id=? AND s.owner_user_id=? "
                "AND r.status='completed' ORDER BY m.id",
                (session_id, owner_user_id),
            ).fetchall()
        return tuple(_message(row).message for row in rows)

    def settle_run(
        self, owner_user_id: str, run_id: str, status: str, stop_reason: str,
        result_summary: JsonObject, messages: tuple[Message, ...],
    ) -> RunRecord:
        """Commit final messages, outcome and the last event as one unit."""
        if status not in TERMINAL:
            raise ValueError("run status must be terminal")
        _required_text(stop_reason, "stop reason")
        encoded_summary = _safe_json(result_summary)
        encoded_messages = tuple((message.role.value, _safe_json(message_to_dict(message)))
                                 for message in messages)
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.session_id FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=? AND r.status IN ('queued','running','cancelling')",
                (run_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise KeyError("active run not found")
            session_id = str(row["session_id"])
            for role, payload in encoded_messages:
                db.execute(
                    "INSERT INTO messages(session_id,run_id,role,content_json,created_at) "
                    "VALUES(?,?,?,?,?)", (session_id, run_id, role, payload, now),
                )
            db.execute(
                "UPDATE runs SET status=?,ended_at=?,stop_reason=?,result_summary_json=? WHERE id=?",
                (status, now, stop_reason, encoded_summary, run_id),
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            reason = ("cancelled" if status == "cancelled" else
                      "limit" if stop_reason == "limit" else "run_failed" if status == "failed" else
                      "run_completed" if status == "completed" else "run_settled")
            self._finish_queued_inputs(db, run_id, reason, now)
            seq = self._next_seq(db, run_id)
            settled = _safe_json({"status": status, "stop_reason": stop_reason,
                                  "result_summary": result_summary})
            db.execute(
                "INSERT INTO events(run_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, seq, "run_settled", settled, now),
            )
        result = self.get_run(owner_user_id, run_id)
        assert result is not None
        return result

    def _finish_queued_inputs(self, db: sqlite3.Connection, run_id: str,
                              reason: str, now: str) -> None:
        rows = db.execute(
            "SELECT request_id FROM control_commands WHERE run_id=? AND status='queued' ORDER BY position",
            (run_id,),
        ).fetchall()
        for row in rows:
            request_id = str(row["request_id"])
            db.execute("UPDATE control_commands SET status='not_consumed',reason=?,updated_at=? "
                       "WHERE run_id=? AND request_id=?", (reason, now, run_id, request_id))
            db.execute("INSERT INTO events(run_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                       (run_id, self._next_seq(db, run_id), "input_not_consumed",
                        _safe_json({"request_id": request_id, "status": "not_consumed", "reason": reason}), now))

    def append_event(self, owner_user_id: str, run_id: str, seq: int, event_type: str, payload: JsonObject) -> EventRecord:
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise ValueError("event sequence must be a positive integer")
        _required_text(event_type, "event type")
        _safe_json({"event_type": event_type})
        encoded, now = _safe_json(payload), _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.id FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=? AND r.status IN "
                "('queued','running','cancelling')",
                (run_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise KeyError("run not found")
            durable_seq = self._next_seq(db, run_id)
            cursor = db.execute(
                "INSERT INTO events(run_id,seq,type,payload_json,created_at) "
                "SELECT r.id,?,?,?,? FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=?",
                (durable_seq, event_type, encoded, now, run_id, owner_user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("run not found")
        return EventRecord(run_id, durable_seq, event_type, _read_json(encoded), now)

    def list_events(self, owner_user_id: str, run_id: str, *, after: int | None = None, limit: int = 50) -> Page[EventRecord]:
        if after is not None and (not isinstance(after, int) or isinstance(after, bool) or after < 0):
            raise ValueError("after_seq must be a nonnegative integer")
        _page_args(None, limit)
        with self._connect() as db:
            db.execute("BEGIN")
            floor = db.execute(
                "SELECT r.replay_floor_seq FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=?", (run_id, owner_user_id),
            ).fetchone()
            if floor is None:
                raise KeyError("run not found")
            if (after or 0) < int(floor["replay_floor_seq"]) - 1:
                raise EventHistoryExpiredError("event history has expired")
            rows = db.execute(
                "SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id "
                "JOIN sessions s ON s.id=r.session_id WHERE e.run_id=? AND s.owner_user_id=? "
                "AND e.seq>? ORDER BY e.seq LIMIT ?",
                (run_id, owner_user_id, after or 0, limit + 1),
            ).fetchall()
        return Page(tuple(_event(row) for row in rows[:limit]),
                    int(rows[limit - 1]["seq"]) if len(rows) > limit else None)

    def prune_events(self, *, now: datetime | None = None) -> int:
        """Discard events of runs settled over 30 days ago; keep snapshots and messages."""
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("now must have a timezone")
        cutoff = (instant.astimezone(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)).isoformat(timespec="microseconds")
        removed = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT r.id,MAX(e.seq) AS last_seq FROM runs r JOIN events e ON e.run_id=r.id "
                "WHERE r.status IN ('completed','needs_input','failed','cancelled','needs_reconciliation') "
                "AND r.ended_at<? GROUP BY r.id", (cutoff,),
            ).fetchall()
            for row in rows:
                db.execute("UPDATE runs SET replay_floor_seq=MAX(replay_floor_seq,?) WHERE id=?",
                           (int(row["last_seq"]) + 1, row["id"]))
                removed += int(db.execute("DELETE FROM events WHERE run_id=?", (row["id"],)).rowcount)
        return removed

    def _session_id(self, run_id: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT session_id FROM runs WHERE id=?", (run_id,)).fetchone()
        assert row is not None
        return cast(str, row["session_id"])


def _page_args(after: int | None, limit: int) -> None:
    if after is not None and (not isinstance(after, int) or isinstance(after, bool) or after <= 0):
        raise ValueError("cursor must be a positive integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def _session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(row["id"], row["owner_user_id"], row["title"],
                         row["created_at"], row["updated_at"])


def _run(row: sqlite3.Row) -> RunRecord:
    summary = row["result_summary_json"]
    return RunRecord(row["id"], row["session_id"], row["owner_user_id"],
                     row["request_id"], row["request_hash"], _read_json(row["request_summary_json"]),
                     row["status"], row["created_at"], row["started_at"], row["ended_at"],
                     row["stop_reason"], _read_json(summary) if summary is not None else None)


def _message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(row["id"], row["session_id"], row["run_id"],
                         message_from_dict(_read_json(row["content_json"])), row["created_at"])


def _event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(row["run_id"], row["seq"], row["type"],
                       _read_json(row["payload_json"]), row["created_at"])


def _control(row: sqlite3.Row) -> ControlRecord:
    return ControlRecord(int(row["position"]), row["run_id"], row["owner_user_id"],
                         row["request_id"], row["kind"], _read_json(row["request_json"]),
                         row["status"], row["reason"], row["created_at"], row["updated_at"])
