"""Call-time authorization is independent of discovery and model arguments."""

import asyncio
from collections.abc import Mapping
from typing import Any

from ktg_agent.core.messages import ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.providers.base import ModelConfig
from ktg_agent.providers.fake import FakeProvider
from ktg_agent.service import QueryService
from ktg_agent.tools.catalog import CatalogIdentity, CatalogSwitches
from ktg_agent.tools.legacy_catalog import build_legacy_catalog
from ktg_agent.tools.registry import (
    ExecutionMode, ToolContext, ToolDefinition, ToolEffect, ToolRegistry,
    ToolResult, ToolChannel,
)
from ktg_agent.tools.runner import CallAuthorization, ToolRunner


class Counter:
    calls = 0

    async def __call__(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        self.calls += 1
        return ToolResult(content="private stock", details={"quantity": 9})


def test_candidate_active_permission_and_switch_are_checked_per_call() -> None:
    async def scenario() -> None:
        execute = Counter()
        tool = ToolDefinition(
            name="ktg_query_stock", version="1.0", description="Read stock", label="Stock",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=None, required_permissions=("mes:wm:wmstock:list",),
            effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
            timeout_seconds=1, execute=execute,
        )
        catalog = build_legacy_catalog((tool,))
        switches = CatalogSwitches({"wm": True}, {tool.name: True})
        initial_permissions = frozenset({"mes:wm:wmstock:list"})
        state: dict[str, object] = {"permissions": initial_permissions,
                                    "switches": switches, "active": frozenset({tool.name})}
        candidate = catalog.candidate_snapshot(
            CatalogIdentity(initial_permissions, "run-1"), switches)

        async def authorize(context: ToolContext) -> CallAuthorization:
            permissions = state["permissions"]
            assert permissions is None or isinstance(permissions, frozenset)
            current_switches = state["switches"]
            active = state["active"]
            assert isinstance(current_switches, CatalogSwitches)
            assert isinstance(active, frozenset)
            return CallAuthorization(permissions, current_switches, candidate, active)

        runner = ToolRunner(ToolRegistry((tool,)), catalog=catalog,
                            authorization_provider=authorize)
        call = ToolCall("call-1", tool.name, {})

        async def invoke() -> ToolResult:
            return await runner.run(call, ToolContext(
                run_id="run-1", tool_call_id=call.id, identity={"userId": 7}))

        state["active"] = frozenset()
        assert (await invoke()).error_code == "TOOL_NOT_ACTIVE"
        state["active"] = frozenset({tool.name})
        assert (await invoke()).details == {"quantity": 9}
        assert execute.calls == 1
        state["permissions"] = frozenset()
        assert (await invoke()).error_code == "PERMISSION_DENIED"
        state["permissions"] = None
        assert (await invoke()).error_code == "PERMISSION_UNAVAILABLE"
        state["permissions"] = frozenset({"mes:wm:wmstock:list"})
        state["switches"] = CatalogSwitches({"wm": True}, {tool.name: False})
        assert (await invoke()).error_code == "TOOL_DISABLED"
        assert execute.calls == 1

        state["switches"] = switches
        candidate = catalog.candidate_snapshot(CatalogIdentity(frozenset(), "run-1"), switches)
        assert (await invoke()).error_code == "TOOL_NOT_FOUND"
        assert execute.calls == 1

    asyncio.run(scenario())


def test_hidden_confirmation_rechecks_permission_and_switch() -> None:
    async def scenario() -> None:
        execute = Counter()
        tool = ToolDefinition(
            name="ktg_execute_item_receipt", version="1.0", description="Execute receipt",
            label="Receipt", input_schema={"type": "object", "properties": {},
                                           "additionalProperties": False},
            output_schema=None, required_permissions=("mes:wm:itemrecpt:edit",),
            effect=ToolEffect.WRITE, execution_mode=ExecutionMode.SEQUENTIAL,
            timeout_seconds=1, execute=execute, expose_to_model=False,
        )
        catalog = build_legacy_catalog((tool,))
        switches = CatalogSwitches({"wm": True}, {tool.name: True})
        permissions: dict[str, frozenset[str] | None] = {
            "value": frozenset({"mes:wm:itemrecpt:edit", "mes:wm:itemrecpt:query",
                                "mes:wm:wmstock:list"})}
        current_switches = {"value": switches}

        async def authorize(context: ToolContext) -> CallAuthorization:
            return CallAuthorization(permissions["value"], current_switches["value"])

        runner = ToolRunner(ToolRegistry((tool,)), catalog=catalog,
                            authorization_provider=authorize)
        call = ToolCall("confirm-1", tool.name, {})
        model = ToolContext(run_id="run", tool_call_id=call.id,
                            identity={"userId": 7}, operation_id="op-1")
        trusted = ToolContext(run_id="new-confirmation-run", tool_call_id=call.id,
                              identity={"userId": 7}, operation_id="op-1",
                              confirmation_id="confirmation-1",
                              channel=ToolChannel.TRUSTED_CONFIRMATION)
        assert (await runner.run(call, model)).error_code == "TOOL_NOT_FOUND"
        assert (await runner.run(call, trusted)).status.value == "succeeded"
        permissions["value"] = frozenset()
        assert (await runner.run(call, trusted)).error_code == "PERMISSION_DENIED"
        permissions["value"] = None
        assert (await runner.run(call, trusted)).error_code == "PERMISSION_UNAVAILABLE"
        permissions["value"] = frozenset({"mes:wm:itemrecpt:edit",
                                          "mes:wm:itemrecpt:query", "mes:wm:wmstock:list"})
        current_switches["value"] = CatalogSwitches({"wm": True}, {tool.name: False})
        assert (await runner.run(call, trusted)).error_code == "TOOL_DISABLED"
        assert execute.calls == 1

    asyncio.run(scenario())


def test_service_rechecks_getinfo_before_each_handler() -> None:
    async def scenario() -> None:
        user = {"userId": 7, "userName": "alice"}
        granted: dict[str, list[str] | None] = {"value": ["mes:wm:wmstock:list"]}
        switches: dict[str, CatalogSwitches] = {"value": CatalogSwitches(
            {"md": True, "wm": True, "pro": True},
            {"ktg_query_stock": True},
        )}
        business_reads = 0

        async def transport(url: str, headers: Mapping[str, str],
                            params: Mapping[str, object], timeout: float) -> object:
            nonlocal business_reads
            if url.endswith("/getInfo"):
                if granted["value"] is None:
                    raise KTGError("KTG_UNAVAILABLE", "permission service unavailable")
                return {"code": 200, "user": user, "permissions": granted["value"]}
            business_reads += 1
            return {"code": 200, "rows": [], "total": 0}

        client = KTGClient(base_url="http://localhost", authorization="Bearer test",
                           transport=transport)
        service = QueryService(
            provider=FakeProvider(()), model_config=ModelConfig(model="fake", max_output_tokens=64),
            ktg_client_factory=lambda token: client,
            switches_provider=lambda: switches["value"],
        )
        candidate = service._catalog.candidate_snapshot(
            CatalogIdentity(frozenset({"mes:wm:wmstock:list"}), "run-1"), switches["value"])
        runner = service._tool_runner(candidate)
        call = ToolCall("call-1", "ktg_query_stock", {"item_code": "A-01"})
        context = ToolContext(run_id="run-1", tool_call_id=call.id,
                              identity=user, ktg_client=client)

        assert (await runner.run(call, context)).status.value == "succeeded"
        assert business_reads == 1
        granted["value"] = []
        assert (await runner.run(call, context)).error_code == "PERMISSION_DENIED"
        granted["value"] = None
        assert (await runner.run(call, context)).error_code == "PERMISSION_UNAVAILABLE"
        granted["value"] = ["mes:wm:wmstock:list"]
        switches["value"] = CatalogSwitches({"wm": True}, {"ktg_query_stock": False})
        assert (await runner.run(call, context)).error_code == "TOOL_DISABLED"
        wrong_user = ToolContext(run_id="run-1", tool_call_id=call.id,
                                 identity={"userId": 8}, ktg_client=client)
        switches["value"] = CatalogSwitches({"wm": True}, {"ktg_query_stock": True})
        assert (await runner.run(call, wrong_user)).error_code == "PERMISSION_UNAVAILABLE"
        assert business_reads == 1

    asyncio.run(scenario())
