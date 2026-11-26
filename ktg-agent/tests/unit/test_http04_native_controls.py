"""HTTP control admissions survive a cancelled request with the real native core."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from ktg_agent.http_app import create_app
from ktg_agent.http_rest import get_mes_client
from ktg_agent.sessions.repository import SessionRepository
from test_fastapi_foundation import config
from test_operation_executor import FakeMES


@pytest.mark.parametrize('kind', ['cancel', 'steer', 'follow_up'])
def test_native_control_commit_survives_request_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    async def scenario() -> None:
        waiting = asyncio.Event()
        release_model = asyncio.Event()
        committed, return_admission = Event(), Event()
        calls: list[list[ModelMessage]] = []
        mes = FakeMES(100)

        async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            calls.append(list(messages))
            if len(calls) == 1:
                waiting.set()
                await release_model.wait()
                yield 'initial response'
            else:
                yield 'response to delivered input'

        app = create_app(config(tmp_path), native_model=FunctionModel(stream_function=model))
        app.dependency_overrides[get_mes_client] = lambda: mes.client
        async with app.router.lifespan_context(app):
            repo: SessionRepository = app.state.resources.repository
            original = repo.admit_control

            def gated_admission(*args: Any, **kwargs: Any) -> Any:
                result = original(*args, **kwargs)
                if result[1]:
                    committed.set()  # real SQLite transaction already committed
                    assert return_admission.wait(5)
                return result

            monkeypatch.setattr(repo, 'admit_control', gated_admission)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                session = await client.post('/agent-api/v1/sessions', json={})
                sid = session.json()['session_id']
                request = await client.post(f'/agent-api/v1/sessions/{sid}/runs', json={
                    'request_id': 'start', 'message': 'first prompt'})
                assert request.status_code == 202, request.text
                rid = request.json()['run_id']
                await asyncio.wait_for(waiting.wait(), 5)
                run_task = app.state.runs.tasks[rid]
                prefix = f'/agent-api/v1/runs/{rid}'
                path = prefix + ('/cancel' if kind == 'cancel' else '/inputs')
                body = {'request_id': 'control-1'}
                if kind != 'cancel':
                    body.update(mode=kind, message='uniquely delivered input')
                response_task = asyncio.create_task(client.post(path, json=body))
                try:
                    assert await asyncio.to_thread(committed.wait, 5)
                    response_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await response_task
                    return_admission.set()
                    replay = await client.post(path, json=body)
                    assert replay.status_code == 202, replay.text
                    if kind == 'cancel':
                        # Native model still waits. Only cooperative stop, not
                        # a normal model result or test release, can finish it.
                        await asyncio.wait_for(asyncio.shield(run_task), 2)
                        assert (await client.get(prefix)).json()['status'] == 'cancelled'
                    else:
                        release_model.set()
                        await asyncio.wait_for(asyncio.shield(run_task), 5)
                        inputs = (await client.get(prefix+'/inputs')).json()['items']
                        assert len(inputs) == 1 and inputs[0]['status'] == 'consumed'
                        assert len(calls) == 2 and 'uniquely delivered input' in str(calls[1])
                        assert (await client.get(prefix)).json()['status'] == 'completed'
                    assert mes.sent == []
                finally:
                    return_admission.set()
                    release_model.set()
                    if not run_task.done():
                        await asyncio.wait_for(asyncio.shield(run_task), 5)
    asyncio.run(scenario())
