"""Application SQLite initialization and short connection management."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic_ai_harness.planning import SqlitePlanStore
from pydantic_ai_harness.step_persistence import SqliteStepStore

# Distinct from the retired application migration chain (versions 1–6).
APP_SCHEMA_VERSION = 100
_APP_TABLES = {'app_sessions', 'app_runs', 'app_conversation_entries', 'app_control_commands'}
_NATIVE_TABLES = {'runs', 'events', 'snapshots', 'snapshot_idempotency_keys', 'tool_effects',
                  'snapshot_messages', 'snapshot_message_refs', 'agent_native_plan_items'}
_ALLOWED_TABLES = _APP_TABLES | _NATIVE_TABLES | {'app_operation_plans'}
_COLUMN_SIGNATURES = {
    'app_sessions': {'position', 'id', 'owner_user_id', 'title', 'created_at', 'updated_at', 'head_id',
                     'revision', 'usage_json', 'deferred_requests_json', 'forked_from', 'forked_at',
                     'metadata_json', 'branches_json', 'format_version'},
    'app_runs': {'position', 'id', 'session_id', 'owner_user_id', 'task_id', 'request_id', 'request_hash',
                 'request_summary_json', 'status', 'created_at', 'started_at', 'ended_at', 'stop_reason',
                 'result_summary_json', 'usage_json', 'context_json'},
    'app_conversation_entries': {'position', 'id', 'session_id', 'run_id', 'parent_id', 'message_json', 'created_at'},
    'app_control_commands': {'position', 'run_id', 'owner_user_id', 'request_id', 'kind', 'request_json',
                             'status', 'reason', 'created_at', 'updated_at'},
    'runs': {'run_id', 'conversation_id', 'parent_run_id', 'metadata', 'started_at'},
    'events': {'seq', 'run_id', 'kind', 'step_index', 'timestamp'},
}


def database_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError('session database path must be absolute')
    resolved = candidate.resolve()
    if any(part.casefold() == 'ktg_agent_v1' for part in resolved.parts):
        raise ValueError('candidate database must not use the formal project path')
    return resolved


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def _tables(db: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _check(db: sqlite3.Connection) -> bool:
    tables = _tables(db)
    version = int(db.execute('PRAGMA user_version').fetchone()[0])
    if not tables and version == 0:
        return False
    if (version != APP_SCHEMA_VERSION or not _APP_TABLES <= tables
            or not tables <= _ALLOWED_TABLES):
        raise RuntimeError('refusing to open incompatible or unrelated application SQLite database')
    # The official tables have no prefix option; never let their CREATE IF NOT
    # EXISTS succeed against a legacy application table with the same name.
    for table, columns in _COLUMN_SIGNATURES.items():
        if table in tables and not columns <= {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            raise RuntimeError(f'incompatible table: {table}')
    return True


def initialize_application(path: Path) -> None:
    if path.exists():
        # Do not set WAL or create schema until a preexisting file is identified.
        if path.stat().st_size:
            read = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=10)
            try:
                _check(read)
            finally:
                read.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        initialized = _check(db)
        if db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
            raise RuntimeError('application SQLite requires WAL mode')
        if initialized:
            return
        db.executescript(f"""BEGIN IMMEDIATE;
            CREATE TABLE app_sessions (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                owner_user_id TEXT NOT NULL,
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                head_id TEXT,
                revision TEXT NOT NULL,
                usage_json BLOB NOT NULL DEFAULT '{{}}',
                deferred_requests_json BLOB,
                forked_from TEXT,
                forked_at TEXT,
                metadata_json BLOB,
                branches_json TEXT NOT NULL DEFAULT '[]',
                format_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX app_sessions_owner_page ON app_sessions(owner_user_id,position DESC);
            CREATE TABLE app_runs (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES app_sessions(id),
                owner_user_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                request_summary_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','completed',
                    'needs_input','failed','cancelled','needs_reconciliation')),
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                stop_reason TEXT,
                result_summary_json TEXT,
                usage_json TEXT NOT NULL,
                context_json BLOB,
                UNIQUE(owner_user_id,session_id,request_id)
            );
            CREATE UNIQUE INDEX app_runs_one_active_per_session ON app_runs(session_id)
                WHERE status IN ('queued','running','cancelling');
            CREATE INDEX app_runs_session_page ON app_runs(session_id,position);
            CREATE INDEX app_runs_task ON app_runs(owner_user_id,session_id,task_id,position DESC);
            CREATE TABLE app_conversation_entries (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                run_id TEXT,
                parent_id TEXT,
                message_json BLOB NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX app_conversation_entries_run ON app_conversation_entries(run_id,position);
            CREATE INDEX app_conversation_entries_session ON app_conversation_entries(session_id,position);
            CREATE TABLE app_control_commands (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES app_runs(id),
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
            CREATE INDEX app_control_commands_page ON app_control_commands(run_id,position);
            PRAGMA user_version={APP_SCHEMA_VERSION};
            COMMIT;
        """)


async def initialize_framework_stores(path: Path) -> None:
    # Both native stores lazily create schema on their first public call.
    # Empty reads initialize without inserting invented runs or plans.
    await SqliteStepStore(database=path, media_store=None).list_runs()
    await SqlitePlanStore(str(path), table='agent_native_plan_items').get_items()
