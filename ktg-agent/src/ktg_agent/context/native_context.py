"""Native Pydantic AI history and automatic compaction integration.

The application owns the durable journal and the terminal run settlement.
This module only serializes native messages and archives the original working
history before the framework is allowed to rewrite it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from pydantic_ai import ModelRequestContext, RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models import Model
from pydantic_ai_harness import FallbackCompaction, SlidingWindowCompaction, SummarizingCompaction

DepsT = TypeVar('DepsT')
ArchiveCallback = Callable[[RunContext[DepsT], bytes], Awaitable[None]]


def serialize_messages(messages: Sequence[ModelMessage]) -> bytes:
    """Serialize a native message snapshot without projecting to legacy roles."""
    return ModelMessagesTypeAdapter.dump_json(list(messages))


def deserialize_messages(raw_json: bytes) -> list[ModelMessage]:
    """Validate and restore native messages for the next run."""
    return ModelMessagesTypeAdapter.validate_json(raw_json)


def build_context_capabilities(
    archive: ArchiveCallback[DepsT],
    *,
    max_fraction: float = 0.8,
    keep_tokens: int = 20_000,
    context_window: int | None = None,
    summary_model: Model | str | None = None,
) -> tuple[Hooks[DepsT], FallbackCompaction[DepsT]]:
    """Return ordered capabilities: durable snapshot, then native compaction.

    The awaited callback receives the current run context (for routing to the
    authorized session/run) and immutable native JSON. It must durably preserve
    the snapshot without replacing previously archived originals. Snapshots may
    include prior compacted context and overlapping prefixes; deduplication and
    terminal-state persistence belong to the application, not this capability.

    Register the returned capabilities together in their given order, before
    any other history-rewriting capability. Every model step archives before
    compression; an archival failure aborts that step instead of losing evidence.
    """
    async def archive_before_request(
        ctx: RunContext[DepsT], request: ModelRequestContext,
    ) -> ModelRequestContext:
        await archive(ctx, serialize_messages(ctx.messages))
        return request

    return (Hooks(before_model_request=archive_before_request), build_compaction_capability(
        max_fraction=max_fraction, keep_tokens=keep_tokens, context_window=context_window,
        summary_model=summary_model,
    ))


def build_compaction_capability(
    *,
    max_fraction: float = 0.8,
    keep_tokens: int = 20_000,
    context_window: int | None = None,
    summary_model: Model | str | None = None,
) -> FallbackCompaction[DepsT]:
    """Native compaction alone, for the execution's ordered archive hooks."""
    if not 0 < max_fraction < 1:
        raise ValueError('max_fraction must lie between zero and one')
    if keep_tokens <= 0:
        raise ValueError('keep_tokens must be positive')
    if context_window is not None and keep_tokens >= context_window * max_fraction:
        raise ValueError('keep_tokens must be smaller than the compaction trigger')

    return FallbackCompaction(
            max_fraction=max_fraction,
            context_window=context_window,
            fallback_chain=[
                SummarizingCompaction(model=summary_model, keep_tokens=keep_tokens),
                SlidingWindowCompaction(keep_tokens=keep_tokens),
            ],
            fallback_on=(ModelAPIError, FallbackExceptionGroup, UnexpectedModelBehavior, UsageLimitExceeded),
    )
