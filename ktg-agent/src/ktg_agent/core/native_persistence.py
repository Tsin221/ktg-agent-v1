"""Thin process-history adapter for the official Harness StepPersistence."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from itertools import groupby
from pathlib import Path

from pydantic_ai.messages import (
    CompactionPart, ModelMessage, ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart,
    ToolReturnPart, UserPromptPart,
)
from pydantic_ai_harness.step_persistence import SqliteStepStore, StepPersistence
from pydantic_ai_harness.step_persistence.recovery import inspect_recovery

from ktg_agent.sessions.native_planning import planning_task_id
from ktg_agent.tools.native import MesToolDeps


class RecoveryRequired(RuntimeError):
    """A prior run has incomplete evidence; do not silently replay it."""


def _original(message: ModelMessage) -> ModelMessage | None:
    if message.run_id is None:
        return None
    if isinstance(message, ModelRequest) and any(isinstance(part, CompactionPart) for part in message.parts):
        # Native compaction can put its derived marker and an original prompt in
        # the same request. Retain only the native non-compaction parts here.
        parts = [part for part in message.parts if not isinstance(part, CompactionPart)]
        return replace(message, parts=parts) if parts else None
    return message


def _same_boundary(before: ModelMessage, after: ModelMessage) -> bool:
    if before == after:
        return True
    if (before.kind != after.kind or before.run_id != after.run_id or before.timestamp is None
            or before.timestamp != after.timestamp):
        return False
    if isinstance(before, ModelRequest) and isinstance(after, ModelRequest):
        remaining = list(after.parts)
        for part in before.parts:
            if part not in remaining:
                return False
            remaining.remove(part)
        return True
    return before.parts == after.parts


def recovery_delta(snapshot: Sequence[ModelMessage], archived: Sequence[ModelMessage],
                   run_id: str) -> tuple[ModelMessage, ...]:
    """Select only original prior-run tail after the canonical archive boundary.

    A compacted snapshot can omit an archived prefix. Never concatenate its
    entire history or promote its synthesized compaction messages to raw history.
    """
    candidates = [original for message in snapshot if message.run_id == run_id
                  and (original := _original(message)) is not None]
    if not candidates:
        return ()
    if not archived:
        if any(isinstance(p, CompactionPart) for m in snapshot for p in m.parts):
            raise RecoveryRequired('compacted snapshot has no corresponding original archive')
        return tuple(candidates)
    anchor = archived[-1]
    for index in range(len(candidates) - 1, -1, -1):
        match = candidates[index]
        if not _same_boundary(anchor, match):
            continue
        extras: tuple[ModelMessage, ...] = ()
        if isinstance(anchor, ModelRequest) and isinstance(match, ModelRequest):
            # An ASAP input may have been spliced into the request after the
            # first archive. Several canonical entries can share its boundary.
            remaining = list(match.parts)
            for recorded in archived:
                if (isinstance(recorded, ModelRequest) and recorded.run_id == match.run_id
                        and recorded.timestamp == match.timestamp):
                    for part in recorded.parts:
                        if part not in remaining:
                            raise RecoveryRequired('snapshot no longer contains archived request evidence')
                        remaining.remove(part)
            if remaining:
                if not all(isinstance(part, UserPromptPart) for part in remaining):
                    raise RecoveryRequired('snapshot changed non-prompt request evidence')
                extras = (replace(match, parts=remaining),)
        return (*extras, *candidates[index + 1:])
    # The last committed original may be a newer pending model request than
    # the last settled process checkpoint. Do not append stale messages.
    latest = candidates[-1]
    if (anchor.timestamp is not None and latest.timestamp is not None
            and anchor.timestamp > latest.timestamp):
        return ()
    raise RecoveryRequired('cannot align recovered native messages with the formal conversation')


def working_history(canonical: Sequence[ModelMessage], *contexts: Sequence[ModelMessage] | None) -> tuple[ModelMessage, ...]:
    """Reuse the newest aligned derived context while keeping canonical history authoritative."""
    best: Sequence[ModelMessage] = canonical
    best_index = -1
    for context in contexts:
        if not context:
            continue
        anchor = next((original for message in reversed(context) if (original := _original(message)) is not None), None)
        if anchor is None:
            continue
        for index in range(len(canonical) - 1, -1, -1):
            if index > best_index and _same_boundary(canonical[index], anchor):
                best = (*context, *canonical[index + 1:])
                best_index = index
                break
    # Provider histories containing a dangling tool invocation could implicitly
    # repeat it. Keep only a complete prefix for a fresh, unrelated Run. Explicit
    # continuation already rejects unresolved or interrupted native frontiers.
    safe: list[ModelMessage] = []
    for _, grouped in groupby(best, key=lambda message: message.run_id):
        group = tuple(grouped)
        open_calls: set[str] = set()
        settled_through = 0
        for index, message in enumerate(group):
            if isinstance(message, ModelResponse):
                open_calls.update(part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart))
            elif isinstance(message, ModelRequest):
                if message.state != 'complete':
                    break
                valid = True
                for part in message.parts:
                    if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id:
                        if part.tool_call_id not in open_calls:
                            valid = False
                            break
                        open_calls.remove(part.tool_call_id)
                if not valid:
                    break
            if not open_calls:
                settled_through = index + 1
        # Discard only this interrupted Run's unsettled tail; later Runs have
        # their own independently valid message and tool pairing boundaries.
        safe.extend(group[:settled_through])
    return tuple(safe)


class NativeStepHistory:
    """Official process store; only the ConversationStore owns raw history."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        if not self.database.is_absolute():
            raise ValueError('an explicit absolute SQLite path is required')
        if self.database.exists():
            with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
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
                or record.metadata.get('task') != planning_task_id(deps)):
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
