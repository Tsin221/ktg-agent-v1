from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

from ktg_agent.core.native_execution import NativeExecution
from ktg_agent.core.native_persistence import NativeStepHistory, RecoveryRequired, recovery_delta, working_history
from ktg_agent.ktg.client import KTGClient
from ktg_agent.tools.native import MesToolDeps


def deps(run: str, owner: str = 'owner') -> MesToolDeps:
    # No business tools are installed in this persistence probe.
    return MesToolDeps(run, 'session', owner, {}, cast(KTGClient, None), 'task')


def test_store_same_path_fails_closed_on_legacy_schema(tmp_path: Path) -> None:
    database = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY)')
    with pytest.raises(ValueError, match='legacy'):
        NativeStepHistory(database)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT name FROM sqlite_master WHERE type=\'table\'').fetchall() == [('runs',)]


def test_native_step_store_and_owner_scoped_continuation(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / 'one.sqlite'
        history = NativeStepHistory(database)
        first = deps('first')
        agent = Agent(TestModel(custom_output_text='first answer'), deps_type=MesToolDeps)
        await agent.run('first prompt', deps=first, capabilities=[history.capability(first)])
        reopened = NativeStepHistory(database)
        second = deps('second')
        messages = await reopened.continuation(second, 'first')
        assert 'first answer' in str(messages)
        with pytest.raises(KeyError):
            await reopened.continuation(deps('outsider', 'other'), 'first')
        result = await agent.run('second prompt', deps=second, message_history=messages,
                                 capabilities=[reopened.capability(second)])
        assert 'first prompt' in str(result.all_messages())
        assert await reopened.store.get_run(run_id='second') is not None
        assert (await reopened.store.list_events(run_id='first'))[-1].kind == 'run_completed'
    asyncio.run(scenario())


def test_step_store_is_not_a_parallel_conversation_and_recovery_selects_delta(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / 'one.sqlite'
        history = NativeStepHistory(database)
        scope = deps('current')
        agent = Agent(TestModel(custom_output_text='original answer'), deps_type=MesToolDeps)
        result = await agent.run('original', deps=scope, run_id=scope.run_id, conversation_id=scope.session_id,
                                 capabilities=[history.capability(scope)])
        settled = await history.store.latest_snapshot(run_id=scope.run_id)
        assert settled is not None
        archive = result.all_messages()[:1]
        recovered = recovery_delta(settled.messages, archive, scope.run_id)
        assert len(recovered) == 1 and 'original answer' in str(recovered)
        assert recovery_delta(settled.messages, result.all_messages(), scope.run_id) == ()
        assert working_history(result.all_messages(), settled.messages) == tuple(settled.messages)
        with sqlite3.connect(database) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {'runs', 'events', 'snapshots'} <= tables
            assert not any(name.startswith('native_raw_') for name in tables)
    asyncio.run(scenario())


def test_working_history_never_passes_unsettled_tool_calls_into_a_fresh_run() -> None:
    user = ModelRequest(parts=[UserPromptPart('original')], run_id='old')
    previous = ModelResponse(parts=[TextPart('known')], run_id='old')
    following = ModelRequest(parts=[UserPromptPart('do more')], run_id='old')
    unresolved = ModelResponse(parts=[ToolCallPart('write', '{}', 'not-settled')], run_id='old')
    assert working_history([user, previous, following, unresolved]) == (user, previous, following)
    later_user = ModelRequest(parts=[UserPromptPart('new Run')], run_id='new')
    later_answer = ModelResponse(parts=[TextPart('later complete answer')], run_id='new')
    assert working_history([user, previous, following, unresolved, later_user, later_answer]) == (
        user, previous, following, later_user, later_answer)


def test_unknown_tool_effect_prevents_resume(tmp_path: Path) -> None:
    from pydantic_ai_harness.step_persistence import ToolEffectRecord

    async def scenario() -> None:
        history = NativeStepHistory(tmp_path / 'one.sqlite')
        first = deps('first')
        agent = Agent(TestModel(), deps_type=MesToolDeps)
        await agent.run('first', deps=first, capabilities=[history.capability(first)])
        await history.store.record_tool_effect(ToolEffectRecord(run_id='first', tool_call_id='unknown',
                                                               tool_name='write', status='started'))
        with pytest.raises(RecoveryRequired):
            await history.continuation(deps('second'), 'first')
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', [False, True])
def test_settled_tool_survives_model_error_or_stop(tmp_path: Path, stop: bool) -> None:
    async def scenario() -> None:
        history = NativeStepHistory(tmp_path / 'one.sqlite')
        first = deps('first')
        second_model_started = asyncio.Event()
        calls = 0

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[dict[int, DeltaToolCall]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {0: DeltaToolCall(name='lookup', json_args='{}', tool_call_id='read')}
                return
            second_model_started.set()
            if stop:
                await asyncio.Event().wait()
            raise RuntimeError('offline simulated model failure')

        async def lookup() -> str:
            return 'verified read'

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps, tools=[lookup])
        execution = NativeExecution(agent, first, 'initial', capabilities=[history.capability(first)])
        task = asyncio.create_task(execution.run())
        await asyncio.wait_for(second_model_started.wait(), 5)
        if stop:
            execution.stop()
        outcome = await asyncio.wait_for(task, 5)
        assert outcome.status == ('cancelled' if stop else 'failed')
        reopened = NativeStepHistory(history.database)
        settled = await reopened.store.latest_snapshot(run_id='first')
        assert settled is not None and 'verified read' in str(settled.messages)
        assert (await reopened.store.list_events(run_id='first'))[-1].kind == 'run_failed'
        assert 'verified read' in str(await reopened.continuation(deps('next'), 'first'))
    asyncio.run(scenario())


def test_hard_process_exit_retains_settled_result_and_unknown_tool(tmp_path: Path) -> None:
    database = tmp_path / 'one.sqlite'
    # A child with a synthetic local tool exits during its second tool call.
    # No external system or actual side effect is involved.
    script = '''
import asyncio, os, sys
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from ktg_agent.core.native_persistence import NativeStepHistory
from ktg_agent.tools.native import MesToolDeps

async def main():
    history = NativeStepHistory(sys.argv[1])
    deps = MesToolDeps('crash', 'session', 'owner', {}, None, 'task')
    count = 0
    async def model(messages, info):
        nonlocal count
        count += 1
        name = 'read_fact' if count == 1 else 'synthetic_interruption'
        yield {0: DeltaToolCall(name=name, json_args='{}', tool_call_id=name)}
    async def read_fact():
        return 'known previous result'
    async def synthetic_interruption():
        os._exit(63)
    agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps,
                  tools=[read_fact, synthetic_interruption])
    from ktg_agent.core.native_execution import NativeExecution
    await NativeExecution(agent, deps, 'hard exit', capabilities=[history.capability(deps)]).run()

asyncio.run(main())
'''
    completed = subprocess.run([sys.executable, '-c', script, str(database)], timeout=30,
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 63, completed.stderr

    async def inspect() -> None:
        history = NativeStepHistory(database)
        settled = await history.store.latest_snapshot(run_id='crash')
        assert settled is not None and 'known previous result' in str(settled.messages)
        unresolved = await history.store.list_unresolved_tool_effects(run_id='crash')
        assert [effect.tool_name for effect in unresolved] == ['synthetic_interruption']
        with pytest.raises(RecoveryRequired):
            await history.continuation(deps('new'), 'crash')
    asyncio.run(inspect())
