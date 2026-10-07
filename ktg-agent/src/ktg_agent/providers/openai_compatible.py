"""OpenAI-compatible chat completions provider adapter."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ktg_agent.providers.base import (
    FinalResponse,
    ModelEvent,
    ProviderError,
    ProviderRequest,
    ResponseCompleted,
    TextDelta,
    Usage,
)
from ktg_agent.core.messages import StopReason


type JsonTransport = Callable[
    [str, Mapping[str, str], dict[str, object], float],
    Awaitable[dict[str, object]],
]


class OpenAICompatibleProvider:
    """Call a fixed OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 60.0,
        transport: JsonTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("MODEL_API_KEY is required")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("MODEL_BASE_URL must be an HTTP(S) URL")
        if timeout_seconds <= 0:
            raise ValueError("model timeout must be positive")
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout_seconds = timeout_seconds
        self._transport = transport or self._default_transport

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        if request.cancellation is not None and request.cancellation.is_cancelled:
            yield ResponseCompleted(
                FinalResponse(
                    message=None,
                    stop_reason=StopReason.CANCELLED,
                    usage=Usage(0, 0),
                )
            )
            return

        payload = self._request_payload(request)
        response = await self._transport(
            self._url,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            payload,
            self._timeout_seconds,
        )
        final = self._parse_response(response)
        if final.message is not None:
            text = "".join(
                block.text
                for block in final.message.content
                if isinstance(block, TextBlock)
            )
            if text:
                yield TextDelta(text)
        yield ResponseCompleted(final)

    @staticmethod
    async def _default_transport(
        url: str,
        headers: Mapping[str, str],
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            OpenAICompatibleProvider._post_json,
            url,
            headers,
            body,
            timeout_seconds,
        )

    @staticmethod
    def _post_json(
        url: str,
        headers: Mapping[str, str],
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            retryable = exc.code in {408, 409, 429} or exc.code >= 500
            raise ProviderError(
                "MODEL_HTTP_ERROR",
                f"Model request failed with HTTP {exc.code}.",
                retryable=retryable,
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise ProviderError(
                "MODEL_CONNECTION_ERROR",
                "Could not reach the configured model endpoint.",
                retryable=True,
            ) from exc
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model response must be an object.")
        return cast(dict[str, object], value)

    @staticmethod
    def _request_payload(request: ProviderRequest) -> dict[str, object]:
        for tool in request.tools:
            if re.fullmatch(r"[A-Za-z0-9_-]+", tool.name) is None:
                raise ProviderError(
                    "MODEL_INVALID_TOOL_NAME",
                    f"OpenAI-compatible tool name is invalid: {tool.name!r}.",
                )
        payload: dict[str, object] = {
            "model": request.config.model,
            "messages": [
                OpenAICompatibleProvider._message_payload(message)
                for message in request.messages
            ],
            "max_tokens": request.config.max_output_tokens,
        }
        if request.config.temperature is not None:
            payload["temperature"] = request.config.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _message_payload(message: Message) -> dict[str, object]:
        text = "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        )
        if isinstance(message, SystemMessage):
            return {"role": "system", "content": text}
        if isinstance(message, UserMessage):
            return {"role": "user", "content": text}
        if isinstance(message, ToolResultMessage):
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": text,
            }
        tool_calls = [
            {
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.arguments, ensure_ascii=False),
                },
            }
            for block in message.content
            if isinstance(block, ToolCall)
        ]
        payload: dict[str, object] = {
            "role": "assistant",
            "content": text or None,
        }
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return payload

    @staticmethod
    def _parse_response(payload: Mapping[str, object]) -> FinalResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model response must contain one choice.")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model choice is invalid.")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model message is missing.")

        blocks: list[TextBlock | ToolCall] = []
        content = message.get("content")
        if isinstance(content, str) and content:
            blocks.append(TextBlock(content))
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool calls are invalid.")
        for raw_call in raw_calls:
            blocks.append(OpenAICompatibleProvider._parse_tool_call(raw_call))

        finish_reason = choice.get("finish_reason")
        stop_reason = {
            "stop": StopReason.COMPLETED,
            "tool_calls": StopReason.TOOL_CALL,
            "length": StopReason.LENGTH,
        }.get(finish_reason if isinstance(finish_reason, str) else "")
        if stop_reason is None:
            raise ProviderError(
                "MODEL_STOP_REASON",
                f"Unsupported model finish reason: {finish_reason!r}.",
            )
        if not blocks:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model returned no content.")

        usage_payload = payload.get("usage", {})
        if not isinstance(usage_payload, Mapping):
            usage_payload = {}
        usage = Usage(
            input_tokens=OpenAICompatibleProvider._token_count(
                usage_payload.get("prompt_tokens")
            ),
            output_tokens=OpenAICompatibleProvider._token_count(
                usage_payload.get("completion_tokens")
            ),
        )
        return FinalResponse(
            message=AssistantMessage(tuple(blocks)),
            stop_reason=stop_reason,
            usage=usage,
        )

    @staticmethod
    def _parse_tool_call(payload: object) -> ToolCall:
        if not isinstance(payload, Mapping):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool call is invalid.")
        call_id = payload.get("id")
        function = payload.get("function")
        if not isinstance(call_id, str) or not isinstance(function, Mapping):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool call is incomplete.")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool call function is invalid.")
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool arguments are invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool arguments must be an object.")
        return ToolCall(call_id, name, cast(JsonObject, parsed))

    @staticmethod
    def _token_count(value: object) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

