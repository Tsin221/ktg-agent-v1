"""Thin process-history adapter for the official Harness StepPersistence.

The application owns authorization and terminal settlement. This adapter only
stores process evidence and the original messages before any history rewrite.
Use an explicit isolated SQLite file until the app table namespace is migrated.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai_harness.step_persistence import SqliteStepStore, StepPersistence
from pydantic_ai_harness.step_persistence.recovery import inspect_recovery

from ktg_agent.context.native_context import deserialize_messages, serialize_messages
from ktg_agent.sessions.native_planning import planning_task_id
from ktg_agent.tools.native import MesToolDeps


class RecoveryRequired(RuntimeError):
    """A prior run has incomplete evidence; do not silently replay it."""


class NativeStepHistory:
    """Official process store + append-only, deduplicated raw-history journal.

    The SQLite path must be the application's single isolated database, not a
    separate fallback database. Reject its legacy incompatible schema rather
    than letting upstream CREATE IF NOT EXISTS interact with it.
    """

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        if not self.database.is_absolute():
            raise ValueError('an explicit absolute SQLite path is required')
        if self.database.exists():
            with sqlite3.connect(self.database) as db:
                for name, required in (('runs', {'run_id', 'metadata'}),
                                       ('events', {'run_id', 'kind'})):
                    columns = {row[1] for row in db.execute(f'PRAGMA table_info({name})')}
                    if columns and not required <= columns:
                        raise ValueError(f'legacy SQLite table {name} conflicts with the native StepStore')

        # https://pydantic.dev/docs/ai/harness/step-persistence/
        # Keep exact native payloads inline and all settled recovery points.
        self.store = SqliteStepStore(database=self.database, media_store=None)

    def capability(self, deps: MesToolDeps) -> StepPersistence[MesToolDeps]:
        # Explicit one-shot application Run ID avoids ambiguous native mapping.
        return StepPersistence(store=self.store, run_id=deps.run_id, capture_frontier=True,
                               metadata={'owner': deps.owner, 'session': deps.session_id,
                                         'task': planning_task_id(deps)})

    async def continuation(self, deps: MesToolDeps, prior_run_id: str) -> list[ModelMessage]:
        """Read the known-settled history for an explicitly requested new Run.

        Only the trusted app may choose the previous authorized Run; this is
        not an automatic restart method or permission to repeat a write.
        """
        if prior_run_id == deps.run_id:
            raise ValueError('continuation requires a new run id')
        record = await self.store.get_run(run_id=prior_run_id)
        if (record is None or record.metadata.get('owner') != deps.owner
                or record.metadata.get('session') != deps.session_id
                or (deps.task_id is not None and record.metadata.get('task') != deps.task_id)):
            raise KeyError('prior authorized run not found')
        facts = await inspect_recovery(store=self.store, run_id=prior_run_id)
        if facts.unresolved or (facts.latest is not None and facts.latest.state != 'complete'):
            raise RecoveryRequired('interrupted tool work requires reconciliation before continuation')
        if facts.settled is None:
            raise RecoveryRequired('no settled native history exists')
        if any(isinstance(part, ToolReturnPart) and isinstance(part.metadata, dict)
               and part.metadata.get('status') == 'unknown'
               for message in facts.settled.messages for part in message.parts):
            raise RecoveryRequired('unknown business tool receipt requires reconciliation')
        return list(facts.settled.messages)

    async def archive(self, ctx: RunContext[MesToolDeps], raw_json: bytes) -> None:
        await self.archive_messages(ctx.deps, raw_json)

    async def archive_messages(self, deps: MesToolDeps, raw_json: bytes) -> None:
        await asyncio.to_thread(self._archive, deps, raw_json)

    def _schema(self, db: sqlite3.Connection) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS native_raw_messages (
            owner TEXT NOT NULL, session_id TEXT NOT NULL,
            digest TEXT NOT NULL, data BLOB NOT NULL,
            PRIMARY KEY(owner, session_id, digest))''')
        db.execute('''CREATE TABLE IF NOT EXISTS native_raw_snapshots (
            seq INTEGER PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT NOT NULL,
            run_id TEXT NOT NULL, digest TEXT NOT NULL, refs TEXT NOT NULL,
            UNIQUE(owner, session_id, run_id, digest))''')

    def _archive(self, deps: MesToolDeps, raw_json: bytes) -> None:
        messages = deserialize_messages(raw_json)
        # Hash exact native message representations once per authorized session;
        # preserve repeated occurrences and order in each snapshot's references.
        encoded = [serialize_messages([message]) for message in messages]
        hashes = [hashlib.sha256(item).hexdigest() for item in encoded]
        refs = json.dumps(hashes, separators=(',', ':'))
        digest = hashlib.sha256(refs.encode()).hexdigest()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database, timeout=30) as db:
            self._schema(db)
            db.executemany('INSERT OR IGNORE INTO native_raw_messages VALUES(?,?,?,?)',
                           ((deps.owner, deps.session_id, key, data) for key, data in zip(hashes, encoded, strict=True)))
            db.execute('''INSERT OR IGNORE INTO native_raw_snapshots(owner,session_id,run_id,digest,refs)
                VALUES(?,?,?,?,?)''', (deps.owner, deps.session_id, deps.run_id, digest, refs))

    async def raw_snapshots(self, deps: MesToolDeps) -> tuple[bytes, ...]:
        return await asyncio.to_thread(self._raw_snapshots, deps)

    def _raw_snapshots(self, deps: MesToolDeps) -> tuple[bytes, ...]:
        if not self.database.exists():
            return ()
        with sqlite3.connect(self.database, timeout=30) as db:
            self._schema(db)
            snapshots = db.execute('SELECT refs FROM native_raw_snapshots WHERE owner=? AND session_id=? AND run_id=? ORDER BY seq',
                                   (deps.owner, deps.session_id, deps.run_id)).fetchall()
            result: list[bytes] = []
            for (refs,) in snapshots:
                messages: list[ModelMessage] = []
                for digest in json.loads(refs):
                    row = db.execute('SELECT data FROM native_raw_messages WHERE owner=? AND session_id=? AND digest=?',
                                     (deps.owner, deps.session_id, digest)).fetchone()
                    if row is None:
                        raise ValueError('missing original native message')
                    messages.extend(deserialize_messages(row[0]))
                result.append(serialize_messages(messages))
            return tuple(result)
