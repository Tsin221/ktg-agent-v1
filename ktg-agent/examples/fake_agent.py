"""Run a deterministic model -> tool -> model loop without network access."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ktg_agent.core.agent import Agent
from ktg_agent.core.loop import AgentLoop
from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall
from ktg_agent.providers.base import (
    FinalResponse,
    ModelConfig,
    ResponseCompleted,
    TextDelta,
    Usage,
)
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.tools import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
    ToolRunner,
)


async def query_stock(
    context: ToolContext, arguments: Mapping[str, Any]
) -> ToolResult:
    warehouse = arguments["warehouse"]
    assert isinstance(warehouse, str)
    return ToolResult(
        content=f"warehouse={warehouse}, available=8",
        details={"warehouse": warehouse, "available": 8},
    )


async def main() -> None:
    call = ToolCall("call-1", "demo.query_stock", {"warehouse": "WH-A"})
    provider = FakeProvider(
        (
            ScriptedTurn(
                (
                    ResponseCompleted(
                        FinalResponse(
                            message=AssistantMessage((call,)),
                            stop_reason=StopReason.TOOL_CALL,
                            usage=Usage(input_tokens=8, output_tokens=4),
                        )
                    ),
                )
            ),
            ScriptedTurn(
                (
                    TextDelta("仓库 WH-A 的可用库存为 8。"),
                    ResponseCompleted(
                        FinalResponse(
                            message=AssistantMessage(
                                (TextBlock("仓库 WH-A 的可用库存为 8。"),)
                            ),
                            stop_reason=StopReason.COMPLETED,
                            usage=Usage(input_tokens=18, output_tokens=9),
                        )
                    ),
                )
            ),
        )
    )
    tool = ToolDefinition(
        name="demo.query_stock",
        version="1.0",
        description="Query deterministic demo stock",
        label="Demo stock query",
        input_schema={
            "type": "object",
            "properties": {"warehouse": {"type": "string"}},
            "required": ["warehouse"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "warehouse": {"type": "string"},
                "available": {"type": "integer"},
            },
            "required": ["warehouse", "available"],
        },
        required_permissions=(),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=1,
        execute=query_stock,
    )
    registry = ToolRegistry((tool,))
    loop = AgentLoop(
        provider=provider,
        tool_registry=registry,
        tool_runner=ToolRunner(registry),
        model_config=ModelConfig(model="fake-model", max_output_tokens=128),
    )
    result = await Agent(loop).prompt("查询 WH-A 库存", run_id="offline-demo")

    final_message = result.final_message
    assert final_message is not None
    final_text = next(
        block.text for block in final_message.content if isinstance(block, TextBlock)
    )
    print(f"final: {final_text}")
    print(f"provider_requests: {len(provider.requests)}")
    print("tool_calls: 1")
    print("events: " + " -> ".join(event.type.value for event in result.events))


if __name__ == "__main__":
    asyncio.run(main())
