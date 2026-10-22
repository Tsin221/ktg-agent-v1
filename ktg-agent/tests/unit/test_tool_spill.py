from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from ktg_agent.context import SpillSettings, estimate_text_tokens, spill_tool_result
from ktg_agent.context.spill import SpillAfterHook
from ktg_agent.tools import (ExecutionMode, ToolContext, ToolDefinition, ToolEffect,
                             ToolRegistry, ToolResult, ToolRunner, ToolStatus)
from ktg_agent.core.messages import (AssistantMessage, JsonObject, Message, StopReason,
                                     TextBlock, ToolCall, ToolResultMessage)
from ktg_agent.core.events import EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RunCoordinator, RegisterAgent
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.ktg import KTGClient
from ktg_agent.tools.legacy_catalog import build_legacy_catalog


def _split_spilled(content: str, target: Path) -> tuple[str, str, int]:
    prefix, rest = content.split("\n[已省略 ", 1)
    notice, tail = rest.split("]\n", 1)
    suffix, path = tail.split("\n完整内容路径：", 1)
    assert path == str(target)
    match = re.fullmatch(r"(\d+) 个字符，约 (\d+) tokens。.*", notice)
    assert match is not None
    return prefix, suffix, int(match.group(1))


def test_spill_preserves_edges_details_and_is_idempotent(tmp_path: Path) -> None:
    original_text = "A" * 1200 + "结尾"
    original = ToolResult(original_text, details={"rows": 3}, operation_id="op")
    settings = SpillSettings(200, tmp_path / "spill")
    first = spill_tool_result(original, settings, run_id="run", tool_call_id="../../etc/passwd",
                              effect=ToolEffect.READ)
    second = spill_tool_result(original, settings, run_id="run", tool_call_id="../../etc/passwd",
                               effect=ToolEffect.READ)
    expected_name = hashlib.sha256(b"run\0../../etc/passwd").hexdigest()[:32] + ".txt"
    target = tmp_path / "spill" / expected_name
    assert target.is_file() and target.read_text(encoding="utf-8") == original_text
    assert re.fullmatch(r"[0-9a-f]{32}\.txt", target.name)
    assert target.resolve().parent == settings.directory.resolve()
    assert len(list(settings.directory.iterdir())) == 1
    assert first == second
    prefix, suffix, omitted = _split_spilled(first.content, target)
    assert prefix and set(prefix) == {"A"}
    assert suffix.endswith("结尾") and set(suffix[:-2]) == {"A"}
    assert original_text == prefix + original_text[len(prefix):len(original_text) - len(suffix)] + suffix
    assert omitted == len(original_text) - len(prefix) - len(suffix)
    assert estimate_text_tokens(first.content) <= 200
    assert estimate_text_tokens(first.content) < estimate_text_tokens(original_text)
    assert first.content.count("已省略") == 1
    assert first.details == {"rows": 3, "spill": {
        "path": str(target), "omitted_chars": omitted,
    }}
    assert first.status is original.status and first.operation_id == "op"
    assert original.details == {"rows": 3}


def test_spill_skips_write_failure_and_small_result(tmp_path: Path) -> None:
    settings = SpillSettings(1, tmp_path / "spill")
    large = ToolResult("abcdef")
    failed = ToolResult("abcdef", status=ToolStatus.FAILED, error_code="BAD")
    assert spill_tool_result(large, settings, run_id="r", tool_call_id="c",
                             effect=ToolEffect.WRITE) is large
    assert spill_tool_result(failed, settings, run_id="r", tool_call_id="c",
                             effect=ToolEffect.READ) is failed
    assert spill_tool_result(ToolResult("a"), settings, run_id="r", tool_call_id="c",
                             effect=ToolEffect.READ).content == "a"
    assert not settings.directory.exists()


def test_spill_skips_when_notice_budget_cannot_fit(tmp_path: Path) -> None:
    original = ToolResult("abcdef")
    settings = SpillSettings(8, tmp_path / "spill")
    returned = spill_tool_result(original, settings, run_id="r", tool_call_id="c",
                                 effect=ToolEffect.READ)
    assert returned is original
    assert returned.details is None
    assert not settings.directory.exists()


def test_spill_io_failure_returns_original_and_logs_no_content(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("existing", encoding="utf-8")
    original = ToolResult("secret payload " * 100)
    with caplog.at_level(logging.WARNING):
        returned = spill_tool_result(original, SpillSettings(200, blocked / "child"),
                                     run_id="r", tool_call_id="c", effect=ToolEffect.READ)
    assert returned is original
    assert len(caplog.records) == 1
    assert "secret payload" not in caplog.text


def test_after_hook_replaces_result_and_rejects_invalid_replacement(tmp_path: Path) -> None:
    async def execute(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult("A" * 4000, details={"rows": 1})

    tool = ToolDefinition("read", "1", "read", "read", {"type": "object"}, None,
                          (), ToolEffect.READ, ExecutionMode.SEQUENTIAL, 1, execute)
    registry = ToolRegistry((tool,))
    runner = ToolRunner(registry, after_hooks=(SpillAfterHook(SpillSettings(200, tmp_path)),))
    result = asyncio.run(runner.run(ToolCall("c", "read", {}), ToolContext("r", "c")))
    assert result.details is not None and "spill" in result.details
    assert estimate_text_tokens(result.content) <= 200

    async def invalid(context: ToolContext, tool: ToolDefinition,
                      arguments: Mapping[str, Any], result: ToolResult) -> Any:
        return "wrong"

    with pytest.raises(TypeError, match="after hook"):
        asyncio.run(ToolRunner(registry, after_hooks=(invalid,)).run(
            ToolCall("c", "read", {}), ToolContext("r", "c")))


@pytest.mark.parametrize("blocked_directory", (False, True))
def test_session_persists_spill_or_original_on_io_failure(
    tmp_path: Path, blocked_directory: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    original = "A" * 3200

    async def execute_tool(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(original, details={"rows": 7})

    tool = ToolDefinition("ktg_count_products", "1", "统计产品", "统计产品数量", {"type": "object"}, None,
                          ("mes:md:mditem:list",), ToolEffect.READ, ExecutionMode.SEQUENTIAL, 1, execute_tool)
    provider = FakeProvider((
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((ToolCall("load", "load_tools", {"tools": [
                {"name": "ktg_count_products", "version": "1"}]}),)),
            StopReason.TOOL_CALL, Usage(2, 1),
        )),)),
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((ToolCall("call-1", "ktg_count_products", {}),)),
            StopReason.TOOL_CALL, Usage(2, 1),
        )),)),
        ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((TextBlock("done"),)), StopReason.COMPLETED, Usage(3, 2),
        )),)),
    ))

    async def transport(url: str, _headers: Mapping[str, str],
                        _params: Mapping[str, object], _timeout: float) -> object:
        assert url.endswith("/getInfo")
        return {"code": 200, "user": {"userId": 7},
                "permissions": ["mes:md:mditem:list"]}

    spill_dir = tmp_path / "spill"
    if blocked_directory:
        spill_dir.write_text("existing file", encoding="utf-8")
    service = QueryService(provider=provider, model_config=ModelConfig("fake", 128),
                           ktg_client_factory=lambda token: KTGClient(
                               base_url="http://127.0.0.1:8080", authorization=token,
                               transport=transport),
                           spill_settings=SpillSettings(200, spill_dir))
    service._catalog = build_legacy_catalog((tool,))
    db = tmp_path / "sessions.sqlite3"
    repo = SessionRepository(db)
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "request", "hash", {})

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        return await service.run_session(run_id=run_id, message=message, history=history,
                                         page_context=page_context, authorization=authorization,
                                         event_sink=sink, on_agent_ready=register_agent)

    coordinator = RunCoordinator(repo, execute, service.budget_snapshot)
    with caplog.at_level(logging.WARNING):
        coordinator.schedule(run, "question", "Bearer test")
        coordinator.close()
    reopened = SessionRepository(db)
    saved = reopened.get_run("id:7", run.id)
    assert saved is not None and saved.status == "completed"
    results = [record.message for record in reopened.list_messages("id:7", session.id).items
               if isinstance(record.message, ToolResultMessage)]
    business = [message for message in results
                if message.details is not None and "rows" in message.details]
    assert len(business) == 1
    first = business[0]
    assert first.details is not None and first.details["rows"] == 7
    if blocked_directory:
        assert first.content[0].text == original
        assert "spill" not in first.details
        assert "tool result spill failed" in caplog.text
        assert original not in caplog.text
    else:
        assert first.content[0].text != original
        assert "完整内容路径" in first.content[0].text
        spill = first.details["spill"]
        assert isinstance(spill, dict)
        assert Path(str(spill["path"])).read_text(encoding="utf-8") == original
    assert saved.usage is not None and "estimate" not in str(saved.usage)
    assert all("estimate" not in str(request) for request in saved.model_requests)
    settled = reopened.list_events("id:7", run.id).items[-1]
    assert settled.type == "run_settled" and "estimate" not in str(settled.payload["usage"])


def test_service_defaults_to_no_spill_hook(tmp_path: Path) -> None:
    service = QueryService(provider=FakeProvider(()), model_config=ModelConfig("fake", 128),
                           ktg_client_factory=lambda _: cast(KTGClient, object()))
    assert service._tool_runner()._after_hooks == ()
    assert not (tmp_path / "spill").exists()
