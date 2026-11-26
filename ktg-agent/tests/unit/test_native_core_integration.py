"""Native execution through actual FastAPI routes + SSE on one isolated DB.

The application repository below is explicitly a TEST DOUBLE, because the old
SessionRepository tables collide with official StepStore. It is not a proposed
new app persistence implementation or a production runtime.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel

from ktg_agent.context.native_context import deserialize_messages, serialize_messages
from ktg_agent.core.messages import JsonObject
from ktg_agent.core.native_core import NativeCore, build_deepseek_agent
from ktg_agent.core.native_persistence import NativeStepHistory
from ktg_agent.http_app import create_app
from ktg_agent.http_rest import Authentication, authenticate
from ktg_agent.http_sse import RunEvents
from ktg_agent.ktg.client import KTGClient
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.sessions.native_planning import read_plan
from ktg_agent.sessions.repository import ControlRecord, Page, RunRecord, SessionRecord, SessionRepository
from ktg_agent.tools.native import MesToolDeps
from test_fastapi_foundation import config


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


class IsolatedAppRepository:
    """Only test contract fixtures: app admission/control, atomic terminal blob."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.records: dict[str, RunRecord] = {}
        self.controls: dict[tuple[str, str], ControlRecord] = {}
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE test_app_runs(id TEXT PRIMARY KEY, status TEXT, native_history BLOB)')
            db.execute('CREATE TABLE test_app_inputs(run_id TEXT, request_id TEXT, status TEXT, PRIMARY KEY(run_id,request_id))')

    def get_session(self, owner: str, sid: str) -> SessionRecord | None:
        return SessionRecord('session', 'owner', None, 't', 't') if (owner, sid) == ('owner', 'session') else None

    def get_task(self, owner: str, sid: str, task_id: str) -> JsonObject:
        assert owner == 'owner' and sid == 'session' and task_id == 'run-first'
        items = asyncio.run(read_plan(str(self.database), owner=owner, session_id=sid, task_id=task_id))
        return {'versions': [{'steps': [{'status': item.status.value} for item in items]}]}

    def admit_run(self, owner: str, sid: str, request_id: str, digest: str, payload: JsonObject) -> tuple[RunRecord, bool]:
        rid = f'run-{request_id}'
        if rid in self.records:
            return self.records[rid], False
        record = RunRecord(rid, sid, owner, request_id, digest, payload, 'queued', 't', None, None, None, None)
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT INTO test_app_runs VALUES(?,?,NULL)', (rid, record.status))
        self.records[rid] = record
        return record, True

    def get_run(self, owner: str, rid: str) -> RunRecord | None:
        record = self.records.get(rid)
        return record if record is not None and owner == record.owner_user_id else None

    def update_run_status(self, owner: str, rid: str, status: str) -> None:
        assert self.get_run(owner, rid) is not None
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE test_app_runs SET status=? WHERE id=?', (status, rid))
        self.records[rid] = replace(self.records[rid], status=status)

    def admit_control(self, owner: str, rid: str, request_id: str, kind: str, request: JsonObject,
                      *, accepting: bool, ready: bool, closed: bool) -> tuple[ControlRecord, bool]:
        assert self.get_run(owner, rid) is not None and accepting and ready and not closed
        key = (rid, request_id)
        if key in self.controls:
            return self.controls[key], False
        status = 'accepted' if kind == 'cancel' else 'queued'
        record = ControlRecord(len(self.controls)+1, rid, owner, request_id, kind, request, status, None, 't', 't')
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT INTO test_app_inputs VALUES(?,?,?)', (rid, request_id, status))
        self.controls[key] = record
        if kind == 'cancel':
            self.update_run_status(owner, rid, 'cancelling')
        return record, True

    def mark_input_consumed(self, owner: str, rid: str, request_id: str) -> None:
        assert self.get_run(owner, rid) is not None
        key = (rid, request_id)
        assert self.controls[key].status == 'queued'
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE test_app_inputs SET status=\'consumed\' WHERE run_id=? AND request_id=?', key)
        self.controls[key] = replace(self.controls[key], status='consumed')

    def list_inputs(self, owner: str, rid: str, *, after: int | None, limit: int) -> Page[ControlRecord]:
        return Page(tuple(record for record in self.controls.values() if record.run_id == rid
                          and record.owner_user_id == owner and record.kind != 'cancel'), None)

    def settle_native_run(self, owner: str, rid: str, status: str, reason: str,
                          summary: JsonObject, messages: tuple[ModelMessage, ...]) -> RunRecord:
        assert self.get_run(owner, rid) is not None
        with sqlite3.connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE test_app_runs SET status=?,native_history=? WHERE id=?',
                       (status, serialize_messages(messages), rid))
            db.execute('UPDATE test_app_inputs SET status=\'not_consumed\' WHERE run_id=? AND status=\'queued\'', (rid,))
        for key, record in list(self.controls.items()):
            if record.run_id == rid and record.status == 'queued':
                self.controls[key] = replace(record, status='not_consumed')
        settled = replace(self.records[rid], status=status, stop_reason=reason, result_summary=summary)
        self.records[rid] = settled
        return settled


def test_create_app_native_control_sse_and_new_run_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = config(tmp_path)
        database = Path(settings.session_db_path)
        repo = IsolatedAppRepository(database)
        history = NativeStepHistory(database)
        model_waiting, model_continue = asyncio.Event(), asyncio.Event()
        stop_waiting = asyncio.Event()
        model_calls = 0
        calls: list[list[ModelMessage]] = []

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            nonlocal model_calls
            model_calls += 1
            calls.append(list(messages))
            if model_calls == 1:
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

        agent = Agent(FunctionModel(stream_function=model), deps_type=MesToolDeps)
        credential = KTGClient(base_url='https://test.invalid', authorization='Bearer offline')
        class VerifyingEvents(RunEvents):
            def publish(self, run_id: str, kind: str, payload: JsonObject) -> None:
                if kind == 'run_settled':
                    with sqlite3.connect(database) as db:
                        status, raw = db.execute('SELECT status,native_history FROM test_app_runs WHERE id=?', (run_id,)).fetchone()
                        assert status == payload['status'] and raw is not None
                super().publish(run_id, kind, payload)

        events = VerifyingEvents()
        async def prior(run: RunRecord) -> str | None:
            return 'run-first' if run.request_id == 'next' else None

        async def consumed(owner: str, rid: str, request_id: str) -> None:
            await asyncio.to_thread(repo.mark_input_consumed, owner, rid, request_id)

        def publish(rid: str, kind: str, payload: JsonObject) -> None:
            app.state.runs.publish_progress(rid, kind, payload)

        def build_deps(run: RunRecord, _: object) -> MesToolDeps:
            task = run.request_summary.get('task_id')
            return MesToolDeps(run.id, run.session_id, run.owner_user_id, {}, credential,
                               task if isinstance(task, str) else None)

        core = NativeCore(agent, history, deps_factory=build_deps, prior_run=prior,
            mark_consumed=consumed, publish=publish)
        app = create_app(settings, resource_factory=lambda *_: SimpleNamespace(repository=repo), run_core=core, run_events=events)
        app.dependency_overrides[authenticate] = lambda: Authentication(credential, {}, 'owner')
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                first = await client.post('/agent-api/v1/sessions/session/runs', json={
                    'request_id':'first', 'message':'first prompt', 'page_context': {'route_name':'offline-page'}})
                assert first.status_code == 202, first.text
                rid = first.json()['run_id']
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
                assert (await client.get(path)).json()['status'] == 'completed'
                assert (await client.get(path+'/inputs')).json()['items'][0]['status'] == 'consumed'
                with sqlite3.connect(database) as db:
                    status, raw = db.execute('SELECT status,native_history FROM test_app_runs WHERE id=?', (rid,)).fetchone()
                    assert status == 'completed' and 'later input' in str(deserialize_messages(raw))
                assert 'read evidence' in str(await read_plan(str(database), owner='owner', session_id='session', task_id=rid))
                assert await history.raw_snapshots(MesToolDeps(rid, 'session', 'owner', {}, credential))
                previous_count = len(calls)
                second = await client.post('/agent-api/v1/sessions/session/runs', json={'request_id':'next','message':'continue','task_id':rid})
                assert second.status_code == 202
                second_id = second.json()['run_id']
                assert second_id != rid
                async def wait_complete() -> None:
                    while repo.records[second_id].status != 'completed':
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_complete(), 10)
                assert 'first prompt' in str(calls[previous_count])
                assert 'later input' in str(calls[previous_count])
                assert 'read evidence' in str(await read_plan(str(database), owner='owner', session_id='session', task_id=rid))
                third = await client.post('/agent-api/v1/sessions/session/runs', json={'request_id':'stop','message':'stop me'})
                third_id = third.json()['run_id']
                await asyncio.wait_for(stop_waiting.wait(), 5)
                stopping = app.state.runs.tasks[third_id]
                stop = await client.post(f'/agent-api/v1/runs/{third_id}/cancel', json={'request_id':'cancel'})
                assert stop.status_code == 202
                await asyncio.wait_for(stopping, 5)
                assert (await client.get(f'/agent-api/v1/runs/{third_id}')).json()['status'] == 'cancelled'
    asyncio.run(scenario())
