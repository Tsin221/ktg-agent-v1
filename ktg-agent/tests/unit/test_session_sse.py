import asyncio
from ktg_agent.http_sse import RunEvents


def test_bounded_independent_replay() -> None:
    async def scenario() -> None:
        sink = RunEvents(capacity=2)
        sink.publish('r', 'message_update', {'text': '中文\n下一行'})
        state = sink.states['r']
        assert sink.read(state, 0)[0]['seq'] == 1
        assert sink.read(state, 0)[0]['seq'] == 1
        sink.publish('r', 'message_update', {'text': 'b'})
        sink.publish('r', 'message_update', {'text': 'c'})
        assert len(state.items) == 2
        assert sink.expired(state, 0)
        assert [e['seq'] for e in sink.read(state, 1)] == [2, 3]
        sink.publish('r', 'run_settled', {'status': 'completed'})
        assert 'r' not in sink.states and state.closed
        assert state.changed.is_set()
    asyncio.run(scenario())
import asyncio
import json
import socket
from pathlib import Path
from typing import Any
import httpx
import uvicorn
from ktg_agent.http_app import create_app
from ktg_agent.http_rest import get_mes_client
from ktg_agent.http_sse import RunEvents
from ktg_agent.http_runs import Outcome
from ktg_agent.core.messages import AssistantMessage, TextBlock
from test_fastapi_foundation import config


def test_real_network(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RunEvents(capacity=2)
        finish = asyncio.Event()
        started = asyncio.Event()
        class Execution:
            async def run(self) -> Outcome:
                sink.publish(rid[0], 'message_update', {'text': '中文\n第二行'})
                started.set()
                await finish.wait()
                return Outcome('completed', 'done', {'answer': '完整答案'},
                               (AssistantMessage(content=(TextBlock(text='完整答案'),)),))
            def stop(self) -> None:
                finish.set()
            async def input(self, request_id: str, mode: str, message: str) -> bool:
                return False
        class Core:
            calls = 0
            def prepare(self, run: Any, credential: object) -> Execution:
                self.calls += 1
                rid[:] = [run.id]
                return Execution()
        # Exercise actual HTTPBearer dependency and identity scope, no MES network.
        class Client:
            def __init__(self, visitor: bool = False) -> None:
                self.visitor = visitor
            async def get_current_user(self) -> dict[str, Any]:
                return {'userId': 7, 'userName': 'test', '_agent_scope': 'other' if self.visitor else ''}
        from ktg_agent.http_rest import bearer
        from fastapi import Depends
        from fastapi.security import HTTPAuthorizationCredentials
        from ktg_agent.http_app import PublicError
        async def mes(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Client:
            if credentials is None or credentials.credentials not in ('synthetic', 'visitor'):
                raise PublicError(401, 'AUTH_INVALID', 'Invalid authentication.')
            return Client(credentials.credentials == 'visitor')
        rid: list[str] = []
        core = Core()
        app = create_app(config(tmp_path), run_core=core, run_events=sink)
        app.dependency_overrides[get_mes_client] = mes
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                         headers={'Authorization': 'Bearer synthetic'}, timeout=20, trust_env=False) as client:
                response = await client.post('/agent-api/v1/sessions', json={})
                assert response.status_code == 201, response.text
                sid = response.json()['session_id']
                root = '/agent-api/v1/sessions/'+sid+'/runs'
                body = {'request_id': 'once', 'message': 'hi'}
                response = await client.post(root, json=body)
                assert response.status_code == 202, response.text
                await started.wait()
                path = '/agent-api/v1/runs/'+rid[0]
                assert (await client.post(root, json=body)).json()['run_id'] == rid[0]
                assert (await client.get(path+'/events', headers={'Authorization': 'Bearer invalid'})).status_code == 401
                assert (await client.get(path+'/events', headers={'Authorization': 'Bearer visitor'})).status_code == 404
                assert (await client.get(path+'/events', headers={'Authorization': ''})).status_code == 401
                assert (await client.get(path+'/events?after_seq=-1')).status_code == 400
                assert (await client.get(path+'/events?after_seq=99')).status_code == 400
                assert (await client.get(path+'/events?token=x')).status_code == 400
                assert (await client.get('/agent-api/v1/runs/missing/events')).status_code == 404
                async with client.stream('GET',path+'/events') as first, client.stream('GET',path+'/events') as second:
                    assert first.status_code == second.status_code == 200
                    assert 'no-store' in first.headers['cache-control']
                    assert first.headers['x-accel-buffering'] == 'no'
                    async def frame(lines: Any) -> list[str]:
                        result: list[str] = []
                        async for line in lines:
                            if not line:
                                if result:
                                    return result
                            else:
                                result.append(line)
                        return result
                    a, b = first.aiter_lines(), second.aiter_lines()
                    one, two = await asyncio.gather(frame(a), frame(b))
                    assert one == two and 'id: 1' in one
                    data = json.loads(next(x[6:] for x in one if x.startswith('data: ')))
                    assert data['payload']['text'] == '中文\n第二行'
                    assert not finish.is_set() # First bytes arrived before settlement can occur.
                    assert (await client.get(path)).json()['status'] == 'running'
                    heartbeat = await frame(b)
                    assert heartbeat[0].startswith(':') and sink.last_seq(rid[0]) == 1
                    await first.aclose()
                    sink.publish(rid[0], 'message_update', {'text': 'two'})
                    assert 'id: 2' in await frame(b)
                # Disconnect did not cancel execution. Replay from exact displayed cursor.
                async with client.stream('GET', path+'/events?after_seq=1') as replay:
                    lines = replay.aiter_lines()
                    assert 'id: 2' in await frame(lines)
                    sink.publish(rid[0], 'message_update', {'text': 'three'})
                    assert 'id: 3' in await frame(lines)
                    assert (await client.get(path+'/events?after_seq=0')).status_code == 410
                    finish.set()
                    terminal = await frame(lines)
                    assert 'event: run_settled' in terminal
                    # Notification proves both persisted outcome and messages already readable.
                    assert (await client.get(path)).json()['status'] == 'completed'
                    messages = (await client.get('/agent-api/v1/sessions/'+sid+'/messages')).json()['items']
                    assert any('完整答案' in json.dumps(m, ensure_ascii=False) for m in messages)
                assert core.calls == 1 and rid[0] not in sink.states
                assert (await client.get(path+'/events')).status_code == 410
                async with asyncio.timeout(2):
                    while sink.subscribers:
                        await asyncio.sleep(.01)
                import sqlite3
                with sqlite3.connect(app.state.resources.repository.path) as db:
                    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    # No display delta was written to any database table.
                    dump = '\n'.join(db.iterdump())
                    assert '第二行' not in dump and '完整答案' in dump
        finally:
            finish.set()
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            sock.close()
    asyncio.run(scenario())


def test_open_stream_gap() -> None:
    from ktg_agent.http_sse import events
    async def scenario() -> None:
        sink = RunEvents(capacity=1)
        sink.publish('r', 'message_update', {'text': 'one'})
        state = sink.states['r']
        stream = events('r', (sink, state, 0, 's'))
        assert (await anext(stream)).id == '1'
        sink.publish('r', 'message_update', {'text': 'two'})
        sink.publish('r', 'message_update', {'text': 'three'})
        recovery = await anext(stream)
        assert recovery.event == 'history_expired' and recovery.id is None
        await stream.aclose()
        assert sink.subscribers == 0
    asyncio.run(scenario())


def test_restart_terminal_without_cache(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from ktg_agent.http_rest import authenticate, Authentication
    from ktg_agent.ktg.client import KTGClient
    from ktg_agent.sessions.repository import SessionRepository
    settings = config(tmp_path)
    repo = SessionRepository(settings.session_db_path)
    sid = repo.create_session('u').id
    run, _ = repo.admit_run('u', sid, 'interrupted', 'digest', {'message': 'x'})
    repo.update_run_status('u', run.id, 'running')
    app = create_app(settings)
    app.dependency_overrides[authenticate] = lambda: Authentication(
        KTGClient(base_url='http://mes.invalid', authorization='Bearer fake'), {}, 'u')
    with TestClient(app) as client:
        path = '/agent-api/v1/runs/'+run.id
        assert client.get(path).json()['status'] == 'needs_reconciliation'
        assert client.get(path+'/events').status_code == 410
        assert client.get('/agent-api/v1/sessions/'+sid).json().get('active_run_id') is None
