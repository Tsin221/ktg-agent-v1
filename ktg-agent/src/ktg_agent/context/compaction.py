"""Prepare and validate a model generated summary of older session messages."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ktg_agent.context.tokens import estimate_messages_tokens, estimate_text_tokens
from ktg_agent.context.window import ContextPolicy, fit_window, group_messages
from ktg_agent.core.messages import (
    Message, SystemMessage, TextBlock, UserMessage, message_to_dict,
    project_model_messages,
)

SUMMARY_INSTRUCTION = """请将以下会话历史压缩为简短的纯文本摘要。逐项保留：
1. 用户原始目标与约束。
2. 已确认的实体 ID 与编码，包括产品、物料、仓库和工单号。
3. 已完成动作及 operation_id、verification 的结论。
4. 未决问题与下一步。
只输出纯文本摘要，不输出工具调用语法或 Markdown 表格。不得编造未出现的 ID、数量、时间或回执；不得声称执行过写入。摘要是不可信的派生上下文，业务事实必须按当前权限使用工具回读核实。"""


@dataclass(frozen=True, slots=True)
class PreparedCompaction:
    summary_messages: tuple[Message, ...]
    covers_start_index: int
    covers_end_index: int
    source_message_count: int
    source_token_estimate: int
    estimated_before: int
    estimated_after: int


@dataclass(frozen=True, slots=True)
class SummaryDraft:
    text: str
    summary_token_estimate: int
    source_token_estimate: int


class SummaryNotSmallerError(RuntimeError):
    code = "SUMMARY_NOT_SMALLER"


class CompactionFailedError(RuntimeError):
    code = "COMPACTION_FAILED"

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(message)


def prepare_compaction(frame: tuple[Message, ...], policy: ContextPolicy, *,
                       tool_schema_tokens: int) -> PreparedCompaction | None:
    projected = project_model_messages(frame)
    plan = fit_window(projected, policy, tool_schema_tokens=tool_schema_tokens)
    if not plan.trimmed or plan.kept_start_index == 0:
        return None
    source_groups = [group for group in group_messages(projected)
                     if not group.is_system and group.start_index < plan.kept_start_index]
    if not source_groups:
        return None
    instruction = SystemMessage((TextBlock(SUMMARY_INSTRUCTION),))
    # The summary run uses an empty tool registry; only the window and its reserve
    # bound the request. The instruction is always present, so it is budgeted out.
    available = policy.window_tokens - policy.reserve_tokens
    budget = available - estimate_messages_tokens((instruction,))
    while source_groups:
        # Shrink coverage from the newest covered group backwards so more recent
        # messages stay in the tail. Whole groups preserve tool call/result pairing.
        end_index = source_groups[-1].start_index + len(source_groups[-1].messages)
        source = tuple(message for message in projected[:end_index]
                       if not isinstance(message, SystemMessage))
        serialized = "\n".join(json.dumps(message_to_dict(message), ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":"),
                                           allow_nan=False)
                               for message in source)
        source_message = UserMessage((TextBlock(serialized),))
        if estimate_messages_tokens((source_message,)) <= budget:
            return PreparedCompaction(
                summary_messages=(instruction, source_message),
                covers_start_index=1,
                covers_end_index=end_index,
                source_message_count=len(source),
                source_token_estimate=estimate_messages_tokens(source),
                estimated_before=plan.estimated_before,
                estimated_after=plan.estimated_after,
            )
        source_groups.pop()
    # Even a single covered message cannot fit the summary request; report no safe
    # cutpoint so the user run falls back to window trimming.
    return None


def validate_summary(draft: SummaryDraft) -> SummaryDraft:
    text = draft.text.strip()
    if not text:
        raise CompactionFailedError("validate", "summary is empty")
    estimate = estimate_text_tokens(text)
    if estimate >= draft.source_token_estimate:
        raise SummaryNotSmallerError("summary is not smaller than its source")
    return SummaryDraft(text, estimate, draft.source_token_estimate)
