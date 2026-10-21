"""Deterministic context sizing and optional tool-result spill."""

from ktg_agent.context.tokens import TokenEstimate, estimate_request, estimate_text_tokens
from ktg_agent.context.spill import SpillSettings, spill_tool_result

__all__ = (
    "TokenEstimate", "estimate_text_tokens", "estimate_request",
    "SpillSettings", "spill_tool_result",
)
