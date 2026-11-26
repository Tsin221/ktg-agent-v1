import asyncio
from pathlib import Path
from typing import Any
from ktg_agent.ktg.client import KTGClient
from fastapi.testclient import TestClient
from ktg_agent.http_app import create_app
from ktg_agent.http_rest import authenticate, Authentication
from ktg_agent.http_runs import Outcome, RunRuntime
from ktg_agent.sessions.repository import SessionRepository
from test_fastapi_foundation import config

class FakeExecution:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.inputs: list[str] = []
        self.stopped = False
    async def run(self) -> Outcome:
        self.started.set()
        await self.finish.wait()
        return Outcome('cancelled' if self.stopped else 'completed', 'done', {'answer':'verified'})
    def stop(self) -> None:
        self.stopped = True
        self.finish.set()
    async def input(self, request_id: str, mode: str, message: str) -> bool:
        self.inputs.append(mode)
        return mode == 'steer'
class FakeCore:
    def __init__(self) -> None:
        self.items: list[FakeExecution] = []
    def prepare(self, run: Any, credential: object) -> FakeExecution:
        item = FakeExecution()
        self.items.append(item)
        return item

def test_runtime_control(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = SessionRepository(tmp_path/'runs.db')
        sid = repo.create_session('u').id
        core = FakeCore()
        rt = RunRuntime(repo, core)
        a,b = await asyncio.gather(*(rt.create('u',sid,'r',{'message':'hi'},None) for _ in range(2)))
        assert a.id == b.id and len(core.items) == 1
        await core.items[0].started.wait()
        for mode in ['steer','follow_up']:
            await rt.control('u',a.id,mode,mode,{'message':mode})
            await rt.control('u',a.id,mode,mode,{'message':mode})
        assert core.items[0].inputs == ['steer','follow_up']
        task = rt.tasks[a.id]
        await rt.control('u',a.id,'stop','cancel',{})
        await task
        saved = repo.get_run('u',a.id)
        assert saved is not None and saved.status == 'cancelled'
        assert repo.list_inputs('u',a.id).items[1].status == 'not_consumed'
        await rt.close()
    asyncio.run(scenario())

def test_routes(tmp_path: Path) -> None:
    app = create_app(config(tmp_path), run_core=FakeCore())
    app.dependency_overrides[authenticate] = lambda: Authentication(KTGClient(base_url='http://mes.invalid', authorization='Bearer test'), {}, 'u')
    with TestClient(app) as c:
        sid = app.state.resources.repository.create_session('u').id
        root = '/agent-api/v1/sessions/'+sid+'/runs'
        r = c.post(root,json={'request_id':'r','message':'hi'})
        assert r.status_code == 202, r.text
        rid = r.json()['run_id']
        assert c.post(root,json={'request_id':'r','message':'hi'}).json()['run_id'] == rid
        assert c.post(root,json={'request_id':'r','message':'different'}).status_code == 409
        assert c.post(root,json={'request_id':'s','message':'hi'}).status_code == 409
        path = '/agent-api/v1/runs/'+rid
        assert c.get(path).status_code == 200
        assert c.post(path+'/inputs',json={'request_id':'i','mode':'steer','message':'x'}).status_code == 202
        assert c.get(path+'/inputs').status_code == 200
        assert c.post(path+'/cancel',json={'request_id':'c'}).status_code == 202
        app.dependency_overrides[authenticate] = lambda: Authentication(KTGClient(base_url='http://mes.invalid', authorization='Bearer test'), {}, 'other')
        assert c.get(path).status_code == 404


def test_failure_and_publish_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = SessionRepository(tmp_path/'events.db')
        sid = repo.create_session('u').id
        notifications: list[str] = []
        class Events:
            def publish(self, rid: str, kind: str, payload: Any) -> None:
                saved = repo.get_run('u',rid)
                assert saved is not None and saved.status == 'completed'
                notifications.append(kind)
            def last_seq(self, rid: str) -> int:
                return len(notifications)
        core = FakeCore()
        rt = RunRuntime(repo,core,Events())
        run = await rt.create('u',sid,'a',{'message':'hi'},None)
        task = rt.tasks[run.id]
        core.items[0].finish.set()
        await task
        assert notifications == ['run_settled']
        # A failed SQLite commit never publishes a terminal success.
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise OSError('commit failed')
        repo.settle_run = fail  # type: ignore[method-assign]
        run = await rt.create('u',sid,'b',{'message':'hi'},None)
        task = rt.tasks[run.id]
        core.items[1].finish.set()
        try:
            await task
        except OSError:
            pass
        else:
            raise AssertionError('commit error hidden')
        assert run.id in rt.unresolved and notifications == ['run_settled']
        saved = repo.get_run('u',run.id)
        assert saved is not None and saved.status == 'running'
        reopened = SessionRepository(tmp_path/'events.db')
        assert reopened.recover_interrupted_runs() == 1
        saved = reopened.get_run('u',run.id)
        assert saved is not None and saved.status == 'needs_reconciliation'
        assert reopened.get_active_run('u',sid) is None
    asyncio.run(scenario())


def test_shutdown_bound_and_prestart_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = SessionRepository(tmp_path/'shutdown.db')
        core = FakeCore()
        rt = RunRuntime(repo,core,shutdown_timeout=0.01)
        sid = repo.create_session('u').id
        run = await rt.create('u',sid,'a',{'message':'x'},None)
        await core.items[0].started.wait()
        # A noncooperative adapter must remain visible, with its DB reservation.
        core.items[0].stop = lambda: None  # type: ignore[method-assign]
        await asyncio.wait_for(rt.close(),0.5)
        assert run.id in rt.tasks and run.id in rt.unresolved
        assert repo.get_active_run('u',sid) is not None
        core.items[0].finish.set()
        await rt.tasks[run.id]
    asyncio.run(scenario())


def test_default_core_unavailable(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    app.dependency_overrides[authenticate] = lambda: Authentication(KTGClient(base_url='http://mes.invalid',authorization='Bearer test'),{},'u')
    with TestClient(app) as c:
        sid = app.state.resources.repository.create_session('u').id
        root = '/agent-api/v1/sessions/'+sid+'/runs'
        for body in [{'request_id':'','message':'x'}, {'request_id':'a','message':' '},
                     {'request_id':'a','message':'x','owner':'other'}]:
            assert c.post(root,json=body).status_code == 400
        assert c.post(root,json={'request_id':'a','message':'x'}).status_code == 503
        assert app.state.resources.repository.get_active_run('u',sid) is None


def test_schedule_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = SessionRepository(tmp_path/'schedule.db')
        sid = repo.create_session('u').id
        class BrokenCore:
            def prepare(self, run: Any, credential: object) -> FakeExecution:
                raise RuntimeError('private failure')
        rt = RunRuntime(repo,BrokenCore())
        try:
            await rt.create('u',sid,'r',{'message':'x'},None)
        except RuntimeError:
            pass
        else:
            raise AssertionError('expected schedule failure')
        run = repo.find_run_by_request('u',sid,'r')
        assert run is not None and run.status == 'failed'
        assert repo.get_active_run('u',sid) is None and not rt.tasks
        replay = await rt.create('u',sid,'r',{'message':'x'},None)
        assert replay.id == run.id
    asyncio.run(scenario())


def test_independent_sessions_and_unconfirmed_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = SessionRepository(tmp_path/'independent.db')
        core = FakeCore()
        rt = RunRuntime(repo,core)
        runs = [await rt.create('u',repo.create_session('u').id,str(i),{'message':'x'},None) for i in range(2)]
        for item in core.items:
            await item.started.wait()
        async def failure(request_id: str, mode: str, message: str) -> bool:
            raise RuntimeError('adapter unavailable')
        core.items[0].input = failure  # type: ignore[method-assign]
        reply = await rt.control('u',runs[0].id,'i','steer',{'message':'x'})
        assert reply['input_status'] == 'queued'
        tasks = tuple(rt.tasks.values())
        await rt.close()
        await asyncio.gather(*tasks)
        assert repo.list_inputs('u',runs[0].id).items[0].status == 'not_consumed'
        for run in runs:
            assert repo.get_active_run('u',run.session_id) is None
    asyncio.run(scenario())


def test_cancel_before_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        class DelayedRuntime(RunRuntime):
            async def _execute(self, owner: str, run: Any, execution: Any) -> None:
                await gate.wait()
                await super()._execute(owner,run,execution)
        repo = SessionRepository(tmp_path/'queued.db')
        core = FakeCore()
        rt = DelayedRuntime(repo,core)
        run = await rt.create('u',repo.create_session('u').id,'r',{'message':'x'},None)
        task = rt.tasks[run.id]
        await rt.control('u',run.id,'c','cancel',{})
        gate.set()
        await task
        assert not core.items[0].started.is_set()
        saved = repo.get_run('u',run.id)
        assert saved is not None and saved.status == 'cancelled'
        late = await rt.control('u',run.id,'late','cancel',{})
        assert late['cancel_status'] == 'already_settled'
    asyncio.run(scenario())
