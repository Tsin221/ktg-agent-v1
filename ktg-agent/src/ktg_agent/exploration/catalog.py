"""Immutable, reviewed business facts for exploration tools.

``operators``/``sortable`` describe the existing HTTP backend, not a promise
that the generic query engine or an external semantic index exists. Permissions
are conjunctions and must be checked again at execution, not just discovery.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from ktg_agent.exploration.catalog_data import DATA, FieldData


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    wire: str
    type: str
    searchable: bool = False
    sortable: bool = False
    operators: tuple[str, ...] = ()
    list_filter: str | None = None


@dataclass(frozen=True, slots=True)
class RelationSpec:
    key: str
    source: str
    target: str
    source_field: str
    target_field: str
    cardinality: str
    evidence: str = "explicit_id"
    note: str = ""


@dataclass(frozen=True, slots=True)
class EntitySpec:
    key: str
    module: str
    label: str
    path: str
    id_field: str
    fields: Mapping[str, FieldSpec]
    permissions: Mapping[str, tuple[str, ...]]
    code_field: str | None = None
    name_field: str | None = None
    status: str = "supported"
    relations: tuple[RelationSpec, ...] = ()
    list_shape: str = "page"


@dataclass(frozen=True, slots=True)
class ApiSpec:
    endpoint_id: str
    entity: str
    operation: str
    path: str
    permissions: tuple[str, ...]
    parameters: Mapping[str, str]
    method: str = "GET"


def _field(data: FieldData) -> FieldSpec:
    return FieldSpec(*data)


def _build_entities() -> Mapping[str, EntitySpec]:
    result: dict[str, EntitySpec] = {}
    for key, module, label, path, id_field, fields, permissions, code, name in DATA:
        result[key] = EntitySpec(
            key=key, module=module, label=label, path=path, id_field=id_field,
            fields=MappingProxyType({field[0]: _field(field) for field in fields}),
            permissions=MappingProxyType(dict(permissions)),
            code_field=code, name_field=name,
            list_shape="array" if key == "dv.machinery_type" else "page",
        )

    # Source/target fields are catalog logical keys, never SQL expressions.
    # Each reverse edge is separately registered with its own target permissions.
    edges = (
        ("workorder.tasks", "pro.workorder", "pro.task", "id", "workorder_id", "task.workorder", ""),
        ("workorder.feedback", "pro.workorder", "pro.feedback", "id", "workorder_id", "feedback.workorder", ""),
        ("task.feedback", "pro.task", "pro.feedback", "id", "task_id", "feedback.task", ""),
        ("workorder.bom", "pro.workorder", "pro.workorder_bom", "id", "workorder_id", "bom.workorder", "需求数量不是实际领料"),
        ("workorder.issues", "pro.workorder", "wm.issue", "id", "workorder_id", "issue.workorder", ""),
        ("task.issues", "pro.task", "wm.issue", "id", "task_id", "issue.task", ""),
        ("machinery.repairs", "dv.machinery", "dv.repair", "id", "machinery_id", "repair.machinery", ""),
        ("repair.lines", "dv.repair", "dv.repair_line", "id", "repair_id", "line.repair", "验收字段位于维修头，不是独立验收单"),
        ("subject.repair_lines", "dv.subject", "dv.repair_line", "id", "subject_id", "line.subject", ""),
        ("item.workorders", "md.item", "pro.workorder", "id", "product_id", "workorder.product", ""),
        ("workshop.machinery", "md.workshop", "dv.machinery", "id", "workshop_id", "machinery.workshop", ""),
        ("issue.lines", "wm.issue", "wm.issue_line", "id", "issue_id", "issue_line.issue", ""),
        ("receipt.lines", "wm.item_receipt", "wm.item_receipt_line", "id", "recpt_id", "receipt_line.receipt", "物料入库，不是成品入库"),
        ("item.bom", "md.item", "md.item_bom", "id", "item_id", "item_bom.product", "产品BOM不是工单实际领料"),
        ("item.component_of", "md.item", "md.item_bom", "id", "bom_item_id", "item_bom.component", "BOM物料和所属产品是不同角色"),
        ("item.workorder_bom", "md.item", "pro.workorder_bom", "id", "item_id", "workorder_bom.item", "需求不是已发料数量"),
        ("item.receipt_lines", "md.item", "wm.item_receipt_line", "id", "item_id", "receipt_line.item", ""),
        ("item.issue_lines", "md.item", "wm.issue_line", "id", "item_id", "issue_line.item", ""),
        ("item.stock", "md.item", "wm.stock", "id", "item_id", "stock.item", "同物料库存不是某工单已入库证据"),
        ("stock.issue_lines", "wm.stock", "wm.issue_line", "id", "material_stock_id", "issue_line.stock", "按明确库存记录ID关联，不按相同批次猜测"),
        ("workorder.stock", "pro.workorder", "wm.stock", "id", "workorder_id", "stock.workorder", "库存归属字段，不证明独立成品入库流程已完成"),
        ("machinery_type.machinery", "dv.machinery_type", "dv.machinery", "id", "machinery_type_id", "machinery.type", ""),
        ("machinery_type.children", "dv.machinery_type", "dv.machinery_type", "id", "parent_type_id", "machinery_type.parent", "分类层级，不代表设备实际生产关系"),
        ("plan.machinery_config", "dv.check_plan", "dv.check_machinery", "id", "plan_id", "machinery_config.plan", "计划不等于执行记录"),
        ("plan.subject_config", "dv.check_plan", "dv.check_subject", "id", "plan_id", "subject_config.plan", "计划不等于执行记录"),
        ("machinery.plan_config", "dv.machinery", "dv.check_machinery", "id", "machinery_id", "plan_config.machinery", ""),
        ("subject.plan_config", "dv.subject", "dv.check_subject", "id", "subject_id", "plan_config.subject", ""),
        ("tool_type.tools", "tm.tool_type", "tm.tool", "id", "tool_type_id", "tool.type", ""),
    )
    relations: dict[str, list[RelationSpec]] = {key: [] for key in result}
    for key, source, target, source_field, target_field, reverse, note in edges:
        if source_field not in result[source].fields or target_field not in result[target].fields:
            raise ValueError(f"Invalid audited relation: {key}")
        relations[source].append(RelationSpec(
            key, source, target, source_field, target_field, "one_to_many", note=note,
        ))
        relations[target].append(RelationSpec(
            reverse, target, source, target_field, source_field, "many_to_one", note=note,
        ))
    return MappingProxyType({
        key: replace(entity, relations=tuple(relations[key]))
        for key, entity in result.items()
    })


ENTITIES = _build_entities()


def entities() -> tuple[EntitySpec, ...]:
    """Return all reviewed resources; callers still enforce role visibility."""
    return tuple(ENTITIES.values())


def get_entity(key: str) -> EntitySpec:
    """Unknown entity keys fail closed (there is no arbitrary table fallback)."""
    return ENTITIES[key]


def _build_apis() -> Mapping[str, ApiSpec]:
    result: dict[str, ApiSpec] = {}
    for entity in entities():
        for operation in entity.permissions:
            endpoint_id = f"{entity.key}.{operation}"
            parameters: dict[str, str] = {}
            if operation == "list":
                path = entity.path + "/list"
                if entity.list_shape == "page":
                    parameters.update({"pageNum": "integer", "pageSize": "integer"})
                parameters.update({
                    field.list_filter: field.type for field in entity.fields.values()
                    if field.list_filter is not None
                })
            else:
                path = entity.path + "/{record_id}"
                parameters["record_id"] = "integer"
            result[endpoint_id] = ApiSpec(
                endpoint_id, entity.key, operation, path,
                entity.permissions[operation], MappingProxyType(parameters),
            )
    return MappingProxyType(result)


READ_APIS = _build_apis()
