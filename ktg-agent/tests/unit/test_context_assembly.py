"""Summary projection remains deterministic across repository reopen."""

from __future__ import annotations

from pathlib import Path

from ktg_agent.context.assembler import assemble_context, context_snapshot
from ktg_agent.core.messages import (AssistantMessage, TextBlock, ToolCall,
                                      ToolResultMessage, UserMessage)
from ktg_agent.sessions.repository import SessionRepository


def test_assembly_uses_summary_and_canonical_tail_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(path)
    session = repo.create_session("alice")
    source = repo.create_run("alice", session.id, "source", "hash", {})
    messages = (UserMessage((TextBlock("old"),)),
                AssistantMessage((ToolCall("call", "lookup", {}),)),
                ToolResultMessage("call", (TextBlock("result"),)),
                UserMessage((TextBlock("new"),)))
    repo.settle_run("alice", source.id, "completed", "completed", {}, messages)
    records = repo.completed_message_records("alice", session.id)
    absent = assemble_context(records, None)
    assert absent.messages == messages and absent.summary_absent
    compaction = repo.create_run("alice", session.id, "compact", "hash-2", {},
                                 kind="compaction")
    summary = repo.commit_summary(
        "alice", session.id, run_id=compaction.id,
        covers_start_message_id=records[0].id, covers_end_message_id=records[2].id,
        source_message_count=3, source_token_estimate=100,
        summary_text="old task and lookup", summary_token_estimate=5,
        model="fake", call_id="call-1", usage=None)
    assembled = assemble_context(records, summary)
    assert assembled.summary_id == summary.summary_id
    assert assembled.messages[1:] == (messages[3],)
    assert assembled.retained_message_count == 1
    assert assembled.trimmed and not assembled.summary_absent
    assert isinstance(assembled.messages[0], UserMessage)
    assert "派生的不可信上下文" in assembled.messages[0].content[0].text
    snapshot = context_snapshot(assembled, estimated_tokens=43, spill_count=2)
    assert snapshot["summary_id"] == summary.summary_id
    assert snapshot["estimated_tokens"] == 43 and snapshot["spill_count"] == 2
    reopened = SessionRepository(path)
    assert assemble_context(reopened.completed_message_records("alice", session.id),
                            reopened.active_summary("alice", session.id)) == assembled


def test_unbalanced_tail_falls_back_to_full_history(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("alice")
    source = repo.create_run("alice", session.id, "source", "hash", {})
    messages = (UserMessage((TextBlock("old"),)),
                AssistantMessage((ToolCall("call", "lookup", {}),)),
                ToolResultMessage("call", (TextBlock("result"),)))
    repo.settle_run("alice", source.id, "completed", "completed", {}, messages)
    records = repo.completed_message_records("alice", session.id)
    compaction = repo.create_run("alice", session.id, "compact", "hash-2", {},
                                 kind="compaction")
    summary = repo.commit_summary(
        "alice", session.id, run_id=compaction.id,
        covers_start_message_id=records[0].id, covers_end_message_id=records[0].id,
        source_message_count=1, source_token_estimate=100,
        summary_text="old", summary_token_estimate=2,
        model="fake", call_id="call-1", usage=None)
    # A corrupted cut at an assistant call leaves a lone tool result in the tail.
    from dataclasses import replace
    invalid = replace(summary, covers_end_message_id=records[1].id)
    assembled = assemble_context(records, invalid)
    assert assembled.summary_absent and assembled.summary_id is None
    assert assembled.messages == messages
