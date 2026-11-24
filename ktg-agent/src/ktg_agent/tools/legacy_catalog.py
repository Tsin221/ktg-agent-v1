"""Explicit catalog adapter for reviewed MES read tools."""

from collections.abc import Iterable

from ktg_agent.tools.catalog import ToolCatalog, ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import ToolDefinition, ToolDefinitionError


_DOMAINS: dict[str, tuple[str, str, PermissionPolicy]] = {
    "ktg_count_products": ("md", "items", PermissionLeaf("mes:md:mditem:list")),
    "ktg_query_stock_summary": ("wm", "stock", PermissionLeaf("mes:wm:wmstock:list")),
    "ktg_query_workorder_overview": ("pro", "workorder", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:execution:query"),
    ))),
    "ktg_query_workorder_materials": ("pro", "materials", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:workorderbom:list"),
        PermissionLeaf("mes:pro:execution:query"), PermissionLeaf("mes:wm:wmstock:list"),
    ))),
    "ktg_query_workorder_quality": ("pro", "quality", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:execution:query"),
    ))),
}


def _factory(definition: ToolDefinition) -> ToolFactory:
    return lambda: definition


def build_legacy_catalog(definitions: Iterable[ToolDefinition]) -> ToolCatalog:
    """Register only the reviewed definitions; do not activate them."""
    manifests: list[ToolManifest] = []
    factories: dict[str, ToolFactory] = {}
    for definition in definitions:
        if definition.name not in _DOMAINS:
            raise ToolDefinitionError("unreviewed legacy tool name")
        module, domain, permission = _DOMAINS[definition.name]
        reference = f"legacy:{definition.name}"
        if reference in factories:
            raise ToolDefinitionError("duplicate legacy tool name")
        factories[reference] = _factory(definition)
        manifests.append(ToolManifest(
            name=definition.name, version=definition.version,
            module=module, domain=domain, label=definition.label,
            description=definition.description,
            permission_policy=permission,
            effect=definition.effect, execution_mode=definition.execution_mode,
            retry_policy=definition.retry_policy, timeout_seconds=definition.timeout_seconds,
            expose_to_model=definition.expose_to_model,
            confirmation_policy="none", proposal_store_effect=False,
            handler_ref=reference,
        ))
    catalog = ToolCatalog(factories)
    for manifest in manifests:
        catalog.register_factory(manifest, factories[manifest.handler_ref])
    return catalog
