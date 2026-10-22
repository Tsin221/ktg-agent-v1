"""Regression coverage for standalone imports and existing MES definitions."""

import subprocess
import sys
from pathlib import Path

from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore, build_inventory_tools
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.ktg.workorders import PreviewStore, build_workorder_tools
from ktg_agent.tools import RetryPolicy
from ktg_agent.tools import CatalogIdentity, CatalogSwitches
from ktg_agent.tools.legacy_catalog import build_legacy_catalog


def test_tools_import_in_fresh_process() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import ktg_agent.tools; from ktg_agent.core import Agent, InputQueue"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_existing_definitions_have_permissions_and_nonretryable_previews(tmp_path: Path) -> None:
    definitions = (
        *build_query_tools(), *build_workorder_tools(PreviewStore()),
        *build_inventory_tools(InventoryStore(str(tmp_path / "inventory.db")), frozenset({RECEIPT, ISSUE})),
    )
    assert len(definitions) == 12
    for definition in definitions:
        assert definition.required_permissions, definition.name
        if definition.name.startswith("ktg_preview_"):
            assert definition.retry_policy is RetryPolicy.NEVER, definition.name


def test_business_catalog_resolves_and_filters_existing_tools(tmp_path: Path) -> None:
    definitions = (
        *build_query_tools(), *build_workorder_tools(PreviewStore()),
        *build_inventory_tools(InventoryStore(str(tmp_path / "inventory.db")), frozenset({RECEIPT, ISSUE})),
    )
    catalog = build_legacy_catalog(definitions)
    switches = CatalogSwitches(
        {"md": True, "pro": True, "wm": True},
        {definition.name: True for definition in definitions},
    )
    admin = CatalogIdentity(frozenset({"*:*:*"}), "run")
    assert len(catalog.candidate_snapshot(admin, switches).candidates) == 8
    for definition in definitions:
        assert catalog.resolve_definition(definition.name, definition.version) is definition
        if not definition.expose_to_model:
            assert definition.name not in catalog.candidate_snapshot(admin, switches).candidates
        for missing in definition.required_permissions:
            identity = CatalogIdentity(frozenset(set(definition.required_permissions) - {missing}), "run")
            assert definition.name not in catalog.candidate_snapshot(identity, switches).candidates
    assert not catalog.candidate_snapshot(admin, CatalogSwitches({}, {})).candidates
    assert not catalog.candidate_snapshot(CatalogIdentity(None, "run"), switches).candidates


def test_disabled_inventory_is_not_added_by_adapter() -> None:
    definitions = (*build_query_tools(), *build_workorder_tools(PreviewStore()))
    catalog = build_legacy_catalog(definitions)
    assert catalog.manifest("ktg_preview_create_workorder_draft").proposal_store_effect
    import pytest
    with pytest.raises(KeyError):
        catalog.manifest("ktg_preview_item_receipt")
