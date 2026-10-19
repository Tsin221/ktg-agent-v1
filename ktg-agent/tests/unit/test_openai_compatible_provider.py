import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from ktg_agent.core.messages import StopReason, TextBlock, ToolCall, UserMessage
from ktg_agent.core.agent import CancellationToken
from ktg_agent.core.loop import AgentLoop, RunLimits
from ktg_agent.tools import ToolRegistry, ToolRunner
from ktg_agent.providers.base import FinalResponse, ModelConfig, ProviderError, ProviderRequest, RequestStarted, ResponseCompleted, TextDelta, Usage, UsageReported
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider


def request(cancellation: object | None = None) -> ProviderRequest:
    return ProviderRequest(
        messages=(UserMessage((TextBlock("查询"),)),), tools=(),
        config=ModelConfig("test-model", 32), cancellation=cancellation,  # type: ignore[arg-type]
    )


def frame(value: object) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n"


def choice(delta: dict[str, object], finish: str | None = None) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": None}


@asynccontextmanager
async def transport_bytes(chunks: list[bytes]) -> AsyncIterator[AsyncIterator[bytes]]:
    async def produce() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk
    yield produce()


def provider_for(chunks: list[bytes]) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="key", base_url="http://localhost/v1",
        transport=lambda *_args: transport_bytes(chunks),
    )


def test_split_utf8_json_and_usage_tail() -> None:
    async def scenario() -> None:
        first = frame(choice({"content": "中文"}))
        split = first.index("中".encode("utf-8")) + 1
        chunks = [first[:split], first[split:split + 1], first[split + 1:14 + split],
                  first[14 + split:], b"\r\n", frame(choice({}, "stop")),
                  frame({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
                  b"data: [DONE]\n\n"]
        events = [event async for event in provider_for(chunks).stream(request())]
        assert isinstance(events[-1], ResponseCompleted)
        assert events == [RequestStarted(), TextDelta("中文"), UsageReported(Usage(5, 2)),
                          ResponseCompleted(events[-1].response)]
        assert events[-1].response.message is not None
        assert events[-1].response.message.content == (TextBlock("中文"),)
    asyncio.run(scenario())


def test_interleaved_tool_arguments_are_only_released_after_done() -> None:
    async def scenario() -> None:
        chunks = [
            frame(choice({"tool_calls": [
                {"index": 1, "id": "call-b", "type": "function", "function": {"name": "lookup_b", "arguments": "{\"b\":"}},
                {"index": 0, "id": "call-a", "type": "function", "function": {"name": "lookup_a", "arguments": "{\"a\":"}},
            ]})),
            frame(choice({"tool_calls": [
                {"index": 0, "function": {"arguments": "1}"}},
                {"index": 1, "function": {"arguments": "2}"}},
            ]})),
            frame(choice({}, "tool_calls")), b"data: [DONE]\n\n",
        ]
        events = [event async for event in provider_for(chunks).stream(request())]
        assert len(events) == 2
        assert events[-1].response.message.content == (  # type: ignore[union-attr]
            ToolCall("call-a", "lookup_a", {"a": 1}),
            ToolCall("call-b", "lookup_b", {"b": 2}),
        )
    asyncio.run(scenario())


@pytest.mark.parametrize("chunks", [
    [frame(choice({"tool_calls": [{"index": 0, "id": "id", "function": {"name": "x", "arguments": "{\"a\":"}}]})), frame(choice({}, "tool_calls"))],
    [frame(choice({"tool_calls": [{"index": 0, "function": {"name": "x", "arguments": "{}"}}]})), frame(choice({}, "tool_calls")), b"data: [DONE]\n\n"],
    [frame(choice({"tool_calls": [{"index": 0, "id": "id", "function": {"arguments": "{}"}}]})), frame(choice({}, "tool_calls")), b"data: [DONE]\n\n"],
    [frame(choice({"tool_calls": [{"index": 0, "id": "id", "function": {"name": "x", "arguments": "oops"}}]})), frame(choice({}, "tool_calls")), b"data: [DONE]\n\n"],
    [frame(choice({"tool_calls": [{"index": 0, "id": "id", "function": {"name": "x", "arguments": "{\"a\":NaN}"}}]})), frame(choice({}, "tool_calls")), b"data: [DONE]\n\n"],
])
def test_bad_tool_stream_never_completes(chunks: list[bytes]) -> None:
    async def scenario() -> None:
        events = []
        with pytest.raises(ProviderError):
            async for event in provider_for(chunks).stream(request()):
                events.append(event)
        assert events == [RequestStarted()]
    asyncio.run(scenario())


def test_length_discards_partial_tool_call() -> None:
    async def scenario() -> None:
        chunks = [frame(choice({"content": "partial", "tool_calls": [
            {"index": 0, "id": "id", "function": {"name": "x", "arguments": "{"}}]}, "length")), b"data: [DONE]\n\n"]
        events = [event async for event in provider_for(chunks).stream(request())]
        assert events[-1].response.stop_reason is StopReason.LENGTH  # type: ignore[union-attr]
        assert events[-1].response.message.content == (TextBlock("partial"),)  # type: ignore[union-attr]
    asyncio.run(scenario())


def test_known_usage_survives_bad_tail() -> None:
    async def scenario() -> None:
        chunks = [frame({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}), b"data: broken\n\n"]
        events = []
        with pytest.raises(ProviderError):
            async for event in provider_for(chunks).stream(request()):
                events.append(event)
        assert events == [RequestStarted(), UsageReported(Usage(5, 2))]
    asyncio.run(scenario())


def test_invalid_usage_is_rejected() -> None:
    async def scenario() -> None:
        for bad in ({"prompt_tokens": -1, "completion_tokens": 0},
                    {"prompt_tokens": True, "completion_tokens": 0},
                    {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 2}}):
            events = []
            with pytest.raises(ProviderError) as raised:
                async for event in provider_for([frame({"choices": [], "usage": bad})]).stream(request()):
                    events.append(event)
            assert raised.value.code == "MODEL_INVALID_USAGE"
            assert events == [RequestStarted()]
    asyncio.run(scenario())


def test_real_http_delivers_text_before_tail_and_closes_on_early_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    async def scenario() -> None:
        first_sent = asyncio.Event()
        closed = asyncio.Event()
        captured: dict[str, object] = {}

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")))
                body = json.loads(await reader.readexactly(length))
                captured.update(body)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                first = frame(choice({"content": "第一段"}))
                writer.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
                await writer.drain()
                first_sent.set()
                await reader.read()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            provider = OpenAICompatibleProvider(api_key="key", base_url=f"http://127.0.0.1:{port}/v1")
            stream = cast(AsyncGenerator[object, None], provider.stream(request()))
            assert isinstance(await asyncio.wait_for(anext(stream), 2), RequestStarted)
            delta = await asyncio.wait_for(anext(stream), 2)
            assert delta == TextDelta("第一段")
            assert first_sent.is_set() and not closed.is_set()
            assert captured["stream"] is True
            assert captured["stream_options"] == {"include_usage": True}
            await stream.aclose()
            await asyncio.wait_for(closed.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_http_error_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    async def scenario() -> None:
        closed = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            provider = OpenAICompatibleProvider(api_key="key", base_url=f"http://127.0.0.1:{port}/v1")
            with pytest.raises(ProviderError) as raised:
                _ = [event async for event in provider.stream(request())]
            assert raised.value.code == "MODEL_HTTP_ERROR"
            await asyncio.wait_for(closed.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_non_stream_response_fails_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    async def scenario() -> None:
        closed = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            provider = OpenAICompatibleProvider(api_key="key", base_url=f"http://127.0.0.1:{port}/v1")
            with pytest.raises(ProviderError) as raised:
                _ = [event async for event in provider.stream(request())]
            assert raised.value.code == "MODEL_STREAM_UNSUPPORTED"
            await asyncio.wait_for(closed.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_read_timeout_and_cancellation_close_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    class Signal:
        is_cancelled = False

    async def scenario(cancel: bool) -> None:
        connected = asyncio.Event()
        closed = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")))
                await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                connected.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            provider = OpenAICompatibleProvider(api_key="key", base_url=f"http://127.0.0.1:{port}/v1", timeout_seconds=1.0)
            signal = Signal()
            events: list[object] = []

            async def consume() -> None:
                async for event in provider.stream(request(signal)):
                    events.append(event)

            task = asyncio.create_task(consume())
            await asyncio.wait_for(connected.wait(), 2)
            if cancel:
                signal.is_cancelled = True
                await asyncio.wait_for(task, 2)
                assert isinstance(events[-1], ResponseCompleted)
                assert events[-1].response.stop_reason is StopReason.CANCELLED
            else:
                with pytest.raises(ProviderError) as raised:
                    await asyncio.wait_for(task, 2)
                assert raised.value.code == "MODEL_TIMEOUT"
            await asyncio.wait_for(closed.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_absolute_run_deadline_closes_stalled_stream_and_preserves_usage() -> None:
    async def scenario() -> None:
        closed = asyncio.Event()

        @asynccontextmanager
        async def stalled(*_args: object) -> AsyncIterator[AsyncIterator[bytes]]:
            async def chunks() -> AsyncIterator[bytes]:
                yield frame({"choices": [], "usage": {
                    "prompt_tokens": 2, "completion_tokens": 1,
                }})
                await asyncio.Event().wait()
                yield b"unreachable"
            try:
                yield chunks()
            finally:
                closed.set()

        provider = OpenAICompatibleProvider(
            api_key="key", base_url="http://localhost/v1",
            timeout_seconds=2, transport=stalled,
        )
        original = request()
        bounded = ProviderRequest(original.messages, original.tools, original.config,
                                  remaining_run_seconds=0.15)
        events = [event async for event in provider.stream(bounded)]
        assert closed.is_set()
        assert events[-1] == ResponseCompleted(FinalResponse(None, StopReason.LIMIT, Usage(2, 1)))

    asyncio.run(scenario())


def test_cancellation_during_model_connection_closes_transport() -> None:
    async def scenario() -> None:
        entering = asyncio.Event()
        closed = asyncio.Event()

        @asynccontextmanager
        async def blocked(*_args: object) -> AsyncIterator[AsyncIterator[bytes]]:
            entering.set()
            try:
                await asyncio.Event().wait()
                yield cast(AsyncIterator[bytes], None)
            finally:
                closed.set()

        provider = OpenAICompatibleProvider(
            api_key="key", base_url="http://localhost/v1",
            transport=blocked,
        )
        registry = ToolRegistry(())
        loop = AgentLoop(provider=provider, tool_registry=registry,
                         tool_runner=ToolRunner(registry),
                         model_config=ModelConfig("test-model", 32),
                         limits=RunLimits(max_run_seconds=2))
        signal = CancellationToken()
        task = asyncio.create_task(loop.run(
            run_id="run-1", messages=(UserMessage((TextBlock("query"),)),),
            cancellation=signal,
        ))
        await asyncio.wait_for(entering.wait(), 1)
        signal.cancel()
        result = await asyncio.wait_for(task, 1)
        assert result.stop_reason is StopReason.CANCELLED
        assert result.usage.unknown_request_count == 1
        assert closed.is_set()

    asyncio.run(scenario())


def test_continuous_model_deltas_cannot_extend_run_deadline() -> None:
    async def scenario() -> None:
        closed = asyncio.Event()

        @asynccontextmanager
        async def continuous(*_args: object) -> AsyncIterator[AsyncIterator[bytes]]:
            async def chunks() -> AsyncIterator[bytes]:
                while True:
                    await asyncio.sleep(0.01)
                    yield frame(choice({"content": "x"}))
            try:
                yield chunks()
            finally:
                closed.set()

        provider = OpenAICompatibleProvider(
            api_key="key", base_url="http://localhost/v1",
            timeout_seconds=2, transport=continuous,
        )
        original = request()
        bounded = ProviderRequest(original.messages, original.tools, original.config,
                                  remaining_run_seconds=0.15)
        events = [event async for event in provider.stream(bounded)]
        assert any(isinstance(event, TextDelta) for event in events)
        assert isinstance(events[-1], ResponseCompleted)
        assert events[-1].response.stop_reason is StopReason.LIMIT
        assert closed.is_set()

    asyncio.run(scenario())
