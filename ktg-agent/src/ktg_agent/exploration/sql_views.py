"""Reviewed physical SQL projections for the fixed showcase, never inferred at runtime.

Column pairs were checked against each source mapper resultMap and select*Vo.
No Java permission or row filter is inherited by a database SELECT-only account.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from ktg_agent.exploration.catalog import ENTITIES
from ktg_agent.exploration.scope import SHOWCASE_ENTITIES, SHOWCASE_PROFILE

_MAPPINGS: dict[str, tuple[str, dict[str, str]]] = {'md.item': ('md_item',
             {'id': 'item_id',
              'code': 'item_code',
              'name': 'item_name',
              'specification': 'specification',
              'item_type_id': 'item_type_id',
              'item_type_code': 'item_type_code',
              'item_type_name': 'item_type_name',
              'item_or_product': 'item_or_product',
              'unit_of_measure': 'unit_of_measure',
              'enable_flag': 'enable_flag'}),
 'md.workshop': ('md_workshop',
                 {'id': 'workshop_id',
                  'code': 'workshop_code',
                  'name': 'workshop_name',
                  'enable_flag': 'enable_flag'}),
 'dv.machinery_type': ('dv_machinery_type',
                       {'id': 'machinery_type_id',
                        'code': 'machinery_type_code',
                        'name': 'machinery_type_name',
                        'parent_type_id': 'parent_type_id',
                        'ancestors': 'ancestors',
                        'enable_flag': 'enable_flag'}),
 'dv.machinery': ('dv_machinery',
                  {'id': 'machinery_id',
                   'code': 'machinery_code',
                   'name': 'machinery_name',
                   'machinery_brand': 'machinery_brand',
                   'specification': 'machinery_spec',
                   'machinery_type_id': 'machinery_type_id',
                   'machinery_type_code': 'machinery_type_code',
                   'machinery_type_name': 'machinery_type_name',
                   'workshop_id': 'workshop_id',
                   'workshop_code': 'workshop_code',
                   'workshop_name': 'workshop_name',
                   'status': 'status'}),
 'dv.subject': ('dv_subject',
                {'id': 'subject_id',
                 'code': 'subject_code',
                 'name': 'subject_name',
                 'subject_type': 'subject_type',
                 'subject_content': 'subject_content',
                 'subject_standard': 'subject_standard',
                 'enable_flag': 'enable_flag'}),
 'dv.check_plan': ('dv_check_plan',
                   {'id': 'plan_id',
                    'code': 'plan_code',
                    'name': 'plan_name',
                    'plan_type': 'plan_type',
                    'status': 'status'}),
 'dv.repair': ('dv_repair',
               {'id': 'repair_id',
                'code': 'repair_code',
                'name': 'repair_name',
                'machinery_id': 'machinery_id',
                'machinery_code': 'machinery_code',
                'machinery_name': 'machinery_name',
                'machinery_brand': 'machinery_brand',
                'machinery_spec': 'machinery_spec',
                'machinery_type_id': 'machinery_type_id',
                'require_date': 'require_date',
                'finish_date': 'finish_date',
                'confirm_date': 'confirm_date',
                'repair_result': 'repair_result',
                'accepted_by': 'accepted_by',
                'confirm_by': 'confirm_by',
                'status': 'status'}),
 'tm.tool_type': ('tm_tool_type',
                  {'id': 'tool_type_id',
                   'code': 'tool_type_code',
                   'name': 'tool_type_name',
                   'mainten_type': 'mainten_type',
                   'mainten_period': 'mainten_period'}),
 'tm.tool': ('tm_tool',
             {'id': 'tool_id',
              'code': 'tool_code',
              'name': 'tool_name',
              'brand': 'brand',
              'spec': 'spec',
              'tool_type_id': 'tool_type_id',
              'tool_type_code': 'tool_type_code',
              'tool_type_name': 'tool_type_name',
              'quantity': 'quantity',
              'quantity_avail': 'quantity_avail',
              'status': 'status'}),
 'pro.workorder': ('pro_workorder',
                   {'id': 'workorder_id',
                    'code': 'workorder_code',
                    'name': 'workorder_name',
                    'product_id': 'product_id',
                    'product_code': 'product_code',
                    'product_name': 'product_name',
                    'product_spc': 'product_spc',
                    'client_id': 'client_id',
                    'client_code': 'client_code',
                    'client_name': 'client_name',
                    'quantity': 'quantity',
                    'quantity_produced': 'quantity_produced',
                    'quantity_changed': 'quantity_changed',
                    'quantity_scheduled': 'quantity_scheduled',
                    'unit_of_measure': 'unit_of_measure',
                    'request_date': 'request_date',
                    'status': 'status',
                    'version': 'version'}),
 'pro.task': ('pro_task',
              {'id': 'task_id',
               'code': 'task_code',
               'name': 'task_name',
               'workorder_id': 'workorder_id',
               'workorder_code': 'workorder_code',
               'workorder_name': 'workorder_name',
               'workstation_id': 'workstation_id',
               'workstation_code': 'workstation_code',
               'workstation_name': 'workstation_name',
               'process_id': 'process_id',
               'process_code': 'process_code',
               'process_name': 'process_name',
               'item_id': 'item_id',
               'item_code': 'item_code',
               'item_name': 'item_name',
               'quantity': 'quantity',
               'quantity_produced': 'quantity_produced',
               'quantity_quanlify': 'quantity_quanlify',
               'quantity_unquanlify': 'quantity_unquanlify',
               'unit_of_measure': 'unit_of_measure',
               'start_time': 'start_time',
               'end_time': 'end_time',
               'status': 'status'}),
 'pro.feedback': ('pro_feedback',
                  {'id': 'record_id',
                   'workorder_id': 'workorder_id',
                   'workorder_code': 'workorder_code',
                   'task_id': 'task_id',
                   'task_code': 'task_code',
                   'process_id': 'process_id',
                   'process_code': 'process_code',
                   'process_name': 'process_name',
                   'item_id': 'item_id',
                   'item_code': 'item_code',
                   'item_name': 'item_name',
                   'quantity': 'quantity',
                   'quantity_feedback': 'quantity_feedback',
                   'quantity_qualified': 'quantity_qualified',
                   'quantity_unquanlified': 'quantity_unquanlified',
                   'feedback_time': 'feedback_time',
                   'status': 'status',
                   'unit_of_measure': 'unit_of_measure'}),
 'wm.stock': ('wm_material_stock',
              {'id': 'material_stock_id',
               'item_id': 'item_id',
               'item_code': 'item_code',
               'item_name': 'item_name',
               'specification': 'specification',
               'unit_of_measure': 'unit_of_measure',
               'warehouse_id': 'warehouse_id',
               'warehouse_code': 'warehouse_code',
               'warehouse_name': 'warehouse_name',
               'location_id': 'location_id',
               'location_name': 'location_name',
               'area_id': 'area_id',
               'area_name': 'area_name',
               'batch_code': 'batch_code',
               'quantity_onhand': 'quantity_onhand',
               'location_code': 'location_code',
               'area_code': 'area_code',
               'workorder_id': 'workorder_id',
               'workorder_code': 'workorder_code',
               'recpt_date': 'recpt_date',
               'expire_date': 'expire_date'}),
 'wm.item_receipt': ('wm_item_recpt',
                     {'id': 'recpt_id',
                      'code': 'recpt_code',
                      'name': 'recpt_name',
                      'po_code': 'po_code',
                      'vendor_id': 'vendor_id',
                      'vendor_code': 'vendor_code',
                      'vendor_name': 'vendor_name',
                      'warehouse_id': 'warehouse_id',
                      'warehouse_code': 'warehouse_code',
                      'warehouse_name': 'warehouse_name',
                      'location_id': 'location_id',
                      'location_code': 'location_code',
                      'location_name': 'location_name',
                      'area_id': 'area_id',
                      'area_code': 'area_code',
                      'area_name': 'area_name',
                      'recpt_date': 'recpt_date',
                      'status': 'status',
                      'version': 'version'}),
 'wm.issue': ('wm_issue_header',
              {'id': 'issue_id',
               'code': 'issue_code',
               'name': 'issue_name',
               'workorder_id': 'workorder_id',
               'workorder_code': 'workorder_code',
               'task_id': 'task_id',
               'task_code': 'task_code',
               'warehouse_id': 'warehouse_id',
               'warehouse_name': 'warehouse_name',
               'issue_date': 'issue_date',
               'status': 'status',
               'version': 'version',
               'warehouse_code': 'warehouse_code',
               'location_id': 'location_id',
               'location_code': 'location_code',
               'location_name': 'location_name',
               'area_id': 'area_id',
               'area_code': 'area_code',
               'area_name': 'area_name'}),
 'md.item_bom': ('md_product_bom',
                 {'id': 'bom_id',
                  'item_id': 'item_id',
                  'bom_item_id': 'bom_item_id',
                  'bom_item_code': 'bom_item_code',
                  'bom_item_name': 'bom_item_name',
                  'bom_item_spec': 'bom_item_spec',
                  'unit_of_measure': 'unit_of_measure',
                  'item_or_product': 'item_or_product',
                  'quantity': 'quantity',
                  'enable_flag': 'enable_flag'}),
 'pro.workorder_bom': ('pro_workorder_bom',
                       {'id': 'line_id',
                        'workorder_id': 'workorder_id',
                        'item_id': 'item_id',
                        'item_code': 'item_code',
                        'item_name': 'item_name',
                        'item_spc': 'item_spc',
                        'quantity': 'quantity',
                        'unit_of_measure': 'unit_of_measure'}),
 'dv.repair_line': ('dv_repair_line',
                    {'id': 'line_id',
                     'repair_id': 'repair_id',
                     'subject_id': 'subject_id',
                     'subject_code': 'subject_code',
                     'subject_name': 'subject_name',
                     'subject_type': 'subject_type',
                     'subject_content': 'subject_content',
                     'subject_standard': 'subject_standard',
                     'malfunction': 'malfunction',
                     'repair_des': 'repair_des'}),
 'dv.check_machinery': ('dv_check_machinery',
                        {'id': 'record_id',
                         'plan_id': 'plan_id',
                         'machinery_id': 'machinery_id',
                         'machinery_code': 'machinery_code',
                         'machinery_name': 'machinery_name'}),
 'dv.check_subject': ('dv_check_subject',
                      {'id': 'record_id',
                       'plan_id': 'plan_id',
                       'subject_id': 'subject_id',
                       'subject_code': 'subject_code',
                       'subject_name': 'subject_name',
                       'subject_type': 'subject_type',
                       'subject_content': 'subject_content',
                       'subject_standard': 'subject_standard'}),
 'wm.item_receipt_line': ('wm_item_recpt_line',
                          {'id': 'line_id',
                           'recpt_id': 'recpt_id',
                           'item_id': 'item_id',
                           'item_code': 'item_code',
                           'item_name': 'item_name',
                           'specification': 'specification',
                           'unit_of_measure': 'unit_of_measure',
                           'quantity_recived': 'quantity_recived',
                           'batch_code': 'batch_code',
                           'warehouse_id': 'warehouse_id',
                           'warehouse_code': 'warehouse_code',
                           'warehouse_name': 'warehouse_name',
                           'location_id': 'location_id',
                           'location_code': 'location_code',
                           'location_name': 'location_name',
                           'area_id': 'area_id',
                           'area_code': 'area_code',
                           'area_name': 'area_name',
                           'expire_date': 'expire_date'}),
 'wm.issue_line': ('wm_issue_line',
                   {'id': 'line_id',
                    'issue_id': 'issue_id',
                    'material_stock_id': 'material_stock_id',
                    'item_id': 'item_id',
                    'item_code': 'item_code',
                    'item_name': 'item_name',
                    'quantity_issued': 'quantity_issued',
                    'unit_of_measure': 'unit_of_measure',
                    'batch_code': 'batch_code',
                    'specification': 'specification',
                    'warehouse_id': 'warehouse_id',
                    'warehouse_code': 'warehouse_code',
                    'warehouse_name': 'warehouse_name',
                    'location_id': 'location_id',
                    'location_code': 'location_code',
                    'location_name': 'location_name',
                    'area_id': 'area_id',
                    'area_code': 'area_code',
                    'area_name': 'area_name'})}

# END_REVIEWED_MAPPINGS


@dataclass(frozen=True, slots=True)
class SQLViewSpec:
    view_name: str
    entity: str
    columns: tuple[str, ...]
    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SQLScopePolicy:
    """Server assertion derived from current trusted roles, not a model argument.

    The initial implementation supports only verified all-row roles (dataScope=1).
    Re-evaluate roles on each request; a historical audit snapshot is not a policy.
    """
    profile: str
    role_data_scopes: tuple[str, ...]
    allowed_entities: frozenset[str]


def available_views(
    current_permissions: frozenset[str], scope_policy: SQLScopePolicy | None,
) -> tuple[SQLViewSpec, ...]:
    if (scope_policy is None or scope_policy.profile != SHOWCASE_PROFILE
            or not scope_policy.role_data_scopes
            or any(scope != "1" for scope in scope_policy.role_data_scopes)
            or not scope_policy.allowed_entities <= SHOWCASE_ENTITIES):
        return ()
    views: list[SQLViewSpec] = []
    for entity in sorted(scope_policy.allowed_entities):
        spec = ENTITIES.get(entity)
        mapping = _MAPPINGS.get(entity)
        if spec is None or mapping is None or set(spec.fields) != set(mapping[1]):
            raise ValueError("SQL projection does not match the reviewed catalog")
        requirements = spec.permissions.get("list", ())
        if requirements and ("*:*:*" in current_permissions or set(requirements) <= current_permissions):
            views.append(SQLViewSpec("agent_" + entity.replace(".", "_"), entity,
                                     tuple(mapping[1]), requirements))
    return tuple(views)


def validate_database(database: str) -> None:
    if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", database)
            or not (database == "ktg_demo" or re.fullmatch(r"ktg_query_test_[a-z0-9_]+", database))):
        raise ValueError("Database must be ktg_demo or an explicit ktg_query_test_* isolated schema")


def view_statements(
    database: str, current_permissions: frozenset[str], scope_policy: SQLScopePolicy | None,
) -> tuple[str, ...]:
    """Create only static projections; database administrator owns their definer.

    Runtime receives SELECT on views only. The fixed projections expose no
    functions, administration tables, secrets, grants or write statements.
    """
    validate_database(database)
    statements: list[str] = []
    for view in available_views(current_permissions, scope_policy):
        table, columns = _MAPPINGS[view.entity]
        projection = ", ".join(f"`{physical}` AS `{logical}`" for logical, physical in columns.items())
        statements.append(f"CREATE OR REPLACE SQL SECURITY DEFINER VIEW `{database}`.`{view.view_name}` "
                          f"AS SELECT {projection} FROM `{database}`.`{table}`")
    return tuple(statements)
