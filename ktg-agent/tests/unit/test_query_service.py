import asyncio
from collections.abc import Mapping

from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall
from ktg_agent.core.loop import RunLimits
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService


def test_legacy_query_returns_token_stop_reasons_without_running_tools() -> None:
    async def scenario(usage: Usage | None) -> None:
        async def transport(
            _url: str, _headers: Mapping[str, str], _params: Mapping[str, object],
            _timeout_seconds: float,
        ) -> object:
            return {"code": 200, "user": {"userId": 7, "userName": "alice"},
                    "permissions": ["*:*:*"]}

        provider = FakeProvider([ScriptedTurn((ResponseCompleted(FinalResponse(
            AssistantMessage((ToolCall("call-1", "ktg_query_stock_summary", {"item_code": "A-01"}),)),
            StopReason.TOOL_CALL, usage,
        )),))])
        service = QueryService(
            provider=provider, model_config=ModelConfig("fake", 64),
            ktg_client_factory=lambda authorization: KTGClient(
                base_url="http://127.0.0.1:8080", authorization=authorization,
                transport=transport,
            ),
            limits=RunLimits(max_total_tokens=2),
        )
        result = await service.query(message="stock", authorization="Bearer test")
        assert result["stop_reason"] == ("limit" if usage else "error")
        assert result.get("limit_reason") == ("max_total_tokens" if usage else None)
        assert result.get("error_code") == ("MODEL_USAGE_UNAVAILABLE" if usage is None else None)
        assert result["results"] == [{
            "execution_status": "not_executed",
            "stop_reason": "limit" if usage else "error",
            **({"limit_reason": "max_total_tokens"} if usage else
               {"error_code": "MODEL_USAGE_UNAVAILABLE"}),
        }]
        assert len(provider.requests) == 1

    asyncio.run(scenario(Usage(1, 1)))
    asyncio.run(scenario(None))


def test_query_service_validates_identity_runs_tool_and_returns_business_results() -> None:
    asyncio.run(_query_service_scenario())


def test_query_service_does_not_expose_retired_business_write_tools() -> None:
    async def scenario() -> None:
        async def transport(url: str, _headers: Mapping[str, str], _params: Mapping[str, object],
                            _timeout: float) -> object:
            assert url.endswith("/getInfo")
            return {"code": 200, "user": {"userId": 7, "userName": "alice"}, "permissions": ["*:*:*"]}

        provider = FakeProvider((
            ScriptedTurn((ResponseCompleted(FinalResponse(
                AssistantMessage((ToolCall("old", "load_tools", {"tools": [
                    {"name": "ktg_preview_create_workorder_draft", "version": "1.0"}]}),)),
                StopReason.TOOL_CALL)),)),
            ScriptedTurn((ResponseCompleted(FinalResponse(
                AssistantMessage((TextBlock("未开放该操作。"),)), StopReason.COMPLETED)),)),
        ))
        service = QueryService(provider=provider, model_config=ModelConfig("fake", 64),
            ktg_client_factory=lambda token: KTGClient(
                base_url="http://localhost", authorization=token, transport=transport))
        result = await service.query(message="创建工单", authorization="Bearer test")
        assert result["stop_reason"] == "completed"
        assert {schema.name for schema in provider.requests[1].tools} == {"search_tools", "load_tools"}
        assert not hasattr(service, "confirm")

    asyncio.run(scenario())


async def _query_service_scenario() -> None:
    requested_urls: list[str] = []

    async def ktg_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        requested_urls.append(url)
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": 7, "userName": "alice"},
                    "permissions": ["*:*:*"]}
        return {
            "code": 200,
            "total": 1,
            "rows": [
                {
                    "itemCode": "A-01",
                    "itemName": "产品 A",
                    "warehouseName": "成品仓",
                    "quantityOnhand": 8,
                    "unitOfMeasure": "件",
                }
            ],
        }

    provider = FakeProvider(
        (
            ScriptedTurn((ResponseCompleted(FinalResponse(
                message=AssistantMessage((ToolCall(
                    id="load-stock", name="load_tools",
                    arguments={"tools": [{"name": "ktg_count_products", "version": "1.0"}]},
                ),)), stop_reason=StopReason.TOOL_CALL, usage=Usage(2, 1))),)),
            ScriptedTurn(
                (
                    ResponseCompleted(FinalResponse(
                        message=AssistantMessage(
                            (
                                ToolCall(
                                    id="call-stock",
                                    name="ktg_count_products",
                                    arguments={},
                                ),
                            )
                        ),
                        stop_reason=StopReason.TOOL_CALL,
                        usage=Usage(4, 2),
                    )),
                )
            ),
            ScriptedTurn(
                (
                    ResponseCompleted(FinalResponse(
                        message=AssistantMessage(
                            (TextBlock("共有 1 个产品。"),)
                        ),
                        stop_reason=StopReason.COMPLETED,
                        usage=Usage(8, 6),
                    )),
                )
            ),
        )
    )

    def client_factory(authorization: str) -> KTGClient:
        return KTGClient(
            base_url="http://127.0.0.1:8080",
            authorization=authorization,
            transport=ktg_transport,
        )

    service = QueryService(
        provider=provider,
        model_config=ModelConfig(model="test-model", max_output_tokens=256),
        ktg_client_factory=client_factory,
    )

    response = await service.query(
        message="有几个产品",
        authorization="Bearer current-user-token",
    )

    assert response["answer"] == "共有 1 个产品。"
    assert response["stop_reason"] == "completed"
    assert response["current_user"] == "alice"
    results = response["results"]
    assert isinstance(results, list)
    first_result = results[0]
    assert isinstance(first_result, dict)
    assert first_result["kind"] == "product_count"
    assert requested_urls[-1] == "http://127.0.0.1:8080/mes/md/mditem/list"
    assert all(url.endswith("/getInfo") for url in requested_urls[:-1])
