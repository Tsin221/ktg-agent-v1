"""SQLite backend for the core Pydantic AI ConversationStore.

The official Store owns history/branch semantics. SQLite owns revision CAS and
entry/record atomicity. A bound backend participates in its caller's transaction
and must only be used in the thread that opened that connection.
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import TypeAdapter
from pydantic_ai.conversation_store import ConversationConflict, ConversationDetails, ConversationEntry, ConversationRecord, ConversationStore
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import RunUsage

from ktg_agent.sessions.database import connect, database_path


_T = TypeVar('_T')
_USAGE: TypeAdapter[RunUsage] = TypeAdapter(RunUsage)
_DEFERRED: TypeAdapter[DeferredToolRequests] = TypeAdapter(DeferredToolRequests)
_METADATA: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])
_BRANCHES: TypeAdapter[list[str]] = TypeAdapter(list[str])


class SQLiteConversationStore(ConversationStore):
    """The application's scoped conversation backend on the unified database.

    Deleting a record does not erase its immutable entries: another record's
    branch/fork may still refer to them.
    """

    def __init__(self, database: str | Path, *, connection: sqlite3.Connection | None = None,
                 run_id: str | None = None) -> None:
        self.database = database_path(database)
        self._connection = connection
        self._run_id = run_id

    async def _call(self, work: Callable[[sqlite3.Connection], _T], *, write: bool = False) -> _T:
        if self._connection is not None:
            # The worker opened this connection and owns begin/commit/rollback.
            return work(self._connection)

        def perform() -> _T:
            with connect(self.database) as db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                return work(db)

        return await asyncio.to_thread(perform)

    @staticmethod
    def _query(db: sqlite3.Connection, sql: str, args: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = db.cursor()
        cursor.row_factory = cast(Callable[[sqlite3.Cursor, sqlite3.Row], object], sqlite3.Row)
        return cursor.execute(sql, args)

    @staticmethod
    def _record(row: sqlite3.Row) -> ConversationRecord:
        if row['format_version'] != 1:
            raise ValueError('unsupported conversation record format')
        return ConversationRecord(
            conversation_id=row['id'], head_id=row['head_id'], scope=row['owner_user_id'],
            revision=row['revision'], title=row['title'], updated_at=datetime.fromisoformat(row['updated_at']),
            usage=_USAGE.validate_json(row['usage_json']),
            deferred_tool_requests=None if row['deferred_requests_json'] is None else _DEFERRED.validate_json(row['deferred_requests_json']),
            forked_from=row['forked_from'], forked_at=row['forked_at'],
            metadata=None if row['metadata_json'] is None else _METADATA.validate_json(row['metadata_json']),
            branches=_BRANCHES.validate_json(row['branches_json']), format_version=1,
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> ConversationEntry:
        messages = ModelMessagesTypeAdapter.validate_json(row['message_json'])
        (message,) = messages
        return ConversationEntry(id=row['id'], parent_id=row['parent_id'], message=message)

    async def read_record(self, conversation_id: str) -> ConversationRecord | None:
        def read(db: sqlite3.Connection) -> ConversationRecord | None:
            row = self._query(db, 'SELECT * FROM app_sessions WHERE id=?', (conversation_id,)).fetchone()
            return None if row is None else self._record(row)
        return await self._call(read)

    async def read_records(self, *, scope: str | None = None, limit: int | None = None) -> Sequence[ConversationRecord]:
        def read(db: sqlite3.Connection) -> list[ConversationRecord]:
            rows = self._query(db, 'SELECT * FROM app_sessions WHERE (? IS NULL OR owner_user_id=?) '
                               'ORDER BY updated_at DESC, id LIMIT ?', (scope, scope, -1 if limit is None else limit))
            return [self._record(row) for row in rows]
        return await self._call(read)

    async def read_entry(self, entry_id: str) -> ConversationEntry:
        def read(db: sqlite3.Connection) -> ConversationEntry:
            row = self._query(db, 'SELECT * FROM app_conversation_entries WHERE id=?', (entry_id,)).fetchone()
            if row is None:
                raise KeyError(entry_id)
            return self._entry(row)
        return await self._call(read)

    async def read_history(self, head_id: str | None, *, limit: int | None = None) -> list[ConversationEntry]:
        if head_id is None or limit == 0:
            return []

        def read(db: sqlite3.Connection) -> list[ConversationEntry]:
            rows = self._query(db, '''WITH RECURSIVE chain(id, depth) AS (
                SELECT ?, 0 UNION ALL SELECT e.parent_id, c.depth+1 FROM chain c
                JOIN app_conversation_entries e ON e.id=c.id WHERE e.parent_id IS NOT NULL AND c.depth<?)
                SELECT e.*, c.depth FROM chain c LEFT JOIN app_conversation_entries e ON e.id=c.id
                ORDER BY c.depth DESC''', (head_id, 9223372036854775807 if limit is None else limit - 1))
            history: list[ConversationEntry] = []
            for row in rows:
                if row['id'] is None:
                    raise KeyError('broken conversation history')
                history.append(self._entry(row))
            return history
        return await self._call(read)

    async def write_record(self, record: ConversationRecord, *, new_entries: Sequence[ConversationEntry] = (),
                           expected_revision: str | None) -> None:
        def write(db: sqlite3.Connection) -> None:
            row = self._query(db, 'SELECT revision, owner_user_id FROM app_sessions WHERE id=?',
                              (record.conversation_id,)).fetchone()
            if (None if row is None else row['revision']) != expected_revision:
                raise ConversationConflict(record.conversation_id)
            if record.scope is None or (row is not None and row['owner_user_id'] != record.scope):
                raise ConversationConflict(record.conversation_id)
            now = record.updated_at.isoformat()
            if row is None:
                db.execute('''INSERT INTO app_sessions(id,owner_user_id,title,created_at,updated_at,revision,metadata_json)
                    VALUES(?,?,?,?,?,?,?)''', (record.conversation_id, record.scope, record.title, now, now, record.revision,
                                                None if record.metadata is None else _METADATA.dump_json(record.metadata)))
            for entry in new_entries:
                if entry.parent_id is not None and not db.execute('SELECT 1 FROM app_conversation_entries WHERE id=?',
                                                                  (entry.parent_id,)).fetchone():
                    raise ConversationConflict(record.conversation_id)
                timestamp = entry.message.timestamp or datetime.now(UTC)
                db.execute('''INSERT INTO app_conversation_entries(id,session_id,run_id,parent_id,message_json,created_at)
                    VALUES(?,?,?,?,?,?)''', (entry.id, record.conversation_id, self._run_id or entry.message.run_id, entry.parent_id,
                                              ModelMessagesTypeAdapter.dump_json([entry.message]), timestamp.isoformat()))
            if record.head_id is not None and not db.execute('SELECT 1 FROM app_conversation_entries WHERE id=?',
                                                            (record.head_id,)).fetchone():
                raise ConversationConflict(record.conversation_id)
            # Existing title and metadata may have been changed independently while
            # official save loaded history. Preserve them per write_details contract.
            db.execute('''UPDATE app_sessions SET head_id=?,revision=?,usage_json=?,deferred_requests_json=?,
                forked_from=?,forked_at=?,branches_json=?,format_version=?,updated_at=? WHERE id=?''', (
                    record.head_id, record.revision, _USAGE.dump_json(record.usage),
                    None if record.deferred_tool_requests is None else _DEFERRED.dump_json(record.deferred_tool_requests),
                    record.forked_from, record.forked_at, _BRANCHES.dump_json(record.branches), record.format_version,
                    now, record.conversation_id))

        await self._call(write, write=True)

    async def write_details(self, conversation_id: str, details: ConversationDetails, *, expected_revision: str,
                            expected: ConversationDetails | None = None) -> ConversationRecord:
        def write(db: sqlite3.Connection) -> ConversationRecord:
            row = self._query(db, 'SELECT * FROM app_sessions WHERE id=?', (conversation_id,)).fetchone()
            if row is None or row['revision'] != expected_revision:
                raise ConversationConflict(conversation_id)
            record = self._record(row)
            if expected and any(getattr(record, key) != value for key, value in expected.items()):
                raise ConversationConflict(conversation_id)
            if 'title' in details:
                db.execute('UPDATE app_sessions SET title=? WHERE id=?', (details['title'], conversation_id))
            if 'metadata' in details:
                metadata = details['metadata']
                db.execute('UPDATE app_sessions SET metadata_json=? WHERE id=?',
                           (None if metadata is None else _METADATA.dump_json(metadata), conversation_id))
            updated = self._query(db, 'SELECT * FROM app_sessions WHERE id=?', (conversation_id,)).fetchone()
            assert updated is not None
            return self._record(updated)
        return await self._call(write, write=True)

    async def delete_record(self, conversation_id: str, *, expected_revision: str) -> None:
        def write(db: sqlite3.Connection) -> None:
            changed = db.execute('DELETE FROM app_sessions WHERE id=? AND revision=?', (conversation_id, expected_revision))
            if not changed.rowcount:
                raise ConversationConflict(conversation_id)
        await self._call(write, write=True)

    async def append_messages(self, owner: str, session_id: str, messages: Sequence[ModelMessage], *,
                              usage: RunUsage | None = None) -> list[ConversationEntry]:
        """Append an already selected native delta without branching the canonical history.

        `usage`, when present, is the total conversation usage, never a run delta.
        """
        # An empty loaded window carries the current revision and head boundary.
        # The official save then appends directly without rereading the full log.
        conversation = await self.load(session_id, scope=owner, limit=0)
        if conversation is None:
            raise KeyError('session not found')
        conversation.messages.extend(messages)
        if usage is not None:
            conversation.usage = usage
        return await self.save(conversation, scope=owner)
