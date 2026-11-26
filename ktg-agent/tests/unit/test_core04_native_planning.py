"""Actual Harness planning tools with isolated, file-backed SQLite."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import AgentStreamEvent, ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai_harness.planning import (
    PlanCompletedEvent, PlanCreatedEvent, PlanDeletedEvent, PlanStatusChangedEvent,
    PlanUpdatedEvent, TaskStatus,
)

from ktg_agent.ktg.client import KTGClient
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.sessions.native_planning import (
    build_planning_capability, planning_task_id, read_plan,
)
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.tools.native import MesToolDeps, build_mes_toolset


def deps(run: str, *, owner: str = 'id:1', session: str = 'session-a', task: str | None = None) -> MesToolDeps:
    async def forbidden(*args: object) -> object:
        raise AssertionError('Planning must not invoke a MES tool')

    return MesToolDeps(run, session, owner, {'userId': 1}, KTGClient(
        base_url='https://test.invalid', authorization='Bearer offline', transport=forbidden,
    ), task)


def test_planning_lifecycle_and_events(tmp_path: Path) -> None:
    async def run_case() -> None:
        database = str(tmp_path / 'app.sqlite')
        # Deliberately coexist with an application-owned table of the common name.
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE plan_items (marker TEXT)')
            connection.execute("INSERT INTO plan_items VALUES ('unchanged')")
        events: list[AgentStreamEvent] = []
        actions = iter([
            ('write_plan', {'items': [{'id': 'a', 'content': '核对库存'}, {'id': 'b', 'content': '旧步骤'}]}),
            ('write_plan', {'items': [{'id': 'a', 'content': '核对各仓库存'}, {'id': 'b', 'content': '旧步骤'}]}),
            ('add_task', {'content': '整理报告'}),
            ('update_task_status', {'task_id': 'a', 'status': 'in_progress'}),
            ('update_task_status', {'task_id': 'a', 'status': 'completed'}),
            ('update_task_status', {'task_id': 'a', 'status': 'completed'}),
            ('remove_task', {'task_id': 'b'}),
        ])

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            assert {tool.name for tool in info.function_tools} == {
                'write_plan', 'read_plan', 'add_task', 'update_task_status', 'update_task_statuses', 'remove_task',
            }
            action = next(actions, None)
            if action is None:
                yield '计划已更新，业务结果仍需独立证明。'
            else:
                yield {0: DeltaToolCall(name=action[0], json_args=json.dumps(action[1]))}

        async def on_event(ctx: RunContext[MesToolDeps], event: AgentStreamEvent) -> None:
            events.append(event)

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps, capabilities=[
            build_planning_capability(database), Hooks(event=on_event),
        ])
        initial = deps('run-a')
        result = await agent.run('制定并调整计划', deps=initial)
        assert '业务结果' in result.output
        items = await read_plan(database, owner=initial.owner, session_id=initial.session_id, task_id=planning_task_id(initial))
        assert [(item.content, item.status) for item in items] == [
            ('核对各仓库存', TaskStatus.completed), ('整理报告', TaskStatus.pending),
        ]
        assert items[0].id == 'a'
        assert len([e for e in events if isinstance(e, PlanCreatedEvent)]) == 3
        assert len([e for e in events if isinstance(e, PlanUpdatedEvent)]) == 3
        assert len([e for e in events if isinstance(e, PlanStatusChangedEvent)]) == 2
        assert len([e for e in events if isinstance(e, PlanCompletedEvent)]) == 1
        assert [e.item.id for e in events if isinstance(e, PlanDeletedEvent)] == ['b']
        with sqlite3.connect(database) as connection:
            assert connection.execute('SELECT marker FROM plan_items').fetchone() == ('unchanged',)

    asyncio.run(run_case())


def test_model_completed_does_not_change_verified_business_failure(tmp_path: Path) -> None:
    async def run_case() -> None:
        database = str(tmp_path / 'app.sqlite')
        repository = SessionRepository(database)
        session = repository.create_session('id:1')
        run = repository.create_run('id:1', session.id, 'request', 'hash', {})
        repository.update_run_status('id:1', run.id, 'running')

        async def fake_mes(url: str, *args: object) -> object:
            if url.endswith('/getInfo'):
                return {'code': 200, 'user': {'userId': 1}, 'permissions': ['*:*:*']}
            raise RuntimeError('stock service unavailable')

        ctx_deps = MesToolDeps(run.id, session.id, 'id:1', {'userId': 1}, KTGClient(
            base_url='https://test.invalid', authorization='Bearer offline', transport=fake_mes,
        ))
        actions = iter([
            ('write_plan', {'items': [{'id': 'a', 'content': '核实库存', 'status': 'in_progress'}]}),
            ('ktg_query_stock_summary', {'item_code': 'LAMP'}),
            # Deliberately noncompliant model: demonstrate that completion cannot attest to a business outcome.
            ('update_task_status', {'task_id': 'a', 'status': 'completed'}),
        ])

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            assert info.instructions is not None and '业务事实以实际工具结果为准' in info.instructions
            action = next(actions, None)
            if action is None:
                yield '库存查询失败。'
            else:
                yield {0: DeltaToolCall(name=action[0], json_args=json.dumps(action[1]))}

        async def ignore_event(ctx: RunContext[MesToolDeps], event: AgentStreamEvent) -> None:
            pass

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps,
                      toolsets=[build_mes_toolset(OperationPlanStore(repository))], capabilities=[
                          build_planning_capability(database), Hooks(event=ignore_event),
                      ])
        result = await agent.run('核实库存', deps=ctx_deps)
        items = await read_plan(database, owner='id:1', session_id=session.id, task_id=run.id)
        assert items[0].status == TaskStatus.completed
        stock = next(part for message in result.all_messages() if isinstance(message, ModelRequest)
                     for part in message.parts if isinstance(part, ToolReturnPart)
                     and part.tool_name == 'ktg_query_stock_summary')
        assert stock.metadata['status'] == 'failed'
        assert stock.metadata['error_code'] == 'TOOL_EXECUTION_ERROR'
        assert stock.metadata['details'] is None and stock.metadata['verification'] is None
        assert '库存查询失败' in result.output

    asyncio.run(run_case())


def test_planning_resume_and_isolation(tmp_path: Path) -> None:
    async def run_case() -> None:
        database = str(tmp_path / 'app.sqlite')
        active = deps('old-run')
        fresh = deps('fresh-run')
        resumed = deps('new-run', task='old-run')

        async def ignore_event(ctx: RunContext[MesToolDeps], event: AgentStreamEvent) -> None:
            pass

        async def create(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            if '保留步骤' in str(messages):
                yield 'created'
            else:
                yield {0: DeltaToolCall(name='write_plan', json_args='{"items":[{"id":"step1","content":"保留步骤"}]}')}

        agent = Agent(FunctionModel(stream_function=create), deps_type=MesToolDeps, capabilities=[build_planning_capability(database), Hooks(event=ignore_event)])
        initial = await agent.run('创建计划', deps=active)
        assert initial.output == 'created'
        assert planning_task_id(active) == planning_task_id(resumed) == 'old-run'
        assert planning_task_id(fresh) == 'fresh-run'
        for other in [fresh, deps('new-run', owner='id:2', task='old-run'), deps('new-run', session='session-b', task='old-run')]:
            assert await read_plan(database, owner=other.owner, session_id=other.session_id, task_id=planning_task_id(other)) == []

        # Rebuild the Agent and use no old message history: the store must restore the checklist.
        calls = 0

        async def resume(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            nonlocal calls
            calls += 1
            assert '保留步骤' in str(messages)
            if calls == 1:
                yield {0: DeltaToolCall(name='update_task_status', json_args='{"task_id":"step1","status":"completed"}')}
            else:
                yield 'resumed'

        resumed_agent = Agent(FunctionModel(stream_function=resume), deps_type=MesToolDeps, capabilities=[build_planning_capability(database), Hooks(event=ignore_event)])
        result = await resumed_agent.run('续办', deps=resumed)
        assert result.output == 'resumed'
        items = await read_plan(database, owner=resumed.owner, session_id=resumed.session_id, task_id=planning_task_id(resumed))
        assert len(items) == 1 and items[0].id == 'step1' and items[0].status == TaskStatus.completed

        # Same session, normal new task: an old plan may be in conversation history, but not in its own store.
        async def normal(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            assert 'No plan yet.' in str(messages)
            yield '你好'

        normal_agent = Agent(FunctionModel(stream_function=normal), deps_type=MesToolDeps, capabilities=[build_planning_capability(database), Hooks(event=ignore_event)])
        result = await normal_agent.run('你好', deps=fresh, message_history=initial.all_messages())
        assert result.output == '你好'
        assert await read_plan(database, owner=fresh.owner, session_id=fresh.session_id, task_id=planning_task_id(fresh)) == []

    asyncio.run(run_case())
