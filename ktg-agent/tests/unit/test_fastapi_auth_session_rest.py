import asyncio
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktg_agent.http_app import create_app, Resources
from ktg_agent.http_rest import get_mes_client
from ktg_agent.ktg.client import KTGClient
from test_fastapi_foundation import config
from test_operation_executor import FakeMES, create_plan, plan_requests


def test_auth_sessions_and_boundaries(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    calls = []
    async def read(url: str, headers: Any, params: Any, timeout: float) -> object:
        calls.append(url)
        token = headers['Authorization']
        return {'user': {'userId': 2 if token.endswith('other') else 1}, 'permissions': ['*:*:*']}
    def client(request_credentials: object = None) -> KTGClient:
        return KTGClient(base_url='http://mes.invalid', authorization='Bearer local', transport=read)
    # Override only the transport: exercise actual HTTPBearer and scope derivation.
    from unittest.mock import patch
    with patch('ktg_agent.http_rest.KTGClient', side_effect=lambda **kw: KTGClient(**kw, transport=read)):
        with TestClient(app) as c:
            base = '/agent-api/v1/sessions'
            assert c.get(base).status_code == 401
            h = {'Authorization': 'Bearer local'}
            other = {'Authorization': 'Bearer other'}
            r = c.post(base, json={'title': 'Test'}, headers=h)
            assert r.status_code == 201, r.text
            sid = r.json()['session_id']
            root = base + '/' + sid
            for tail in ['', '/messages', '/stats', '/tasks', '/tasks/missing', '/operations', '/operations/missing']:
                assert c.get(root+tail, headers=other).status_code == 404
            for tail in ['', '/messages', '/stats', '/tasks', '/operations']:
                assert c.get(root+tail, headers=h).status_code == 200
            assert c.post(root+'/title', json={'title':'Renamed'}, headers=h).json()['title']=='Renamed'
            for q in ['limit=0','limit=1&limit=2','limit=','foo=1','cursor=-1']:
                assert c.get(base+'?'+q, headers=h).status_code == 400
            assert c.get(base+'?limit=101', headers=h).status_code == 200
            for body in [{'title':''}, {'title':1}, {'userId':2}]:
                assert c.post(base,json=body,headers=h).status_code==400
            assert c.post(root+'/compact',json={'request_id':'x'},headers=h).status_code==404
            assert c.get('/agent-api/v1/tools',headers=h).status_code==200
            assert 'Bearer local' not in c.get(base,headers=h).text


@pytest.mark.parametrize('lost', [False, True])
def test_http_operation_receipts_and_duplicate(tmp_path: Path, lost: bool) -> None:
    mes = FakeMES(100)
    store, plan = asyncio.run(create_plan(tmp_path, mes, plan_requests()))
    app = create_app(config(tmp_path), resource_factory=lambda settings, stack: Resources(store._repository, store))
    app.dependency_overrides[get_mes_client] = lambda: mes.client
    with TestClient(app) as c:
        # Lifespan recovery does not alter completed source runs or pending plans.
        root = '/agent-api/v1/sessions/'+plan['session_id']+'/operations/'+plan['plan_id']
        assert c.post(root+'/confirm',json={'digest':plan['digest'],'extra':True}).status_code==400
        assert c.post(root+'/confirm',json={'digest':'sha256:'+'0'*64}).status_code==400
        mes.lost=lost
        mes.hide_receipts=lost
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _: c.post(root+'/confirm',json={'digest':plan['digest']}),range(2)))
        assert all(r.status_code==200 for r in results), [r.text for r in results]
        assert len(mes.sent)==(1 if lost else 3)
        if lost:
            assert any(r.json()['status']=='needs_reconciliation' for r in results)
            operation_id=mes.sent[0]['operationId']
            mes.hide_receipts=False
            r=c.post(root+'/reconcile',json={})
            assert r.status_code==200, r.text
            assert r.json()['status']=='partial'
            assert len(mes.sent)==1 and operation_id in mes.receipts
from typing import Any


@pytest.mark.parametrize('scoped', [False, True])
def test_identity_scope_and_auth_fail_closed(tmp_path: Path, scoped: bool) -> None:
    from unittest.mock import patch
    settings=replace(config(tmp_path),scope_by_token=scoped)
    app=create_app(settings)
    payload: dict[str, Any]={'user':{'userId':1,'_agent_scope':'client-forged'},'permissions':['*:*:*']}
    async def read(*args: Any) -> object:
        return payload
    with patch('ktg_agent.http_rest.KTGClient',side_effect=lambda **kw: KTGClient(**kw,transport=read)):
        with TestClient(app) as c:
            a={'Authorization':'Bearer visitorA'}
            b={'Authorization':'Bearer visitorB'}
            sid=c.post('/agent-api/v1/sessions',json={},headers=a).json()['session_id']
            root='/agent-api/v1/sessions/'+sid
            for tail in ['', '/messages','/stats','/tasks','/operations']:
                assert c.get(root+tail,headers=b).status_code==(404 if scoped else 200)
            payload['permissions']=None
            r=c.get('/agent-api/v1/tools',headers=a)
            assert r.status_code==503 and r.json()['error']['code']=='PERMISSION_UNAVAILABLE'
            for user in [{},{'userId':True},{'userId':0},None]:
                payload['user']=user
                assert c.get(root,headers=a).status_code==401

@pytest.mark.parametrize('case', ['cancel','permission','source','cross'])
def test_operation_rejections(tmp_path: Path, case: str) -> None:
    mes=FakeMES(100)
    store,plan=asyncio.run(create_plan(tmp_path,mes,plan_requests(),settle=case!='source'))
    app=create_app(config(tmp_path),resource_factory=lambda settings,stack: Resources(store._repository,store))
    app.dependency_overrides[get_mes_client]=lambda:mes.client
    with TestClient(app) as c:
        root='/agent-api/v1/sessions/'+plan['session_id']+'/operations/'+plan['plan_id']
        if case=='cancel':
            assert c.post(root+'/cancel',json={'digest':plan['digest']}).status_code==200
        elif case=='permission':
            mes.permissions=[]
        elif case=='source':
            store._repository.settle_native_run('id:1',plan['run_id'],'failed','execution_failed',{},())
        else:
            async def other(*args: Any) -> object:
                return {'user':{'userId':2},'permissions':['*:*:*']}
            app.dependency_overrides[get_mes_client]=lambda: KTGClient(base_url='http://other.invalid',authorization='Bearer other',transport=other)
            for suffix in ['', '/confirm','/cancel','/reconcile']:
                r=c.get(root) if not suffix else c.post(root+suffix,json={} if suffix=='/reconcile' else {'digest':plan['digest']})
                assert r.status_code==404
        c.post(root+'/confirm',json={'digest':plan['digest']})
        assert len(mes.sent)==0


def test_messages_and_latest_task(tmp_path: Path) -> None:
    import json
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai_harness.planning import SqlitePlanStore, PlanItem, TaskStatus
    mes=FakeMES(100)
    store,plan=asyncio.run(create_plan(tmp_path,mes,plan_requests()))
    repo=store._repository
    run=repo.create_run('id:1',plan['session_id'],'native-history','digest',{'message':'first'})
    repo.update_run_status('id:1',run.id,'running')
    task=SqlitePlanStore(str(repo.path),session=json.dumps(['id:1',plan['session_id'],run.id], ensure_ascii=False, separators=(',', ':')),table='agent_native_plan_items')
    asyncio.run(task.set_items([PlanItem(id='one',content='first',status=TaskStatus.completed)]))
    repo.settle_native_run('id:1',run.id,'completed','completed',{},tuple(ModelRequest(parts=[UserPromptPart(content=str(i))],run_id=run.id) for i in range(3)))
    app=create_app(config(tmp_path),resource_factory=lambda settings,stack: Resources(repo,store))
    app.dependency_overrides[get_mes_client]=lambda:mes.client
    with TestClient(app) as c:
        root='/agent-api/v1/sessions/'+plan['session_id']
        page=c.get(root+'/messages?limit=2').json()
        assert len(page['items'])==2 and page['next_cursor'] is not None
        page2=c.get(root+'/messages?cursor='+str(page['next_cursor'])).json()
        assert len(page2['items'])==1
        assert page['items'][-1]['message_id']!=page2['items'][0]['message_id']
        detail=c.get(root+'/tasks/'+run.id).json()
        assert len(detail['versions'])==1 and detail['current_version']==1
        assert detail['versions'][0]['steps'][0]['status']=='completed'


def test_native_history_public_projection_filters_private_parts() -> None:
    from pydantic_ai.messages import ModelResponse, ModelRequest, ThinkingPart, TextPart, ToolReturnPart
    from ktg_agent.sessions.repository import MessageRecord
    from ktg_agent.sessions.service import project_message
    response = ModelResponse(parts=[ThinkingPart(content='private reasoning'), TextPart(content='visible')])
    tool = ModelRequest(parts=[ToolReturnPart(tool_name='read', tool_call_id='c',
        content='{"password":"hidden","data":"ok"}', metadata={'details': {'kind': 'result', 'refresh_token': 'hidden'}})])
    rows = [*project_message(MessageRecord(1, 's', 'r', response, 'now')),
            *project_message(MessageRecord(2, 's', 'r', tool, 'now'))]
    assert 'visible' in str(rows) and 'ok' in str(rows)
    assert 'private reasoning' not in str(rows) and 'hidden' not in str(rows)

