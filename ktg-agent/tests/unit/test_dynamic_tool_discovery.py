"""MES-05 request-level discovery and activation checks."""

import asyncio
import json
from collections.abc import Mapping
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import httpx

from ktg_agent.core.events import AgentEventType
from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall, ToolResultMessage
from ktg_agent.core.loop import RunLimits
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_checkplan_writes import DvCheckPlanPreviewStore
from ktg_agent.ktg.dv_machinery_writes import DvMachineryPreviewStore
from ktg_agent.ktg.dv_machinerytype_writes import DvMachineryTypePreviewStore
from ktg_agent.ktg.dv_repair_writes import DvRepairPreviewStore
from ktg_agent.ktg.dv_subject_writes import DvSubjectPreviewStore
from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore
from ktg_agent.ktg.tm_tool_writes import TmToolPreviewStore
from ktg_agent.ktg.tm_tooltype_writes import TmToolTypePreviewStore
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider
from ktg_agent.service import QueryService
from ktg_agent.server import AgentHTTPServer
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.tools.catalog import CatalogIdentity, CatalogSwitches
from ktg_agent.tools.discovery import RunToolDiscovery
from ktg_agent.tools.legacy_catalog import build_legacy_catalog
from ktg_agent.tools.registry import ToolRegistry
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.ktg.workorders import PreviewStore, build_workorder_tools
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
        assert "ktg_create_workorder_draft" not in json.dumps(search_result)
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


def test_loaded_tm_tool_detail_constraint_reaches_model_http_request() -> None:
    name = "ktg_tm_tool_detail"
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
            delta = {"content": "请提供 ID 或编码。"}
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
            "permissions": ["mes:tm:tool:list", "mes:tm:tool:query"],
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
            run_id="tm-tool-http", message="按编码查询工装夹具详情", history=(),
            authorization="Bearer test", event_sink=lambda _event: None,
        ))

    assert len(requests) == 4
    functions = [{tool["function"]["name"]: tool["function"] for tool in body["tools"]}
                 for body in requests]
    assert [set(step) for step in functions[:2]] == [{"search_tools", "load_tools"}] * 2
    assert functions[2][name]["parameters"]["oneOf"] == [
        {"required": ["tool_id"]}, {"required": ["tool_code"]},
    ]
    assert functions[2][name]["parameters"]["additionalProperties"] is False
    assert any(event.type is AgentEventType.TOOL_EXECUTION_END
               and event.payload.get("tool_call_id") == "call-2"
               and event.payload.get("error_code") == "VALIDATION_ERROR" for event in result.events)
    tool_message = next(message for message in requests[3]["messages"]
                        if message.get("tool_call_id") == "call-2")
    assert "必须且只能提供 tool_id 或 tool_code" in tool_message["content"]
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
        for query in ("全部库存", "查询所有仓库的全部库存汇总", "查询库存列表 分页 仓库 物料 数量 单位", "仓储"):
            result = await discovery.execute(ToolCall("s", "search_tools", {"query": query}))
            assert result.details is not None
            assert cast(list[dict[str, object]], result.details["items"])[0]["name"] == "ktg_query_stock"
            assert "ktg_query_stock" in result.content and "1.0" in result.content
        overview = await discovery.execute(ToolCall("s", "search_tools", {"query": ""}))
        assert overview.details is not None and len(cast(list[object], overview.details["items"])) == 4
        invalid = await discovery.execute(ToolCall("s", "search_tools", {"module": "库存"}))
        assert invalid.error_code == "VALIDATION_ERROR" and "wm" in invalid.content
        wrong = await discovery.execute(ToolCall("l", "load_tools", {"tools": [
            {"name": "ktg_query_stock", "version": "1.0.0"}]}))
        assert wrong.error_code == "TOOL_VERSION_MISMATCH"
        assert "1.0" in wrong.content and "ktg_query_stock" in wrong.content
        failed_batch = await discovery.execute(ToolCall("l", "load_tools", {"tools": [
            {"name": "ktg_query_stock", "version": "1.0"},
            {"name": "invented", "version": "1.0"}]}))
        assert "tools[1]" in failed_batch.content
        assert discovery.begin_request("next").names == frozenset({"search_tools", "load_tools"})
        fallback = await discovery.execute(ToolCall("s", "search_tools", {"query": "不存在的能力xyz"}))
        assert fallback.details is not None
        assert fallback.details["match_type"] == "available_alternatives"
        assert "not evidence" in fallback.content
        permissions = frozenset()
        empty = await discovery.execute(ToolCall("s", "search_tools", {"query": "anything"}))
        assert "ktg_query_stock" not in empty.content

    asyncio.run(scenario())


def test_tm_search_distinguishes_nearby_intents() -> None:
    async def scenario() -> None:
        service = QueryService(
            provider=FakeProvider(()), model_config=ModelConfig("fake", 32),
            ktg_client_factory=lambda token: KTGClient(base_url="http://mes.test", authorization=token),
            tm_tool_store=TmToolPreviewStore(),
            tm_tooltype_store=TmToolTypePreviewStore(),
        )
        permissions = frozenset({"*:*:*"})
        switches = service._switches_provider()
        catalog = service._catalog
        candidate = catalog.candidate_snapshot(CatalogIdentity(permissions, "tm-search"), switches)

        async def access() -> tuple[frozenset[str], CatalogSwitches]:
            return permissions, switches

        discovery = RunToolDiscovery(catalog, candidate, ToolRegistry(), access, page_module="tm")
        cases = {
            "分页查询在库工装夹具台账": "ktg_tm_tool_list",
            "按完整编码查一条工装夹具详情": "ktg_tm_tool_detail",
            "查询有哪些工装夹具类型": "ktg_tm_tooltype_list",
            "查询工装夹具类型详情": "ktg_tm_tooltype_detail",
            "查询新增夹具可用的类型和台账状态选项": "ktg_tm_tool_options",
            "查询工装类型的维护方式和一件一码选项": "ktg_tm_tooltype_options",
            "为工装夹具新增生成预览": "ktg_preview_tm_tool_create",
            "生成工装夹具类型新增预览": "ktg_preview_tm_tooltype_create",
            "修改工装夹具品牌生成预览": "ktg_preview_tm_tool_update",
            "修改工装夹具类型维护周期生成预览": "ktg_preview_tm_tooltype_update",
            "删除一条在库工装夹具生成预览": "ktg_preview_tm_tool_delete",
            "生成删除工装夹具类型的预览": "ktg_preview_tm_tooltype_delete",
        }
        mismatches = {}
        for query, expected in cases.items():
            result = await discovery.execute(ToolCall("s", "search_tools", {"query": query}))
            assert result.details is not None and result.details["match_type"] == "matched"
            items = cast(list[dict[str, object]], result.details["items"])
            assert expected in {item["name"] for item in items}, query
            if items[0]["name"] != expected:
                mismatches[query] = (expected, items[0]["name"])
            assert not any(item["name"] in {
                f"ktg_tm_{kind}_{action}" for kind in ("tool", "tooltype")
                for action in ("create", "update", "delete")
            } for item in items)
        assert not mismatches

        fallback = await discovery.execute(ToolCall("s", "search_tools", {
            "query": "火星气候", "module": "tm",
        }))
        assert fallback.details is not None
        assert fallback.details["match_type"] == "available_alternatives"

        permissions = frozenset({"mes:tm:tool:list"})
        restricted = await discovery.execute(ToolCall("s", "search_tools", {
            "query": "工装夹具详情", "module": "tm",
        }))
        assert restricted.details is not None
        items = cast(list[dict[str, object]], restricted.details["items"])
        assert {item["name"] for item in items} == {"ktg_tm_tool_list"}

    asyncio.run(scenario())


def test_device_search_distinguishes_records_options_and_previews() -> None:
    async def scenario() -> None:
        service = QueryService(
            provider=FakeProvider(()), model_config=ModelConfig("fake", 32),
            ktg_client_factory=lambda token: KTGClient(base_url="http://mes.test", authorization=token),
            dv_repair_store=DvRepairPreviewStore(),
            dv_checkplan_store=DvCheckPlanPreviewStore(),
            dv_subject_store=DvSubjectPreviewStore(),
            dv_machinery_store=DvMachineryPreviewStore(),
            dv_machinerytype_store=DvMachineryTypePreviewStore(),
        )
        permissions = frozenset({"*:*:*"})
        switches = service._switches_provider()
        catalog = service._catalog
        candidate = catalog.candidate_snapshot(CatalogIdentity(permissions, "dv-search"), switches)

        async def access() -> tuple[frozenset[str], CatalogSwitches]:
            return permissions, switches

        discovery = RunToolDiscovery(catalog, candidate, ToolRegistry(), access, page_module="dv")
        cases = {
            "维修单列表 维修结果筛选": "ktg_dv_repair_list",
            "维修单详情 完工日期 验收": "ktg_dv_repair_detail",
            "维修内容 故障现象 lineId": "ktg_dv_repair_lines",
            "维修选项 字典值": "ktg_dv_repair_options",
            "维修设备候选 关联设备": "ktg_dv_repair_machinery_candidates",
            "维修项目候选 关联项目": "ktg_dv_repair_subject_candidates",
            "预览删除维修单 内容行数": "ktg_preview_dv_repair_delete",
            "预览删除维修内容 内容行ID": "ktg_preview_dv_repair_line_delete",
            "分页筛选点检保养计划": "ktg_dv_checkplan_list",
            "查询单条点检保养计划详情": "ktg_dv_checkplan_detail",
            "查询点检计划已关联设备明细": "ktg_dv_checkplan_links",
            "查询计划可选设备候选": "ktg_dv_checkplan_machinery_candidates",
            "查询计划可关联项目候选": "ktg_dv_checkplan_subject_candidates",
            "查询点检保养项目台账": "ktg_dv_subject_list",
            "查询点检保养项目详情": "ktg_dv_subject_detail",
            "查询计划周期类型选项": "ktg_dv_checkplan_options",
            "分页筛选车间设备台账": "ktg_dv_machinery_list",
            "按编码查询设备详情": "ktg_dv_machinery_detail",
            "查询设备分类树": "ktg_dv_machinerytype_tree",
            "查询设备分类详情与影响面": "ktg_dv_machinerytype_detail",
            "查询启用车间候选": "ktg_md_machinery_workshop_options",
            "生成新增设备预览": "ktg_preview_dv_machinery_create",
            "生成新增设备分类预览": "ktg_preview_dv_machinerytype_create",
        }
        mismatches = {}
        for query, expected in cases.items():
            result = await discovery.execute(ToolCall("s", "search_tools", {"query": query}))
            assert result.details is not None and result.details["match_type"] == "matched"
            items = cast(list[dict[str, object]], result.details["items"])
            for item in items:
                assert catalog.manifest(cast(str, item["name"])).expose_to_model
            if items[0]["name"] != expected:
                mismatches[query] = (expected, items[0]["name"])
        assert not mismatches

    asyncio.run(scenario())


def test_production_and_inventory_search_separates_nearby_intents(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = QueryService(
            provider=FakeProvider(()), model_config=ModelConfig("fake", 32),
            ktg_client_factory=lambda token: KTGClient(base_url="http://mes.test", authorization=token),
            inventory_store=InventoryStore(str(tmp_path / "inventory.sqlite3")),
            inventory_actions=frozenset({RECEIPT, ISSUE}),
            enable_workorder_progress=True,
        )
        permissions = frozenset({"*:*:*"})
        switches = service._switches_provider()
        catalog = service._catalog
        candidate = catalog.candidate_snapshot(CatalogIdentity(permissions, "pro-wm-search"), switches)

        async def access() -> tuple[frozenset[str], CatalogSwitches]:
            return permissions, switches

        discovery = RunToolDiscovery(catalog, candidate, ToolRegistry(), access)
        cases = {
            "一共有多少产品": "ktg_count_products",
            "分页查询产品资料明细": "ktg_search_items",
            "按状态分页查询生产工单列表": "ktg_list_workorders",
            "查询单张生产工单详情": "ktg_pro_workorder_get",
            "查询工单生产进度": "ktg_pro_workorder_progress",
            "生产任务列表 工序": "ktg_pro_task_list",
            "生产任务详情 开始时间": "ktg_pro_task_get",
            "报工列表 报工时间": "ktg_pro_feedback_list",
            "报工详情 不合格数量": "ktg_pro_feedback_get",
            "生成新增生产工单预览": "ktg_preview_create_workorder_draft",
            "编辑工单草稿生成预览": "ktg_preview_edit_workorder_draft",
            "查指定物料的分仓库存": "ktg_query_stock",
            "预览物料入库单": "ktg_preview_item_receipt",
            "预览生产领料单": "ktg_preview_production_issue",
            "查询库存操作执行回执": "ktg_wm_operation_result",
            "查看生产领料执行回执": "ktg_wm_operation_result",
        }
        mismatches = {}
        for query, expected in cases.items():
            result = await discovery.execute(ToolCall("s", "search_tools", {"query": query}))
            assert result.details is not None and result.details["match_type"] == "matched"
            items = cast(list[dict[str, object]], result.details["items"])
            assert all(catalog.manifest(cast(str, item["name"])).expose_to_model for item in items)
            if items[0]["name"] != expected:
                mismatches[query] = (expected, items[0]["name"])
        assert not mismatches

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
        assert "尚未完成" in text and "查询产品" in text and "不代表已执行业务" in text
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
            "ktg_count_products", "ktg_search_items"}
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
        definitions = (*build_query_tools(), *build_workorder_tools(PreviewStore()))
        catalog = build_legacy_catalog(definitions)
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
            {"name": "ktg_create_workorder_draft", "version": "1.0"}]}))
        assert hidden.error_code == "TOOL_NOT_FOUND"
        wrong = await discovery.execute(ToolCall("b", "load_tools", {"tools": [
            {"name": "ktg_count_products", "version": "2.0"}]}))
        assert wrong.error_code == "TOOL_VERSION_MISMATCH"
        with patch.object(catalog, "resolve_definition", wraps=catalog.resolve_definition) as resolver:
            over = await discovery.execute(ToolCall("c", "load_tools", {"tools": [
                {"name": "ktg_count_products", "version": "1.0"},
                {"name": "ktg_search_items", "version": "1.0"}]}))
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
