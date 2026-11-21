"""Discovery pagination and bounded, request-scoped tool replacement."""

import asyncio
from typing import cast

from ktg_agent.core.messages import ToolCall
from ktg_agent.ktg.dv_repair import build_dv_repair_tools
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.ktg.workorders import PreviewStore, build_workorder_tools
from ktg_agent.tools.catalog import CatalogIdentity, CatalogSwitches, ToolCatalog
from ktg_agent.tools.discovery import RunToolDiscovery
from ktg_agent.tools.legacy_catalog import build_legacy_catalog
from ktg_agent.tools.registry import ToolRegistry, ToolResult
from ktg_agent.tools.registry import ToolContext
from ktg_agent.tools.runner import CallAuthorization, ToolRunner


def setup_discovery(*, maximum: int = 1) -> tuple[
    RunToolDiscovery, ToolRegistry, list[frozenset[str]], ToolCatalog,
]:
    definitions = (*build_query_tools(), *build_workorder_tools(PreviewStore()))
    catalog = build_legacy_catalog(definitions)
    switches = CatalogSwitches({"md": True, "wm": True, "pro": True},
                              {definition.name: True for definition in definitions})
    permissions = [frozenset({"*:*:*"})]

    async def access() -> tuple[frozenset[str], CatalogSwitches]:
        return permissions[0], switches

    registry = ToolRegistry()
    discovery = RunToolDiscovery(catalog, catalog.candidate_snapshot(
        CatalogIdentity(permissions[0], "test"), switches), registry, access,
        max_active_tools=maximum)
    return discovery, registry, permissions, catalog


async def load(discovery: RunToolDiscovery, *names: str, mode: str = "add") -> ToolResult:
    return await discovery.execute(ToolCall("load", "load_tools", {
        "mode": mode, "tools": [{"name": name, "version": "1.0"} for name in names],
    }))


def test_replace_clears_and_reloads_without_mutating_prior_snapshots() -> None:
    async def scenario() -> None:
        discovery, registry, _, _ = setup_discovery()
        count, search = "ktg_count_products", "ktg_search_items"
        assert (await load(discovery, count)).error_code is None
        old = discovery.begin_request("old")
        registered = registry.get(count)
        assert (await load(discovery, search)).error_code == "ACTIVE_LIMIT_EXCEEDED"
        assert (await load(discovery, search, mode="replace")).error_code is None
        new = discovery.begin_request("new")
        assert count in old.names and search not in old.names
        assert search in new.names and count not in new.names
        assert discovery.active_names("old") == old.names
        assert registry.get(count) is registered  # Retain audit definition, not authority.
        assert (await load(discovery, count, mode="replace")).error_code is None
        assert registry.get(count) is registered
        assert (await load(discovery, count)).error_code is None
        assert (await load(discovery, mode="replace")).error_code is None
        assert discovery.begin_request("cleared").names == {"search_tools", "load_tools"}
        assert (await load(discovery)).error_code == "VALIDATION_ERROR"

    asyncio.run(scenario())


def test_failed_replacement_and_revoked_retained_tool_are_atomic() -> None:
    async def scenario() -> None:
        discovery, _, permissions, _ = setup_discovery(maximum=2)
        count, stock = "ktg_count_products", "ktg_query_stock"
        assert (await load(discovery, count)).error_code is None
        before = discovery.begin_request("before").names
        for names, expected in [((stock, "missing"), "TOOL_NOT_FOUND"),
                                (("ktg_create_workorder_draft",), "TOOL_NOT_FOUND"),
                                ((stock, stock), "VALIDATION_ERROR")]:
            result = await load(discovery, *names, mode="replace")
            assert result.error_code == expected
            assert discovery.begin_request("failed").names == before
        wrong = await discovery.execute(ToolCall("bad", "load_tools", {
            "mode": "replace", "tools": [{"name": stock, "version": "2.0"}],
        }))
        assert wrong.error_code == "TOOL_VERSION_MISMATCH"
        discovery.max_schema_tokens = 1
        assert (await load(discovery, stock, mode="replace")).error_code == "ACTIVE_LIMIT_EXCEEDED"
        assert discovery.begin_request("budget").names == before
        discovery.max_schema_tokens = 6000
        permissions[0] = frozenset({"mes:wm:wmstock:list"})
        # add retains count; it must revalidate that retained definition too.
        assert (await load(discovery, stock)).error_code == "TOOL_NOT_FOUND"
        assert discovery.begin_request("revoked").names == before
        assert (await load(discovery, stock, mode="replace")).error_code is None
        assert count not in discovery.begin_request("allowed").names

    asyncio.run(scenario())


def test_unloaded_registered_tool_cannot_execute_in_new_request() -> None:
    async def scenario() -> None:
        discovery, registry, permissions, _ = setup_discovery()
        name = "ktg_count_products"
        assert (await load(discovery, name)).error_code is None
        discovery.begin_request("old")
        assert (await load(discovery, mode="replace")).error_code is None
        discovery.begin_request("new")

        async def authorize(context: ToolContext) -> CallAuthorization:
            current_permissions, switches = await discovery.current_access()
            assert current_permissions == permissions[0]
            return CallAuthorization(current_permissions, switches, discovery.candidate,
                                     discovery.active_names(context.active_snapshot_id))

        runner = ToolRunner(registry, catalog=discovery.catalog, authorization_provider=authorize)
        call = ToolCall("count", name, {})
        result = await runner.run(call, ToolContext("test", "count", identity={"userId": 7},
                                                   active_snapshot_id="new"))
        assert result.error_code == "TOOL_NOT_ACTIVE"
        # The already issued model request keeps its snapshot; no retroactive mutation.
        old = await runner.run(call, ToolContext("test", "count", identity={"userId": 7},
                                                active_snapshot_id="old"))
        assert old.error_code == "KTG_CLIENT_MISSING"
        assert (await load(discovery, name)).error_code is None
        assert name in discovery.begin_request("reloaded").names

    asyncio.run(scenario())


def test_search_pages_have_stable_total_next_offset_and_fallback_semantics() -> None:
    async def scenario() -> None:
        discovery, _, _, _ = setup_discovery()
        names: list[str] = []
        offset = 0
        while True:
            result = await discovery.execute(ToolCall("search", "search_tools", {
                "query": "", "limit": 2, "offset": offset,
            }))
            assert result.error_code is None
            details = result.details
            assert details is not None and details["total"] == 7
            names.extend(str(item["name"]) for item in cast(list[dict[str, object]], details["items"]))
            next_offset = details["next_offset"]
            if next_offset is None:
                assert details["truncated"] is False
                break
            assert isinstance(next_offset, int) and next_offset > offset
            offset = next_offset
        assert len(names) == len(set(names)) == 7
        empty = await discovery.execute(ToolCall("empty", "search_tools", {"query": "", "offset": 100}))
        assert empty.details is not None and empty.details["items"] == []
        assert empty.details["total"] == 7 and empty.details["next_offset"] is None
        assert "No available tool" not in empty.content
        fallback = await discovery.execute(ToolCall("fallback", "search_tools", {
            "query": "火星气候", "limit": 2, "offset": 2,
        }))
        assert fallback.details is not None
        assert fallback.details["match_type"] == "available_alternatives"
        assert fallback.details["total"] == 7 and "not evidence" in fallback.content

    asyncio.run(scenario())


def test_colloquial_capability_synonyms_find_repair_tools_not_business_records() -> None:
    async def scenario() -> None:
        from ktg_agent.ktg.dv_repair import dv_repair_factories, dv_repair_manifests

        definitions = build_dv_repair_tools()
        catalog = build_legacy_catalog(definitions, extra_manifests=dv_repair_manifests(),
                                       extra_factories=dv_repair_factories())
        switches = CatalogSwitches({"dv": True}, {tool.name: True for tool in definitions})
        permissions = frozenset({"*:*:*"})

        async def access() -> tuple[frozenset[str], CatalogSwitches]:
            return permissions, switches

        discovery = RunToolDiscovery(catalog, catalog.candidate_snapshot(
            CatalogIdentity(permissions, "synonyms"), switches), ToolRegistry(), access)
        for query in ("修理", "机器坏了", "检修"):
            result = await discovery.execute(ToolCall("search", "search_tools", {"query": query}))
            assert result.details is not None and result.details["match_type"] == "matched"
            assert "ktg_dv_repair_list" in {
                item["name"] for item in cast(list[dict[str, object]], result.details["items"])
            }

    asyncio.run(scenario())
