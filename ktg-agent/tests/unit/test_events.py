from __future__ import annotations

import pytest
from typing import cast

from ktg_agent.core.events import AgentEvent, AgentEventType
from ktg_agent.core.messages import JsonObject, JsonValue, MessageValidationError


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


NEW_PAYLOADS: dict[AgentEventType, JsonObject] = {
    AgentEventType.CONTEXT_ASSEMBLED: {
        "estimated_tokens": 1, "threshold_tokens": 0, "summary_id": None,
        "retained_message_count": 0, "spill_count": 0, "trimmed": False,
    },
    AgentEventType.COMPACTION_STARTED: {
        "session_id": "s1", "trigger": "preflight", "parent_run_id": None,
        "source_message_count": 1, "covers_start_index": 0, "covers_end_index": 0,
        "covers_start_message_id": None, "covers_end_message_id": None,
    },
    AgentEventType.COMPACTION_SUMMARY: {
        "summary_id": None, "source_token_estimate": 2,
        "summary_token_estimate": 1, "call_id": "c1",
    },
    AgentEventType.COMPACTION_FAILED: {
        "error_code": "COMPACTION_FAILED", "stage": "summary",
    },
    AgentEventType.CONTEXT_OVERFLOW: {
        "estimated_tokens": 2, "available_tokens": 0, "largest_group_tokens": 1,
    },
}


@pytest.mark.parametrize("event_type", tuple(NEW_PAYLOADS))
def test_new_event_payloads_require_every_field(event_type: AgentEventType) -> None:
    payload = NEW_PAYLOADS[event_type]
    AgentEvent("run", 1, event_type, payload)
    for key in payload:
        incomplete = {name: value for name, value in payload.items() if name != key}
        with pytest.raises(MessageValidationError, match=key):
            AgentEvent("run", 1, event_type, incomplete)


@pytest.mark.parametrize("event_type", tuple(NEW_PAYLOADS))
def test_new_event_payloads_reject_wrong_type_for_every_field(event_type: AgentEventType) -> None:
    for key in NEW_PAYLOADS[event_type]:
        with pytest.raises(MessageValidationError, match=key):
            AgentEvent("run", 1, event_type, {**NEW_PAYLOADS[event_type], key: []})


def test_compaction_range_must_be_ordered() -> None:
    payload = {**NEW_PAYLOADS[AgentEventType.COMPACTION_STARTED], "covers_start_index": 2}
    with pytest.raises(MessageValidationError, match="covers_start_index"):
        AgentEvent("run", 1, AgentEventType.COMPACTION_STARTED, payload)


@pytest.mark.parametrize("event_type,key,value", [
    (AgentEventType.CONTEXT_ASSEMBLED, "estimated_tokens", True),
    (AgentEventType.CONTEXT_ASSEMBLED, "trimmed", 1),
    (AgentEventType.COMPACTION_STARTED, "trigger", "other"),
    (AgentEventType.COMPACTION_STARTED, "covers_end_index", True),
    (AgentEventType.COMPACTION_STARTED, "covers_start_message_id", True),
    (AgentEventType.COMPACTION_SUMMARY, "summary_token_estimate", 0),
    (AgentEventType.COMPACTION_FAILED, "stage", "other"),
    (AgentEventType.CONTEXT_OVERFLOW, "available_tokens", -1),
])
def test_new_event_payloads_reject_invalid_values(
    event_type: AgentEventType, key: str, value: object,
) -> None:
    with pytest.raises(MessageValidationError, match=key):
        AgentEvent("run", 1, event_type, {**NEW_PAYLOADS[event_type], key: cast(JsonValue, value)})


def test_existing_event_payload_remains_unrestricted() -> None:
    event = AgentEvent("run", 1, AgentEventType.MESSAGE_UPDATE, {"arbitrary": True})
    assert event.payload == {"arbitrary": True}
