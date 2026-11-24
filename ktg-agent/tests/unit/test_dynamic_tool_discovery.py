"""MES-05 request-level discovery and activation checks."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import httpx

from ktg_agent.core.events import AgentEventType
from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall, ToolResultMessage
from ktg_agent.core.loop import RunLimits
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider
from ktg_agent.service import QueryService
from ktg_agent.server import AgentHTTPServer
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.tools.catalog import CatalogIdentity, CatalogSwitches, ToolCatalog, ToolManifest
from ktg_agent.tools.discovery import RunToolDiscovery
from ktg_agent.tools.legacy_catalog import build_legacy_catalog
from ktg_agent.tools.permissions import PermissionLeaf
from ktg_agent.tools.registry import ExecutionMode, RetryPolicy, ToolEffect, ToolRegistry
from ktg_agent.ktg.queries import build_query_tools
from typing import cast


def turn(*calls: ToolCall, answer: str | None = None) -> ScriptedTurn:
    content = (TextBlock(answer),) if answer is not None else calls
    return ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage(content),
        StopReason.COMPLETED if answer is not None else StopReason.TOOL_CALL,
        Usage(1, 1),
    )),))


def test_fake_provider_receives_loaded_schema_only_on_next_request() -> None:
    async def scenario() -> None:
        requested: list[str] = []

        async def transport(url: str, _headers: Mapping[str, str],
                            _params: Mapping[str, object], _timeout: float) -> object:
            requested.append(url)
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7, "userName": "alice"},
                        "permissions": ["mes:md:mditem:list"]}
            if url.endswith("/mes/md/mditem/list"):
                return {"code": 200, "total": 3, "rows": []}
            raise AssertionError(url)

        provider = FakeProvider((
            turn(ToolCall("search", "search_tools", {"query": "产品"})),
            turn(ToolCall("load", "load_tools", {"tools": [
                {"name": "ktg_count_products", "version": "1.0"}]}),
                 ToolCall("early", "ktg_count_products", {})),
            turn(ToolCall("count", "ktg_count_products", {})),
            turn(answer="共有 3 个产品。"),
        ))
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 128),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://127.0.0.1:8080", authorization=token,
                transport=transport),
        )
        result = await service.run_session(
            run_id="discovery-run", message="有多少产品", history=(),
            authorization="Bearer test", event_sink=lambda _event: None)
        names = [set(schema.name for schema in request.tools) for request in provider.requests]
        search_message = next(message for message in provider.requests[1].messages
                              if isinstance(message, ToolResultMessage))
        model_text = " ".join(block.text for block in search_message.content if isinstance(block, TextBlock))
        assert "ktg_count_products" in model_text and "1.0" in model_text
        assert names[:2] == [{"search_tools", "load_tools"}] * 2
        assert all("ktg_count_products" in item for item in names[2:])
        search_events = [event for event in result.events
                         if event.type is AgentEventType.TOOL_EXECUTION_END
                         and event.payload.get("tool_call_id") == "search"]
        assert len(search_events) == 1
        search_result = cast(dict[str, object], search_events[0].payload["result"])
        assert "ktg_count_products" in {
            item["name"] for item in cast(list[dict[str, object]], search_result["items"])}
        assert "ktg_query_stock_summary" not in json.dumps(search_result)
        assert result.final_message is not None
        assert result.final_message.content == (TextBlock("共有 3 个产品。"),)
        assert any(event.type is AgentEventType.TOOL_EXECUTION_END
                   and event.payload.get("tool_call_id") == "early"
                   and event.payload.get("error_code") == "TOOL_NOT_ACTIVE"
                   for event in result.events)
        assert any(event.type is AgentEventType.TOOL_EXECUTION_END
                   and event.payload.get("tool_call_id") == "count"
                   and event.payload.get("status") == "succeeded"
                   for event in result.events)
        assert sum(url.endswith("/mes/md/mditem/list") for url in requested) == 1

    asyncio.run(scenario())


def test_workorder_overview_is_discoverable_loadable_and_executable() -> None:
    name = "ktg_query_workorder_overview"

    async def scenario() -> None:
        queried: list[str] = []

        async def transport(url: str, _headers: Mapping[str, str],
                            _params: Mapping[str, object], _timeout: float) -> object:
            queried.append(url)
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7, "userName": "leader"},
                        "permissions": ["mes:pro:workorder:list", "mes:pro:execution:query"]}
            if url.endswith("/mes/pro/workorder/list"):
                return {"code": 200, "total": 0, "rows": []}
            raise AssertionError(url)

        provider = FakeProvider((
            turn(ToolCall("search", "search_tools", {"query": "生产订单做到哪了"})),
            turn(ToolCall("load", "load_tools", {"tools": [{"name": name, "version": "1.0"}]})),
            turn(ToolCall("query", name, {"unfinished_only": True, "include_progress": True})),
            turn(answer="未找到匹配工单。"),
        ))
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 128),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://127.0.0.1:8080", authorization=token, transport=transport),
        )
        result = await service.run_session(
            run_id="workorder-run", message="未完成的生产订单做到哪了？", history=(),
            authorization="Bearer test", event_sink=lambda _event: None)
        events = [event.payload for event in result.events if event.type is AgentEventType.TOOL_EXECUTION_END]
        search = next(payload for payload in events if payload.get("tool_call_id") == "search")
        assert name in json.dumps(search.get("result"))
        assert name in {schema.name for schema in provider.requests[2].tools}
        query = next(payload for payload in events if payload.get("tool_call_id") == "query")
        assert query.get("status") == "succeeded"
        details = cast(dict[str, object], query.get("result"))
        assert details["kind"] == "workorder_overview" and details["total"] == 0
        assert any(url.endswith("/mes/pro/workorder/list") for url in queried)

    asyncio.run(scenario())


def test_materials_tool_is_discoverable_loadable_and_read_only() -> None:
    name = "ktg_query_workorder_materials"

    async def scenario() -> None:
        requests: list[tuple[str, Mapping[str, object]]] = []

        async def transport(url: str, _headers: Mapping[str, str],
                            params: Mapping[str, object], _timeout: float) -> object:
            requests.append((url, params))
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7, "userName": "leader"},
                        "permissions": ["mes:pro:workorder:list", "mes:pro:workorderbom:list",
                                        "mes:pro:execution:query", "mes:wm:wmstock:list"]}
            if url.endswith("/mes/pro/workorder/list"):
                return {"code": 200, "total": 0, "rows": []}
            raise AssertionError(url)

        provider = FakeProvider((
            turn(ToolCall("search", "search_tools", {"query": "那它的料够吗"})),
            turn(ToolCall("load", "load_tools", {"tools": [{"name": name, "version": "1.0"}]})),
            turn(ToolCall("query", name, {"workorder_codes": ["WO-NOT-FOUND"]})),
            turn(answer="结论：所给工单未找到，不能判断缺料。"),
        ))
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 128),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://127.0.0.1:8080", authorization=token, transport=transport),
        )
        result = await service.run_session(
            run_id="materials-run", message="这个工单料够不够？", history=(),
            authorization="Bearer test", event_sink=lambda _event: None)
        events = [event.payload for event in result.events if event.type is AgentEventType.TOOL_EXECUTION_END]
        search = next(payload for payload in events if payload.get("tool_call_id") == "search")
        assert name in json.dumps(search.get("result"))
        assert name in {schema.name for schema in provider.requests[2].tools}
        query = next(payload for payload in events if payload.get("tool_call_id") == "query")
        assert query.get("status") == "succeeded"
        details = cast(dict[str, object], query.get("result"))
        assert details["kind"] == "workorder_materials" and details["missing_codes"] == ["WO-NOT-FOUND"]
        assert any(url.endswith("/mes/pro/workorder/list") and params.get("workorderCode") == "WO-NOT-FOUND"
                   for url, params in requests)

    asyncio.run(scenario())


def test_quality_tool_is_discoverable_loadable_and_read_only() -> None:
    name = "ktg_query_workorder_quality"

    async def scenario() -> None:
        requests: list[tuple[str, Mapping[str, object]]] = []

        async def transport(url: str, _headers: Mapping[str, str],
                            params: Mapping[str, object], _timeout: float) -> object:
            requests.append((url, params))
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7, "userName": "leader"},
                        "permissions": ["mes:pro:workorder:list", "mes:pro:execution:query"]}
            if url.endswith("/mes/pro/workorder/list"):
                return {"code": 200, "total": 0, "rows": []}
            raise AssertionError(url)

        provider = FakeProvider((
            turn(ToolCall("search", "search_tools", {"query": "哪个没检过，为什么不合格"})),
            turn(ToolCall("load", "load_tools", {"tools": [{"name": name, "version": "1.0"}]})),
            turn(ToolCall("query", name, {"workorder_codes": ["WO-NOT-FOUND"]})),
            turn(answer="结论：工单未匹配，质量明细待核实。"),
        ))
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 128),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://127.0.0.1:8080", authorization=token, transport=transport),
        )
        result = await service.run_session(
            run_id="quality-run", message="没检过的有多少，不合格是什么原因？", history=(),
            authorization="Bearer test", event_sink=lambda _event: None)
        events = [event.payload for event in result.events if event.type is AgentEventType.TOOL_EXECUTION_END]
        search = next(payload for payload in events if payload.get("tool_call_id") == "search")
        assert name in json.dumps(search.get("result"))
        assert name in {schema.name for schema in provider.requests[2].tools}
        query = next(payload for payload in events if payload.get("tool_call_id") == "query")
        assert query.get("status") == "succeeded"
        details = cast(dict[str, object], query.get("result"))
        assert details["kind"] == "workorder_quality" and details["coverage"] == "partial"
        assert any(url.endswith("/mes/pro/workorder/list") and params.get("workorderCode") == "WO-NOT-FOUND"
                   for url, params in requests)

    asyncio.run(scenario())


def test_loaded_stock_summary_constraint_reaches_model_http_request() -> None:
    name = "ktg_query_stock_summary"
    requests = []
    mes_requests = []

    def model_response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        calls = [
            ("search_tools", {"query": name}),
            ("load_tools", {"tools": [{"name": name, "version": "1.0"}]}),
            (name, {}),
        ]
        step = len(requests) - 1
        delta: dict[str, object]
        if step < len(calls):
            tool_name, arguments = calls[step]
            delta = {"tool_calls": [{
                "index": 0, "id": f"call-{step}", "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(arguments)},
            }]}
            finish_reason = "tool_calls"
        else:
            delta = {"content": "请提供物料编码。"}
            finish_reason = "stop"
        frame = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n",
        )

    async def mes_transport(url: str, *_args: object) -> object:
        mes_requests.append(url)
        assert url.endswith("/getInfo")
        return {
            "code": 200, "user": {"userId": 7, "userName": "alice"},
            "permissions": ["mes:wm:wmstock:list"],
        }

    provider = OpenAICompatibleProvider(api_key="test-key", base_url="http://model.test/v1")
    service = QueryService(
        provider=provider, model_config=ModelConfig("test-model", 128),
        ktg_client_factory=lambda token: KTGClient(
            base_url="http://mes.test", authorization=token, transport=mes_transport,
        ),
    )
    client_factory = httpx.AsyncClient
    with patch(
        "ktg_agent.providers.openai_compatible.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_factory(
            transport=httpx.MockTransport(model_response), **kwargs,
        ),
    ):
        result = asyncio.run(service.run_session(
            run_id="stock-summary-http", message="查询分仓库存", history=(),
            authorization="Bearer test", event_sink=lambda _event: None,
        ))

    assert len(requests) == 4
    functions = [{tool["function"]["name"]: tool["function"] for tool in body["tools"]}
                 for body in requests]
    assert [set(step) for step in functions[:2]] == [{"search_tools", "load_tools"}] * 2
    assert functions[2][name]["parameters"]["required"] == ["item_code"]
    assert functions[2][name]["parameters"]["additionalProperties"] is False
    assert any(event.type is AgentEventType.TOOL_EXECUTION_END
               and event.payload.get("tool_call_id") == "call-2"
               and event.payload.get("error_code") == "VALIDATION_ERROR" for event in result.events)
    tool_message = next(message for message in requests[3]["messages"]
                        if message.get("tool_call_id") == "call-2")
    assert "VALIDATION_ERROR" in tool_message["content"] and "item_code" in tool_message["content"]
    assert mes_requests and all(url.endswith("/getInfo") for url in mes_requests)


def test_natural_language_discovery_and_actionable_load_errors() -> None:
    async def scenario() -> None:
        definitions = build_query_tools()
        catalog = build_legacy_catalog(definitions)
        switches = CatalogSwitches({"md": True, "wm": True, "pro": True},
                                   {definition.name: True for definition in definitions})
        permissions = frozenset({"*:*:*"})
        candidate = catalog.candidate_snapshot(CatalogIdentity(permissions, "search-run"), switches)

        async def access() -> tuple[frozenset[str] | None, CatalogSwitches]:
            return permissions, switches

        discovery = RunToolDiscovery(catalog, candidate, ToolRegistry(), access)
        cases = {"查询指定物料分仓库存汇总": "ktg_query_stock_summary",
                 "统计产品记录总数量": "ktg_count_products"}
        for query, expected in cases.items():
            result = await discovery.execute(ToolCall("s", "search_tools", {"query": query}))
            assert result.details is not None
            assert cast(list[dict[str, object]], result.details["items"])[0]["name"] == expected
            assert expected in result.content and "1.0" in result.content
        overview = await discovery.execute(ToolCall("s", "search_tools", {"query": ""}))
        assert overview.details is not None and len(cast(list[object], overview.details["items"])) == 2
        invalid = await discovery.execute(ToolCall("s", "search_tools", {"module": "库存"}))
        assert invalid.error_code == "VALIDATION_ERROR" and "wm" in invalid.content
        wrong = await discovery.execute(ToolCall("l", "load_tools", {"tools": [
            {"name": "ktg_query_stock_summary", "version": "1.0.0"}]}))
        assert wrong.error_code == "TOOL_VERSION_MISMATCH"
        assert "1.0" in wrong.content and "ktg_query_stock_summary" in wrong.content
        failed_batch = await discovery.execute(ToolCall("l", "load_tools", {"tools": [
            {"name": "ktg_query_stock_summary", "version": "1.0"},
            {"name": "invented", "version": "1.0"}]}))
        assert "tools[1]" in failed_batch.content
        assert discovery.begin_request("next").names == frozenset({"search_tools", "load_tools"})
        fallback = await discovery.execute(ToolCall("s", "search_tools", {"query": "不存在的能力xyz"}))
        assert fallback.details is not None
        assert fallback.details["match_type"] == "available_alternatives"
        assert "not evidence" in fallback.content
        permissions = frozenset()
        empty = await discovery.execute(ToolCall("s", "search_tools", {"query": "anything"}))
        assert "ktg_query_stock_summary" not in empty.content

    asyncio.run(scenario())


def test_capability_budget_limit_preserves_overview_without_business_calls() -> None:
    async def scenario() -> None:
        async def transport(url: str, _headers: Mapping[str, str],
                            _params: Mapping[str, object], _timeout: float) -> object:
            assert url.endswith("/getInfo"), "capability questions must not read business data"
            return {"code": 200, "user": {"userId": 7}, "permissions": ["mes:md:mditem:list"]}

        provider = FakeProvider((turn(ToolCall("overview", "search_tools", {"query": ""})),))
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 128),
            limits=RunLimits(max_tool_calls=1),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://localhost", authorization=token, transport=transport),
        )
        result = await service.run_session(run_id="overview-limit", message="你能做什么",
            history=(), authorization="Bearer test", event_sink=lambda _event: None)
        assert result.stop_reason is StopReason.LIMIT
        assert result.final_message is not None
        text = " ".join(block.text for block in result.final_message.content if isinstance(block, TextBlock))
        assert "尚未完成" in text and "统计产品数量" in text and "不代表已执行业务" in text
        assert len(provider.requests) == 1

    asyncio.run(scenario())


def test_http_tool_diagnostics_are_filtered_by_current_user(tmp_path: Path) -> None:
    async def transport(url: str, headers: Mapping[str, str],
                        _params: Mapping[str, object], _timeout: float) -> object:
        assert url.endswith("/getInfo")
        token = headers.get("Authorization", "")
        if token == "Bearer alice":
            return {"code": 200, "user": {"userId": 7},
                    "permissions": ["mes:md:mditem:list"]}
        if token == "Bearer bob":
            return {"code": 200, "user": {"userId": 8}, "permissions": []}
        raise KTGError("AUTH_REQUIRED", "Authentication required.", status=401)

    def client(token: str) -> KTGClient:
        return KTGClient(base_url="http://127.0.0.1:8080",
                         authorization=token, transport=transport)

    service = QueryService(provider=FakeProvider(()), model_config=ModelConfig("fake", 64),
                           ktg_client_factory=client)
    sessions = SessionService(SessionRepository(tmp_path / "sessions.sqlite3"), client)
    server = AgentHTTPServer(("127.0.0.1", 0), service, sessions)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def get(token: str) -> tuple[int, dict[str, object]]:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/agent-api/v1/tools", headers={"Authorization": token})
            response = connection.getresponse()
            body = cast(dict[str, object], json.loads(response.read()))
            status = response.status
            connection.close()
            return status, body

        alice_status, alice = get("Bearer alice")
        bob_status, bob = get("Bearer bob")
        anonymous_status, _ = get("")
        assert alice_status == bob_status == 200
        assert anonymous_status == 401
        assert {item["name"] for item in cast(list[dict[str, object]], alice["items"])} == {
            "ktg_count_products"}
        assert bob["items"] == []
        assert "handler_ref" not in json.dumps(alice)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_loaded_names_do_not_cross_runs_or_users() -> None:
    async def scenario() -> None:
        async def transport(url: str, headers: Mapping[str, str],
                            _params: Mapping[str, object], _timeout: float) -> object:
            assert url.endswith("/getInfo")
            alice = headers.get("Authorization") == "Bearer alice"
            return {"code": 200, "user": {"userId": 7 if alice else 8},
                    "permissions": ["mes:md:mditem:list"] if alice else []}

        provider = FakeProvider((
            turn(ToolCall("load", "load_tools", {"tools": [
                {"name": "ktg_count_products", "version": "1.0"}]})),
            turn(answer="loaded"),
            turn(ToolCall("guess", "ktg_count_products", {})),
            turn(answer="not available"),
        ))
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 64),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://127.0.0.1:8080", authorization=token,
                transport=transport),
        )

        async def sink(_event: object) -> None:
            pass

        await service.run_session(run_id="alice-run", message="load", history=(),
                                  authorization="Bearer alice", event_sink=sink)
        second = await service.run_session(run_id="bob-run", message="guess", history=(),
                                           authorization="Bearer bob", event_sink=sink)
        assert {schema.name for schema in provider.requests[2].tools} == {
            "search_tools", "load_tools"}
        assert any(event.type is AgentEventType.TOOL_EXECUTION_END
                   and event.payload.get("error_code") == "TOOL_NOT_FOUND"
                   for event in second.events)

    asyncio.run(scenario())


def test_load_rejects_hidden_wrong_version_revoked_and_over_budget_atomically() -> None:
    async def scenario() -> None:
        count, summary = sorted(build_query_tools(), key=lambda item: item.name)
        # Hidden confirmations are a generic runtime feature even though the
        # current business profile no longer registers any write tools.
        hidden_definition = replace(
            count, name="example_hidden_confirmation", expose_to_model=False,
            effect=ToolEffect.WRITE, execution_mode=ExecutionMode.SEQUENTIAL,
            retry_policy=RetryPolicy.NEVER,
        )
        definitions = (count, summary, hidden_definition)
        factories = {item.name: lambda item=item: item for item in definitions}
        catalog = ToolCatalog(factories)
        for item in definitions:
            catalog.register_factory(ToolManifest(
                name=item.name, version=item.version,
                module="wm" if item.name == summary.name else "md", domain="test",
                label=item.label, description=item.description,
                permission_policy=PermissionLeaf(item.required_permissions[0]),
                effect=item.effect, execution_mode=item.execution_mode,
                retry_policy=item.retry_policy, timeout_seconds=item.timeout_seconds,
                expose_to_model=item.expose_to_model,
                confirmation_policy="none" if item.expose_to_model else "trusted_confirmation",
                proposal_store_effect=False, handler_ref=item.name,
            ), factories[item.name])
        switches = CatalogSwitches({"md": True, "wm": True, "pro": True},
                                    {definition.name: True for definition in definitions})
        permissions = frozenset({"*:*:*"})
        candidate = catalog.candidate_snapshot(CatalogIdentity(permissions, "run-a"), switches)

        async def access() -> tuple[frozenset[str] | None, CatalogSwitches]:
            return permissions, switches

        registry = ToolRegistry()
        discovery = RunToolDiscovery(catalog, candidate, registry, access,
                                     max_active_tools=1)
        hidden = await discovery.execute(ToolCall("a", "load_tools", {"tools": [
            {"name": "example_hidden_confirmation", "version": "1.0"}]}))
        assert hidden.error_code == "TOOL_NOT_FOUND"
        wrong = await discovery.execute(ToolCall("b", "load_tools", {"tools": [
            {"name": "ktg_count_products", "version": "2.0"}]}))
        assert wrong.error_code == "TOOL_VERSION_MISMATCH"
        with patch.object(catalog, "resolve_definition", wraps=catalog.resolve_definition) as resolver:
            over = await discovery.execute(ToolCall("c", "load_tools", {"tools": [
                {"name": "ktg_count_products", "version": "1.0"},
                {"name": "ktg_query_stock_summary", "version": "1.0"}]}))
            assert resolver.call_count == 0
        assert over.error_code == "ACTIVE_LIMIT_EXCEEDED"
        assert discovery.begin_request("one").names == frozenset({"search_tools", "load_tools"})
        permissions = frozenset()
        revoked = await discovery.execute(ToolCall("d", "load_tools", {"tools": [
            {"name": "ktg_count_products", "version": "1.0"}]}))
        assert revoked.error_code == "TOOL_NOT_FOUND"
        permissions = frozenset({"*:*:*"})
        switches = CatalogSwitches({"md": False, "wm": True, "pro": True},
                                    {definition.name: True for definition in definitions})
        disabled = await discovery.execute(ToolCall("disabled", "load_tools", {"tools": [
            {"name": "ktg_count_products", "version": "1.0"}]}))
        assert disabled.error_code == "TOOL_NOT_FOUND"
        switches = CatalogSwitches({"md": True, "wm": True, "pro": True},
                                    {definition.name: True for definition in definitions})
        tiny = RunToolDiscovery(catalog, candidate, ToolRegistry(), access,
                                max_schema_tokens=1)
        too_large = await tiny.execute(ToolCall("small", "load_tools", {"tools": [
            {"name": "ktg_count_products", "version": "1.0"}]}))
        assert too_large.error_code == "ACTIVE_LIMIT_EXCEEDED"
        with patch.object(catalog, "resolve_definition", wraps=catalog.resolve_definition) as resolver:
            loaded = await discovery.execute(ToolCall("e", "load_tools", {"tools": [
                {"name": "ktg_count_products", "version": "1.0"}]}))
            assert resolver.call_count == 1
        assert loaded.error_code is None
        assert discovery.active_names("one") == frozenset({"search_tools", "load_tools"})
        assert "ktg_count_products" in discovery.begin_request("two").names

    asyncio.run(scenario())
