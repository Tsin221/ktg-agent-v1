"""Actual native framework, FastAPI, SSE and the application repository on one SQLite."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel

from ktg_agent.core.messages import JsonObject
from ktg_agent.core.native_core import build_deepseek_agent
from ktg_agent.http_app import create_app
from ktg_agent.http_rest import Authentication, authenticate, get_mes_client
from ktg_agent.http_sse import RunEvents
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.sessions.native_planning import read_plan
from ktg_agent.sessions.repository import SessionRepository
from test_fastapi_foundation import config
from test_operation_executor import FakeMES

def test_offline_deepseek_factory_uses_official_model(tmp_path: Path) -> None:
    settings = replace(config(tmp_path), model_id='deepseek-flash', model_base_url='https://model.invalid/v1',
                       model_api_key='offline-test-key', max_output_tokens=512)
    operations = OperationPlanStore(SessionRepository(tmp_path / 'legacy-tool-fixture.sqlite'))
    agent = build_deepseek_agent(settings, operations, instructions='Use only authorized business tools.')
    model = agent.model
    assert isinstance(model, OpenAIChatModel) and model.system == 'deepseek'
    assert model.model_name == 'deepseek-flash'
    assert model.base_url.rstrip('/') == settings.model_base_url
    assert isinstance(agent.model_settings, dict) and agent.model_settings['max_tokens'] == 512



def test_create_app_native_control_sse_and_new_run_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = config(tmp_path)
        database = Path(settings.session_db_path)
        model_waiting, model_continue = asyncio.Event(), asyncio.Event()
        stop_waiting = asyncio.Event()
        calls: list[list[ModelMessage]] = []

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            calls.append(list(messages))
            assert 'search_tools' not in {tool.name for tool in info.function_tools}
            if len(calls) == 1:
                model_waiting.set()
                await model_continue.wait()
                yield 'checking '
                yield {1: DeltaToolCall(name='write_plan', json_args=json.dumps({'items': [{'id': 'read', 'content': 'read evidence'}]}), tool_call_id='p')}
            elif 'stop me' in str(messages):
                stop_waiting.set()
                await asyncio.Event().wait()
                yield 'unreachable'
            else:
                yield 'native answer'

        class VerifyingEvents(RunEvents):
            def publish(self, run_id: str, kind: str, payload: JsonObject) -> None:
                if kind == 'run_settled':
                    with sqlite3.connect(database) as db:
                        status = db.execute('SELECT status FROM app_runs WHERE id=?', (run_id,)).fetchone()[0]
                        assert status == payload['status']
                        if status == 'completed':
                            assert db.execute('SELECT count(*) FROM app_conversation_entries WHERE run_id=?', (run_id,)).fetchone()[0] > 0
                super().publish(run_id, kind, payload)

        events = VerifyingEvents()
        mes = FakeMES(100)
        app = create_app(settings, native_model=FunctionModel(stream_function=model), run_events=events)
        app.dependency_overrides[get_mes_client] = lambda: mes.client
        async with app.router.lifespan_context(app):
            repo: SessionRepository = app.state.resources.repository
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                session = await client.post('/agent-api/v1/sessions', json={})
                assert session.status_code == 201, session.text
                sid = session.json()['session_id']
                root = f'/agent-api/v1/sessions/{sid}'
                first_run = await client.post(root+'/runs', json={
                    'request_id':'first', 'message':'first prompt', 'page_context': {'route_name':'offline-page'}})
                assert first_run.status_code == 202, first_run.text
                rid = first_run.json()['run_id']
                await asyncio.wait_for(model_waiting.wait(), 5)
                assert 'offline-page' in str(calls[0])
                path = f'/agent-api/v1/runs/{rid}'
                response = await client.post(path+'/inputs', json={'request_id':'f', 'mode':'follow_up', 'message':'later input'})
                assert response.json()['input_status'] == 'queued'
                assert (await client.get(path+'/inputs')).json()['items'][0]['status'] == 'queued'
                stream = asyncio.create_task(client.get(path+'/events'))
                async def wait_subscription() -> None:
                    while events.subscribers != 1:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_subscription(), 5)
                model_continue.set()
                response = await asyncio.wait_for(stream, 10)
                assert response.status_code == 200
                assert all(f'event: {name}' in response.text for name in ('message_update', 'tool_execution_end', 'plan_changed', 'input_consumed', 'run_settled'))
                ids = [int(line.split(': ',1)[1]) for line in response.text.splitlines() if line.startswith('id: ')]
                assert ids == list(range(1, len(ids)+1))
                settled = (await client.get(path)).json()
                assert settled['status'] == 'completed' and settled['result_summary']['answer'] == 'native answer'
                with sqlite3.connect(database) as db:
                    raw_summary = db.execute('SELECT result_summary_json FROM app_runs WHERE id=?', (rid,)).fetchone()[0]
                    assert 'native answer' not in raw_summary
                assert (await client.get(path+'/inputs')).json()['items'][0]['status'] == 'consumed'
                history = (await client.get(root+'/messages')).json()['items']
                assert 'later input' in str(history) and 'native answer' in str(history)
                assert 'read evidence' in str(await read_plan(str(database), owner='id:1', session_id=sid, task_id=rid))
                previous_count = len(calls)
                second = await client.post(root+'/runs', json={'request_id':'next','message':'continue','task_id':rid})
                assert second.status_code == 202, second.text
                second_id = second.json()['run_id']
                assert second_id != rid

                async def wait_complete(run_id: str) -> None:
                    while True:
                        saved = await asyncio.to_thread(repo.get_run, 'id:1', run_id)
                        if saved is not None and saved.status not in ('queued', 'running', 'cancelling'):
                            assert saved.status == 'completed', saved
                            return
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_complete(second_id), 10)
                assert 'first prompt' in str(calls[previous_count])
                assert 'later input' in str(calls[previous_count])
                assert 'read evidence' in str(await read_plan(str(database), owner='id:1', session_id=sid, task_id=rid))
                ordinary_index = len(calls)
                ordinary = await client.post(root+'/runs', json={'request_id':'third','message':'normal follow-up'})
                assert ordinary.status_code == 202
                await asyncio.wait_for(wait_complete(ordinary.json()['run_id']), 10)
                assert 'first prompt' in str(calls[ordinary_index]) and 'continue' in str(calls[ordinary_index])
                app.dependency_overrides[authenticate] = lambda: Authentication(mes.client, {'userId': 2}, 'id:2')
                assert (await client.get(root+'/messages')).status_code == 404
                del app.dependency_overrides[authenticate]
                third = await client.post(root+'/runs', json={'request_id':'stop','message':'stop me'})
                third_id = third.json()['run_id']
                await asyncio.wait_for(stop_waiting.wait(), 5)
                stopping = app.state.runs.tasks[third_id]
                stop = await client.post(f'/agent-api/v1/runs/{third_id}/cancel', json={'request_id':'cancel'})
                assert stop.status_code == 202
                await asyncio.wait_for(stopping, 5)
                assert (await client.get(f'/agent-api/v1/runs/{third_id}')).json()['status'] == 'cancelled'
                assert mes.sent == []
                with sqlite3.connect(database) as db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    assert {'app_runs','app_sessions','app_conversation_entries','app_operation_plans','runs','events','agent_native_plan_items'} <= tables
                    assert not {'messages','native_raw_messages','native_raw_snapshots','task_versions','model_requests'} & tables
    asyncio.run(scenario())
