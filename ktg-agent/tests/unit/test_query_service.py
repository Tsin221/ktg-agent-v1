import asyncio
from collections.abc import Mapping

from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService


def test_query_service_validates_identity_runs_tool_and_returns_business_results() -> None:
    asyncio.run(_query_service_scenario())


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
            return {"code": 200, "user": {"userId": 7, "userName": "alice"}}
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
            ScriptedTurn(
                (
                    ResponseCompleted(FinalResponse(
                        message=AssistantMessage(
                            (
                                ToolCall(
                                    id="call-stock",
                                    name="ktg_query_stock",
                                    arguments={"item_code": "A-01"},
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
                            (TextBlock("产品 A 在成品仓有 8 件库存。"),)
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
        message="查询 A-01 库存",
        authorization="Bearer current-user-token",
    )

    assert response["answer"] == "产品 A 在成品仓有 8 件库存。"
    assert response["stop_reason"] == "completed"
    assert response["current_user"] == "alice"
    results = response["results"]
    assert isinstance(results, list)
    first_result = results[0]
    assert isinstance(first_result, dict)
    assert first_result["kind"] == "stock"
    assert requested_urls == [
        "http://127.0.0.1:8080/getInfo",
        "http://127.0.0.1:8080/mes/wm/wmstock/list",
    ]

