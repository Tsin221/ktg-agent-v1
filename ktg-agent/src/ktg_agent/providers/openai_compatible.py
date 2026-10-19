"""OpenAI-compatible chat completions provider adapter."""

from __future__ import annotations

import asyncio
import codecs
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import NoReturn, cast

import httpx

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
    RequestStarted,
    ResponseCompleted,
    TextDelta,
    Usage,
    UsageReported,
)
from ktg_agent.core.messages import StopReason


type StreamTransport = Callable[
    [str, Mapping[str, str], dict[str, object], float],
    AbstractAsyncContextManager[AsyncIterator[bytes]],
]


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


class _SseFrames:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._line = ""
        self._data: list[str] = []
        self._after_cr = False

    def feed(self, chunk: bytes) -> list[str]:
        frames: list[str] = []
        try:
            decoded = self._decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model stream contains invalid UTF-8.") from exc
        for char in decoded:
            if self._after_cr:
                self._after_cr = False
                if char == "\n":
                    continue
            if char in "\r\n":
                line, self._line = self._line, ""
                if not line:
                    if self._data:
                        frames.append("\n".join(self._data))
                        self._data = []
                elif line.startswith("data:"):
                    value = line[5:]
                    self._data.append(value[1:] if value.startswith(" ") else value)
                elif line == "data":
                    self._data.append("")
                if char == "\r":
                    self._after_cr = True
            else:
                self._line += char
        return frames


@asynccontextmanager
async def _http_stream(
    url: str, headers: Mapping[str, str], body: dict[str, object], timeout_seconds: float
) -> AsyncIterator[AsyncIterator[bytes]]:
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        "MODEL_HTTP_ERROR",
                        f"Model request failed with HTTP {response.status_code}.",
                        retryable=response.status_code in {408, 409, 429} or response.status_code >= 500,
                    )
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                    raise ProviderError("MODEL_STREAM_UNSUPPORTED", "Model endpoint did not return an SSE stream.")
                yield response.aiter_bytes()
    except httpx.TimeoutException as exc:
        raise ProviderError("MODEL_TIMEOUT", "Model stream timed out.", retryable=True) from exc
    except httpx.RequestError as exc:
        raise ProviderError("MODEL_CONNECTION_ERROR", "Could not reach the configured model endpoint.", retryable=True) from exc


class OpenAICompatibleProvider:
    """Call a fixed OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 60.0,
        transport: StreamTransport | None = None,
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
        self._transport = transport or _http_stream

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
        yield RequestStarted()
        frames = _SseFrames()
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: Usage | None = None
        done = False
        run_timeout = request.remaining_run_seconds
        timeout_seconds = min(self._timeout_seconds, run_timeout) if run_timeout is not None else self._timeout_seconds
        run_deadline_first = run_timeout is not None and run_timeout <= self._timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._transport(
                    self._url,
                    {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    payload,
                    timeout_seconds,
                ) as chunks:
                    iterator = aiter(chunks)
                    while not done:
                        chunk = await self._next_chunk(iterator, request)
                        if chunk is None:
                            break
                        for data in frames.feed(chunk):
                            if data == "[DONE]":
                                done = True
                                break
                            try:
                                frame = json.loads(data, parse_constant=_reject_json_constant)
                            except (json.JSONDecodeError, ValueError) as exc:
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model SSE data is invalid JSON.") from exc
                            if not isinstance(frame, dict):
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model SSE data must be an object.")
                            frame_usage = self._parse_usage(frame)
                            if frame_usage is not None:
                                if usage is not None and usage != frame_usage:
                                    raise ProviderError("MODEL_INVALID_USAGE", "Model usage snapshots conflict.")
                                usage = frame_usage
                                yield UsageReported(usage)
                            choices = frame.get("choices")
                            if not isinstance(choices, list) or len(choices) > 1:
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model stream must contain at most one choice.")
                            if not choices:
                                if frame_usage is None:
                                    raise ProviderError("MODEL_INVALID_RESPONSE", "Empty model choices require usage.")
                                continue
                            if finish_reason is not None:
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model emitted a choice after finish reason.")
                            choice = choices[0]
                            if not isinstance(choice, dict) or choice.get("index") != 0:
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model choice index is invalid.")
                            reason = choice.get("finish_reason")
                            if reason is not None:
                                if finish_reason is not None:
                                    raise ProviderError("MODEL_INVALID_RESPONSE", "Model choice finished twice.")
                                if reason not in ("stop", "tool_calls", "length"):
                                    raise ProviderError("MODEL_STOP_REASON", f"Unsupported model finish reason: {reason!r}.")
                                finish_reason = reason
                            delta = choice.get("delta")
                            if not isinstance(delta, dict):
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model choice delta is invalid.")
                            content = delta.get("content")
                            if content is not None:
                                if not isinstance(content, str):
                                    raise ProviderError("MODEL_INVALID_RESPONSE", "Model text delta is invalid.")
                                if content:
                                    text_parts.append(content)
                                    yield TextDelta(content)
                            raw_calls = delta.get("tool_calls", [])
                            if not isinstance(raw_calls, list):
                                raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool delta is invalid.")
                            for raw_call in raw_calls:
                                self._append_tool_delta(calls, raw_call)
                    if request.cancellation is not None and request.cancellation.is_cancelled:
                        yield ResponseCompleted(FinalResponse(None, StopReason.CANCELLED, usage))
                        return
        except TimeoutError as exc:
            if request.cancellation is not None and request.cancellation.is_cancelled:
                yield ResponseCompleted(FinalResponse(None, StopReason.CANCELLED, usage))
                return
            if run_deadline_first:
                yield ResponseCompleted(FinalResponse(None, StopReason.LIMIT, usage))
                return
            raise ProviderError("MODEL_TIMEOUT", "Model request timed out.", retryable=True) from exc
        if not done or finish_reason is None:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model stream ended without normal completion.")
        blocks: list[TextBlock | ToolCall] = []
        if text_parts:
            blocks.append(TextBlock("".join(text_parts)))
        if finish_reason == "tool_calls":
            for index in sorted(calls):
                parts = calls[index]
                blocks.append(self._parse_tool_call({
                    "id": parts["id"],
                    "function": {"name": parts["name"], "arguments": parts["arguments"]},
                }))
        elif calls and finish_reason != "length":
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model returned tool deltas without tool_calls finish reason.")
        if not blocks:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model returned no content.")
        reason = {"stop": StopReason.COMPLETED, "tool_calls": StopReason.TOOL_CALL, "length": StopReason.LENGTH}[finish_reason]
        yield ResponseCompleted(FinalResponse(AssistantMessage(tuple(blocks)), reason, usage))

    @staticmethod
    async def _next_chunk(iterator: AsyncIterator[bytes], request: ProviderRequest) -> bytes | None:
        task = asyncio.create_task(anext(iterator, None))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.1)
                if request.cancellation is not None and request.cancellation.is_cancelled:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return None
                if done:
                    return task.result()
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _append_tool_delta(calls: dict[int, dict[str, str]], raw: object) -> None:
        if not isinstance(raw, dict):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool delta is invalid.")
        index = raw.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool index is invalid.")
        parts = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if "type" in raw and raw["type"] != "function":
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool type is invalid.")
        call_id = raw.get("id")
        if call_id is not None:
            if not isinstance(call_id, str):
                raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool ID is invalid.")
            parts["id"] += call_id
        function = raw.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool function is invalid.")
            for field in ("name", "arguments"):
                value = function.get(field)
                if value is not None:
                    if not isinstance(value, str):
                        raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool function is invalid.")
                    parts[field] += value

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
            "stream": True,
            "stream_options": {"include_usage": True},
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
                    "arguments": json.dumps(
                        block.arguments,
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
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
    def _parse_tool_call(payload: object) -> ToolCall:
        if not isinstance(payload, Mapping):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool call is invalid.")
        call_id = payload.get("id")
        function = payload.get("function")
        if not isinstance(call_id, str) or not call_id or not isinstance(function, Mapping):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool call is incomplete.")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, str):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool call function is invalid.")
        try:
            parsed = json.loads(arguments, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool arguments are invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("MODEL_INVALID_RESPONSE", "Model tool arguments must be an object.")
        return ToolCall(call_id, name, cast(JsonObject, parsed))

    @staticmethod
    def _parse_usage(payload: Mapping[str, object]) -> Usage | None:
        if "usage" not in payload or payload["usage"] is None:
            return None
        raw = payload["usage"]
        if not isinstance(raw, Mapping):
            raise ProviderError("MODEL_INVALID_USAGE", "Model usage must be an object.")
        input_tokens = raw.get("prompt_tokens")
        output_tokens = raw.get("completion_tokens")
        details = raw.get("prompt_tokens_details")
        cached_tokens: object = 0
        if details is not None:
            if not isinstance(details, Mapping):
                raise ProviderError("MODEL_INVALID_USAGE", "Model prompt token details are invalid.")
            cached_tokens = details.get("cached_tokens", 0)
        if (
            not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0
            or not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0
            or not isinstance(cached_tokens, int) or isinstance(cached_tokens, bool) or cached_tokens < 0
        ):
            raise ProviderError("MODEL_INVALID_USAGE", "Model token counts are invalid.")
        if cached_tokens > input_tokens:
            raise ProviderError("MODEL_INVALID_USAGE", "Cached tokens exceed input tokens.")
        return Usage(input_tokens, output_tokens, cached_tokens)

