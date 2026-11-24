"""Regression coverage for imports and the retained MES read-only definitions."""

import subprocess
import sys
from dataclasses import replace
from http import HTTPStatus
from unittest.mock import Mock, patch

import pytest

from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.tools import RetryPolicy, ToolEffect
from ktg_agent.tools import CatalogIdentity, CatalogSwitches
from ktg_agent.tools.legacy_catalog import build_legacy_catalog
from ktg_agent.server import AgentRequestHandler, Settings, create_service
from ktg_agent.tools.permissions import PermissionLeaf
from ktg_agent.tools.registry import ToolDefinitionError

RETAINED = {"ktg_count_products", "ktg_query_stock_summary"}


def test_tools_import_in_fresh_process() -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import ktg_agent.tools; from ktg_agent.core import Agent, InputQueue"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_retained_definitions_are_reads_with_permissions() -> None:
    definitions = build_query_tools()
    assert {definition.name for definition in definitions} == RETAINED
    for definition in definitions:
        assert definition.required_permissions, definition.name
        assert definition.effect is ToolEffect.READ
        assert definition.retry_policy is RetryPolicy.BOUNDED_READ


def test_business_catalog_resolves_and_filters_retained_tools() -> None:
    definitions = build_query_tools()
    catalog = build_legacy_catalog(definitions)
    switches = CatalogSwitches({"md": True, "wm": True}, {item.name: True for item in definitions})
    admin = CatalogIdentity(frozenset({"*:*:*"}), "run")
    assert set(catalog.candidate_snapshot(admin, switches).candidates) == RETAINED
    for definition in definitions:
        assert catalog.resolve_definition(definition.name, definition.version) is definition
        policy = catalog.manifest(definition.name).permission_policy
        assert isinstance(policy, PermissionLeaf)
        assert policy.permission == definition.required_permissions[0]
        identity = CatalogIdentity(frozenset({policy.permission}), "run")
        assert set(catalog.candidate_snapshot(identity, switches).candidates) == {definition.name}
    assert not catalog.candidate_snapshot(admin, CatalogSwitches({}, {})).candidates
    assert not catalog.candidate_snapshot(CatalogIdentity(None, "run"), switches).candidates


def test_retired_definition_cannot_register_in_business_catalog() -> None:
    definition = replace(build_query_tools()[0], name="retired_business_tool")
    with pytest.raises(ToolDefinitionError, match="unreviewed"):
        build_legacy_catalog((definition,))


def test_service_assembles_exact_read_only_candidate_set() -> None:
    settings = Settings(
        model_api_key="test", model_base_url="https://example.test/v1",
        model_id="test-model", ktg_base_url="http://127.0.0.1:8080")
    service = create_service(settings)
    admin = CatalogIdentity(frozenset({"*:*:*"}), "run")
    assert set(service._catalog._manifests) == RETAINED
    assert set(service._catalog.candidate_snapshot(admin, service._switches_provider()).candidates) == RETAINED
    assert {schema.name for schema in service.model_tool_schemas} == {"search_tools", "load_tools"}


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/agent-api/v1/inventory-operations/retired-id"),
    ("POST", "/agent-api/v1/exploration/view"),
    ("POST", "/agent-api/v1/workorder-drafts/confirm"),
    ("POST", "/agent-api/v1/inventory-operations/confirm"),
    ("POST", "/agent-api/v1/dv-machinerytype/confirm"),
    ("POST", "/agent-api/v1/dv-machinery/confirm"),
    ("POST", "/agent-api/v1/dv-subject/confirm"),
    ("POST", "/agent-api/v1/dv-checkplan/confirm"),
    ("POST", "/agent-api/v1/dv-repair/confirm"),
    ("POST", "/agent-api/v1/tm-tooltype/confirm"),
    ("POST", "/agent-api/v1/tm-tool/confirm"),
])
def test_retired_business_http_routes_are_not_found(method: str, path: str) -> None:
    # The handler must reject removed routes before opening the query service,
    # creating an event loop, or reading the request body.
    handler = object.__new__(AgentRequestHandler)
    handler.path = path
    write_json = Mock()
    with patch.object(handler, "_write_json", write_json):
        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()
    write_json.assert_called_once_with(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
