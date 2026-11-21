"""Reviewed showcase resources, intersected with call-time role permissions.

This resource boundary is a product scope, never an authorization grant. Even
administrators remain inside it; current account permissions are checked by
the execution layer separately. Runtime configuration never reads task docs.
"""
from __future__ import annotations

from typing import Final

from ktg_agent.ktg.client import KTGError


SHOWCASE_PROFILE: Final = "showcase_v1"
SHOWCASE_ENTITIES: Final[frozenset[str]] = frozenset({
    # Fifteen audited demo pages.
    "md.item", "md.workshop", "dv.machinery_type", "dv.machinery", "dv.subject",
    "dv.check_plan", "dv.repair", "tm.tool_type", "tm.tool", "pro.workorder",
    "pro.task", "pro.feedback", "wm.stock", "wm.item_receipt", "wm.issue",
    # Seven supporting resources needed to read those pages and their details.
    "md.item_bom", "pro.workorder_bom", "dv.repair_line", "dv.check_machinery",
    "dv.check_subject", "wm.item_receipt_line", "wm.issue_line",
})


def is_allowed_entity(key: str) -> bool:
    return key in SHOWCASE_ENTITIES


def require_entity(key: str) -> None:
    """Reject unknown and known out-of-scope keys with the same diagnostic."""
    if not is_allowed_entity(key):
        raise KTGError("OUT_OF_SCOPE", "该资源不在当前展示范围内。", status=403)
