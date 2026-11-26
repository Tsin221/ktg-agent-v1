from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.messages import ModelRequest, UserPromptPart

from ktg_agent.sessions.conversation_store import SQLiteConversationStore
from ktg_agent.sessions.repository import SessionRepository


def test_official_conversation_store_scope_conflict_and_repeat(tmp_path: Path) -> None:
    database = tmp_path / 'app.sqlite'
    repository = SessionRepository(database)
    session = repository.create_session('owner', title='original')

    async def case() -> None:
        store = SQLiteConversationStore(database)
        first = await store.load(session.id, scope='owner')
        stale = await store.load(session.id, scope='owner')
        assert first is not None and stale is not None
        msg = ModelRequest(parts=[UserPromptPart('repeat')])
        first.messages.extend([msg, msg])
        await store.update(session.id, scope='owner', title='renamed')
        entries = await store.save(first, scope='owner')
        assert len(entries) == 2 and entries[0].id != entries[1].id
        assert await store.save(first, scope='owner') == []
        with pytest.raises(ConversationConflict):
            await store.save(stale, scope='owner')
        assert await store.load(session.id, scope='outsider') is None
        loaded = await SQLiteConversationStore(database).load(session.id, scope='owner')
        assert loaded is not None and loaded.messages == [msg, msg]
        assert (await store.list_conversations(scope='owner'))[0].title == 'renamed'
        assert await store.list_conversations(scope='outsider') == []
        with pytest.raises(ConversationConflict):
            await store.save(Conversation(conversation_id=session.id), scope='outsider')

    asyncio.run(case())


def test_transaction_bound_store_rolls_back_entries_and_head(tmp_path: Path) -> None:
    database = tmp_path / 'app.sqlite'
    repository = SessionRepository(database)
    session = repository.create_session('owner')
    db = sqlite3.connect(database)
    try:
        db.execute('BEGIN IMMEDIATE')
        store = SQLiteConversationStore(database, connection=db)
        asyncio.run(store.append_messages('owner', session.id, (ModelRequest(parts=[UserPromptPart('temporary')]),)))
        assert db.execute('SELECT count(*) FROM app_conversation_entries').fetchone() == (1,)
        db.rollback()
        assert db.execute('SELECT count(*) FROM app_conversation_entries').fetchone() == (0,)
        assert db.execute('SELECT head_id FROM app_sessions').fetchone() == (None,)
    finally:
        db.close()
