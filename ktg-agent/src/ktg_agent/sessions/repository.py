"""Owner-scoped application state over the unified native SQLite file.

The framework owns conversation history and step snapshots. Application writes
use one connection per short transaction and never persist SSE deltas.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar, cast
from uuid import uuid4

from pydantic_ai.messages import (ModelMessage, ModelMessagesTypeAdapter, ModelResponse,
                                   RetryPromptPart, ToolReturnPart, UserPromptPart)
from pydantic_ai.usage import RunUsage

from ktg_agent.core.messages import JsonObject, JsonValue, _json_object, _required_text
from ktg_agent.sessions.database import (connect, database_path, initialize_application,
                                         initialize_framework_stores)

T = TypeVar('T')
ACTIVE = frozenset({'queued', 'running', 'cancelling'})
TERMINAL = frozenset({'completed', 'needs_input', 'failed', 'cancelled', 'needs_reconciliation'})
STATUSES = ACTIVE | TERMINAL
_SECRET_KEY = re.compile(
    r'(?:authorization|api[_-]?key|access[_-]?token|bearer[_-]?token|secret|'
    r'headers?|cookies?|traceback|stack[_-]?trace|exception[_-]?stack)', re.I,
)
_SECRET_VALUE = re.compile(
    r'\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}\b|\bapi[_ -]?key\s*[:=]\s*\S+|'
    r'Traceback \(most recent call last\):|\bFile "[^\"]+", line \d+', re.I,
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
    usage: JsonObject | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    session_id: str
    run_id: str | None
    message: ModelMessage
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
    code = 'REQUEST_ID_CONFLICT'


class RunBusyError(ValueError):
    code = 'RUN_BUSY'


class ControlConflictError(ValueError):
    code = 'REQUEST_ID_CONFLICT'


class ControlStateError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _safe_json(value: object) -> str:
    data = _json_object(value, 'stored JSON')

    def check(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if _SECRET_KEY.search(key):
                    raise ValueError('sensitive data must not be stored')
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, str) and _SECRET_VALUE.search(item):
            raise ValueError('sensitive data must not be stored')

    check(data)
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _read_json(raw: str) -> JsonObject:
    return _json_object(json.loads(raw), 'stored JSON')


def _usage(value: RunUsage, *, complete: bool = False) -> JsonObject:
    # A supplier may omit usage. Known token totals are lower bounds, while
    # entirely unmeasured totals are unknown rather than reported as zero.
    all_unknown = value.unmeasured_requests > 0 and value.unmeasured_requests >= value.requests

    def known(count: int) -> int | None:
        return None if count == 0 and all_unknown else count

    return {
        'requests': value.requests, 'tool_calls': value.tool_calls,
        'input_tokens': known(value.input_tokens), 'output_tokens': known(value.output_tokens),
        'cache_read_tokens': known(value.cache_read_tokens), 'cache_write_tokens': known(value.cache_write_tokens),
        'known_total_tokens': known(value.total_tokens), 'unmeasured_requests': value.unmeasured_requests,
        'complete': complete and value.unmeasured_requests == 0,
    }


def _aggregate_usage(usages: list[JsonObject]) -> JsonObject:
    def sum_known(key: str) -> int | None:
        numbers = [v for usage in usages if isinstance(v := usage.get(key), int) and not isinstance(v, bool)]
        return sum(numbers) if numbers or not usages else None

    return {'known_input_tokens': sum_known('input_tokens'), 'known_output_tokens': sum_known('output_tokens'),
            'known_cached_input_tokens': sum_known('cache_read_tokens'),
            'known_total_tokens': sum_known('known_total_tokens'),
            'unknown_request_count': sum_known('unmeasured_requests'),
            'requests': sum_known('requests'), 'tool_calls': sum_known('tool_calls'),
            'complete': all(item.get('complete') is True for item in usages)}


def _native_usage_total(usages: list[JsonObject]) -> RunUsage:
    """Rebuild the official conversation header's basic aggregate from Runs."""
    def sum_field(key: str) -> int:
        return sum(value for item in usages
                   if isinstance(value := item.get(key), int) and not isinstance(value, bool))

    return RunUsage(requests=sum_field('requests'), tool_calls=sum_field('tool_calls'),
                    input_tokens=sum_field('input_tokens'), output_tokens=sum_field('output_tokens'),
                    cache_read_tokens=sum_field('cache_read_tokens'), cache_write_tokens=sum_field('cache_write_tokens'),
                    unmeasured_requests=sum_field('unmeasured_requests'))


def _page_args(after: int | None, limit: int) -> None:
    if after is not None and (not isinstance(after, int) or isinstance(after, bool) or after <= 0):
        raise ValueError('cursor must be a positive integer')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')


def _session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(row['id'], row['owner_user_id'], row['title'], row['created_at'], row['updated_at'])


def _run(row: sqlite3.Row) -> RunRecord:
    summary = row['result_summary_json']
    return RunRecord(row['id'], row['session_id'], row['owner_user_id'], row['request_id'], row['request_hash'],
                     _read_json(row['request_summary_json']), row['status'], row['created_at'], row['started_at'],
                     row['ended_at'], row['stop_reason'], _read_json(summary) if summary is not None else None,
                     _read_json(row['usage_json']), row['task_id'])


def _control(row: sqlite3.Row) -> ControlRecord:
    return ControlRecord(row['position'], row['run_id'], row['owner_user_id'], row['request_id'], row['kind'],
                         _read_json(row['request_json']), row['status'], row['reason'], row['created_at'], row['updated_at'])


def _message(row: sqlite3.Row) -> MessageRecord:
    messages = ModelMessagesTypeAdapter.validate_json(row['message_json'])
    if len(messages) != 1:
        raise ValueError('invalid conversation entry payload')
    return MessageRecord(row['position'], row['session_id'], row['run_id'], messages[0], row['created_at'])


def _message_role(message: ModelMessage) -> str:
    if isinstance(message, ModelResponse):
        return 'assistant'
    if any(isinstance(part, UserPromptPart) for part in message.parts):
        return 'user'
    if any(isinstance(part, (ToolReturnPart, RetryPromptPart)) for part in message.parts):
        return 'tool'
    return 'system'


# The head chain is authoritative, including a native branch sharing ancestry.
# Unreachable entries are never shown as the current conversation.
_CHAIN = """WITH RECURSIVE chain(id) AS (
    SELECT head_id FROM app_sessions WHERE id=? AND owner_user_id=?
    UNION
    SELECT e.parent_id FROM app_conversation_entries e JOIN chain c ON c.id=e.id
    WHERE e.parent_id IS NOT NULL
) """


class SessionRepository:
    """Single application repository; callers supply the verified owner ID."""

    def __init__(self, path: str | Path) -> None:
        self.path = database_path(path)
        initialize_application(self.path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with connect(self.path) as db:
            yield db

    # The official schema initializer is async (its methods use worker threads).
    async def initialize_framework_stores(self) -> None:
        await initialize_framework_stores(self.path)

    def __enter__(self) -> SessionRepository:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    @staticmethod
    def _title(title: str) -> str:
        title = _required_text(title, 'title').strip()
        if not title or len(title) > 200:
            raise ValueError('会话名称须为 1 至 200 个字符。')
        _safe_json({'title': title})
        return title

    def create_session(self, owner_user_id: str, *, title: str | None = None) -> SessionRecord:
        owner_user_id = _required_text(owner_user_id, 'owner user id')
        _safe_json({'owner_user_id': owner_user_id})
        if title is not None:
            title = self._title(title)
        session_id, now = str(uuid4()), _now()
        with self.connect() as db:
            db.execute('INSERT INTO app_sessions(id,owner_user_id,title,created_at,updated_at,revision) '
                       'VALUES(?,?,?,?,?,?)', (session_id, owner_user_id, title, now, now, str(uuid4())))
        return SessionRecord(session_id, owner_user_id, title, now, now)

    def rename_session(self, owner_user_id: str, session_id: str, title: str) -> SessionRecord:
        title = self._title(title)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE app_sessions SET title=?,updated_at=? WHERE id=? AND owner_user_id=?',
                                 (title, _now(), session_id, owner_user_id))
            if changed.rowcount != 1:
                raise KeyError('session not found')
            row = db.execute('SELECT * FROM app_sessions WHERE id=?', (session_id,)).fetchone()
            return _session(row)

    def get_session(self, owner_user_id: str, session_id: str) -> SessionRecord | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM app_sessions WHERE id=? AND owner_user_id=?',
                             (session_id, owner_user_id)).fetchone()
            return _session(row) if row else None

    def list_sessions(self, owner_user_id: str, *, after: int | None = None, limit: int = 50) -> Page[SessionRecord]:
        _page_args(after, limit)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM app_sessions WHERE owner_user_id=? AND (? IS NULL OR position<?) '
                              'ORDER BY position DESC LIMIT ?', (owner_user_id, after, after, limit+1)).fetchall()
        return Page(tuple(_session(row) for row in rows[:limit]),
                    int(rows[limit-1]['position']) if len(rows) > limit else None)

    @staticmethod
    def _owned_run(db: sqlite3.Connection, owner: str, run_id: str) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, db.execute('SELECT r.* FROM app_runs r JOIN app_sessions s ON s.id=r.session_id '
            'WHERE r.id=? AND r.owner_user_id=? AND s.owner_user_id=?', (run_id, owner, owner)).fetchone())

    def admit_run(self, owner_user_id: str, session_id: str, request_id: str,
                  request_hash: str, request_summary: JsonObject, *, kind: str = 'user') -> tuple[RunRecord, bool]:
        _required_text(request_id, 'request id')
        _required_text(request_hash, 'request hash')
        _safe_json({'request_id': request_id, 'request_hash': request_hash})
        summary = _safe_json(request_summary)
        if kind != 'user':
            raise ValueError('only user runs are supported')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM app_sessions WHERE id=? AND owner_user_id=?',
                              (session_id, owner_user_id)).fetchone():
                raise KeyError('session not found')
            existing = db.execute('SELECT * FROM app_runs WHERE owner_user_id=? AND session_id=? AND request_id=?',
                                  (owner_user_id, session_id, request_id)).fetchone()
            if existing is not None:
                run = _run(existing)
                if run.request_hash != request_hash:
                    raise RunConflictError('request_id 已用于不同请求。')
                return run, False
            if db.execute("SELECT 1 FROM app_runs WHERE session_id=? AND status IN ('queued','running','cancelling')",
                          (session_id,)).fetchone() is not None:
                raise RunBusyError('会话已有活动运行。')
            run_id, now = str(uuid4()), _now()
            task_id = request_summary.get('task_id')
            if task_id is None:
                task_id = run_id
            else:
                if not isinstance(task_id, str) or not task_id or not db.execute(
                    'SELECT 1 FROM app_runs WHERE owner_user_id=? AND session_id=? AND task_id=?',
                    (owner_user_id, session_id, task_id)).fetchone():
                    raise KeyError('task not found')
            db.execute('INSERT INTO app_runs(id,session_id,owner_user_id,task_id,request_id,request_hash,'
                       'request_summary_json,status,created_at,usage_json) VALUES(?,?,?,?,?,?,?,\'queued\',?,?)',
                       (run_id, session_id, owner_user_id, task_id, request_id, request_hash, summary, now,
                        _safe_json(_usage(RunUsage()))))
            db.execute('UPDATE app_sessions SET updated_at=? WHERE id=?', (now, session_id))
            row = self._owned_run(db, owner_user_id, run_id)
            assert row is not None
            return _run(row), True

    def create_run(self, owner_user_id: str, session_id: str, request_id: str,
                   request_hash: str, request_summary: JsonObject, *, kind: str = 'user') -> RunRecord:
        return self.admit_run(owner_user_id, session_id, request_id, request_hash, request_summary, kind=kind)[0]

    def find_run_by_request(self, owner_user_id: str, session_id: str, request_id: str) -> RunRecord | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM app_runs WHERE owner_user_id=? AND session_id=? AND request_id=?',
                             (owner_user_id, session_id, request_id)).fetchone()
            return _run(row) if row else None

    def get_run(self, owner_user_id: str, run_id: str) -> RunRecord | None:
        with self.connect() as db:
            row = self._owned_run(db, owner_user_id, run_id)
            return _run(row) if row else None

    def list_runs(self, owner_user_id: str, session_id: str, *, after: int | None = None,
                  limit: int = 50) -> Page[RunRecord]:
        _page_args(after, limit)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM app_runs WHERE owner_user_id=? AND session_id=? AND position>? '
                              'ORDER BY position LIMIT ?', (owner_user_id, session_id, after or 0, limit+1)).fetchall()
        return Page(tuple(_run(row) for row in rows[:limit]),
                    int(rows[limit-1]['position']) if len(rows) > limit else None)

    def get_active_run(self, owner_user_id: str, session_id: str) -> RunRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM app_runs WHERE owner_user_id=? AND session_id=? AND "
                             "status IN ('queued','running','cancelling')", (owner_user_id, session_id)).fetchone()
            return _run(row) if row else None

    def get_task_prior_run(self, owner: str, session_id: str, task_id: str, exclude_run_id: str) -> RunRecord | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM app_runs WHERE owner_user_id=? AND session_id=? AND task_id=? '
                             'AND id<>? AND ended_at IS NOT NULL ORDER BY position DESC LIMIT 1',
                             (owner, session_id, task_id, exclude_run_id)).fetchone()
            return _run(row) if row else None

    def get_run_context(self, owner: str, run_id: str) -> tuple[ModelMessage, ...] | None:
        with self.connect() as db:
            row = self._owned_run(db, owner, run_id)
            if row is None:
                raise KeyError('run not found')
            payload = row['context_json']
        return tuple(ModelMessagesTypeAdapter.validate_json(payload)) if payload is not None else None

    def get_latest_run_context(self, owner: str, session_id: str,
                               exclude_run_id: str | None = None) -> tuple[ModelMessage, ...] | None:
        with self.connect() as db:
            row = db.execute('SELECT context_json FROM app_runs WHERE owner_user_id=? AND session_id=? '
                             'AND context_json IS NOT NULL AND ended_at IS NOT NULL AND (? IS NULL OR id<>?) '
                             "AND COALESCE(stop_reason,'')<>'history_alignment_failed' "
                             'ORDER BY position DESC LIMIT 1', (owner, session_id, exclude_run_id, exclude_run_id)).fetchone()
        return tuple(ModelMessagesTypeAdapter.validate_json(row[0])) if row is not None else None

    def load_conversation(self, owner: str, session_id: str) -> tuple[ModelMessage, ...]:
        from ktg_agent.sessions.conversation_store import SQLiteConversationStore
        conversation = asyncio.run(SQLiteConversationStore(self.path).load(session_id, scope=owner))
        if conversation is None:
            raise KeyError('session not found')
        return tuple(conversation.messages)

    def load_run_messages(self, owner: str, run_id: str) -> tuple[ModelMessage, ...]:
        with self.connect() as db:
            db.execute('BEGIN')
            run = self._owned_run(db, owner, run_id)
            if run is None:
                raise KeyError('run not found')
            rows = db.execute(_CHAIN + 'SELECT e.* FROM app_conversation_entries e JOIN chain c ON c.id=e.id '
                              'WHERE e.run_id=? ORDER BY e.position', (run['session_id'], owner, run_id)).fetchall()
        return tuple(_message(row).message for row in rows)

    def update_run_status(self, owner_user_id: str, run_id: str, status: str, *,
                          stop_reason: str | None = None, result_summary: JsonObject | None = None) -> RunRecord:
        if status not in ACTIVE:
            raise ValueError('terminal updates require settle_native_run')
        if stop_reason is not None:
            _required_text(stop_reason, 'stop reason')
            _safe_json({'stop_reason': stop_reason})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned_run(db, owner_user_id, run_id)
            if row is None or row['status'] not in ACTIVE:
                raise KeyError('active run not found')
            current = str(row['status'])
            if status not in {'queued': {'queued', 'running', 'cancelling'},
                              'running': {'running', 'cancelling'}, 'cancelling': {'cancelling'}}[current]:
                raise ValueError('invalid run status transition')
            db.execute("UPDATE app_runs SET status=?,started_at=CASE WHEN ?='running' "
                       "THEN COALESCE(started_at,?) ELSE started_at END,stop_reason=?,result_summary_json=? WHERE id=?",
                       (status, status, _now(), stop_reason,
                        _safe_json(result_summary) if result_summary is not None else None, run_id))
            changed = self._owned_run(db, owner_user_id, run_id)
            assert changed is not None
            return _run(changed)

    def update_run_usage(self, owner: str, run_id: str, usage: RunUsage) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned_run(db, owner, run_id)
            if row is None or row['status'] not in ACTIVE:
                raise KeyError('active run not found')
            db.execute('UPDATE app_runs SET usage_json=? WHERE id=?', (_safe_json(_usage(usage)), run_id))

    def _append_native(self, db: sqlite3.Connection, owner: str, session_id: str, run_id: str,
                       messages: tuple[ModelMessage, ...], *, usage: RunUsage | None = None) -> None:
        # The binding is used only inside this worker's owning sqlite thread.
        # The async native save algorithm awaits primitives that reuse this db,
        # with no independent commit or cross-thread connection handoff.
        from ktg_agent.sessions.conversation_store import SQLiteConversationStore
        if messages or usage is not None:
            asyncio.run(SQLiteConversationStore(self.path, connection=db, run_id=run_id)
                        .append_messages(owner, session_id, messages, usage=usage))

    def archive_native_run(self, owner: str, run_id: str, messages: tuple[ModelMessage, ...]) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned_run(db, owner, run_id)
            if row is None or row['status'] not in ACTIVE:
                raise KeyError('active run not found')
            self._append_native(db, owner, row['session_id'], run_id, messages)

    def recover_native_messages(self, owner: str, current_run_id: str, prior_run_id: str,
                                messages: tuple[ModelMessage, ...]) -> None:
        """Attach verified prior-run recovery messages without altering its outcome."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._owned_run(db, owner, current_run_id)
            prior = self._owned_run(db, owner, prior_run_id)
            if (current is None or prior is None or current['status'] not in ACTIVE
                    or prior['status'] not in TERMINAL or prior['position'] >= current['position']
                    or current['session_id'] != prior['session_id'] or current['task_id'] != prior['task_id']):
                raise KeyError('authorized prior run not found')
            if messages and db.execute(
                _CHAIN + 'SELECT 1 FROM app_conversation_entries e JOIN chain c ON c.id=e.id '
                'JOIN app_runs r ON r.id=e.run_id WHERE r.position>? AND r.position<? LIMIT 1',
                (current['session_id'], owner, prior['position'], current['position'])
            ).fetchone() is not None:
                # The store is an ordered append-only history. Do not insert
                # an older checkpoint after messages from a newer Run.
                raise ValueError('prior recovery messages precede newer formal history')
            self._append_native(db, owner, prior['session_id'], prior_run_id, messages)

    def settle_native_run(self, owner_user_id: str, run_id: str, status: str, stop_reason: str,
                          result_summary: JsonObject, messages: tuple[ModelMessage, ...], *,
                          usage: RunUsage | None = None, context: tuple[ModelMessage, ...] | None = None) -> RunRecord:
        if status not in TERMINAL:
            raise ValueError('run status must be terminal')
        _required_text(stop_reason, 'stop reason')
        _safe_json({'stop_reason': stop_reason})
        now = _now()
        # Encoding is outside the transaction except the official save itself.
        encoded_context = (ModelMessagesTypeAdapter.dump_json(list(context))
                           if context is not None and stop_reason != 'history_alignment_failed' else None)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned_run(db, owner_user_id, run_id)
            if row is None or row['status'] not in ACTIVE:
                raise KeyError('active run not found')
            session_id = str(row['session_id'])
            if row['status'] == 'cancelling' and status == 'completed':
                status, stop_reason = 'cancelled', 'cancelled'
                result_summary = {**result_summary, 'stop_reason': 'cancelled'}
            # The formal native entries are authoritative for the answer, and
            # app_runs.usage_json is authoritative for usage. Retaining either
            # duplicate in the summary would also disagree after a cancellation.
            encoded_summary = _safe_json({key: value for key, value in result_summary.items()
                                          if key not in {'answer', 'usage'}})
            if usage is not None:
                final_usage = _usage(usage, complete=status == 'completed')
            else:
                final_usage = {**_read_json(row['usage_json']), 'complete': False}
            # Recompute the native header from the application Run totals once
            # per settlement, including known partial totals of earlier crashes.
            # This is a derived header, never an independent accounting ledger.
            previous_usage = [_read_json(prior[0]) for prior in db.execute(
                'SELECT usage_json FROM app_runs WHERE owner_user_id=? AND session_id=? AND id<>?',
                (owner_user_id, session_id, run_id))]
            conversation_usage = _native_usage_total([*previous_usage, final_usage])
            self._append_native(db, owner_user_id, session_id, run_id, messages, usage=conversation_usage)
            if encoded_context is not None:
                # Only retain genuinely derived context; raw history lives in
                # the official conversation entries, never a second raw copy.
                records = db.execute(_CHAIN + 'SELECT e.* FROM app_conversation_entries e '
                                     'JOIN chain c ON c.id=e.id ORDER BY e.position',
                                     (session_id, owner_user_id)).fetchall()
                if tuple(_message(record).message for record in records) == context:
                    encoded_context = None
            db.execute('UPDATE app_runs SET status=?,ended_at=?,stop_reason=?,result_summary_json=?,usage_json=?,'
                       'context_json=? WHERE id=?', (status, now, stop_reason, encoded_summary,
                       _safe_json(final_usage), encoded_context, run_id))
            db.execute('UPDATE app_sessions SET updated_at=? WHERE id=?', (now, session_id))
            reason = ('cancelled' if status == 'cancelled' else 'limit' if stop_reason == 'limit' else
                      'run_failed' if status == 'failed' else 'run_completed' if status == 'completed' else 'run_settled')
            self._finish_queued_inputs(db, run_id, reason, now)
            changed = self._owned_run(db, owner_user_id, run_id)
            assert changed is not None
            return _run(changed)

    @staticmethod
    def _finish_queued_inputs(db: sqlite3.Connection, run_id: str, reason: str, now: str) -> None:
        db.execute("UPDATE app_control_commands SET status='not_consumed',reason=?,updated_at=? "
                   "WHERE run_id=? AND status='queued'", (reason, now, run_id))

    def recover_interrupted_runs(self) -> int:
        now = _now()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM app_runs WHERE status IN ('queued','running','cancelling')").fetchall()
            for row in rows:
                status = 'cancelled' if row['status'] == 'queued' else 'needs_reconciliation'
                summary: JsonObject = {'error': 'SERVICE_RESTARTED', 'outcome_unknown': status == 'needs_reconciliation',
                    'message': ('服务重启中断运行；请核验已发出的业务操作。' if status == 'needs_reconciliation'
                                else '服务重启前运行尚未开始，请重新提交。')}
                usage = {**_read_json(row['usage_json']), 'complete': False}
                db.execute('UPDATE app_runs SET status=?,ended_at=?,stop_reason=?,result_summary_json=?,usage_json=? '
                           'WHERE id=?', (status, now, 'SERVICE_RESTARTED', _safe_json(summary),
                                          _safe_json(usage), row['id']))
                db.execute('UPDATE app_sessions SET updated_at=? WHERE id=?', (now, row['session_id']))
                self._finish_queued_inputs(db, row['id'], 'process_interrupted', now)
            return len(rows)

    def admit_control(self, owner: str, run_id: str, request_id: str, kind: str, request: JsonObject, *,
                      accepting: bool, ready: bool = True, closed: bool = False) -> tuple[ControlRecord, bool]:
        _required_text(request_id, 'request id')
        _safe_json({'request_id': request_id})
        encoded = _safe_json(request)
        if kind not in {'cancel', 'steer', 'follow_up'}:
            raise ValueError('invalid control kind')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._owned_run(db, owner, run_id)
            if run is None:
                raise KeyError('run not found')
            existing = db.execute('SELECT * FROM app_control_commands WHERE owner_user_id=? AND run_id=? AND request_id=?',
                                  (owner, run_id, request_id)).fetchone()
            if existing is not None:
                item = _control(existing)
                if item.kind != kind or item.request != request:
                    raise ControlConflictError('request_id 已用于不同控制命令。')
                return item, False
            status = str(run['status'])
            reason = None
            if kind == 'cancel':
                command_status = 'accepted'
                if status in TERMINAL:
                    reason = 'already_settled'
                else:
                    db.execute("UPDATE app_runs SET status='cancelling' WHERE id=?", (run_id,))
            else:
                if status != 'running':
                    raise ControlStateError('RUN_NOT_RUNNING' if status == 'queued' else 'RUN_NOT_ACTIVE')
                if closed or not accepting:
                    raise ControlStateError('RUN_NOT_ACTIVE')
                if not ready:
                    raise ControlStateError('RUN_NOT_RUNNING')
                command_status = 'queued'
            now = _now()
            cursor = db.execute('INSERT INTO app_control_commands(run_id,owner_user_id,request_id,kind,request_json,'
                                'status,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                (run_id, owner, request_id, kind, encoded, command_status, reason, now, now))
            row = db.execute('SELECT * FROM app_control_commands WHERE position=?', (cursor.lastrowid,)).fetchone()
            return _control(row), True

    def _mark_input(self, owner: str, run_id: str, request_id: str, status: str, reason: str | None) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('UPDATE app_control_commands SET status=?,reason=?,updated_at=? WHERE owner_user_id=? '
                                "AND run_id=? AND request_id=? AND status='queued' AND kind IN ('steer','follow_up')",
                                (status, reason, _now(), owner, run_id, request_id))
            if cursor.rowcount != 1:
                raise ControlStateError('INPUT_NOT_QUEUED')

    def mark_input_consumed(self, owner: str, run_id: str, request_id: str) -> None:
        self._mark_input(owner, run_id, request_id, 'consumed', None)

    def mark_input_rejected(self, owner: str, run_id: str, request_id: str,
                            reason: str = 'native_not_accepting') -> None:
        self._mark_input(owner, run_id, request_id, 'not_consumed', reason)

    def list_inputs(self, owner: str, run_id: str, *, after: int | None = None, limit: int = 50) -> Page[ControlRecord]:
        _page_args(after, limit)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM app_control_commands WHERE run_id=? AND owner_user_id=? '
                              "AND kind IN ('steer','follow_up') AND position>? ORDER BY position LIMIT ?",
                              (run_id, owner, after or 0, limit+1)).fetchall()
        return Page(tuple(_control(row) for row in rows[:limit]),
                    int(rows[limit-1]['position']) if len(rows) > limit else None)

    def list_messages(self, owner_user_id: str, session_id: str, *, after: int | None = None,
                      limit: int = 50) -> Page[MessageRecord]:
        _page_args(after, limit)
        with self.connect() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM app_sessions WHERE id=? AND owner_user_id=?',
                              (session_id, owner_user_id)).fetchone():
                raise KeyError('session not found')
            rows = db.execute(_CHAIN + 'SELECT e.* FROM app_conversation_entries e JOIN chain c ON c.id=e.id '
                              'WHERE e.position>? ORDER BY e.position LIMIT ?',
                              (session_id, owner_user_id, after or 0, limit+1)).fetchall()
        return Page(tuple(_message(row) for row in rows[:limit]),
                    int(rows[limit-1]['position']) if len(rows) > limit else None)

    def session_stats(self, owner_user_id: str, session_id: str) -> JsonObject:
        with self.connect() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM app_sessions WHERE id=? AND owner_user_id=?',
                              (session_id, owner_user_id)).fetchone():
                raise KeyError('session not found')
            messages = db.execute(_CHAIN + 'SELECT e.* FROM app_conversation_entries e JOIN chain c ON c.id=e.id',
                                  (session_id, owner_user_id)).fetchall()
            runs = db.execute('SELECT * FROM app_runs WHERE session_id=? AND owner_user_id=? ORDER BY position',
                              (session_id, owner_user_id)).fetchall()
        roles: dict[str, int] = {}
        statuses: dict[str, int] = {}
        active: str | None = None
        usages: list[JsonObject] = []
        for message in messages:
            role = _message_role(_message(message).message)
            roles[role] = roles.get(role, 0) + 1
        for run in runs:
            status = str(run['status'])
            statuses[status] = statuses.get(status, 0) + 1
            if status in ACTIVE:
                active = str(run['id'])
            usages.append(_read_json(run['usage_json']))
        return {'session_id': session_id, 'message_count': len(messages), 'messages_by_role': cast(JsonObject, roles),
                'run_count': len(runs), 'runs_by_status': cast(JsonObject, statuses),
                'known_usage': _aggregate_usage(usages), 'active_run_id': active}

    def _task_latest_run(self, owner: str, session_id: str, task_id: str) -> RunRecord:
        with self.connect() as db:
            row = db.execute('SELECT * FROM app_runs WHERE owner_user_id=? AND session_id=? AND task_id=? '
                             'ORDER BY position DESC LIMIT 1', (owner, session_id, task_id)).fetchone()
            if row is None:
                raise KeyError('task not found')
            return _run(row)

    def get_task(self, owner: str, session_id: str, task_id: str, *, history: bool = False) -> JsonObject:
        # Native plan store remains the sole writer and authoritative checklist.
        # The existing client sees a single current view; no historical versions.
        from ktg_agent.sessions.native_planning import read_plan, stock_step_evidence
        run = self._task_latest_run(owner, session_id, task_id)
        items = asyncio.run(read_plan(str(self.path), owner=owner, session_id=session_id, task_id=task_id))
        with self.connect() as db:
            rows = db.execute(_CHAIN + 'SELECT e.* FROM app_conversation_entries e JOIN chain c ON c.id=e.id '
                              'JOIN app_runs r ON r.id=e.run_id WHERE r.owner_user_id=? AND r.session_id=? '
                              'AND r.task_id=? ORDER BY e.position',
                              (session_id, owner, owner, session_id, task_id)).fetchall()
        evidence = stock_step_evidence(tuple(_message(row).message for row in rows))
        steps: list[JsonValue] = [{'step_id': item.id, 'description': item.content, 'status': item.status.value,
                                  'active_form': item.active_form, 'depends_on': cast(list[JsonValue], item.depends_on),
                                  'evidence': list(evidence.get(item.id, []))}
                                 for item in items]
        title = str(run.request_summary.get('message') or '任务')[:200]
        return {'task_id': task_id, 'session_id': session_id, 'title': title, 'current_version': 1,
                'created_at': run.created_at, 'updated_at': run.ended_at or run.started_at or run.created_at,
                'versions': [{'task_id': task_id, 'version': 1, 'steps': steps, 'run_id': run.id,
                              'saved_at': run.ended_at or run.started_at or run.created_at}]}

    def list_tasks(self, owner: str, session_id: str) -> list[JsonValue]:
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM app_sessions WHERE id=? AND owner_user_id=?',
                              (session_id, owner)).fetchone():
                raise KeyError('session not found')
            rows = db.execute('SELECT task_id,MAX(position) latest FROM app_runs WHERE owner_user_id=? AND session_id=? '
                              'GROUP BY task_id ORDER BY latest DESC', (owner, session_id)).fetchall()
        tasks: list[JsonValue] = []
        for row in rows:
            task = self.get_task(owner, session_id, str(row['task_id']))
            versions = cast(list[JsonObject], task['versions'])
            if versions[0]['steps']:
                tasks.append(task)
        return tasks
