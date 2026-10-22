"""Provider-independent agent lifecycle and message contracts."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ktg_agent.core.agent import (
        Agent, AgentBusyError, AgentNotRunningError, AgentState, CancellationToken,
    )
    from ktg_agent.core.queues import InputMode, InputQueue

__all__ = [
    "Agent",
    "AgentBusyError",
    "AgentNotRunningError",
    "AgentState",
    "CancellationToken",
    "InputMode",
    "InputQueue",
]


def __getattr__(name: str) -> Any:
    """Keep public exports without importing the loop when reading messages."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = "queues" if name in ("InputMode", "InputQueue") else "agent"
    value = getattr(import_module(f"ktg_agent.core.{module}"), name)
    globals()[name] = value
    return value
