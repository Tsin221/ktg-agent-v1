"""Fixed showcase scope is independent of usernames and role breadth."""

import pytest

from ktg_agent.exploration.scope import (
    SHOWCASE_ENTITIES, SHOWCASE_PROFILE, is_allowed_entity, require_entity,
)
from ktg_agent.ktg.client import KTGError


EXPECTED = frozenset({
    "md.item", "md.workshop", "dv.machinery_type", "dv.machinery", "dv.subject",
    "dv.check_plan", "dv.repair", "tm.tool_type", "tm.tool", "pro.workorder",
    "pro.task", "pro.feedback", "wm.stock", "wm.item_receipt", "wm.issue",
    "md.item_bom", "pro.workorder_bom", "dv.repair_line", "dv.check_machinery",
    "dv.check_subject", "wm.item_receipt_line", "wm.issue_line",
})


def test_showcase_scope_is_exact_immutable_resource_contract() -> None:
    assert SHOWCASE_PROFILE == "showcase_v1"
    assert isinstance(SHOWCASE_ENTITIES, frozenset)
    assert len(SHOWCASE_ENTITIES) == 22 and SHOWCASE_ENTITIES == EXPECTED
    for entity in EXPECTED:
        assert is_allowed_entity(entity)
        require_entity(entity)


@pytest.mark.parametrize("key", [
    "md.client", "md.vendor", "md.workstation", "qc.ipqc", "wm.product_recpt",
    "cal.team", "system.user", "*", "unknown.entity", "MD.ITEM", " md.item",
])
def test_unknown_and_known_outside_entities_fail_identically(key: str) -> None:
    assert is_allowed_entity(key) is False
    with pytest.raises(KTGError) as failure:
        require_entity(key)
    assert failure.value.code == "OUT_OF_SCOPE"
    assert key not in str(failure.value)  # No existence disclosure for unknown keys.
