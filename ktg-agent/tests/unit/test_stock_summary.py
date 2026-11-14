from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.queries import STOCK_SUMMARY_TOOL, build_query_tools
from ktg_agent.tools import ToolContext, ToolResult, ToolStatus


@pytest.mark.parametrize(("quantity", "has_records", "business_code", "expected_error"), [
    ("308.1250", True, 200, None),
    ("0.0000", True, 200, None),
    ("0.0000", False, 200, None),
    (308.125, True, 200, "KTG_INVALID_RESPONSE"),
    ("NaN", True, 200, "KTG_INVALID_RESPONSE"),
    ("0", False, 403, "PERMISSION_DENIED"),
    ("0", False, 500, "KTG_REQUEST_FAILED"),
])
def test_stock_summary_passes_exact_mes_result(
    quantity: str | float, has_records: bool, business_code: int, expected_error: str | None,
) -> None:
    async def transport(
        url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout_seconds: float,
    ) -> dict[str, object]:
        assert url == "http://mes.test/mes/wm/wmstock/summary"
        assert headers["Authorization"] == "Bearer token"
        assert params == {"itemCode": "A-01", "warehouseId": 7}
        groups = [{"itemId": 1, "itemCode": "A-01", "warehouseId": 7, "warehouseCode": "WH",
                   "warehouseName": "Main", "unitOfMeasure": "pcs", "sourceRowCount": 49,
                   "quantityOnhand": quantity}] if has_records else []
        return {"code": business_code, "data": {
            "itemCode": "A-01", "warehouseId": 7, "hasRecords": has_records,
            "queriedAt": "2026-11-14T08:00:00.123456789Z", "groups": groups,
        }}

    tool = next(tool for tool in build_query_tools() if tool.name == STOCK_SUMMARY_TOOL)
    client = KTGClient(base_url="http://mes.test", authorization="Bearer token", transport=transport)
    async def execute() -> ToolResult:
        return await tool.execute(ToolContext("run", "stock", ktg_client=client),
                                  {"item_code": " A-01 ", "warehouse_id": 7})

    result = asyncio.run(execute())
    assert result.error_code == expected_error
    if expected_error:
        assert result.status is ToolStatus.FAILED and "待核实" in result.content
        return
    assert result.status is ToolStatus.SUCCEEDED and result.details is not None
    assert result.details["no_records"] is (not has_records)
    assert result.details["queried_at"] == "2026-11-14T08:00:00.123456789Z"
    groups = result.details["groups"]
    assert isinstance(groups, list)
    if has_records:
        assert len(groups) == 1 and isinstance(groups[0], dict)
        assert groups[0]["quantityOnhand"] == quantity and groups[0]["sourceRowCount"] == 49
    else:
        assert groups == []
        assert "无库存记录不能等同于数量为零" in result.content
