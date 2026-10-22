"""Explicit migration adapter for already-built, process-owned MES definitions.

This does not construct stores or activate tools. Full lazy business factories
and application wiring remain the integration task's responsibility.
"""

from collections.abc import Iterable

from ktg_agent.tools.catalog import ToolCatalog, ToolFactory, ToolManifest
from ktg_agent.tools.permissions import legacy_permission_policy
from ktg_agent.tools.registry import ToolDefinition, ToolDefinitionError


_DOMAINS = {
    "ktg_count_products": ("md", "items"),
    "ktg_search_items": ("md", "items"),
    "ktg_query_stock": ("wm", "stock"),
    "ktg_list_workorders": ("pro", "workorder"),
    "ktg_preview_create_workorder_draft": ("pro", "workorder"),
    "ktg_preview_edit_workorder_draft": ("pro", "workorder"),
    "ktg_create_workorder_draft": ("pro", "workorder"),
    "ktg_edit_workorder_draft": ("pro", "workorder"),
    "ktg_preview_item_receipt": ("wm", "item_receipt"),
    "ktg_preview_production_issue": ("wm", "production_issue"),
    "ktg_execute_item_receipt": ("wm", "item_receipt"),
    "ktg_execute_production_issue": ("wm", "production_issue"),
}


def _factory(definition: ToolDefinition) -> ToolFactory:
    return lambda: definition


def build_legacy_catalog(definitions: Iterable[ToolDefinition]) -> ToolCatalog:
    """Adapt only reviewed names; preserve conditional inventory registration.

Callers must supply definitions bound only to process-owned stores, never to a
user client/token. All supported input paths use conservative AND permissions.
No switch is enabled by building this catalog.
"""
    manifests: list[ToolManifest] = []
    factories: dict[str, ToolFactory] = {}
    for definition in definitions:
        if definition.name not in _DOMAINS:
            raise ToolDefinitionError("unreviewed legacy tool name")
        module, domain = _DOMAINS[definition.name]
        reference = f"legacy:{definition.name}"
        if reference in factories:
            raise ToolDefinitionError("duplicate legacy tool name")
        factories[reference] = _factory(definition)
        manifests.append(ToolManifest(
            name=definition.name, version=definition.version,
            module=module, domain=domain, label=definition.label,
            description=definition.description,
            permission_policy=legacy_permission_policy(definition.required_permissions),
            effect=definition.effect, execution_mode=definition.execution_mode,
            retry_policy=definition.retry_policy, timeout_seconds=definition.timeout_seconds,
            expose_to_model=definition.expose_to_model,
            confirmation_policy="none" if definition.expose_to_model else "trusted_confirmation",
            proposal_store_effect=definition.name.startswith("ktg_preview_"),
            handler_ref=reference,
        ))
    catalog = ToolCatalog(factories)
    for manifest in manifests:
        catalog.register_factory(manifest, factories[manifest.handler_ref])
    return catalog
