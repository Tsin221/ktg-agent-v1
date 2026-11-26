"""Offline probes of the installed, pinned Pydantic AI APIs (no MES or model API)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelAPIError, RunCancelled, UsageLimitExceeded
from pydantic_ai.messages import (
    AgentStreamEvent, EnqueuedMessagesEvent, FunctionToolCallEvent,
    FunctionToolResultEvent, ModelMessage, ModelMessagesTypeAdapter,
    ModelRequest, ModelResponse, PartStartEvent, TextPart, ToolCallPart,
    ToolReturnPart, UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.compaction import (
    FallbackCompaction, SlidingWindowCompaction, SummarizingCompaction,
)
from pydantic_ai_harness.planning import Planning, PlanCreatedEvent, SqlitePlanStore
from pydantic_ai_harness.step_persistence import StepPersistence, SqliteStepStore, continue_run


def prompts(messages: list[ModelMessage]) -> list[str]:
    return [part.content for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, UserPromptPart) and isinstance(part.content, str)]


def test_native_iter_stream_and_enqueue() -> None:
    async def run_case() -> None:
        observed: list[list[str]] = []
        events: list[AgentStreamEvent] = []
        calls: list[int] = []

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            observed.append(prompts(messages))
            if len(observed) == 1:
                yield {0: DeltaToolCall(name='lookup', json_args='{"number":7}', tool_call_id='read-1')}
            else:
                yield 'answer '
                yield str(len(observed))

        async def lookup(number: int) -> int:
            calls.append(number)
            return number * 2

        agent = Agent(FunctionModel(stream_function=model), tools=[lookup])
        async with agent.iter('start') as run:
            asap_id = run.enqueue('steer', priority='asap')
            idle_id = run.enqueue('follow', priority='when_idle')
            async for node in run:
                if Agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as stream:
                        async for event in stream:
                            events.append(event)
                elif Agent.is_call_tools_node(node):
                    async with node.stream(run.ctx) as stream_tools:
                        async for event in stream_tools:
                            events.append(event)
            assert run.result is not None
            assert run.result.output == 'answer 3'
            assert run.usage.requests == 3
            assert run.usage.tool_calls == 1
            roundtrip = ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(run.all_messages()))
            assert prompts(roundtrip) == ['start', 'steer', 'follow']
        assert calls == [7]
        assert observed == [['start', 'steer'], ['start', 'steer'], ['start', 'steer', 'follow']]
        assert [e.enqueue_id for e in events if isinstance(e, EnqueuedMessagesEvent)] == [asap_id, idle_id]
        assert any(isinstance(e, FunctionToolCallEvent) for e in events)
        assert any(isinstance(e, FunctionToolResultEvent) for e in events)
        assert any(isinstance(e, PartStartEvent) and isinstance(e.part, TextPart) for e in events)

    asyncio.run(run_case())


def test_native_cancel_and_request_limit() -> None:
    async def run_case() -> None:
        started = asyncio.Event()
        never = asyncio.Event()

        async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            started.set()
            await never.wait()
            return ModelResponse(parts=[TextPart('not reached')])

        agent = Agent(FunctionModel(model))
        with pytest.raises(RunCancelled) as caught:
            async with agent.iter('cancel me') as run:
                async def cancel_after_start() -> None:
                    await started.wait()
                    run.cancel()

                task = asyncio.create_task(cancel_after_start())
                try:
                    async for _ in run:
                        pass
                finally:
                    await task
        assert 'cancel me' in prompts(caught.value.all_messages())

        def loop(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart('lookup', {}, 'repeat')])

        limit_agent = Agent(FunctionModel(loop))

        @limit_agent.tool_plain
        def lookup() -> str:
            return 'result'

        with pytest.raises(UsageLimitExceeded):
            await limit_agent.run('limit', usage_limits=UsageLimits(request_limit=1))

    asyncio.run(run_case())


def test_native_schema_and_filter() -> None:
    async def run_case() -> None:
        names: list[list[str]] = []
        observed: list[object] = []

        async def lookup(ctx: RunContext[bool], number: object) -> str:
            observed.append(number)
            return 'read'

        # from_schema passes raw parameters unless an application validator is supplied.
        tool: Tool[bool] = Tool.from_schema(lookup, 'lookup', 'lookup', {
            'type': 'object', 'properties': {'number': {'type': 'integer'}}, 'required': ['number'],
        }, takes_ctx=True, sequential=True)
        toolset = FunctionToolset[bool](tools=[tool]).filtered(lambda ctx, definition: ctx.deps)

        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            names.append([t.name for t in info.function_tools])
            if info.function_tools and not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                return ModelResponse(parts=[ToolCallPart('lookup', {'number': 'invalid'}, 'raw')])
            return ModelResponse(parts=[TextPart('done')])

        agent = Agent(FunctionModel(model), deps_type=bool, toolsets=[toolset])
        await agent.run('denied', deps=False)
        result = await agent.run('allowed', deps=True)
        assert result.output == 'done'
        assert names == [[], ['lookup'], ['lookup']]
        assert observed == ['invalid']

    asyncio.run(run_case())


def test_native_compaction_and_fallback() -> None:
    async def run_case() -> None:
        history: list[ModelMessage] = []
        for i in range(5):
            history.extend([ModelRequest(parts=[UserPromptPart(f'old request {i}')]), ModelResponse(parts=[TextPart(f'old answer {i}')])])
        summary = TestModel(custom_output_text='summary retained')
        agent = Agent(TestModel(custom_output_text='done'), capabilities=[
            SummarizingCompaction(model=summary, max_messages=4, keep_messages=2),
        ])
        result = await agent.run('new query', message_history=history)
        assert result.usage.requests == 2
        assert 'new query' in str(result.all_messages())
        assert 'summary retained' in str(result.all_messages())
        # Native summarization retains the initial user anchor.
        assert 'old request 1' not in prompts(result.all_messages())
        assert 'old request 0' in prompts(history)

        def failed_summary(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise ModelAPIError('test', 'unavailable')

        fallback_agent = Agent(TestModel(custom_output_text='fallback'), capabilities=[FallbackCompaction([
            SummarizingCompaction(model=FunctionModel(failed_summary), keep_messages=2),
            SlidingWindowCompaction(keep_messages=2),
        ], max_tokens=10)])
        result = await fallback_agent.run('latest', message_history=history)
        assert result.output == 'fallback'
        assert len(result.all_messages()) < len(history)
        assert 'latest' in prompts(result.all_messages())

    asyncio.run(run_case())


def test_native_planning_and_step_persistence(tmp_path: Path) -> None:
    async def run_case() -> None:
        database = tmp_path / 'core01.sqlite'
        plan = SqlitePlanStore(str(database), session='owner/session/task')
        steps = SqliteStepStore(database=database, media_store=None)
        requests = 0
        plan_events: list[PlanCreatedEvent] = []

        async def event(ctx: RunContext[Any], event: AgentStreamEvent) -> None:
            if isinstance(event, PlanCreatedEvent):
                plan_events.append(event)

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {0: DeltaToolCall(name='write_plan', json_args='{"items":[{"id":"step1","content":"check quantity"}]}', tool_call_id='p1')}
            else:
                yield 'done'

        agent = Agent(FunctionModel(stream_function=model), capabilities=[
            Planning(store=plan), StepPersistence(store=steps, run_id='run-1'), Hooks(event=event),
        ])
        result = await agent.run('plan work')
        assert result.output == 'done'
        assert (await SqlitePlanStore(str(database), session='owner/session/task').get_items())[0].id == 'step1'
        assert await SqlitePlanStore(str(database), session='different').get_items() == []
        assert plan_events
        assert await steps.list_events(run_id='run-1')
        snapshot = await steps.latest_snapshot(run_id='run-1')
        assert snapshot is not None
        loaded = await continue_run(steps, run_id='run-1')
        assert 'plan work' in prompts(loaded)
        new_agent = Agent(TestModel(custom_output_text='resumed'))
        resumed = await new_agent.run('continue', message_history=loaded)
        assert resumed.output == 'resumed'
        assert 'plan work' in prompts(resumed.all_messages())

    asyncio.run(run_case())
