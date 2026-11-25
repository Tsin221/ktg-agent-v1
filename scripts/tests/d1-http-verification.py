"""Verify session authorization and the actual plan API through the isolated gateway."""
from __future__ import annotations

import json
import runpy
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / '.runtime' / 'mes-d1-validation'
GATEWAY = 'http://127.0.0.1:18855'
MES = 'http://127.0.0.1:18092'
DEMO = runpy.run_path(str(ROOT / 'scripts' / 'verify-mes-demo-agent.py'))


def main() -> None:
    state = json.loads((RUN/'service-pids.json').read_text(encoding='utf-8-sig'))
    assert state['database'] == 'ktg_mes_d1_validation_20261124'
    auth = DEMO['login'](MES)

    def req(path: str, payload: dict | None = None, expect: int = 200) -> dict:
        request = Request(GATEWAY+path, data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Authorization':auth,'Content-Type':'application/json'})
        try:
            with urlopen(request, timeout=120) as response:
                assert response.status == expect, response.status
                return json.load(response)
        except HTTPError as e:
            assert e.code == expect, e.code
            return json.load(e)

    tools=req('/agent-api/v1/tools')
    names={row['name'] for row in tools['items']}
    assert {'ktg_prepare_workorder_operations', 'ktg_resolve_business_reference'} <= names and len(names)==8, names
    session=req('/agent-api/v1/sessions', {'title':'D1 HTTP验收'}, 201)['session_id']
    base='/agent-api/v1/sessions/'+session
    run=req(base+'/runs', {'request_id':str(uuid4()), 'message':'创建6件台灯草稿，改为5件，够料就下达。'}, 202)['run_id']
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        record=req('/agent-api/v1/runs/'+run)
        if record['status'] not in {'running','queued'}: break
        time.sleep(.25)
    assert record['status']=='completed', record
    plan=req(base+'/operations')['items'][0]
    assert plan['source_run_status']=='completed' and plan['confirmable'] and plan['status']=='pending'
    code=plan['steps'][0]['target']['workorder_code']
    assert DEMO['request_json'](MES+'/mes/pro/workorder/list?workorderCode='+code,auth)['total']==0
    path=base+'/operations/'+plan['plan_id']
    other=req('/agent-api/v1/sessions', {'title':'D1隔离检查'}, 201)['session_id']
    req('/agent-api/v1/sessions/'+other+'/operations/'+plan['plan_id']+'/confirm', {'digest':plan['digest']}, 404)
    req(path+'/confirm', {'digest':plan['digest'],'steps':[]}, 400)
    result=req(path+'/confirm', {'digest':plan['digest']})
    assert result['status']=='succeeded', result['results']
    assert result['results'][-1]['sn_count']==5
    assert req(path+'/confirm', {'digest':plan['digest']})['results']==result['results']
    assert req(path)['status']=='succeeded'
    assert req(base+'/operations')['items'][0]['confirmable'] is False
    (RUN/'http-verification.json').write_text(json.dumps({'tool_count':len(names),'session_id':session,'run_id':run,'plan':result,
        'cross_session_rejected':True,'tamper_rejected':True,'duplicate_confirm_idempotent':True},ensure_ascii=False,indent=2),encoding='utf-8')
    print('D1 HTTP plan preview, confirm, persistence, cross-session/tamper rejection and idempotent replay passed.')


if __name__ == '__main__':
    main()
