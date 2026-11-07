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
from ktg_agent.context.window import group_messages

T = TypeVar("T")
ACTIVE = frozenset({"queued", "running", "cancelling"})
TERMINAL = frozenset({
    "completed", "needs_input", "failed", "cancelled", "needs_reconciliation",
})
EVENT_RETENTION_DAYS = 30
SCHEMA_VERSION = 5
SUMMARY_ACTIVE = "active"
SUMMARY_SUPERSEDED = "superseded"
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
    budget: JsonObject | None = None
    usage: JsonObject | None = None
    model_requests: tuple[JsonObject, ...] = ()
    kind: str = "user"
    context: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    session_id: str
    run_id: str
    message: Message
    created_at: str


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    summary_id: str
    session_id: str
    owner_user_id: str
    run_id: str
    covers_start_message_id: int
    covers_end_message_id: int
    source_message_count: int
    source_token_estimate: int
    summary_text: str
    summary_token_estimate: int
    model: str
    call_id: str
    usage: JsonObject | None
    status: str
    superseded_by: str | None
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


def _safe_text(value: str, label: str) -> str:
    _required_text(value, label)
    if _SECRET_KEY.search(value) or _SECRET_VALUE.search(value):
        raise ValueError("sensitive data must not be stored")
    return value


def _read_json(raw: str) -> JsonObject:
    return _json_object(json.loads(raw), "stored JSON")


def _empty_usage() -> JsonObject:
    return {"known_input_tokens": 0, "known_output_tokens": 0,
            "known_cached_input_tokens": 0, "known_total_tokens": 0,
            "unknown_request_count": 0, "complete": True}


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
            if version > SCHEMA_VERSION:
                raise RuntimeError("session database has a newer schema version")
            if version == SCHEMA_VERSION:
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
                version = 3
            if version == 3:
                db.executescript("""BEGIN IMMEDIATE;
                    ALTER TABLE runs ADD COLUMN budget_json TEXT;
                    ALTER TABLE runs ADD COLUMN usage_json TEXT;
                    CREATE TABLE model_requests (
                        run_id TEXT NOT NULL REFERENCES runs(id),
                        call_id TEXT NOT NULL,
                        turn INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        request_started INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        usage_source TEXT NOT NULL,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        cached_input_tokens INTEGER,
                        total_tokens INTEGER,
                        PRIMARY KEY(run_id,call_id)
                    );
                    PRAGMA user_version=4;
                    COMMIT;
                """)
                version = 4
            if version == 4:
                db.executescript("""BEGIN IMMEDIATE;
                    ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'user';
                    ALTER TABLE runs ADD COLUMN context_json TEXT;
                    UPDATE runs SET kind='compaction' WHERE kind='user'
                        AND json_extract(request_summary_json,'$.kind')='compaction';
                    CREATE TABLE context_summaries (
                        summary_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        owner_user_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        covers_start_message_id INTEGER NOT NULL,
                        covers_end_message_id INTEGER NOT NULL,
                        source_message_count INTEGER NOT NULL,
                        source_token_estimate INTEGER NOT NULL,
                        summary_text TEXT NOT NULL,
                        summary_token_estimate INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        usage_json TEXT,
                        status TEXT NOT NULL,
                        superseded_by TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX one_active_summary_per_session
                        ON context_summaries(session_id) WHERE status='active';
                    CREATE UNIQUE INDEX one_summary_per_call
                        ON context_summaries(session_id, call_id);
                    PRAGMA user_version=5;
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
            title = self._title(title)
        session_id, now = str(uuid4()), _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id,owner_user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (session_id, owner_user_id, title, now, now),
            )
        return SessionRecord(session_id, owner_user_id, title, now, now)

    @staticmethod
    def _title(title: str) -> str:
        title = _required_text(title, "title").strip()
        if not title or len(title) > 200:
            raise ValueError("会话名称须为 1 至 200 个字符。")
        _safe_json({"title": title})
        return title

    def rename_session(self, owner_user_id: str, session_id: str, title: str) -> SessionRecord:
        title = self._title(title)
        with self._connect() as db:
            changed = db.execute(
                "UPDATE sessions SET title=?,updated_at=? WHERE id=? AND owner_user_id=?",
                (title, _now(), session_id, owner_user_id),
            )
            if not changed.rowcount:
                raise KeyError("session not found")
            row = db.execute("SELECT * FROM sessions WHERE id=? AND owner_user_id=?",
                             (session_id, owner_user_id)).fetchone()
            return _session(row)

    def get_session(self, owner_user_id: str, session_id: str) -> SessionRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id=? AND owner_user_id=?",
                (session_id, owner_user_id),
            ).fetchone()
        return _session(row) if row else None

    def session_stats(self, owner_user_id: str, session_id: str) -> JsonObject:
        """Read one owner-scoped, transactionally consistent session snapshot."""
        with self._connect() as db:
            db.execute("BEGIN")
            session = db.execute(
                "SELECT id FROM sessions WHERE id=? AND owner_user_id=?",
                (session_id, owner_user_id),
            ).fetchone()
            if session is None:
                raise KeyError("session not found")
            roles = {str(row["role"]): int(row["n"]) for row in db.execute(
                "SELECT role,COUNT(*) n FROM messages WHERE session_id=? GROUP BY role",
                (session_id,),
            )}
            runs = db.execute(
                "SELECT id,status,kind FROM runs WHERE session_id=? ORDER BY position",
                (session_id,),
            ).fetchall()
            statuses: dict[str, int] = {}
            kinds: dict[str, int] = {}
            by_kind: dict[str, list[JsonObject]] = {}
            for run in runs:
                status, kind = str(run["status"]), str(run["kind"])
                statuses[status] = statuses.get(status, 0) + 1
                kinds[kind] = kinds.get(kind, 0) + 1
                by_kind.setdefault(kind, []).extend(self._model_requests(db, str(run["id"])))
            summary_counts = {str(row["status"]): int(row["n"]) for row in db.execute(
                "SELECT status,COUNT(*) n FROM context_summaries WHERE session_id=? GROUP BY status",
                (session_id,),
            )}
            active_summary = db.execute(
                "SELECT summary_id,covers_start_message_id,covers_end_message_id,"
                "summary_token_estimate FROM context_summaries "
                "WHERE session_id=? AND status='active'", (session_id,),
            ).fetchone()
            active_run = db.execute(
                "SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running','cancelling')",
                (session_id,),
            ).fetchone()
            # Compaction runs never carry user spill counts; their context snapshot
            # must not overwrite the last user-run assembly.
            last_context = db.execute(
                "SELECT context_json FROM runs WHERE session_id=? AND kind='user' "
                "AND context_json IS NOT NULL ORDER BY position DESC LIMIT 1", (session_id,),
            ).fetchone()
        requests = tuple(request for group in by_kind.values() for request in group)
        return {
            "session_id": session_id, "message_count": sum(roles.values()),
            "messages_by_role": cast(JsonObject, roles), "run_count": len(runs),
            "runs_by_status": cast(JsonObject, statuses),
            "runs_by_kind": cast(JsonObject, kinds),
            "summary_counts": cast(JsonObject, summary_counts),
            "compaction_count": kinds.get("compaction", 0),
            "known_usage": self._usage_from_requests(requests),
            "known_usage_by_kind": {kind: self._usage_from_requests(tuple(items))
                                    for kind, items in by_kind.items()},
            "active_summary": ({key: active_summary[key] for key in active_summary.keys()}
                               if active_summary else None),
            "active_run_id": str(active_run["id"]) if active_run else None,
            "last_context": _read_json(last_context["context_json"]) if last_context else None,
        }

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
        request_hash: str, request_summary: JsonObject, *, kind: str = "user",
    ) -> RunRecord:
        _required_text(request_id, "request id")
        _required_text(request_hash, "request hash")
        _safe_json({"request_id": request_id, "request_hash": request_hash})
        summary = _safe_json(request_summary)
        if kind not in {"user", "compaction"}:
            raise ValueError("invalid run kind")
        run_id, now = str(uuid4()), _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO runs(id,session_id,owner_user_id,request_id,request_hash,"
                "request_summary_json,status,created_at,usage_json,kind) "
                "SELECT ?,s.id,s.owner_user_id,?,?,?,'queued',?,?,? FROM sessions s "
                "WHERE s.id=? AND s.owner_user_id=?",
                (run_id, request_id, request_hash, summary, now, _safe_json(_empty_usage()), kind,
                 session_id, owner_user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("session not found")
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        return self.get_run(owner_user_id, run_id)  # type: ignore[return-value]

    def admit_run(
        self, owner_user_id: str, session_id: str, request_id: str,
        request_hash: str, request_summary: JsonObject, *, kind: str = "user",
    ) -> tuple[RunRecord, bool]:
        """Atomically return an idempotent run or reserve the session for one new run."""
        _required_text(request_id, "request id")
        _required_text(request_hash, "request hash")
        _safe_json({"request_id": request_id, "request_hash": request_hash})
        summary = _safe_json(request_summary)
        if kind not in {"user", "compaction"}:
            raise ValueError("invalid run kind")
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
                "request_summary_json,status,created_at,usage_json,kind) VALUES(?,?,?,?,?,?,'queued',?,?,?)",
                (run_id, session_id, owner_user_id, request_id, request_hash, summary, now,
                 _safe_json(_empty_usage()), kind),
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        created = self.get_run(owner_user_id, run_id)
        assert created is not None
        return created, True

    def find_run_by_request(self, owner_user_id: str, session_id: str,
                            request_id: str) -> RunRecord | None:
        """Read one idempotent run by request id without admitting or mutating state."""
        _required_text(request_id, "request id")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE owner_user_id=? AND session_id=? AND request_id=?",
                (owner_user_id, session_id, request_id),
            ).fetchone()
        return _run(row) if row else None

    def get_run(self, owner_user_id: str, run_id: str) -> RunRecord | None:
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=?", (run_id, owner_user_id),
            ).fetchone()
            requests = self._model_requests(db, run_id) if row else ()
        return _run(row, requests) if row else None

    @staticmethod
    def _model_requests(db: sqlite3.Connection, run_id: str) -> tuple[JsonObject, ...]:
        rows = db.execute("SELECT * FROM model_requests WHERE run_id=? ORDER BY turn", (run_id,)).fetchall()
        return tuple({"call_id": row["call_id"], "turn": row["turn"], "model": row["model"],
                      "request_started": bool(row["request_started"]), "status": row["status"],
                      "usage_source": row["usage_source"], "input_tokens": row["input_tokens"],
                      "output_tokens": row["output_tokens"],
                      "cached_input_tokens": row["cached_input_tokens"],
                      "total_tokens": row["total_tokens"]} for row in rows)

    @staticmethod
    def _usage_from_requests(requests: tuple[JsonObject, ...]) -> JsonObject:
        result = _empty_usage()
        for request in requests:
            if request["usage_source"] == "provider_reported":
                for target, source in (("known_input_tokens", "input_tokens"),
                                       ("known_output_tokens", "output_tokens"),
                                       ("known_cached_input_tokens", "cached_input_tokens"),
                                       ("known_total_tokens", "total_tokens")):
                    result[target] = cast(int, result[target]) + cast(int, request[source])
            elif request["usage_source"] == "unknown":
                result["unknown_request_count"] = cast(int, result["unknown_request_count"]) + 1
                result["complete"] = False
        return result

    def set_run_budget(self, owner: str, run_id: str, budget: JsonObject) -> None:
        encoded = _safe_json(budget)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE runs SET budget_json=?,usage_json=COALESCE(usage_json,?) "
                "WHERE id=? AND owner_user_id=? AND status='queued' AND budget_json IS NULL",
                (encoded, _safe_json(_empty_usage()), run_id, owner),
            )
            if cursor.rowcount != 1:
                raise KeyError("queued run not found")

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
                    now = _now()
                    cursor = db.execute(
                        "INSERT INTO control_commands(run_id,owner_user_id,request_id,kind,request_json,"
                        "status,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, owner, request_id, kind, encoded, "accepted", "already_settled", now, now),
                    )
                    row = db.execute("SELECT * FROM control_commands WHERE position=?",
                                     (cursor.lastrowid,)).fetchone()
                    assert row is not None
                    return _control(row), True
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
            else:
                payload["status"] = "cancelling"
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
                requests = self._model_requests(db, str(row["id"]))
                usage = self._usage_from_requests(requests) if requests else None
                snapshot = db.execute("SELECT budget_json,usage_json,kind,context_json FROM runs WHERE id=?",
                                      (row["id"],)).fetchone()
                assert snapshot is not None
                budget = _read_json(snapshot["budget_json"]) if snapshot["budget_json"] else None
                if snapshot["usage_json"] is not None:
                    usage = _read_json(snapshot["usage_json"])
                elif usage is None:
                    usage = {"source": "legacy_unknown", "complete": False}
                db.execute(
                    "UPDATE runs SET status=?,ended_at=?,stop_reason=?,result_summary_json=? WHERE id=?",
                    (status, now, "SERVICE_RESTARTED", _safe_json(summary), row["id"]),
                )
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, row["session_id"]))
                self._finish_queued_inputs(db, str(row["id"]), "process_interrupted", now)
                seq = self._next_seq(db, str(row["id"]))
                payload = _safe_json({"status": status, "stop_reason": "SERVICE_RESTARTED",
                                      "result_summary": summary, "budget": budget,
                                      "usage": usage, "model_requests": list(requests),
                                      "kind": snapshot["kind"],
                                      "context": (_read_json(snapshot["context_json"])
                                                  if snapshot["context_json"] else None)})
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

    def completed_message_records(self, owner_user_id: str, session_id: str) -> tuple[MessageRecord, ...]:
        """Load canonical messages with stable IDs from completed runs."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT m.* FROM messages m JOIN runs r ON r.id=m.run_id "
                "JOIN sessions s ON s.id=m.session_id WHERE s.id=? AND s.owner_user_id=? "
                "AND r.status='completed' ORDER BY m.id",
                (session_id, owner_user_id),
            ).fetchall()
        return tuple(_message(row) for row in rows)

    def completed_history(self, owner_user_id: str, session_id: str) -> tuple[Message, ...]:
        return tuple(record.message for record in self.completed_message_records(owner_user_id, session_id))

    def active_summary(self, owner_user_id: str, session_id: str) -> SummaryRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT c.* FROM context_summaries c JOIN sessions s ON s.id=c.session_id "
                "WHERE s.id=? AND s.owner_user_id=? AND c.status='active'",
                (session_id, owner_user_id),
            ).fetchone()
        return _summary(row) if row else None

    def list_summaries(self, owner_user_id: str, session_id: str) -> tuple[SummaryRecord, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.* FROM context_summaries c JOIN sessions s ON s.id=c.session_id "
                "WHERE s.id=? AND s.owner_user_id=? ORDER BY c.created_at,c.rowid",
                (session_id, owner_user_id),
            ).fetchall()
        return tuple(_summary(row) for row in rows)

    def commit_summary(
        self, owner_user_id: str, session_id: str, *, run_id: str,
        covers_start_message_id: int, covers_end_message_id: int,
        source_message_count: int, source_token_estimate: int,
        summary_text: str, summary_token_estimate: int, model: str,
        call_id: str, usage: JsonObject | None,
    ) -> SummaryRecord:
        call_id = _required_text(call_id, "call id")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT 1 FROM sessions WHERE id=? AND owner_user_id=?",
                                 (session_id, owner_user_id)).fetchone()
            if session is None:
                raise KeyError("session not found")
            prior = db.execute("SELECT * FROM context_summaries WHERE session_id=? AND call_id=?",
                               (session_id, call_id)).fetchone()
            if prior is not None:
                return _summary(prior)
            text = _safe_text(summary_text, "summary text")
            model = _safe_text(model, "model")
            encoded_usage = _safe_json(usage) if usage is not None else None
            run = db.execute("SELECT 1 FROM runs WHERE id=? AND session_id=? AND kind='compaction'",
                             (run_id, session_id)).fetchone()
            if run is None:
                raise ValueError("run must be a compaction run in this session")
            if (type(covers_start_message_id) is not int or type(covers_end_message_id) is not int
                    or covers_start_message_id > covers_end_message_id or source_message_count < 1):
                raise ValueError("invalid summary coverage")
            endpoints = db.execute(
                "SELECT m.id FROM messages m JOIN runs r ON r.id=m.run_id "
                "WHERE m.session_id=? AND r.status='completed' AND m.id IN (?,?)",
                (session_id, covers_start_message_id, covers_end_message_id),
            ).fetchall()
            if {int(row["id"]) for row in endpoints} != {covers_start_message_id, covers_end_message_id}:
                raise ValueError("summary endpoints must be completed session messages")
            covered_rows = db.execute(
                "SELECT m.* FROM messages m JOIN runs r ON r.id=m.run_id "
                "WHERE m.session_id=? AND r.status='completed' AND m.id BETWEEN ? AND ? "
                "ORDER BY m.id",
                (session_id, covers_start_message_id, covers_end_message_id),
            ).fetchall()
            all_count = db.execute(
                "SELECT count(*) FROM messages WHERE session_id=? AND id BETWEEN ? AND ?",
                (session_id, covers_start_message_id, covers_end_message_id),
            ).fetchone()[0]
            if len(covered_rows) != all_count:
                raise ValueError("summary coverage includes incomplete run messages")
            try:
                group_messages(tuple(_message(row).message for row in covered_rows))
            except ValueError as error:
                raise ValueError("summary coverage splits a tool group") from error
            if source_token_estimate <= summary_token_estimate:
                raise ValueError("SUMMARY_NOT_SMALLER")
            summary_id, now = str(uuid4()), _now()
            db.execute("UPDATE context_summaries SET status=?,superseded_by=? "
                       "WHERE session_id=? AND status=?",
                       (SUMMARY_SUPERSEDED, summary_id, session_id, SUMMARY_ACTIVE))
            db.execute(
                "INSERT INTO context_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (summary_id, session_id, owner_user_id, run_id, covers_start_message_id,
                 covers_end_message_id, source_message_count, source_token_estimate, text,
                 summary_token_estimate, model, call_id, encoded_usage, SUMMARY_ACTIVE, None, now),
            )
            row = db.execute("SELECT * FROM context_summaries WHERE summary_id=?", (summary_id,)).fetchone()
            assert row is not None
            return _summary(row)

    def settle_run(
        self, owner_user_id: str, run_id: str, status: str, stop_reason: str,
        result_summary: JsonObject, messages: tuple[Message, ...], *,
        context: JsonObject | None = None,
    ) -> RunRecord:
        """Commit final messages, outcome and the last event as one unit."""
        if status not in TERMINAL:
            raise ValueError("run status must be terminal")
        _required_text(stop_reason, "stop reason")
        encoded_summary = _safe_json(result_summary)
        encoded_context = _safe_json(context) if context is not None else None
        encoded_messages = tuple((message.role.value, _safe_json(message_to_dict(message)))
                                 for message in messages)
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.session_id,r.status FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.owner_user_id=? AND r.status IN ('queued','running','cancelling')",
                (run_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise KeyError("active run not found")
            session_id = str(row["session_id"])
            if row["status"] == "cancelling" and status == "completed":
                status = "cancelled"
                stop_reason = "cancelled"
                result_summary = {**result_summary, "stop_reason": "cancelled"}
                encoded_summary = _safe_json(result_summary)
            for role, payload in encoded_messages:
                db.execute(
                    "INSERT INTO messages(session_id,run_id,role,content_json,created_at) "
                    "VALUES(?,?,?,?,?)", (session_id, run_id, role, payload, now),
                )
            db.execute(
                "UPDATE runs SET status=?,ended_at=?,stop_reason=?,result_summary_json=?,context_json=? WHERE id=?",
                (status, now, stop_reason, encoded_summary, encoded_context, run_id),
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            reason = ("cancelled" if status == "cancelled" else
                      "limit" if stop_reason == "limit" else "run_failed" if status == "failed" else
                      "run_completed" if status == "completed" else "run_settled")
            self._finish_queued_inputs(db, run_id, reason, now)
            snapshot = db.execute("SELECT budget_json,usage_json,kind FROM runs WHERE id=?", (run_id,)).fetchone()
            assert snapshot is not None
            budget = _read_json(snapshot["budget_json"]) if snapshot["budget_json"] else None
            usage = (_read_json(snapshot["usage_json"]) if snapshot["usage_json"] else
                     {"source": "legacy_unknown", "complete": False})
            requests = self._model_requests(db, run_id)
            seq = self._next_seq(db, run_id)
            settled = _safe_json({"status": status, "stop_reason": stop_reason,
                                  "result_summary": result_summary, "budget": budget,
                                  "usage": usage, "model_requests": list(requests),
                                  "kind": snapshot["kind"], "context": context})
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
            if event_type == "model_request_started":
                db.execute(
                    "INSERT INTO model_requests(run_id,call_id,turn,model,request_started,status,usage_source) "
                    "VALUES(?,?,?,?,1,'running','unknown')",
                    (run_id, payload["call_id"], payload["turn"], payload["model"]),
                )
            elif event_type == "model_usage_reported":
                cursor = db.execute(
                    "UPDATE model_requests SET usage_source='provider_reported',"
                    "input_tokens=?,output_tokens=?,cached_input_tokens=?,total_tokens=? "
                    "WHERE run_id=? AND call_id=? AND request_started=1",
                    (payload["input_tokens"], payload["output_tokens"],
                     payload["cached_input_tokens"],
                     cast(int, payload["input_tokens"]) + cast(int, payload["output_tokens"]),
                     run_id, payload["call_id"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("usage without started request")
            elif event_type == "model_request_settled":
                cursor = db.execute(
                    "UPDATE model_requests SET status=? WHERE run_id=? AND call_id=?",
                    (payload["status"], run_id, payload["call_id"]),
                )
                if cursor.rowcount == 0:
                    if payload["request_started"]:
                        raise ValueError("started request was not recorded")
                    db.execute(
                        "INSERT INTO model_requests(run_id,call_id,turn,model,request_started,status,"
                        "usage_source,input_tokens,output_tokens,cached_input_tokens,total_tokens) "
                        "VALUES(?,?,?,?,0,?,'not_started',0,0,0,0)",
                        (run_id, payload["call_id"], payload["turn"], payload["model"], payload["status"]),
                    )
            if event_type in {"model_request_started", "model_usage_reported", "model_request_settled"}:
                requests = self._model_requests(db, run_id)
                db.execute("UPDATE runs SET usage_json=? WHERE id=?",
                           (_safe_json(self._usage_from_requests(requests)), run_id))
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


def _run(row: sqlite3.Row, requests: tuple[JsonObject, ...] = ()) -> RunRecord:
    summary = row["result_summary_json"]
    return RunRecord(row["id"], row["session_id"], row["owner_user_id"],
                     row["request_id"], row["request_hash"], _read_json(row["request_summary_json"]),
                     row["status"], row["created_at"], row["started_at"], row["ended_at"],
                     row["stop_reason"], _read_json(summary) if summary is not None else None,
                     _read_json(row["budget_json"]) if row["budget_json"] is not None else None,
                     _read_json(row["usage_json"]) if row["usage_json"] is not None else
                     {"source": "legacy_unknown", "complete": False},
                     requests, row["kind"],
                     _read_json(row["context_json"]) if row["context_json"] is not None else None)


def _summary(row: sqlite3.Row) -> SummaryRecord:
    return SummaryRecord(
        row["summary_id"], row["session_id"], row["owner_user_id"], row["run_id"],
        row["covers_start_message_id"], row["covers_end_message_id"],
        row["source_message_count"], row["source_token_estimate"], row["summary_text"],
        row["summary_token_estimate"], row["model"], row["call_id"],
        _read_json(row["usage_json"]) if row["usage_json"] is not None else None,
        row["status"], row["superseded_by"], row["created_at"],
    )


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
