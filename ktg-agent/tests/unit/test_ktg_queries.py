import asyncio
import re
from collections.abc import Mapping

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.tools import ToolContext


def test_query_tool_names_are_openai_compatible() -> None:
    for tool in build_query_tools():
        assert re.fullmatch(r"[A-Za-z0-9_-]+", tool.name)


def test_client_validates_current_user_and_forwards_bearer_token() -> None:
    asyncio.run(_client_validates_current_user_and_forwards_bearer_token())


async def _client_validates_current_user_and_forwards_bearer_token() -> None:
    requests: list[tuple[str, Mapping[str, str], Mapping[str, object]]] = []

    async def transport(
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        requests.append((url, headers, params))
        return {"code": 200, "user": {"userName": "alice", "userId": 7}}

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=transport,
    )

    identity = await client.get_current_user()

    assert identity == {"userName": "alice", "userId": 7}
    assert requests == [
        (
            "http://127.0.0.1:8080/getInfo",
            {"Authorization": "Bearer current-user-token"},
            {},
        )
    ]


def test_three_query_tools_use_fixed_read_only_endpoints() -> None:
    asyncio.run(_three_query_tools_use_fixed_read_only_endpoints())


def test_product_count_uses_product_filter_and_server_total() -> None:
    asyncio.run(_product_count_uses_product_filter_and_server_total())


def test_products_can_be_listed_without_a_search_term() -> None:
    asyncio.run(_products_can_be_listed_without_a_search_term())


async def _products_can_be_listed_without_a_search_term() -> None:
    requests: list[Mapping[str, object]] = []

    async def transport(url, _headers, params, _timeout_seconds):
        assert url.endswith("/mes/md/mditem/list")
        requests.append(params)
        return {"code": 200, "total": 2, "rows": [
            {"itemId": 1, "itemCode": "P-01", "itemName": "产品一", "itemOrProduct": "PRODUCT"},
            {"itemId": 2, "itemCode": "P-02", "itemName": "产品二", "itemOrProduct": "PRODUCT"},
        ]}

    client = KTGClient(base_url="http://127.0.0.1:8080",
                       authorization="Bearer current-user-token", transport=transport)
    tool = next(tool for tool in build_query_tools() if tool.name == "ktg_search_items")
    context = ToolContext(run_id="run-1", tool_call_id="call-1",
                          identity={"userId": 7}, ktg_client=client)

    result = await tool.execute(context, {})

    assert result.details is not None
    assert result.details["total"] == 2
    assert [row["itemCode"] for row in result.details["rows"]] == ["P-01", "P-02"]
    assert requests == [{"pageNum": 1, "pageSize": 10, "itemOrProduct": "PRODUCT"}]


async def _product_count_uses_product_filter_and_server_total() -> None:
    requests: list[tuple[str, Mapping[str, object]]] = []

    async def transport(
        url: str,
        _headers: Mapping[str, str],
        params: Mapping[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        requests.append((url, params))
        return {"code": 200, "total": 37, "rows": [{"itemId": 1, "itemOrProduct": "PRODUCT"}]}

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=transport,
    )
    tool = next(tool for tool in build_query_tools() if tool.name == "ktg_count_products")
    context = ToolContext(
        run_id="run-1", tool_call_id="call-1", identity={"userId": 7}, ktg_client=client
    )

    result = await tool.execute(context, {})

    assert result.details is not None
    assert result.details["kind"] == "product_count"
    assert result.details["total"] == 37
    assert requests == [
        (
            "http://127.0.0.1:8080/mes/md/mditem/list",
            {"itemOrProduct": "PRODUCT", "pageNum": 1, "pageSize": 1},
        )
    ]


async def _three_query_tools_use_fixed_read_only_endpoints() -> None:
    requests: list[tuple[str, Mapping[str, object]]] = []

    async def transport(
        url: str,
        _headers: Mapping[str, str],
        params: Mapping[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        requests.append((url, params))
        if url.endswith("/mditem/list"):
            return {
                "code": 200,
                "total": 1,
                "rows": [
                    {
                        "itemId": 1,
                        "itemCode": "A-01",
                        "itemName": "产品 A",
                        "unitOfMeasure": "件",
                    }
                ],
            }
        if url.endswith("/wmstock/list"):
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
        return {
            "code": 200,
            "total": 1,
            "rows": [
                {
                    "workorderId": 9,
                    "workorderCode": "WO-001",
                    "workorderName": "产品 A 工单",
                    "workorderType": "SELF",
                    "productCode": "A-01",
                    "quantity": 10,
                    "status": "PREPARE",
                    "version": 3,
                }
            ],
        }

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=transport,
    )
    tools = {tool.name: tool for tool in build_query_tools()}
    context = ToolContext(
        run_id="run-1",
        tool_call_id="call-1",
        identity={"userId": 7},
        ktg_client=client,
    )

    item_result = await tools["ktg_search_items"].execute(
        context, {"item_name": "产品 A", "page": 1, "page_size": 10}
    )
    stock_result = await tools["ktg_query_stock"].execute(
        context, {"item_code": "A-01", "page": 1, "page_size": 10}
    )
    order_result = await tools["ktg_list_workorders"].execute(
        context,
        {
            "product_code": "A-01",
            "workorder_type": "SELF",
            "page": 1,
            "page_size": 10,
        },
    )

    assert item_result.details is not None
    assert item_result.details["kind"] == "items"
    assert stock_result.details is not None
    assert stock_result.details["kind"] == "stock"
    assert order_result.details is not None
    assert order_result.details["kind"] == "workorders"
    order_rows = order_result.details["rows"]
    assert isinstance(order_rows, list)
    assert isinstance(order_rows[0], dict)
    assert order_rows[0]["workorderType"] == "SELF"
    assert order_rows[0]["version"] == 3
    assert requests == [
        (
            "http://127.0.0.1:8080/mes/md/mditem/list",
            {"itemName": "产品 A", "pageNum": 1, "pageSize": 10},
        ),
        (
            "http://127.0.0.1:8080/mes/wm/wmstock/list",
            {"itemCode": "A-01", "pageNum": 1, "pageSize": 10},
        ),
        (
            "http://127.0.0.1:8080/mes/pro/workorder/list",
            {
                "productCode": "A-01",
                "workorderType": "SELF",
                "pageNum": 1,
                "pageSize": 10,
            },
        ),
    ]

