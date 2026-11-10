"""Contract tests for tool ledger read tools."""
import asyncio
from collections.abc import Mapping

import pytest
from typing import cast

from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, JsonValue, ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.tm_tool import (
    DETAIL_NAME,
    LIST_NAME,
    OPTIONS_NAME,
    build_tm_tool_tools,
    project_tool,
    read_tool_detail,
    read_tool_list,
    read_tool_options,
)
from ktg_agent.tools import ToolContext, ToolRegistry, ToolResult, ToolRunner, ToolStatus

ROW = {"toolId": 17, "toolCode": "LUNA-17", "toolName": "试验夹具", "brand": "B",
       "spec": "S", "toolTypeId": 2, "toolTypeCode": "TT2", "toolTypeName": "通用类型",
       "codeFlag": "N", "quantity": 3, "quantityAvail": 3, "maintenType": "REGULAR",
       "nextMaintenPeriod": None, "nextMaintenDate": "2026-12-31", "status": "STORE", "remark": None}


def object_value(value: JsonValue) -> JsonObject:
    return cast(JsonObject, value)

class FakeMES:
    def __init__(self) -> None: self.calls: list[tuple[str, dict[str, object]]] = []
    async def read(self,url: str,headers: Mapping[str, str],params: Mapping[str, object],timeout: float) -> object:
        self.calls.append((url,dict(params)))
        if url.endswith("/mes/tm/tool/list"):
            return {"code":200,"rows":[ROW],"total":1}
        if url.endswith("/mes/tm/tool/17"):
            return {"code":200,"data":ROW}
        if url.endswith("/mes/tm/tooltype/list"):
            return {"code":200,"rows":[{"toolTypeId":2,"toolTypeCode":"TT2","toolTypeName":"通用类型","codeFlag":"N"}],"total":101}
        if url.endswith("/system/dict/data/type/sys_yes_no"):
            return {"code":200,"data":[{"dictValue":"Y","dictLabel":"是"},{"dictValue":"N","dictLabel":"否"}]}
        if url.endswith("/system/dict/data/type/mes_mainten_type"):
            return {"code":200,"data":[{"dictValue":"REGULAR","dictLabel":"定期维护"},{"dictValue":"USAGE","dictLabel":"按使用次数维护"}]}
        if url.endswith("/system/dict/data/type/mes_tool_status"):
            return {"code":200,"data":[{"dictValue":"STORE","dictLabel":"在库"}]}
        if url.endswith("/mes/tm/tool/99"):
            return {"code":200,"data":None}
        raise AssertionError(url)
    def client(self) -> KTGClient: return KTGClient(base_url="http://mes",authorization="Bearer test",transport=self.read)

def test_list_filters_and_bounds_are_forwarded() -> None:
    mes=FakeMES(); result=asyncio.run(read_tool_list(mes.client(),{"tool_code":"LUNA-17","tool_name":"夹具","tool_type_id":2,"page_size":10}))
    rows = cast(list[JsonObject], result["rows"])
    assert result["total"]==1 and rows[0]["toolTypeId"]==2
    assert mes.calls[0][1]=={"toolCode":"LUNA-17","toolName":"夹具","toolTypeId":2,"pageNum":1,"pageSize":10}
    with pytest.raises(KTGError): asyncio.run(read_tool_list(mes.client(),{"tool_id":1}))
    with pytest.raises(KTGError): asyncio.run(read_tool_list(mes.client(),{"status":"INVALID"}))
    with pytest.raises(KTGError): asyncio.run(read_tool_list(mes.client(),{"mainten_type":"UNKNOWN"}))

def test_detail_by_id_and_precise_code_and_not_found() -> None:
    mes=FakeMES(); assert object_value(asyncio.run(read_tool_detail(mes.client(),{"tool_id":17}))["tool"])["toolCode"]=="LUNA-17"
    assert object_value(asyncio.run(read_tool_detail(mes.client(),{"tool_code":"LUNA-17"}))["tool"])["toolId"]==17
    with pytest.raises(KTGError): asyncio.run(read_tool_detail(mes.client(),{"tool_id":99}))


def run_detail(mes: FakeMES, arguments: JsonObject) -> ToolResult:
    runner = ToolRunner(ToolRegistry(build_tm_tool_tools()))
    call = ToolCall("detail", DETAIL_NAME, arguments)
    context = ToolContext("run", call.id, ktg_client=mes.client())
    return asyncio.run(runner.run(call, context))


@pytest.mark.parametrize(("arguments", "expected_calls"), [
    ({"tool_id": 17}, [("http://mes/mes/tm/tool/17", {})]),
    ({"tool_code": "LUNA-17"}, [
        ("http://mes/mes/tm/tool/list", {"toolCode": "LUNA-17", "pageNum": 1, "pageSize": 100}),
        ("http://mes/mes/tm/tool/17", {}),
    ]),
], ids=["id", "code"])
def test_detail_runner_accepts_one_locator(
    arguments: JsonObject, expected_calls: list[tuple[str, dict[str, object]]],
) -> None:
    mes = FakeMES()
    result = run_detail(mes, arguments)
    assert result.status is ToolStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["tool"] == ROW
    assert mes.calls == expected_calls


@pytest.mark.parametrize("arguments", [
    {},
    {"tool_id": 17, "tool_code": "LUNA-17"},
    {"tool_id": "17"},
    {"tool_code": 17},
    {"toolId": 17},
    {"tool_id": 17, "toolCode": "LUNA-17"},
    {"tool_code": ""},
    {"tool_code": " \t"},
    {"tool_id": 9223372036854775808},
], ids=[
    "missing", "both", "id-type", "code-type", "misspelled", "extra", "empty-code",
    "blank-code", "out-of-range-id",
])
def test_detail_runner_rejects_invalid_arguments_before_execution(arguments: JsonObject) -> None:
    mes = FakeMES()
    result = run_detail(mes, arguments)
    assert result.error_code == "VALIDATION_ERROR"
    assert result.details is not None
    assert "path" in result.details
    assert mes.calls == []

def test_options_are_bounded_and_report_truncation() -> None:
    result=asyncio.run(read_tool_options(FakeMES().client()))
    options = object_value(result["options"])
    types = cast(list[JsonObject], options["tool_types"])
    assert options["tool_types_total"]==101
    assert options["tool_types_truncated"] is True
    assert types[0]["codeFlag"]=="N"


def test_list_and_options_input_declarations() -> None:
    registry = ToolRegistry(build_tm_tool_tools())
    listing = Draft202012Validator(registry.get(LIST_NAME).input_schema)
    assert listing.is_valid({})
    assert listing.is_valid({"tool_code": "LUNA-17", "tool_type_id": 2, "status": "REPARE",
                             "mainten_type": "USAGE", "page": 2, "page_size": 100})
    for invalid in ({"page": 1000001}, {"tool_type_id": 9223372036854775808},
                    {"tool_name": " "}, {"status": "REPAIR"}, {"tool_id": 17}):
        assert not listing.is_valid(invalid)
    options = Draft202012Validator(registry.get(OPTIONS_NAME).input_schema)
    assert options.is_valid({})
    assert not options.is_valid({"tool_type_id": 2})


def test_bad_ids_and_numeric_response_are_rejected() -> None:
    with pytest.raises(KTGError): project_tool({**ROW,"toolTypeId":"2"})
    with pytest.raises(KTGError): project_tool({**ROW,"quantity":True})
