"""Store oversized successful read results while preserving audit details."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ktg_agent.context.tokens import estimate_text_tokens
from ktg_agent.core.messages import JsonObject
from ktg_agent.tools.registry import ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus

logger = logging.getLogger(__name__)

_NOTICE = (
    "\n[已省略 {omitted_chars} 个字符，约 {omitted_tokens} tokens。"
    "完整内容已保存为服务端文件；此结果不可作为业务事实依据，"
    "需要按当前权限使用工具重新查询。]\n"
)


@dataclass(frozen=True, slots=True)
class SpillSettings:
    max_inline_tokens: int
    directory: Path

    def __post_init__(self) -> None:
        if (not isinstance(self.max_inline_tokens, int)
                or isinstance(self.max_inline_tokens, bool)
                or self.max_inline_tokens <= 0):
            raise ValueError("max_inline_tokens must be a positive integer")
        if not self.directory.is_absolute():
            raise ValueError("spill directory must be an absolute path")


def _longest_edge(text: str, budget: int, *, suffix: bool = False) -> str:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[-middle:] if suffix else text[:middle]
        if estimate_text_tokens(candidate) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[-low:] if suffix else text[:low]


def _omission_notice(omitted_chars: int, omitted_tokens: int) -> str:
    return _NOTICE.format(omitted_chars=omitted_chars, omitted_tokens=omitted_tokens)


def _recovery_line(target: Path) -> str:
    return f"\n完整内容路径：{target}"


def spill_tool_result(
    result: ToolResult, settings: SpillSettings, *,
    run_id: str, tool_call_id: str, effect: ToolEffect,
) -> ToolResult:
    if (result.status is not ToolStatus.SUCCEEDED or effect is not ToolEffect.READ
            or estimate_text_tokens(result.content) <= settings.max_inline_tokens):
        return result
    if result.details is not None and "spill" in result.details:
        return result

    digest = hashlib.sha256(f"{run_id}\0{tool_call_id}".encode()).hexdigest()[:32]
    target = settings.directory / f"{digest}.txt"
    original_tokens = estimate_text_tokens(result.content)

    # Reserve the notice (including the recovery path) before spending any budget on
    # content. Worst-case omission numbers are used so the real notice cannot grow.
    notice_tokens = estimate_text_tokens(
        _omission_notice(len(result.content), original_tokens) + _recovery_line(target))
    if settings.max_inline_tokens <= notice_tokens + 2:
        return result

    content_budget = settings.max_inline_tokens - notice_tokens
    half = max(1, content_budget // 2)
    prefix = _longest_edge(result.content, half)
    suffix = _longest_edge(result.content[len(prefix):], half, suffix=True)
    omitted = result.content[len(prefix):len(result.content) - len(suffix)]
    omitted_chars = len(omitted)
    if omitted_chars == 0:
        return result
    omitted_tokens = estimate_text_tokens(omitted)
    content = (
        f"{prefix}{_omission_notice(omitted_chars, omitted_tokens)}"
        f"{suffix}{_recovery_line(target)}"
    )
    # Never let spill inflate the model-visible text.
    if estimate_text_tokens(content) >= original_tokens:
        return result

    try:
        settings.directory.mkdir(parents=True, exist_ok=True)
        if target.resolve().parent != settings.directory.resolve():
            raise OSError("spill target escaped directory")
        try:
            with target.open("x", encoding="utf-8") as output:
                output.write(result.content)
        except FileExistsError:
            pass
    except OSError as exc:
        logger.warning("tool result spill failed run_id=%s tool_call_id=%s exception_type=%s",
                       run_id, tool_call_id, type(exc).__name__)
        return result

    details: JsonObject = {**(result.details or {}), "spill": {
        "path": str(target), "omitted_chars": omitted_chars,
    }}
    return replace(result, content=content, details=details)


class SpillAfterHook:
    def __init__(self, settings: SpillSettings) -> None:
        self._settings = settings

    async def __call__(
        self, context: ToolContext, tool: ToolDefinition,
        arguments: Mapping[str, Any], result: ToolResult,
    ) -> ToolResult:
        return spill_tool_result(
            result, self._settings, run_id=context.run_id,
            tool_call_id=context.tool_call_id, effect=tool.effect,
        )
