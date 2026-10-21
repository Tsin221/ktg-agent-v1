"""Spill token budget: notice and recovery path count, and spill never grows text."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ktg_agent.context import SpillSettings, estimate_text_tokens, spill_tool_result
from ktg_agent.core.messages import JsonObject
from ktg_agent.tools import ToolEffect, ToolResult


def _cjk(tokens: int) -> str:
    """One CJK character costs exactly one estimated token."""
    return "测" * tokens


def _target(directory: Path, run_id: str, tool_call_id: str) -> Path:
    digest = hashlib.sha256(f"{run_id}\0{tool_call_id}".encode()).hexdigest()[:32]
    return directory / f"{digest}.txt"


def _split_spilled(content: str, target: Path) -> tuple[str, str, int]:
    prefix, rest = content.split("\n[已省略 ", 1)
    notice, tail = rest.split("]\n", 1)
    suffix, path = tail.split("\n完整内容路径：", 1)
    assert path == str(target)
    omitted_chars = int(notice.split(" 个字符", 1)[0])
    return prefix, suffix, omitted_chars


def test_cjk_result_just_over_limit_is_not_inflated(tmp_path: Path) -> None:
    original_text = _cjk(130)
    original = ToolResult(original_text, details={"rows": 1})
    settings = SpillSettings(64, tmp_path / "spill")

    returned = spill_tool_result(original, settings, run_id="r", tool_call_id="c",
                                 effect=ToolEffect.READ)

    # The notice plus path alone exceed 64 estimated tokens, so spilling here could
    # only grow the text; the original result must be returned untouched.
    assert returned is original
    assert estimate_text_tokens(returned.content) == 130
    assert estimate_text_tokens(returned.content) <= estimate_text_tokens(original_text)
    assert returned.details == {"rows": 1}
    assert not settings.directory.exists()


def test_tiny_threshold_result_returns_original_without_file(tmp_path: Path) -> None:
    original_text = _cjk(18)
    original = ToolResult(original_text)
    settings = SpillSettings(8, tmp_path / "spill")

    returned = spill_tool_result(original, settings, run_id="r", tool_call_id="c",
                                 effect=ToolEffect.READ)

    assert returned is original
    assert returned.details is None
    assert estimate_text_tokens(returned.content) == 18
    assert not settings.directory.exists()


@pytest.mark.parametrize("original_tokens", (201, 220, 260))
def test_near_threshold_spill_stays_within_budget(tmp_path: Path, original_tokens: int) -> None:
    original_text = _cjk(original_tokens)
    original = ToolResult(original_text)
    settings = SpillSettings(200, tmp_path / "spill")
    target = _target(settings.directory, "r", "c")

    returned = spill_tool_result(original, settings, run_id="r", tool_call_id="c",
                                 effect=ToolEffect.READ)

    assert returned is not original
    prefix, suffix, omitted = _split_spilled(returned.content, target)
    assert returned.content.count("已省略") == 1
    assert "完整内容路径" in returned.content
    assert original_text == prefix + original_text[len(prefix):len(original_text) - len(suffix)] + suffix
    assert omitted == original_tokens - len(prefix) - len(suffix) > 0
    assert estimate_text_tokens(returned.content) <= 200
    assert estimate_text_tokens(returned.content) < original_tokens
    assert target.read_text(encoding="utf-8") == original_text
    assert returned.details == {"spill": {"path": str(target), "omitted_chars": omitted}}


def test_large_result_spills_within_budget(tmp_path: Path) -> None:
    original_text = _cjk(600)
    original = ToolResult(original_text, details={"rows": 9})
    settings = SpillSettings(200, tmp_path / "spill")
    target = _target(settings.directory, "run", "call")

    returned = spill_tool_result(original, settings, run_id="run", tool_call_id="call",
                                 effect=ToolEffect.READ)

    prefix, suffix, omitted = _split_spilled(returned.content, target)
    assert estimate_text_tokens(returned.content) <= 200
    assert estimate_text_tokens(returned.content) < 600
    assert omitted == 600 - len(prefix) - len(suffix)
    assert prefix and suffix
    assert target.is_file() and target.read_text(encoding="utf-8") == original_text
    assert len(list(settings.directory.iterdir())) == 1
    assert returned.details is not None
    assert returned.details["rows"] == 9
    assert returned.details["spill"] == {"path": str(target), "omitted_chars": omitted}


def test_already_spilled_result_is_not_rewritten(tmp_path: Path) -> None:
    original = ToolResult(_cjk(600))
    settings = SpillSettings(200, tmp_path / "spill")
    first = spill_tool_result(original, settings, run_id="run", tool_call_id="call",
                              effect=ToolEffect.READ)
    assert first.details is not None and "spill" in first.details

    second = spill_tool_result(first, settings, run_id="run", tool_call_id="call",
                               effect=ToolEffect.READ)

    assert second is first
    assert second.content == first.content
    assert second.details == first.details
    assert second.content.count("已省略") == 1
    assert len(list(settings.directory.iterdir())) == 1


def test_spill_marker_blocks_rewrite_even_above_threshold(tmp_path: Path) -> None:
    marker: JsonObject = {"path": str(tmp_path / "existing.txt"), "omitted_chars": 100}
    oversized = ToolResult("A" * 8000, details={"spill": marker, "rows": 2})
    settings = SpillSettings(200, tmp_path / "spill")

    returned = spill_tool_result(oversized, settings, run_id="r", tool_call_id="c",
                                 effect=ToolEffect.READ)

    assert returned is oversized
    assert returned.content == oversized.content
    assert returned.details == {"spill": marker, "rows": 2}
    assert not settings.directory.exists()
