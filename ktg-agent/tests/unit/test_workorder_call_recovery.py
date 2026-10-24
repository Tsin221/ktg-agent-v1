"""Regression for the 2026-10-23 draft preview validation loop."""

import asyncio
from collections.abc import Mapping

from ktg_agent.core.loop import AgentLoop, RunLimits
from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall, ToolResultMessage, UserMessage
from ktg_agent.ktg.workorders import PreviewStore, build_workorder_tools
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.tools import ToolContext, ToolRegistry, ToolRunner


NAME = "ktg_preview_create_workorder_draft"


def test_invalid_preview_feedback_survives_model_projection() -> None:
    from ktg_agent.core.messages import project_model_messages

    registry = ToolRegistry(build_workorder_tools(PreviewStore()))
    call = ToolCall("invalid", NAME, {
        "item_code": "IF2026102302", "quantity": 200, "plan_date": "2026-10-23",
    })
    result = asyncio.run(ToolRunner(registry).run(call, ToolContext(
        run_id="recovery", tool_call_id=call.id,
    )))
    projected = project_model_messages((AssistantMessage((call,)), result.to_message(call.id)))
    feedback = projected[-1]
    assert isinstance(feedback, ToolResultMessage)
    assert feedback.details is None
    text = " ".join(block.text for block in feedback.content)
    assert "code_mode" in text
    assert "plan_date" in text
    assert "item_code" in text
    assert "product_code" in text
    assert "VALIDATION_ERROR" in text


def test_limit_summary_reports_preview_failure_instead_of_search_advice() -> None:
    registry = ToolRegistry(build_workorder_tools(PreviewStore()))
    call = ToolCall("invalid", NAME, {"quantity": 200, "plan_date": "2026-10-23"})
    provider = FakeProvider((ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((call,)), StopReason.TOOL_CALL,
    )),)),))
    loop = AgentLoop(provider=provider, tool_registry=registry, tool_runner=ToolRunner(registry),
                     model_config=ModelConfig(model="fake", max_output_tokens=128),
                     limits=RunLimits(max_model_turns=1))
    result = asyncio.run(loop.run(run_id="recovery", messages=(UserMessage((TextBlock("创建草稿"),)),)))
    assert result.stop_reason is StopReason.LIMIT
    assert result.limit_reason == "max_model_turns"
    assert len(provider.requests) == 1
    assert result.final_message is not None
    text = " ".join(block.text for block in result.final_message.content if isinstance(block, TextBlock))
    assert "code_mode" in text
    assert "未通过参数校验" in text
    assert "缩小查询范围" not in text
    assert "未执行" in text


def test_corrected_preview_completes_within_original_budget_without_business_write() -> None:
    reads: list[str] = []

    async def transport(url: str, _headers: Mapping[str, str],
                        _params: Mapping[str, object], _timeout: float) -> object:
        reads.append(url)
        if url.endswith("/mes/md/mditem/list"):
            return {"code": 200, "total": 1, "rows": [{
                "itemId": 5, "itemCode": "IF2026102302", "itemName": "固原岩",
                "unitOfMeasure": "B1", "enableFlag": "Y", "itemOrProduct": "ITEM",
            }]}
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-TEST"
        raise AssertionError(url)

    invalid = ToolCall("invalid", NAME, {
        "quantity": 200, "item_code": "IF2026102302", "plan_date": "2026-10-23",
    })
    corrected = ToolCall("corrected", NAME, {
        "code_mode": "auto", "workorder_name": "岩石批次生产工单",
        "order_source": "STORE", "quantity": 200,
        "product_code": "IF2026102302", "request_date": "2026-10-23",
    })
    provider = FakeProvider(tuple(ScriptedTurn((ResponseCompleted(FinalResponse(
        AssistantMessage((block,)), reason,
    )),)) for block, reason in (
        (invalid, StopReason.TOOL_CALL), (corrected, StopReason.TOOL_CALL),
        (TextBlock("已生成草稿预览，请在网页确认。"), StopReason.COMPLETED),
    )))
    registry = ToolRegistry(build_workorder_tools(PreviewStore()))
    loop = AgentLoop(provider=provider, tool_registry=registry, tool_runner=ToolRunner(registry),
                     model_config=ModelConfig(model="fake", max_output_tokens=1024),
                     limits=RunLimits(max_model_turns=8, max_tool_calls=12))
    result = asyncio.run(loop.run(
        run_id="recovery", messages=(UserMessage((TextBlock(
            "添加生产工单，产品是固原岩，工单名称岩石批次生产工单，库存备货，数量200，需求日期2026-10-23，自动生成编号。"
        ),)),),
        identity={"userId": 18},
        ktg_client=KTGClient(base_url="http://mock.invalid", authorization="Bearer test", transport=transport),
    ))
    assert result.stop_reason is StopReason.COMPLETED
    feedback = provider.requests[1].messages[-1]
    assert isinstance(feedback, ToolResultMessage)
    assert "request_date" in " ".join(block.text for block in feedback.content)
    previews = [message.details for message in result.new_messages
                if isinstance(message, ToolResultMessage) and not message.is_error]
    assert previews[0] is not None
    assert previews[0]["kind"] == "workorder_draft_preview"
    assert previews[0]["quantity"] == "200"
    assert len(reads) == 2
    assert len(provider.requests) == 3
