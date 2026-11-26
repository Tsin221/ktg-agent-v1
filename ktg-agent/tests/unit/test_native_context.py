from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    CompactionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ktg_agent.context.native_context import (
    build_context_capabilities,
    deserialize_messages,
    serialize_messages,
)


def _historical_messages() -> list[ModelMessage]:
    return [
        ModelRequest([UserPromptPart('只读取工单，不执行写入')]),
        ModelResponse([ToolCallPart('query', {'id': 'WO-1'}, 'call-old')]),
        ModelRequest([ToolReturnPart('query', {'evidence': 'original' * 500}, 'call-old')]),
        ModelResponse([TextPart('已读取历史工单。' * 300)]),
        ModelRequest([UserPromptPart('继续查看 WO-2')]),
        ModelResponse([ToolCallPart('query', {'id': 'WO-2'}, 'call-recent')]),
        ModelRequest([ToolReturnPart('query', {'evidence': 'recent'}, 'call-recent')]),
        ModelResponse([TextPart('近期证据')]),
    ]


def _parts(messages: list[ModelMessage]) -> list[Any]:
    return [p for m in messages for p in m.parts]


def _answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse([TextPart('已完成当前分析')])


def test_native_messages_round_trip_preserves_tool_evidence() -> None:
    history = _historical_messages()
    restored = deserialize_messages(serialize_messages(history))
    assert restored == history
    assert isinstance(restored[1].parts[0], ToolCallPart)
    assert isinstance(restored[2].parts[0], ToolReturnPart)
    assert restored[2].parts[0].tool_call_id == 'call-old'


def test_native_summary_archives_original_and_preserves_recent_pair(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive_files: list[Path] = []

        async def archive(ctx: RunContext[Any], raw: bytes) -> None:
            file = tmp_path / f'{ctx.run_id}-{len(archive_files)}.json'
            file.write_bytes(raw)
            archive_files.append(file)

        def summarize(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            assert archive_files, 'raw archive must complete before the summary request'
            return ModelResponse([TextPart('## Intent\n只读取工单。\n## Current state\nWO-1 已读取。')])

        agent = Agent(FunctionModel(_answer), capabilities=build_context_capabilities(
            archive, context_window=400, keep_tokens=60, summary_model=FunctionModel(summarize),
        ))
        result = await agent.run('请查看当前进度', message_history=_historical_messages())
        archived = deserialize_messages(archive_files[0].read_bytes())
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-old' for p in _parts(archived))
        assert any(isinstance(p, UserPromptPart) and p.content == '只读取工单，不执行写入' for p in _parts(archived))
        parts = _parts(result.all_messages())
        assert len([p for p in parts if isinstance(p, CompactionPart)]) == 1
        assert any(isinstance(p, UserPromptPart) and p.content == '只读取工单，不执行写入' for p in parts)
        assert any(isinstance(p, UserPromptPart) and p.content == '请查看当前进度' for p in parts)
        assert any(isinstance(p, ToolCallPart) and p.tool_call_id == 'call-recent' for p in parts)
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-recent' for p in parts)
        assert not any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-old' for p in parts)
        assert result.usage.requests == 2  # parent model + framework's summary model

        restored = deserialize_messages(serialize_messages(result.all_messages()))
        continued = await agent.run('继续只读查询', message_history=restored)
        continued_parts = _parts(continued.all_messages())
        assert len([p for p in continued_parts if isinstance(p, CompactionPart)]) == 1
        assert any(isinstance(p, UserPromptPart) and p.content == '继续只读查询' for p in continued_parts)
        assert deserialize_messages(archive_files[0].read_bytes()) == archived

    asyncio.run(scenario())


def test_native_fallback_trims_without_fabricating_summary() -> None:
    async def scenario() -> None:
        archived: list[bytes] = []

        async def archive(ctx: RunContext[Any], raw: bytes) -> None:
            archived.append(raw)

        def fail_summary(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise ModelHTTPError(503, 'summary-test')

        agent = Agent(FunctionModel(_answer), capabilities=build_context_capabilities(
            archive, context_window=400, keep_tokens=60, summary_model=FunctionModel(fail_summary),
        ))
        result = await agent.run('继续', message_history=_historical_messages())
        assert result.output == '已完成当前分析'
        assert not any(isinstance(p, CompactionPart) for p in _parts(result.all_messages()))
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-old' for p in _parts(deserialize_messages(archived[0])))
        assert any(isinstance(p, UserPromptPart) and p.content == '只读取工单，不执行写入' for p in _parts(result.all_messages()))
        assert any(isinstance(p, ToolCallPart) and p.tool_call_id == 'call-recent' for p in _parts(result.all_messages()))
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-recent' for p in _parts(result.all_messages()))

    asyncio.run(scenario())


def test_repeated_compaction_updates_existing_summary_instead_of_duplicating_it() -> None:
    async def scenario() -> None:
        summaries: list[str] = []
        archived: list[bytes] = []

        async def archive(ctx: RunContext[Any], raw: bytes) -> None:
            archived.append(raw)

        def summarize(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            summary = f'## Intent\n只读查询；累计处理阶段 {len(summaries) + 1}。'
            summaries.append(summary)
            return ModelResponse([TextPart(summary)])

        def long_answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            # FunctionModel estimates provider usage by whitespace, unlike
            # the harness's unmeasured-text estimator.
            return ModelResponse([TextPart('recent analysis result ' * 500)])

        agent = Agent(FunctionModel(long_answer), capabilities=build_context_capabilities(
            archive, context_window=400, keep_tokens=60, summary_model=FunctionModel(summarize),
        ))
        first = await agent.run('第一阶段', message_history=_historical_messages())
        second = await agent.run('第二阶段', message_history=deserialize_messages(serialize_messages(first.all_messages())))
        # The native strategy deliberately doesn't resummarize an already
        # compressed prefix while the oversized latest answer is still kept.
        third = await agent.run('第三阶段', message_history=deserialize_messages(serialize_messages(second.all_messages())))
        assert len(summaries) >= 2
        summary_parts = [p for p in _parts(third.all_messages()) if isinstance(p, CompactionPart)]
        assert len(summary_parts) == 1
        assert summary_parts[0].content == summaries[-1]
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-old' for p in _parts(deserialize_messages(archived[0])))

    asyncio.run(scenario())


def test_archive_failure_prevents_summary_and_model_request() -> None:
    async def scenario() -> None:
        async def fail_archive(ctx: RunContext[Any], raw: bytes) -> None:
            raise OSError('archive unavailable')

        def unexpected_request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            pytest.fail('neither summary nor main model may run before archival succeeds')

        agent = Agent(FunctionModel(unexpected_request), capabilities=build_context_capabilities(
            fail_archive, context_window=400, keep_tokens=60, summary_model=FunctionModel(unexpected_request),
        ))
        with pytest.raises(OSError, match='archive unavailable'):
            await agent.run('继续', message_history=_historical_messages())

    asyncio.run(scenario())


def test_interrupted_summary_leaves_raw_archive_intact() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        archived: list[bytes] = []

        async def archive(ctx: RunContext[Any], raw: bytes) -> None:
            archived.append(raw)

        async def blocked_summary(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError('unreachable')

        agent = Agent(FunctionModel(_answer), capabilities=build_context_capabilities(
            archive, context_window=400, keep_tokens=60, summary_model=FunctionModel(blocked_summary),
        ))
        task = asyncio.create_task(agent.run('继续', message_history=_historical_messages()))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'call-old' for p in _parts(deserialize_messages(archived[0])))

    asyncio.run(scenario())


def test_concurrent_runs_archive_their_own_native_history() -> None:
    async def scenario() -> None:
        archives: dict[str, bytes] = {}

        async def archive(ctx: RunContext[str], raw: bytes) -> None:
            await asyncio.sleep(0)
            archives[ctx.deps] = raw

        agent = Agent(FunctionModel(_answer), deps_type=str, capabilities=build_context_capabilities(archive))
        await asyncio.gather(agent.run('会话甲', deps='A'), agent.run('会话乙', deps='B'))
        for key, prompt in [('A', '会话甲'), ('B', '会话乙')]:
            parts = _parts(deserialize_messages(archives[key]))
            assert [p.content for p in parts if isinstance(p, UserPromptPart)] == [prompt]

    asyncio.run(scenario())


def test_real_native_tool_loop_archives_result_and_next_run_reuses_native_history() -> None:
    async def scenario() -> None:
        snapshots: list[tuple[str | None, bytes]] = []

        async def archive(ctx: RunContext[Any], raw: bytes) -> None:
            snapshots.append((ctx.run_id, raw))

        def choose_tool(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'actual-tool-call' for p in _parts(messages)):
                return ModelResponse([TextPart('已读取工单')])
            return ModelResponse([ToolCallPart('query', {'workorder_id': 'WO-X'}, 'actual-tool-call')])

        def query(workorder_id: str) -> dict[str, str]:
            return {'workorder_id': workorder_id, 'status': 'released'}

        agent = Agent(FunctionModel(choose_tool), tools=[query], capabilities=build_context_capabilities(archive))
        first = await agent.run('查询工单 WO-X')
        assert first.output == '已读取工单'
        assert len(snapshots) == 2
        assert snapshots[0][0] == snapshots[1][0]
        second_step = _parts(deserialize_messages(snapshots[1][1]))
        tool_result = next(p for p in second_step if isinstance(p, ToolReturnPart))
        assert tool_result.tool_call_id == 'actual-tool-call'
        assert tool_result.content == {'workorder_id': 'WO-X', 'status': 'released'}
        second = await agent.run('继续分析', message_history=deserialize_messages(serialize_messages(first.all_messages())))
        assert second.output == '已读取工单'
        assert snapshots[2][0] != snapshots[1][0]
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'actual-tool-call' for p in _parts(deserialize_messages(snapshots[2][1])))

    asyncio.run(scenario())
