import asyncio
import json
from collections.abc import Mapping

import pytest

from ktg_agent.core.messages import SystemMessage, TextBlock, ToolCall, UserMessage
from ktg_agent.providers.base import ModelConfig, ProviderRequest, ToolSchema
from ktg_agent.providers.base import ProviderError
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider


def test_provider_rejects_tool_names_outside_openai_pattern() -> None:
    async def scenario() -> None:
        async def transport(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout_seconds: float,
        ) -> dict[str, object]:
            raise AssertionError("invalid tool names must fail before transport")

        provider = OpenAICompatibleProvider(
            api_key="test-key",
            base_url="https://models.example/v1",
            transport=transport,
        )
        request = ProviderRequest(
            messages=(UserMessage((TextBlock("查询"),)),),
            tools=(
                ToolSchema(
                    name="ktg.query_stock",
                    description="Query stock",
                    input_schema={"type": "object", "properties": {}},
                ),
            ),
            config=ModelConfig(model="test-model", max_output_tokens=32),
        )

        with pytest.raises(ProviderError, match="tool name"):
            _ = [event async for event in provider.stream(request)]

    asyncio.run(scenario())


def test_provider_converts_tool_call_response() -> None:
    async def scenario() -> None:
        await _provider_converts_tool_call_response()

    asyncio.run(scenario())


async def _provider_converts_tool_call_response() -> None:
    captured: dict[str, object] = {}

    async def transport(
        url: str,
        headers: Mapping[str, str],
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        captured.update(url=url, headers=dict(headers), body=body, timeout=timeout_seconds)
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "ktg_query_stock",
                                    "arguments": json.dumps({"item_code": "A-01"}),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }

    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://models.example/v1",
        transport=transport,
    )
    request = ProviderRequest(
        messages=(
            SystemMessage((TextBlock("Use tools."),)),
            UserMessage((TextBlock("查 A-01 库存"),)),
        ),
        tools=(
            ToolSchema(
                name="ktg_query_stock",
                description="Query stock",
                input_schema={
                    "type": "object",
                    "properties": {"item_code": {"type": "string"}},
                    "required": ["item_code"],
                },
            ),
        ),
        config=ModelConfig(model="test-model", max_output_tokens=256),
    )

    events = [event async for event in provider.stream(request)]

    completed = events[-1].response  # type: ignore[union-attr]
    assert completed.stop_reason.value == "tool_call"
    assert completed.message is not None
    assert completed.message.content == (
        ToolCall(id="call-1", name="ktg_query_stock", arguments={"item_code": "A-01"}),
    )
    assert captured["url"] == "https://models.example/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert captured["body"] == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Use tools."},
            {"role": "user", "content": "查 A-01 库存"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "ktg_query_stock",
                    "description": "Query stock",
                    "parameters": {
                        "type": "object",
                        "properties": {"item_code": {"type": "string"}},
                        "required": ["item_code"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": 256,
    }


def test_provider_emits_text_and_completed_response() -> None:
    asyncio.run(_provider_emits_text_and_completed_response())


async def _provider_emits_text_and_completed_response() -> None:
    async def transport(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "查询完成。"}}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://models.example/v1",
        transport=transport,
    )
    request = ProviderRequest(
        messages=(UserMessage((TextBlock("查询"),)),),
        tools=(),
        config=ModelConfig(model="test-model", max_output_tokens=32),
    )

    events = [event async for event in provider.stream(request)]

    assert events[0].text == "查询完成。"  # type: ignore[union-attr]
    assert events[-1].response.message is not None  # type: ignore[union-attr]

