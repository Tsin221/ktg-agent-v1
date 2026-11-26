"""Supervisor acceptance of real HTTP, native execution and one SQLite file."""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from ktg_agent.http_app import create_app
from ktg_agent.http_rest import get_mes_client
from ktg_agent.sessions.repository import SessionRepository
from test_fastapi_foundation import config
from test_operation_executor import FakeMES


def test_stateless_query_uses_native_read_only_agent_without_persisting(tmp_path: Path) -> None:
    mes = FakeMES(100)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = {tool.name for tool in info.function_tools}
        assert 'ktg_count_products' in names
        assert 'ktg_prepare_workorder_operations' not in names
        assert 'search_tools' not in names and 'write_plan' not in names
        return ModelResponse(parts=[TextPart('offline read only')])

    app = create_app(config(tmp_path), native_model=FunctionModel(model))
    app.dependency_overrides[get_mes_client] = lambda: mes.client
    with TestClient(app) as client:
        response = client.post('/agent-api/v1/query', json={'message': 'read only'})
        assert response.status_code == 200, response.text
        assert response.json()['answer'] == 'offline read only'
        repo: SessionRepository = app.state.resources.repository
        with sqlite3.connect(repo.path) as db:
            assert db.execute('SELECT count(*) FROM app_runs').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
        assert mes.sent == []


def test_sqlite_writer_contention_does_not_block_http_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        mes = FakeMES(100)
        settings = config(tmp_path)
        app = create_app(settings, enable_native=False)
        app.dependency_overrides[get_mes_client] = lambda: mes.client
        async with app.router.lifespan_context(app):
            repo: SessionRepository = app.state.resources.repository
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with sqlite3.connect(repo.path) as blocker:
                    blocker.execute('BEGIN IMMEDIATE')
                    pending = asyncio.create_task(client.post('/agent-api/v1/sessions', json={'title':'locked'}))
                    await asyncio.sleep(.1)
                    assert not pending.done()
                    assert (await asyncio.wait_for(client.get('/health'), 1)).status_code == 200
                    # WAL reads can proceed during a short, contending writer.
                    assert (await asyncio.wait_for(client.get('/agent-api/v1/sessions'), 1)).status_code == 200
                    blocker.commit()
                result = await asyncio.wait_for(pending, 5)
                assert result.status_code == 201
    asyncio.run(scenario())


def test_hard_process_exit_real_app_restarts_and_explicitly_continues(tmp_path: Path) -> None:
    settings = config(tmp_path)
    script = r'''
import asyncio, json, os, sys
import httpx
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from ktg_agent.http_app import create_app
from ktg_agent.http_rest import get_mes_client
from ktg_agent.ktg.client import KTGClient
from ktg_agent.server import Settings

async def main():
    calls = 0
    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: DeltaToolCall(name='write_plan', json_args=json.dumps({'items':[{'id':'a','content':'verify fact'}]}))}
        else:
            os._exit(71)
    async def mes(url, headers, params, timeout):
        assert url.endswith('/getInfo')
        return {'code':200,'user':{'userId':1},'permissions':['*:*:*']}
    app = create_app(Settings('fake','http://model.invalid','fake','http://mes.invalid',session_db_path=sys.argv[1]),
                     native_model=FunctionModel(stream_function=model))
    app.dependency_overrides[get_mes_client] = lambda: KTGClient(base_url='http://mes.invalid', authorization='Bearer offline', transport=mes)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            session = (await client.post('/agent-api/v1/sessions',json={})).json()['session_id']
            reply = await client.post('/agent-api/v1/sessions/'+session+'/runs',json={'request_id':'first','message':'first task'})
            assert reply.status_code == 202, reply.text
            await asyncio.Event().wait()
asyncio.run(main())
'''
    child = subprocess.run([sys.executable, '-c', script, settings.session_db_path], timeout=30,
                           capture_output=True, text=True, check=False)
    assert child.returncode == 71, child.stderr

    async def scenario() -> None:
        calls: list[list[ModelMessage]] = []

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            calls.append(messages)
            yield 'continued offline'

        app = create_app(settings, native_model=FunctionModel(stream_function=model))
        mes = FakeMES(100)
        app.dependency_overrides[get_mes_client] = lambda: mes.client
        async with app.router.lifespan_context(app):
            repo: SessionRepository = app.state.resources.repository
            session = repo.list_sessions('id:1').items[0]
            before = repo.list_runs('id:1', session.id).items[0]
            assert before.status == 'needs_reconciliation' and before.stop_reason == 'SERVICE_RESTARTED'
            assert repo.get_active_run('id:1', session.id) is None and not calls
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post(f'/agent-api/v1/sessions/{session.id}/runs', json={
                    'request_id':'resume','message':'continue task','task_id':before.id})
                assert response.status_code == 202, response.text
                rid = response.json()['run_id']
                assert rid != before.id
                async with asyncio.timeout(10):
                    while True:
                        run = await asyncio.to_thread(repo.get_run, 'id:1', rid)
                        if run is not None and run.status not in ('queued', 'running', 'cancelling'):
                            assert run.status == 'completed', run
                            break
                        await asyncio.sleep(.01)
                assert calls and 'first task' in str(calls[0]) and 'verify fact' in str(calls[0])
                assert run.task_id == before.task_id and mes.sent == []
    asyncio.run(scenario())
