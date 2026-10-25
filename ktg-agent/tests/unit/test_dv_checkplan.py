"""Fake-client contract tests for point-inspection/maintenance plans."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

import pytest

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_checkplan import (build_dv_checkplan_tools, read_candidates,
    read_plan_detail, read_plan_links, read_plan_list, read_plan_options)
from ktg_agent.tools.permissions import PermissionAll
from ktg_agent.core.messages import JsonObject, TextBlock, ToolResultMessage
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider
from ktg_agent.ktg.dv_checkplan import _read_content


PLAN = {"planId": 8, "planCode": "P-8", "planName": "每周点检", "planType": "CHECK",
        "startDate": "2026-02-01", "endDate": "2026-12-01", "cycleType": "WEEK",
        "cycleCount": 1, "status": "PREPARE", "remark": "note", "attr1": "must-not-leak"}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    async def get(self, path: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
        self.calls.append((path, params or {}))
        if path.endswith("/list") and path.startswith("/mes/dv/checkplan"):
            return {"rows": [PLAN], "total": 1}
        if path == "/mes/dv/checkplan/8":
            return {"data": PLAN}
        if path == "/system/dict/data/type/dv_plan_type":
            return {"data": [{"dictLabel": "点检", "dictValue": "CHECK"}]}
        if path == "/system/dict/data/type/mes_cycle_type":
            return {"data": [{"dictLabel": "周", "dictValue": "WEEK"},
                              {"dictLabel": "自定义", "dictValue": "SHIFT"}]}
        if path == "/system/dict/data/type/mes_order_status":
            return {"data": [{"dictLabel": "草稿", "dictValue": "PREPARE"}]}
        if path == "/mes/dv/checkmachinery/list":
            return {"rows": [{"recordId": 4, "planId": 8, "machineryId": 11,
                "machineryCode": "EQ-11", "machineryName": "泵", "machineryBrand": "A", "machinerySpec": "S"}], "total": 1}
        if path == "/mes/dv/checksubject/list":
            return {"rows": [{"recordId": 5, "planId": 8, "subjectId": 12,
                "subjectCode": "S-12", "subjectName": "看压力", "subjectType": "CHECK",
                "subjectContent": "压力", "subjectStandard": "正常"}], "total": 1}
        if path == "/mes/dv/machinery/list":
            return {"rows": [{"machineryId": 11, "machineryCode": "EQ-11", "machineryName": "泵",
                "machineryBrand": "A", "machinerySpec": "S", "status": "STOP"}], "total": 1}
        if path == "/mes/dv/dvsubject/list":
            return {"rows": [{"subjectId": 12, "subjectCode": "S-12", "subjectName": "看压力",
                "subjectType": "CHECK", "subjectContent": "压力", "subjectStandard": "正常",
                "enableFlag": "Y"}], "total": 1}
        raise AssertionError(f"unexpected GET {path}: {params}")


def test_list_page_whitelist_and_upper_bound() -> None:
    client = FakeClient()
    result = asyncio.run(read_plan_list(cast(KTGClient, client), {"plan_name": " 每周 ", "page_size": 20}))
    assert result["rows"] == [{key: value for key, value in PLAN.items() if key != "attr1"}]
    assert result["filters"] == {"plan_name": "每周"}
    assert client.calls[0][1] == {"pageNum": 1, "pageSize": 20, "planName": "每周"}
    with pytest.raises(KTGError, match="page_size"):
        asyncio.run(read_plan_list(cast(KTGClient, client), {"page_size": 101}))


def test_detail_requires_one_selector_and_rejects_ambiguous_code() -> None:
    client = FakeClient()
    detail = cast(dict[str, Any], asyncio.run(read_plan_detail(cast(KTGClient, client), {"plan_id": 8})))
    assert detail["plan"]["planId"] == 8
    with pytest.raises(KTGError, match="只能提供"):
        asyncio.run(read_plan_detail(cast(KTGClient, client), {"plan_id": 8, "plan_code": "P-8"}))
    client.get = ambiguous_get  # type: ignore[method-assign]
    with pytest.raises(KTGError) as caught:
        asyncio.run(read_plan_detail(cast(KTGClient, client), {"plan_code": "P-8"}))
    assert caught.value.code == "AMBIGUOUS_MATCH"


async def ambiguous_get(path: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
    if path.endswith("/list"):
        return {"rows": [PLAN, {**PLAN, "planId": 9}], "total": 2}
    raise AssertionError(path)


def test_options_uses_live_dictionary_and_links_are_scoped() -> None:
    client = FakeClient()
    options = cast(dict[str, Any], asyncio.run(read_plan_options(cast(KTGClient, client))))
    assert [item["value"] for item in options["cycle_types"]] == ["WEEK", "SHIFT"]
    assert options["source_baseline"]["cycle_types"] == ["HOUR", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"]
    links = cast(dict[str, Any], asyncio.run(read_plan_links(cast(KTGClient, client), {"plan_id": 8, "kind": "machinery"})))
    assert links["link_kind"] == "machinery" and links["rows"][0]["recordId"] == 4
    assert all(call[0] != "/mes/dv/checksubject/list" for call in client.calls)


def test_candidates_keep_equipment_status_and_filter_enabled_matching_subjects() -> None:
    client = FakeClient()
    machinery = cast(dict[str, Any], asyncio.run(read_candidates(cast(KTGClient, client), {"plan_id": 8}, "machinery")))
    assert machinery["rows"][0]["status"] == "STOP"
    assert "status" not in client.calls[-1][1]
    subjects = cast(dict[str, Any], asyncio.run(read_candidates(cast(KTGClient, client), {"plan_id": 8}, "subject")))
    assert [row["subjectId"] for row in subjects["rows"]] == [12]
    assert client.calls[-1][1]["subjectType"] == "CHECK" and client.calls[-1][1]["enableFlag"] == "Y"


def test_read_tool_contracts_have_exact_permissions_and_bounded_paging() -> None:
    tools = build_dv_checkplan_tools()
    assert len(tools) == 6
    links = next(tool for tool in tools if tool.name == "ktg_dv_checkplan_links")
    assert links.required_permissions == ("mes:dv:checkplan:query", "mes:dv:checkmachinery:list", "mes:dv:checksubject:list")
    assert isinstance(links.input_schema, Mapping)
    assert links.input_schema["additionalProperties"] is False
    assert links.execution_mode.value == "parallel"


def test_provider_serializes_business_identifiers_and_dictionary_values_for_model() -> None:
    link_details = {"kind": "dv_checkplan_links", "plan_id": 8, "link_kind": "machinery",
        "rows": [{"recordId": 4, "planId": 8, "machineryId": 11, "machineryCode": "EQ-11",
                  "machineryName": "泵", "machineryBrand": "A", "machinerySpec": "S"}],
        "total": 1, "page": 1, "page_size": 20}
    typed_link_details = cast(JsonObject, link_details)
    link_text = _read_content(typed_link_details)
    serialized = OpenAICompatibleProvider._message_payload(ToolResultMessage("tool-1", (TextBlock(link_text),), details=typed_link_details))
    assert "recordId[4]" in cast(str, serialized["content"]) and "machineryId[11]" in cast(str, serialized["content"])
    options = {"kind": "dv_checkplan_options", "plan_types": [{"label": "点检", "value": "CHECK"}],
        "cycle_types": [{"label": "周", "value": "WEEK"}], "statuses": [{"label": "草稿", "value": "PREPARE"}]}
    typed_options = cast(JsonObject, options)
    option_payload = OpenAICompatibleProvider._message_payload(ToolResultMessage("tool-2",
        (TextBlock(_read_content(typed_options)),), details=typed_options))
    assert "WEEK（周）" in cast(str, option_payload["content"]) and "CHECK（点检）" in cast(str, option_payload["content"])
