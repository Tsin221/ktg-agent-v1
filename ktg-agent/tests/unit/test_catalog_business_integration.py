"""Regression coverage for standalone imports and existing MES definitions."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore, build_inventory_tools
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.ktg.workorders import PreviewStore, build_workorder_tools
from ktg_agent.tools import RetryPolicy
from ktg_agent.tools import CatalogIdentity, CatalogSwitches
from ktg_agent.tools.legacy_catalog import build_legacy_catalog
from ktg_agent.server import Settings, create_service
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf


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
        if not definition.name.startswith("ktg_preview_"):
            for missing in definition.required_permissions:
                identity = CatalogIdentity(frozenset(set(definition.required_permissions) - {missing}), "run")
                assert definition.name not in catalog.candidate_snapshot(identity, switches).candidates
    create = catalog.manifest("ktg_preview_create_workorder_draft")
    assert isinstance(create.permission_policy, PermissionAll)
    assert {item.permission for item in create.permission_policy.all_of
            if isinstance(item, PermissionLeaf)} == {
                "mes:pro:workorder:add", "mes:md:mditem:list"}
    edit = catalog.manifest("ktg_preview_edit_workorder_draft")
    assert isinstance(edit.permission_policy, PermissionAll)
    assert {item.permission for item in edit.permission_policy.all_of
            if isinstance(item, PermissionLeaf)} == {
                "mes:pro:workorder:edit", "mes:pro:workorder:query", "mes:md:mditem:list"}
    receipt_user = CatalogIdentity(frozenset({
        "mes:wm:itemrecpt:query", "mes:wm:wmstock:list",
    }), "run")
    receipt_candidates = catalog.candidate_snapshot(receipt_user, switches).candidates
    assert "ktg_preview_item_receipt" in receipt_candidates
    assert "ktg_execute_item_receipt" not in receipt_candidates
    no_stock_read = CatalogIdentity(frozenset({"mes:wm:itemrecpt:query"}), "run")
    assert "ktg_preview_item_receipt" not in catalog.candidate_snapshot(no_stock_read, switches).candidates
    assert not catalog.candidate_snapshot(admin, CatalogSwitches({}, {})).candidates
    assert not catalog.candidate_snapshot(CatalogIdentity(None, "run"), switches).candidates


def test_disabled_inventory_is_not_added_by_adapter() -> None:
    definitions = (*build_query_tools(), *build_workorder_tools(PreviewStore()))
    catalog = build_legacy_catalog(definitions)
    assert catalog.manifest("ktg_preview_create_workorder_draft").proposal_store_effect
    import pytest
    with pytest.raises(KeyError):
        catalog.manifest("ktg_preview_item_receipt")


def test_service_assembles_complete_candidate_set_and_keeps_experiment_off(tmp_path: Path) -> None:
    settings = Settings(
        model_api_key="test", model_base_url="https://example.test/v1",
        model_id="test-model", ktg_base_url="http://127.0.0.1:8080",
        inventory_db_path=str(tmp_path / "inventory.sqlite3"),
        workorder_db_path=str(tmp_path / "workorders.sqlite3"),
        enable_item_receipt=True, enable_production_issue=True,
    )
    service = create_service(settings)
    catalog = service._catalog
    admin = CatalogIdentity(frozenset({"*:*:*"}), "run")
    candidates = catalog.candidate_snapshot(admin, service._switches_provider())
    assert len(catalog._manifests) == 26  # includes two read-only inspection/maintenance subject tools
    assert len(candidates.candidates) == 21
    assert len(service.model_tool_schemas) == 2  # search_tools and load_tools
    for name, manifest in catalog._manifests.items():
        assert catalog.resolve_definition(name, manifest.version).name == name
    assert "ktg_wm_operation_result" in candidates.candidates
    assert {"ktg_dv_machinerytype_tree", "ktg_dv_machinerytype_detail"} <= set(candidates.candidates)
    assert {"ktg_dv_machinery_list", "ktg_dv_machinery_detail",
            "ktg_md_machinery_workshop_options"} <= set(candidates.candidates)
    assert "ktg_pro_workorder_progress" not in candidates.candidates
    assert all(name not in candidates.candidates for name in (
        "ktg_create_workorder_draft", "ktg_edit_workorder_draft",
        "ktg_execute_item_receipt", "ktg_execute_production_issue"))

    progress_settings = replace(settings, enable_workorder_progress=True)
    enabled = create_service(progress_settings)
    assert "ktg_pro_workorder_progress" in enabled._catalog.candidate_snapshot(
        admin, enabled._switches_provider()).candidates
