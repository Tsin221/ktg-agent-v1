import asyncio
from collections.abc import Mapping

from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall
from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.exploration.runtime import ExplorationRuntime
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService
from ktg_agent.tools.catalog import CatalogIdentity


def _turn(call: ToolCall | None = None) -> ScriptedTurn:
    return ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((call,) if call else (TextBlock("可按设备线索查询。"),)),
        StopReason.TOOL_CALL if call else StopReason.COMPLETED, Usage(1, 1))),))


def test_scoped_tools_are_discovered_loaded_and_executed_through_existing_agent() -> None:
    async def scenario() -> None:
        async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            assert url.endswith("/getInfo")
            return {"code": 200, "user": {"userId": 18, "userName": "yuangong2"},
                    "permissions": ["mes:dv:machinery:list", "mes:dv:machinery:query"]}

        provider = FakeProvider([
            _turn(ToolCall("search", "search_tools", {"query": "业务对象目录"})),
            _turn(ToolCall("load", "load_tools", {"tools": [{"name": "ktg_describe_business", "version": "1.0"}]})),
            _turn(ToolCall("catalog", "ktg_describe_business", {"entity_types": ["dv.machinery"]})),
            _turn(),
        ])
        store = ReferenceStore()
        service = QueryService(provider=provider, model_config=ModelConfig("fake", 512),
                               ktg_client_factory=lambda token: KTGClient(base_url="http://test", authorization=token,
                                                                         transport=transport),
                               exploration_runtime=ExplorationRuntime(store))
        result = await service.query(message="根据机器线索查一下", authorization="Bearer test")
        assert result["stop_reason"] == "completed"
        assert "可按设备线索查询" in str(result["answer"])
        assert "exploration_catalog" in str(result["results"])
        assert len(provider.requests) == 4
        restricted = service._catalog.candidate_snapshot(CatalogIdentity(frozenset({"mes:qc:ipqc:list"}), "other"),
                                                        service._switches_provider())
        assert not set(restricted.candidates).intersection(ExplorationRuntime.enabled_names)
        store.close()

    asyncio.run(scenario())
