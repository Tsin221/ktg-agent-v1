"""Explicit migration adapter for already-built, process-owned MES definitions.

This does not construct stores or activate tools. Full lazy business factories
and application wiring remain the integration task's responsibility.
"""

from collections.abc import Iterable

from ktg_agent.tools.catalog import ToolCatalog, ToolFactory, ToolManifest
from ktg_agent.tools.permissions import (PermissionAll, PermissionAny, PermissionLeaf,
                                         PermissionPolicy, legacy_permission_policy)
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
    "ktg_wm_operation_result": ("wm", "operation_result"),
    "ktg_pro_workorder_get": ("pro", "workorder"),
    "ktg_pro_task_list": ("pro", "protask"),
    "ktg_pro_task_get": ("pro", "protask"),
    "ktg_pro_feedback_list": ("pro", "feedback"),
    "ktg_pro_feedback_get": ("pro", "feedback"),
}


def _factory(definition: ToolDefinition) -> ToolFactory:
    return lambda: definition


def build_legacy_catalog(
    definitions: Iterable[ToolDefinition], *,
    extra_manifests: Iterable[ToolManifest] = (),
    extra_factories: dict[str, ToolFactory] | None = None,
) -> ToolCatalog:
    """Adapt only reviewed names; preserve conditional inventory registration.

Callers must supply definitions bound only to process-owned stores, never to a
user client/token. All supported input paths use conservative AND permissions.
No switch is enabled by building this catalog.
"""
    manifests: list[ToolManifest] = []
    factories: dict[str, ToolFactory] = {}
    extra_manifests = tuple(extra_manifests)
    extension_names = {manifest.name for manifest in extra_manifests}
    extension_by_name = {manifest.name: manifest for manifest in extra_manifests}
    for definition in definitions:
        if definition.name in extension_names:
            manifest = extension_by_name[definition.name]
            for field in ("name", "version", "label", "description", "effect",
                          "execution_mode", "retry_policy", "timeout_seconds", "expose_to_model"):
                if getattr(definition, field) != getattr(manifest, field):
                    raise ToolDefinitionError(f"extension manifest/definition mismatch: {field}")
            continue
        if definition.name not in _DOMAINS:
            raise ToolDefinitionError("unreviewed legacy tool name")
        module, domain = _DOMAINS[definition.name]
        reference = f"legacy:{definition.name}"
        if reference in factories:
            raise ToolDefinitionError("duplicate legacy tool name")
        factories[reference] = _factory(definition)
        policy = _reviewed_policy(definition)
        manifests.append(ToolManifest(
            name=definition.name, version=definition.version,
            module=module, domain=domain, label=definition.label,
            description=definition.description,
            permission_policy=policy,
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
    for reference, factory in (extra_factories or {}).items():
        if reference in factories:
            raise ToolDefinitionError("duplicate catalog factory reference")
        factories[reference] = factory
    # Rebuild with the complete static allow-list before registering extension manifests.
    if extra_manifests:
        catalog = ToolCatalog(factories)
        for manifest in manifests:
            catalog.register_factory(manifest, factories[manifest.handler_ref])
        for manifest in extra_manifests:
            catalog.register_factory(manifest, factories[manifest.handler_ref])
    return catalog


def _reviewed_policy(definition: ToolDefinition) -> PermissionPolicy:
    name = definition.name
    if name in {"ktg_count_products", "ktg_search_items"}:
        return PermissionLeaf("mes:md:mditem:list")
    if name == "ktg_query_stock":
        return PermissionLeaf("mes:wm:wmstock:list")
    if name == "ktg_list_workorders":
        return PermissionLeaf("mes:pro:workorder:list")
    if name == "ktg_preview_create_workorder_draft":
        return PermissionAll((PermissionLeaf("mes:pro:workorder:add"), PermissionLeaf("mes:md:mditem:list")))
    if name == "ktg_preview_edit_workorder_draft":
        return PermissionAll((PermissionLeaf("mes:pro:workorder:edit"), PermissionLeaf("mes:pro:workorder:query"), PermissionLeaf("mes:md:mditem:list")))
    if name in {"ktg_preview_item_receipt", "ktg_execute_item_receipt",
                "ktg_preview_production_issue", "ktg_execute_production_issue"}:
        is_receipt = "item_receipt" in name
        document = "itemrecpt" if is_receipt else "issueheader"
        action = "edit" if name.startswith("ktg_execute_") else "query"
        stock = PermissionAny((PermissionLeaf("mes:wm:wmstock:list"), PermissionLeaf("mes:wm:wmstock:query")))
        return PermissionAll((PermissionLeaf(f"mes:wm:{document}:{action}"),
                              PermissionLeaf(f"mes:wm:{document}:query"), stock))
    if name == "ktg_wm_operation_result":
        stock = PermissionAny((PermissionLeaf("mes:wm:wmstock:list"), PermissionLeaf("mes:wm:wmstock:query")))
        receipt = PermissionAll((PermissionLeaf("mes:wm:itemrecpt:query"), stock))
        issue = PermissionAll((PermissionLeaf("mes:wm:issueheader:query"), stock))
        return PermissionAny((receipt, issue))
    if name in {"ktg_pro_workorder_get", "ktg_pro_task_list", "ktg_pro_task_get",
                "ktg_pro_feedback_list", "ktg_pro_feedback_get"}:
        permission = definition.required_permissions[0]
        return PermissionLeaf(permission)
    return legacy_permission_policy(definition.required_permissions)
