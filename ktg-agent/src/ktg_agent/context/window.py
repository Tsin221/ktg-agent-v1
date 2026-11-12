"""Deterministic model request window assembly over complete message groups."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import floor, isfinite

from ktg_agent.context.tokens import estimate_messages_tokens
from ktg_agent.core.messages import (
    AssistantMessage, Message, MessageValidationError, SystemMessage, ToolCall,
    ToolResultMessage,
)


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    window_tokens: int
    reserve_tokens: int
    threshold_ratio: float = 0.8
    retain_ratio: float = 0.16

    def __post_init__(self) -> None:
        if type(self.window_tokens) is not int or self.window_tokens < 1:
            raise ValueError("window_tokens must be a positive integer")
        if (type(self.reserve_tokens) is not int or self.reserve_tokens < 1
                or self.reserve_tokens >= self.window_tokens):
            raise ValueError("reserve_tokens must be positive and less than window_tokens")
        if (isinstance(self.threshold_ratio, bool)
                or not isinstance(self.threshold_ratio, int | float)
                or not isfinite(self.threshold_ratio)
                or not 0 < self.threshold_ratio <= 1):
            raise ValueError("threshold_ratio must be greater than 0 and at most 1")
        if (isinstance(self.retain_ratio, bool)
                or not isinstance(self.retain_ratio, int | float)
                or not isfinite(self.retain_ratio)
                or not 0 < self.retain_ratio < self.threshold_ratio):
            raise ValueError("retain_ratio must be greater than 0 and less than threshold_ratio")


@dataclass(frozen=True, slots=True)
class MessageGroup:
    start_index: int
    messages: tuple[Message, ...]
    estimated_tokens: int
    is_system: bool


@dataclass(frozen=True, slots=True)
class WindowPlan:
    messages: tuple[Message, ...]
    trimmed: bool
    retained_message_count: int
    retained_group_count: int
    kept_start_index: int
    trimmed_group_count: int
    estimated_before: int
    estimated_after: int
    threshold_tokens: int
    available_tokens: int
    largest_group_tokens: int
    spill_count: int


class ContextOverflowError(RuntimeError):
    code = "CONTEXT_OVERFLOW"

    def __init__(self, estimated_tokens: int, available_tokens: int,
                 largest_group_tokens: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.available_tokens = available_tokens
        self.largest_group_tokens = largest_group_tokens
        super().__init__(
            f"context needs {estimated_tokens} tokens; {available_tokens} available "
            f"(largest group: {largest_group_tokens})"
        )


def threshold_tokens(policy: ContextPolicy) -> int:
    return floor(policy.window_tokens * policy.threshold_ratio)


def group_messages(sequence: Sequence[Message]) -> tuple[MessageGroup, ...]:
    groups: list[MessageGroup] = []
    all_calls: set[str] = set()
    index = 0
    while index < len(sequence):
        message = sequence[index]
        if isinstance(message, SystemMessage):
            if groups and not all(group.is_system for group in groups):
                raise MessageValidationError("system message must be leading")
            groups.append(MessageGroup(index, (message,), estimate_messages_tokens((message,)), True))
            index += 1
            continue
        if isinstance(message, ToolResultMessage):
            raise MessageValidationError(
                f"tool result references unknown tool call id: {message.tool_call_id}"
            )
        calls = [block.id for block in message.content if isinstance(block, ToolCall)]
        if calls:
            for call_id in calls:
                if call_id in all_calls:
                    raise MessageValidationError(f"duplicate tool call id: {call_id}")
                all_calls.add(call_id)
            pending = set(calls)
            end = index + 1
            while pending and end < len(sequence):
                result = sequence[end]
                if not isinstance(result, ToolResultMessage):
                    break
                if result.tool_call_id not in all_calls:
                    raise MessageValidationError(
                        f"tool result references unknown tool call id: {result.tool_call_id}"
                    )
                if result.tool_call_id not in pending:
                    raise MessageValidationError(
                        f"duplicate result for tool call id: {result.tool_call_id}"
                    )
                pending.remove(result.tool_call_id)
                end += 1
            if pending:
                raise MessageValidationError(f"missing tool results for: {', '.join(sorted(pending))}")
            chunk = tuple(sequence[index:end])
            groups.append(MessageGroup(index, chunk, estimate_messages_tokens(chunk), False))
            index = end
            continue
        groups.append(MessageGroup(index, (message,), estimate_messages_tokens((message,)), False))
        index += 1
    return tuple(groups)


def fit_window(
    sequence: Sequence[Message], policy: ContextPolicy, *, tool_schema_tokens: int,
    spill_flags: Sequence[bool] | None = None,
    required_indices: frozenset[int] = frozenset(),
    optional_system_indices: frozenset[int] = frozenset(),
    preferred_tool_ids: frozenset[str] = frozenset(),
    prioritize_required: bool = False,
) -> WindowPlan:
    if spill_flags is not None and len(spill_flags) != len(sequence):
        raise ValueError("spill_flags must match sequence length")
    groups = group_messages(sequence)
    systems = tuple(group for group in groups if group.is_system)
    tail = tuple(group for group in groups if not group.is_system)
    threshold = threshold_tokens(policy)
    available = policy.window_tokens - policy.reserve_tokens - tool_schema_tokens
    if prioritize_required:
        available += tool_schema_tokens
    before = tool_schema_tokens + sum(group.estimated_tokens for group in groups)
    largest = max((group.estimated_tokens for group in groups), default=0)
    if available <= 0 or (prioritize_required and tool_schema_tokens >= available):
        raise ContextOverflowError(before, available, largest)

    # Trim when either the soft threshold or the hard available budget is exceeded.
    # A request can fit the threshold yet not the window once reserve/tool schemas
    # are accounted for, so the hard budget must also trigger trimming.
    if prioritize_required and (before > threshold or before > available):
        required = tuple(group for group in groups if (
            (group.is_system and group.start_index not in optional_system_indices)
            or group.start_index in required_indices
        ))
        used = tool_schema_tokens + sum(group.estimated_tokens for group in required)
        if used > available:
            raise ContextOverflowError(used, available, max((group.estimated_tokens for group in required), default=0))
        selected_indices = {group.start_index for group in required}
        # Keep the latest exchange when it fits, including unclassified tool
        # batches whose results are still needed on the next model turn.
        latest = next((group for group in reversed(tail) if group.start_index not in selected_indices), None)
        if latest is not None and used + latest.estimated_tokens <= available:
            selected_indices.add(latest.start_index)
            used += latest.estimated_tokens
        # Optional historical evidence is already ordered by priority.
        for group in systems:
            if group.start_index in optional_system_indices and used + group.estimated_tokens <= available:
                selected_indices.add(group.start_index)
                used += group.estimated_tokens
        for group in reversed(tail):
            if (group.start_index not in selected_indices and used + group.estimated_tokens <= available
                    and any(isinstance(message, ToolResultMessage) and message.tool_call_id in preferred_tool_ids
                            for message in group.messages)):
                selected_indices.add(group.start_index)
                used += group.estimated_tokens
        budget = max(1, floor(policy.window_tokens * policy.retain_ratio))
        recent_used = (latest.estimated_tokens if latest is not None
                       and latest.start_index in selected_indices else 0)
        for group in reversed(tail):
            if group.start_index in selected_indices:
                continue
            cost = group.estimated_tokens
            if used + cost <= available and (recent_used == 0 or recent_used + cost <= budget):
                selected_indices.add(group.start_index)
                used += cost
                recent_used += cost
        retained = tuple(group for group in groups if group.start_index in selected_indices)
        kept_start = next((group.start_index for group in retained if not group.is_system), len(sequence))
        trimmed_count = len(groups) - len(retained)
    elif before <= threshold and before <= available:
        retained = groups
        kept_start = 0
        trimmed_count = 0
    else:
        budget = max(1, floor(policy.window_tokens * policy.retain_ratio))
        selected: list[MessageGroup] = []
        used = 0
        for group in reversed(tail):
            if selected and used + group.estimated_tokens > budget:
                break
            selected.append(group)
            used += group.estimated_tokens
        selected.reverse()
        retained = (*systems, *selected)
        kept_start = selected[0].start_index if selected else len(sequence)
        trimmed_count = len(tail) - len(selected)

    after = tool_schema_tokens + sum(group.estimated_tokens for group in retained)
    # Only retained groups (systems plus the selected newest groups) are hard
    # limits. An oversized older group may be trimmed away, so it must not be
    # reported as an overflow on its own.
    largest_retained = max((group.estimated_tokens for group in retained), default=0)
    if after > available or any(group.estimated_tokens > available for group in retained):
        raise ContextOverflowError(after, available, largest_retained)
    messages = tuple(message for group in retained for message in group.messages)
    flags = spill_flags if spill_flags is not None else (False,) * len(sequence)
    kept_indices = (index for group in retained
                    for index in range(group.start_index, group.start_index + len(group.messages)))
    return WindowPlan(
        messages=messages, trimmed=trimmed_count > 0,
        retained_message_count=len(messages), retained_group_count=len(retained),
        kept_start_index=kept_start, trimmed_group_count=trimmed_count,
        estimated_before=before, estimated_after=after, threshold_tokens=threshold,
        available_tokens=available, largest_group_tokens=largest,
        spill_count=sum(bool(flags[index]) for index in kept_indices),
    )
