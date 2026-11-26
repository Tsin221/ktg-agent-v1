"""Offline integration tests against the real Pydantic AI runner."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pydantic_ai import Agent, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.usage import UsageLimits

from ktg_agent.core.messages import JsonObject
from ktg_agent.core.native_execution import NativeExecution
from ktg_agent.ktg.client import KTGClient
from ktg_agent.sessions.native_planning import build_planning_capability, read_plan
from ktg_agent.tools.native import MesToolDeps


def deps(run: str = 'run-1') -> MesToolDeps:
    return MesToolDeps(run, 's', 'id:1', {'userId': 1}, KTGClient(
        base_url='https://test.invalid', authorization='Bearer private-offline-token'))


def test_native_execution_delivers_inputs_and_streams_tools() -> None:
    async def case() -> None:
        started = asyncio.Event()
        proceed = asyncio.Event()
        events: list[tuple[str, JsonObject]] = []
        receipts: list[str] = []
        prompts: list[list[str]] = []

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            prompts.append([p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts
                            if isinstance(p, UserPromptPart) and isinstance(p.content, str)])
            if len(prompts) == 1:
                started.set()
                await proceed.wait()
                yield 'checking '
                yield {1: DeltaToolCall(name='lookup', json_args='{}', tool_call_id='lookup-1')}
            else:
                yield 'answer '
                yield str(len(prompts))

        async def lookup() -> ToolReturn:
            return ToolReturn('safe result', metadata={'status': 'failed', 'error_code': 'DENIED',
                                                       'secret': 'private-offline-token'})

        async def delivered(run_id: str, request_id: str, enqueue_id: str) -> None:
            assert run_id == 'run-1' and enqueue_id
            receipts.append(request_id)

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps, tools=[lookup])
        execution = NativeExecution(agent, deps(), 'start', publish=lambda _, kind, payload: events.append((kind, payload)),
                                    delivered=delivered)
        assert not execution.input('early', 'steer', 'not ready')
        task = asyncio.create_task(execution.run())
        await started.wait()
        assert execution.input('first', 'steer', 'steer now')
        assert execution.input('second', 'follow_up', 'then follow')
        assert receipts == []
        proceed.set()
        outcome = await task
        assert outcome.status == 'completed'
        assert receipts == ['first', 'second']
        assert prompts == [['start'], ['start', 'steer now'], ['start', 'steer now', 'then follow']]
        assert any(kind == 'message_update' and p['text'] == 'checking ' for kind, p in events)
        assert any(kind == 'tool_execution_end' and p['status'] == 'failed' for kind, p in events)
        assert 'private-offline-token' not in str(events)
        assert not execution.input('late', 'steer', 'too late')
        assert all(kind != 'run_settled' for kind, _ in events)

    asyncio.run(case())


def test_native_execution_budget_preserves_tool_results() -> None:
    async def case() -> None:
        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[dict[int, DeltaToolCall]]:
            yield {0: DeltaToolCall(name='lookup', json_args='{}', tool_call_id='lookup-1')}

        async def lookup() -> str:
            return 'read complete'

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps, tools=[lookup])
        outcome = await NativeExecution(agent, deps(), 'start', usage_limits=UsageLimits(request_limit=1)).run()
        assert outcome.status == 'failed' and outcome.reason == 'usage_limit'
        assert any(isinstance(p, ToolReturnPart) and p.content == 'read complete'
                   for m in outcome.new_messages for p in m.parts)

    asyncio.run(case())


def test_native_stop_and_timeout_are_not_success() -> None:
    async def case() -> None:
        started = asyncio.Event()

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            started.set()
            await asyncio.Event().wait()
            yield 'unreachable'

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps)
        execution = NativeExecution(agent, deps(), 'stop')
        task = asyncio.create_task(execution.run())
        await started.wait()
        execution.stop()
        outcome = await task
        assert outcome.status == 'cancelled' and outcome.reason == 'cancelled'
        assert any(isinstance(p, UserPromptPart) and p.content == 'stop' for m in outcome.messages for p in m.parts)
        timeout = await NativeExecution(agent, deps('run-2'), 'timeout', timeout=0.02).run()
        assert timeout.status == 'failed' and timeout.reason == 'timeout'
        assert not execution.input('late', 'follow_up', 'unreachable')

    asyncio.run(case())


def test_planning_and_unknown_business_outcome(tmp_path: Path) -> None:
    async def case() -> None:
        database = str(tmp_path / 'plan.sqlite')
        events: list[tuple[str, JsonObject]] = []
        actions = iter(['write_plan', 'lookup', 'finish'])

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            action = next(actions)
            if action == 'write_plan':
                yield {0: DeltaToolCall(name=action, json_args='{"items":[{"id":"step","content":"verify"}]}')}
            elif action == 'lookup':
                yield {0: DeltaToolCall(name=action, json_args='{}')}
            else:
                yield 'all done'

        async def lookup() -> ToolReturn:
            return ToolReturn('receipt unconfirmed', metadata={'status': 'unknown', 'error_code': 'UNKNOWN'})

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps, tools=[lookup])
        outcome = await NativeExecution(agent, deps(), 'start', capabilities=[build_planning_capability(database)],
                                        publish=lambda _, kind, payload: events.append((kind, payload))).run()
        assert outcome.status == 'needs_reconciliation' and 'answer' not in outcome.summary
        assert any(kind == 'plan_changed' and payload['task_id'] == 'run-1' and payload['step_id'] == 'step'
                   for kind, payload in events)
        plan = await read_plan(database, owner='id:1', session_id='s', task_id='run-1')
        assert len(plan) == 1 and plan[0].status.value == 'pending'
        assert any(isinstance(p, ToolReturnPart) and isinstance(p.metadata, dict) and p.metadata.get('status') == 'unknown'
                   for m in outcome.new_messages for p in m.parts)

    asyncio.run(case())


def test_concurrent_runs_and_external_cancel_partial_history() -> None:
    async def case() -> None:
        started = {'one': asyncio.Event(), 'two': asyncio.Event()}
        release = asyncio.Event()

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            prompt = next(p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts
                          if isinstance(p, UserPromptPart) and isinstance(p.content, str))
            started[prompt].set()
            await release.wait()
            yield f'answer for {prompt}'

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps)
        one = NativeExecution(agent, deps('run-one'), 'one')
        two = NativeExecution(agent, deps('run-two'), 'two')
        t1, t2 = asyncio.create_task(one.run()), asyncio.create_task(two.run())
        await asyncio.gather(*(event.wait() for event in started.values()))
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert one.interrupted_outcome is not None
        assert one.interrupted_outcome.status == 'needs_reconciliation'
        assert any(isinstance(p, UserPromptPart) and p.content == 'one'
                   for m in one.interrupted_outcome.new_messages for p in m.parts)
        release.set()
        result = await t2
        assert result.status == 'completed' and result.summary['answer'] == 'answer for two'
        assert 'answer for two' not in str(one.interrupted_outcome.messages)

    asyncio.run(case())
