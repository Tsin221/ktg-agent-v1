from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ktg_agent.exploration.tools import (
    build_exploration_tools, exploration_factories, exploration_manifests,
)
from ktg_agent.tools.permissions import PermissionAny, evaluate_permission_policy, PermissionDecision
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolContext, ToolDefinitionError, ToolEffect, ToolResult

NAMES = frozenset({"ktg_describe_business", "ktg_search_business", "ktg_query_business", "ktg_read_record",
    "ktg_follow_relations", "ktg_get_business_flow", "ktg_inspect_result", "ktg_readonly_sql",
    "ktg_call_read_api", "ktg_analyze_result"})


class Runtime:
    def __init__(self, names: frozenset[str] = NAMES) -> None:
        self.enabled_names = names
        self.calls: list[tuple[str, Mapping[str, Any], ToolContext]] = []

    async def invoke(self, name: str, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.calls.append((name, arguments, context))
        return ToolResult(content="runtime result")


def test_only_configured_entries_are_built_and_unknown_configuration_fails() -> None:
    assert build_exploration_tools(Runtime(frozenset())) == ()
    definitions = build_exploration_tools(Runtime(frozenset({"ktg_search_business"})))
    assert [tool.name for tool in definitions] == ["ktg_search_business"]
    with pytest.raises(ToolDefinitionError):
        build_exploration_tools(Runtime(frozenset({"ktg_delete_everything"})))


def test_manifests_are_real_any_read_permissions_not_empty_authorization() -> None:
    definitions = build_exploration_tools(Runtime())
    manifests = exploration_manifests(definitions)
    factories = exploration_factories(definitions)
    assert {tool.name for tool in definitions} == NAMES
    assert len(manifests) == len(factories) == 10
    for manifest in manifests:
        assert manifest.module == "system" and manifest.domain == "exploration"
        assert isinstance(manifest.permission_policy, PermissionAny)
        assert evaluate_permission_policy(manifest.permission_policy, frozenset()) is PermissionDecision.DENIED
        assert evaluate_permission_policy(manifest.permission_policy, frozenset({"mes:dv:machinery:list"})) is PermissionDecision.ALLOWED
        assert evaluate_permission_policy(manifest.permission_policy, frozenset({"mes:qc:ipqc:list"})) is PermissionDecision.DENIED
        assert factories[manifest.handler_ref]().name == manifest.name
        assert manifest.handler_ref == "exploration:" + manifest.name
        assert manifest.effect is ToolEffect.READ and manifest.execution_mode is ExecutionMode.SEQUENTIAL
        assert manifest.confirmation_policy == "none" and not manifest.proposal_store_effect
        assert manifest.retry_policy is (RetryPolicy.NEVER if manifest.name in {"ktg_readonly_sql", "ktg_analyze_result"} else RetryPolicy.BOUNDED_READ)


def test_handlers_dispatch_the_exact_bound_name_and_context() -> None:
    runtime = Runtime()
    definitions = build_exploration_tools(runtime)
    context = ToolContext(run_id="r", tool_call_id="c")

    async def run() -> None:
        for definition in definitions:
            result = await definition.execute(context, {"probe": definition.name})
            assert result.content == "runtime result"

    asyncio.run(run())
    assert [name for name, _, _ in runtime.calls] == [tool.name for tool in definitions]
    assert all(item[2] is context for item in runtime.calls)


def test_query_schema_accepts_recursive_conditions_but_rejects_unknown_structure() -> None:
    schemas = {tool.name: tool.input_schema for tool in build_exploration_tools(Runtime())}
    validator = Draft202012Validator(schemas["ktg_query_business"])
    query = {"entity": "dv.machinery", "filters": {"all": [
        {"field": "code", "op": "starts_with", "value": "R"},
        {"related": {"relation": "machinery.repairs", "where": {"field": "status", "op": "eq", "value": "FINISHED"}}}]},
        "sort": [{"field": "name", "direction": "asc"}]}
    assert not list(validator.iter_errors(query))
    bad_filters: list[dict[str, object]] = [{"raw_sql": "SELECT *"}, {"all": []},
        {"field": "code", "op": "eq", "value": "R", "owner": "other"},
        {"related": {"relation": "x", "where": {"all": []}}}]
    for bad_filter in bad_filters:
        assert list(validator.iter_errors({"entity": "dv.machinery", "filters": bad_filter}))
    assert list(validator.iter_errors({"entity": "dv.machinery", "owner": "other"}))
    assert not list(Draft202012Validator(schemas["ktg_search_business"]).iter_errors({"query": "R开头的机器"}))
    assert list(Draft202012Validator(schemas["ktg_search_business"]).iter_errors(
        {"query": "R", "filters": {"all": [{"field": "name", "op": "eq", "value": "R"}]}, "entity_filters": {}}))


def test_record_schema_requires_either_reference_or_entity_and_id() -> None:
    tool = next(tool for tool in build_exploration_tools(Runtime()) if tool.name == "ktg_read_record")
    validator = Draft202012Validator(tool.input_schema)
    valid: list[dict[str, object]] = [{"record_ref": "rec_a"}, {"entity": "pro.workorder", "record_id": 1}]
    for args in valid:
        assert not list(validator.iter_errors(args))
    invalid: list[dict[str, object]] = [{}, {"entity": "pro.workorder"}, {"record_id": 1},
        {"entity": "x", "record_id": 0}, {"record_ref": "rec_a", "entity": "x", "record_id": 1}]
    for args in invalid:
        assert list(validator.iter_errors(args))


def test_metadata_and_aggregate_outputs_do_not_require_business_records() -> None:
    for tool in build_exploration_tools(Runtime()):
        assert tool.output_schema is not None
        validator = Draft202012Validator(tool.output_schema)
        assert not list(validator.iter_errors({"kind": "exploration_catalog", "result_ref": "res_a",
            "source": "MES", "applied_query": {}, "coverage": "complete", "warnings": []}))
