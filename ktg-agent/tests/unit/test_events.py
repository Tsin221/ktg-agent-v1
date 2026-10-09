from __future__ import annotations

import pytest

from ktg_agent.core.events import AgentEvent, AgentEventType
from ktg_agent.core.messages import MessageValidationError


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_agent_event_rejects_non_finite_payload_numbers(value: float) -> None:
    with pytest.raises(MessageValidationError, match="finite JSON numbers"):
        AgentEvent(
            run_id="run-1",
            seq=1,
            type=AgentEventType.MESSAGE_UPDATE,
            payload={"metrics": {"score": value}},
        )
