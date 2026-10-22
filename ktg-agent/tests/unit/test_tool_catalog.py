"""Contract-v1 catalog and permission boundary tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.tools import (
    CatalogIdentity,
    CatalogSwitches,
    DuplicateToolError,
    ExecutionMode,
    PermissionAll,
    PermissionAny,
    PermissionDecision,
    PermissionLeaf,
    PermissionPolicyError,
    RetryPolicy,
    ToolCatalog,
    ToolDefinition,
    ToolDefinitionError,
    ToolEffect,
    ToolManifest,
    ToolContext,
    ToolResult,
    evaluate_permission_policy,
    legacy_permission_policy,
    parse_permission_policy,
)


async def _execute(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(content="ok")


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="ktg_example", version="1.0", label="Example", description="Read example",
        input_schema={"type": "object", "properties": {}}, output_schema=None,
        required_permissions=(), effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, retry_policy=RetryPolicy.BOUNDED_READ,
        timeout_seconds=20, execute=_execute,
    )


def _manifest() -> ToolManifest:
    return ToolManifest(
        name="ktg_example", version="1.0", module="production", domain="pro",
        label="Example", description="Read example",
        permission_policy=PermissionLeaf("mes:pro:workorder:query"),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20,
        expose_to_model=True, confirmation_policy="none",
        proposal_store_effect=False, handler_ref="example",
    )


def test_permission_and_or_exact_and_full_permission() -> None:
    policy = PermissionAll((
        PermissionLeaf("mes:pro:workorder:query"),
        PermissionAny((PermissionLeaf("mes:pro:protask:list"), PermissionLeaf("mes:pro:feedback:list"))),
    ))
    assert evaluate_permission_policy(policy, {"mes:pro:workorder:query", "mes:pro:feedback:list"}) is PermissionDecision.ALLOWED
    assert evaluate_permission_policy(policy, {"mes:pro:workorder:query"}) is PermissionDecision.DENIED
    assert evaluate_permission_policy(policy, {"mes:pro:protask:list"}) is PermissionDecision.DENIED
    assert evaluate_permission_policy(policy, {"*:*:*"}) is PermissionDecision.ALLOWED
    assert evaluate_permission_policy(policy, {"mes:*"}) is PermissionDecision.DENIED
    assert evaluate_permission_policy(policy, None) is PermissionDecision.UNAVAILABLE


def test_permission_json_and_empty_legacy_are_fail_closed() -> None:
    policy = parse_permission_policy({"all_of": [{"permission": "a"}, {"any_of": [{"permission": "b"}]}]})
    assert evaluate_permission_policy(policy, {"a", "b"}) is PermissionDecision.ALLOWED
    invalid_policies: tuple[object, ...] = ({}, {"all_of": []}, {"any_of": []}, {"permission": ""},
                    {"permission": "mes:*"}, {"permission": "a", "any_of": []})
    for invalid in invalid_policies:
        with pytest.raises(PermissionPolicyError):
            parse_permission_policy(invalid)
    with pytest.raises(PermissionPolicyError):
        legacy_permission_policy(())
    assert evaluate_permission_policy(legacy_permission_policy(("a", "b")), {"a"}) is PermissionDecision.DENIED


def test_catalog_metadata_does_not_load_factory_and_resolves_only_allowed_reference() -> None:
    calls = 0

    def factory() -> ToolDefinition:
        nonlocal calls
        calls += 1
        return _definition()

    catalog = ToolCatalog({"example": factory})
    catalog.register_factory(_manifest(), factory)
    snapshot = catalog.candidate_snapshot(
        CatalogIdentity(frozenset({"mes:pro:workorder:query"}), "run-1"),
        CatalogSwitches({"production": True}, {"ktg_example": True}),
    )
    assert snapshot.candidates == {"ktg_example": "1.0"}
    assert snapshot.run_id == "run-1"
    assert snapshot.catalog_version.startswith("contract-v1:")
    assert calls == 0
    assert catalog.resolve_definition("ktg_example", "1.0").name == "ktg_example"
    assert calls == 1
    with pytest.raises(ToolDefinitionError):
        catalog.resolve_definition("ktg_example", "2.0")
    with pytest.raises(DuplicateToolError):
        catalog.register_factory(_manifest(), factory)
    with pytest.raises(ToolDefinitionError):
        ToolCatalog({"example": factory}).register_factory(replace(_manifest(), handler_ref="os.system"), factory)


def test_candidate_visibility_switches_identity_and_permission_are_closed() -> None:
    factory = _definition
    catalog = ToolCatalog({"example": factory})
    catalog.register_factory(_manifest(), factory)
    identity = CatalogIdentity(frozenset({"mes:pro:workorder:query"}), "run-1")
    enabled = CatalogSwitches({"production": True}, {"ktg_example": True})
    assert not catalog.candidate_snapshot(identity, CatalogSwitches({}, {})).candidates
    assert not catalog.candidate_snapshot(identity, replace(enabled, emergency_disabled=True)).candidates
    assert not catalog.candidate_snapshot(CatalogIdentity(frozenset(), "run-1"), enabled).candidates
    unavailable = catalog.candidate_snapshot(CatalogIdentity(None, "run-1"), enabled)
    assert not unavailable.candidates
    assert unavailable.permission_status is PermissionDecision.UNAVAILABLE
    hidden = ToolCatalog({"example": factory})
    hidden.register_factory(replace(_manifest(), expose_to_model=False), factory)
    assert not hidden.candidate_snapshot(identity, enabled).candidates


def test_manifest_rejects_invalid_confirmation_and_factory_mismatch() -> None:
    with pytest.raises(ToolDefinitionError):
        replace(_manifest(), confirmation_policy="trusted_confirmation")
    with pytest.raises(ToolDefinitionError):
        replace(_manifest(), proposal_store_effect=True)
    catalog = ToolCatalog({"example": _definition})
    catalog.register_factory(replace(_manifest(), label="Mismatch"), _definition)
    with pytest.raises(ToolDefinitionError, match="label"):
        catalog.resolve_definition("ktg_example", "1.0")
