import asyncio
import re
from collections.abc import Mapping

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.tools import ToolContext


def test_query_tool_names_are_openai_compatible() -> None:
    for tool in build_query_tools():
        assert re.fullmatch(r"[A-Za-z0-9_-]+", tool.name)


def test_only_retained_business_query_tools_are_defined() -> None:
    assert {tool.name for tool in build_query_tools()} == {
        "ktg_count_products", "ktg_query_stock_summary",
    }


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


def test_product_count_uses_product_filter_and_server_total() -> None:
    asyncio.run(_product_count_uses_product_filter_and_server_total())


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

