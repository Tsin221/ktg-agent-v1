import asyncio
from collections.abc import Mapping

from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.service import QueryService


def test_query_service_validates_identity_runs_tool_and_returns_business_results() -> None:
    asyncio.run(_query_service_scenario())


def test_query_service_exposes_preview_but_not_write_tools_to_model() -> None:
    asyncio.run(_query_service_exposes_preview_but_not_write_tools_to_model())


async def _query_service_exposes_preview_but_not_write_tools_to_model() -> None:
    async def ktg_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout_seconds: float,
    ) -> object:
        if url.endswith("/getInfo"):
            return {"code": 200, "user": {"userId": 7, "userName": "alice"}}
        if url.endswith("/mes/md/mditem/list"):
            return {
                "code": 200,
                "total": 1,
                "rows": [{
                    "itemId": 11,
                    "itemCode": "A-01",
                    "itemName": "产品 A",
                    "unitOfMeasure": "件",
                }],
            }
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-001"
        if url.endswith("/mes/pro/workorder/51"):
            return {
                "code": 200,
                "data": {
                    "workorderId": 51,
                    "workorderCode": "WO-001",
                    "workorderName": "产品 A 工单",
                    "workorderType": "SELF",
                    "orderSource": "MANUAL",
                    "productId": 11,
                    "productName": "产品 A",
                    "unitOfMeasure": "件",
                    "quantity": 100,
                    "requestDate": "2026-10-31",
                    "parentId": 0,
                    "status": "PREPARE",
                    "version": 0,
                },
            }
        if url.endswith("/mes/pro/workorderbom/list"):
            return {"code": 200, "total": 0, "rows": []}
        raise AssertionError(url)

    async def write_transport(
        method: str,
        url: str,
        _headers: Mapping[str, str],
        _body: Mapping[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        assert method == "POST"
        assert url.endswith("/mes/pro/workorder")
        return {"code": 200, "data": 51}

    provider = FakeProvider(
        (
            ScriptedTurn(
                (
                    ResponseCompleted(FinalResponse(
                        message=AssistantMessage((ToolCall(
                            id="preview",
                            name="ktg_preview_create_workorder_draft",
                            arguments={
                                "product_code": "A-01",
                                "quantity": 100,
                                "request_date": "2026-10-31",
                            },
                        ),)),
                        stop_reason=StopReason.TOOL_CALL,
                        usage=Usage(4, 2),
                    )),
                )
            ),
            ScriptedTurn(
                (
                    ResponseCompleted(FinalResponse(
                        message=AssistantMessage((TextBlock("请确认预览。"),)),
                        stop_reason=StopReason.COMPLETED,
                        usage=Usage(8, 3),
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
            write_transport=write_transport,
        )

    service = QueryService(
        provider=provider,
        model_config=ModelConfig(model="test-model", max_output_tokens=256),
        ktg_client_factory=client_factory,
    )
    response = await service.query(
        message="为 A-01 创建 100 件工单草稿，需求日期 2026-10-31",
        authorization="Bearer current-user-token",
    )

    tool_names = {tool.name for tool in provider.requests[0].tools}
    assert "ktg_preview_create_workorder_draft" in tool_names
    assert "ktg_preview_edit_workorder_draft" in tool_names
    assert "ktg_create_workorder_draft" not in tool_names
    assert "ktg_edit_workorder_draft" not in tool_names
    results = response["results"]
    assert isinstance(results, list)
    assert isinstance(results[0], dict)
    assert results[0]["kind"] == "workorder_draft_preview"
    preview_id = results[0]["preview_id"]
    assert isinstance(preview_id, str)

    confirmation = await service.confirm(
        preview_id=preview_id,
        authorization="Bearer current-user-token",
    )
    result = confirmation["result"]
    assert isinstance(result, dict)
    assert result["outcome"] == "succeeded"
    assert result["workorderId"] == 51


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

