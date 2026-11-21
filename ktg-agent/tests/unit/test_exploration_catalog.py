"""Business facts must survive discovery without opening arbitrary endpoints."""

import json
from pathlib import Path
from typing import cast

import pytest

from ktg_agent.exploration.catalog import ENTITIES, READ_APIS, entities, get_entity


SHOWCASE = {
    "md.item", "md.workshop", "dv.machinery_type", "dv.machinery", "dv.subject",
    "dv.check_plan", "dv.repair", "tm.tool_type", "tm.tool", "pro.workorder",
    "pro.task", "pro.feedback", "wm.stock", "wm.item_receipt", "wm.issue",
    "md.item_bom", "pro.workorder_bom", "dv.repair_line", "dv.check_machinery",
    "dv.check_subject", "wm.item_receipt_line", "wm.issue_line",
}


def test_catalog_is_exact_showcase_not_a_whole_mes_directory() -> None:
    assert set(ENTITIES) == SHOWCASE
    assert len(ENTITIES) == 22
    assert {entity.module for entity in entities()} == {"md", "pro", "wm", "dv", "tm"}
    assert get_entity("pro.feedback").id_field == "recordId"
    assert get_entity("dv.check_machinery").id_field == "recordId"
    assert get_entity("dv.subject").path == "/mes/dv/dvsubject"
    assert get_entity("wm.item_receipt").path == "/mes/wm/itemrecpt"
    for entity in ("system.password", "qc.ipqc", "wm.product_recpt", "wm.product_produce",
                   "md.client", "md.workstation", "cal.team", "report.definition"):
        with pytest.raises(KeyError):
            get_entity(entity)


def test_repair_operations_preserve_existing_compound_permissions() -> None:
    repair = get_entity("dv.repair")
    assert repair.permissions["detail"] == (
        "mes:dv:repair:list", "mes:dv:repair:query"
    )
    assert set(get_entity("dv.repair_line").permissions["list"]) == {
        "mes:dv:repair:query", "mes:dv:repairline:list"
    }
    assert READ_APIS["dv.repair.detail"].permissions == repair.permissions["detail"]


def test_projection_retains_real_relationship_keys_but_excludes_private_columns() -> None:
    fields = get_entity("pro.workorder").fields
    assert fields["product_id"].wire == "productId"
    assert fields["client_name"].wire == "clientName"
    assert get_entity("pro.task").fields["workstation_id"].wire == "workstationId"
    for entity in entities():
        assert not {"password", "token", "phone", "mobile", "email", "content"} & {
            field.wire for field in entity.fields.values()
        }


def test_capabilities_do_not_pretend_existing_backends_support_prefix_or_semantics() -> None:
    machine = get_entity("dv.machinery")
    assert machine.fields["code"].operators == ("eq",)
    assert machine.fields["name"].operators == ("contains",)
    assert not machine.fields["code"].sortable
    assert get_entity("dv.repair_line").fields["malfunction"].operators == ("eq",)
    assert get_entity("dv.repair").fields["require_date"].type == "date"


def test_device_type_flat_array_and_bom_roles_are_explicit() -> None:
    machinery_type = get_entity("dv.machinery_type")
    assert machinery_type.list_shape == "array"
    assert machinery_type.fields["parent_type_id"].wire == "parentTypeId"
    assert "pageNum" not in READ_APIS["dv.machinery_type.list"].parameters
    assert get_entity("md.item_bom").fields["item_id"].wire == "itemId"
    assert get_entity("md.item_bom").fields["bom_item_id"].wire == "bomItemId"
    assert get_entity("wm.item_receipt_line").fields["quantity_recived"].wire == "quantityRecived"
    stock = get_entity("wm.stock")
    for key in ("item_id", "warehouse_id", "location_id", "area_id", "batch_code", "quantity_onhand"):
        assert key in stock.fields


def test_relationships_resolve_to_real_catalog_fields_and_are_bidirectional() -> None:
    repair = get_entity("dv.repair")
    assert any(r.target == "dv.repair_line" for r in repair.relations)
    assert any(r.target == "dv.machinery" for r in repair.relations)
    for entity in entities():
        for relation in entity.relations:
            assert relation.source == entity.key
            assert relation.source_field in entity.fields
            target = get_entity(relation.target)
            assert relation.target_field in target.fields
            assert relation.evidence == "explicit_id"
            assert any(r.target == entity.key for r in target.relations)


def test_audited_api_allowlist_is_immutable_fixed_and_has_projection_and_permission() -> None:
    for endpoint in READ_APIS.values():
        assert endpoint.operation in {"list", "detail"}
        assert endpoint.permissions
        assert get_entity(endpoint.entity).fields
        assert endpoint.path.startswith("/mes/")
        assert not any(word in endpoint.path for word in ("autocode", "execute", "Home", "export"))
    with pytest.raises(TypeError):
        ENTITIES["evil"] = get_entity("dv.repair")  # type: ignore[index]
    with pytest.raises(TypeError):
        get_entity("dv.repair").fields["evil"] = get_entity("dv.repair").fields["id"]  # type: ignore[index]


def test_snapshot_accounts_are_not_unioned_or_granted_missing_operations() -> None:
    snapshot = Path(__file__).resolve().parents[4] / "docs/优先文档/目标开放工具/账号与展示范围快照.json"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert {resource["entity"] for resource in data["primary_resources"] + data["support_resources"]} == SHOWCASE
    employee = set(cast(list[str], data["employee"]["permissions"]))
    demo = set(cast(list[str], data["demo"]["permissions"]))
    assert set(get_entity("md.item").permissions["list"]) <= employee
    assert not set(get_entity("pro.task").permissions["list"]) <= employee
    assert not set(get_entity("wm.stock").permissions["list"]) <= employee
    assert set(get_entity("pro.task").permissions["list"]) <= demo
    assert set(get_entity("dv.check_machinery").permissions["list"]) <= employee
    assert not set(get_entity("dv.check_machinery").permissions["detail"]) <= employee
    for api in READ_APIS.values():
        assert set(api.permissions) <= employee or set(api.permissions) <= demo
    assert "wm.item_receipt_line.detail" not in READ_APIS
    assert "wm.issue_line.detail" not in READ_APIS
    assert set(get_entity("md.item_bom").permissions["detail"]) <= employee
    assert not set(get_entity("md.item_bom").permissions["detail"]) <= demo
    assert set(get_entity("dv.check_machinery").permissions["detail"]) <= demo


def test_existing_specialized_detail_permissions_are_not_weakened() -> None:
    for entity, query, listing in (
        ("dv.machinery", "mes:dv:machinery:query", "mes:dv:machinery:list"),
        ("dv.subject", "mes:dv:dvsubject:query", "mes:dv:dvsubject:list"),
        ("dv.check_plan", "mes:dv:checkplan:query", "mes:dv:checkplan:list"),
        ("tm.tool", "mes:tm:tool:query", "mes:tm:tool:list"),
        ("tm.tool_type", "mes:tm:tooltype:query", "mes:tm:tooltype:list"),
    ):
        assert {query, listing} <= set(get_entity(entity).permissions["detail"])
    assert set(get_entity("dv.machinery_type").permissions["detail"]) == {
        "mes:dv:machinerytype:query", "mes:dv:machinery:list"
    }
