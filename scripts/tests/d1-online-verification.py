"""Authorized real-model D1 acceptance. Only run with explicit user approval on the isolated clone."""
from __future__ import annotations

import json
import argparse
import runpy
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / '.runtime/mes-d1-validation'
AGENT = 'http://127.0.0.1:18094'
MES = 'http://127.0.0.1:18092'
DEMO = runpy.run_path(str(ROOT / 'scripts/verify-mes-demo-agent.py'))


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument('--resume', action='store_true', help='Continue the existing authorized session and pending plan; do not duplicate it.')
    args=parser.parse_args()
    state = json.loads((RUN/'online-agent-pid.json').read_text(encoding='utf-8-sig'))
    assert state['database']=='ktg_mes_d1_validation_20261124' and state['backendPort']==18092 and state['agentPort']==18094
    auth = DEMO['login'](MES)

    def request(path: str, payload: dict | None = None, expected: int = 200) -> dict:
        req = Request(AGENT+path, data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode(),
                      headers={'Authorization':auth,'Content-Type':'application/json'})
        with urlopen(req,timeout=120) as response:
            assert response.status==expected
            return json.load(response)

    if args.resume:
        evidence=json.loads((RUN/'online-verification.json').read_text(encoding='utf-8'))
        session=evidence['session_id']
    else:
        session=request('/agent-api/v1/sessions',{'title':'D1 真实模型验收'},201)['session_id']
        evidence={'session_id':session,'rounds':[]}
    base='/agent-api/v1/sessions/'+session

    def run(question: str, need_plan: bool) -> tuple[dict,list]:
        run_id=request(base+'/runs',{'request_id':str(uuid4()),'message':question},202)['run_id']
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            record=request('/agent-api/v1/runs/'+run_id)
            if record['status'] not in {'queued','running','cancelling'}: break
            time.sleep(.5)
        messages=request(base+'/messages?limit=100')['items']
        rows=[row for row in messages if row['run_id']==run_id]
        plans=[p for p in request(base+'/operations')['items'] if p['run_id']==run_id]
        entry={'question':question,'run':record,'messages':rows,'plans':plans}
        evidence['rounds'].append(entry)
        (RUN/'online-verification.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Round',len(evidence['rounds']),record['status'],'plans',len(plans),flush=True)
        assert record['status']=='completed', record
        if need_plan:
            assert len(plans)==1 and plans[0]['status']=='pending' and plans[0]['confirmable'], entry
        return entry,plans

    def confirm(entry: dict, plan: dict) -> dict:
        target=plan['steps'][0]['target']['workorder_code']
        assert DEMO['request_json'](MES+'/mes/pro/workorder/list?workorderCode='+target,auth)['total']==0
        result=request(base+'/operations/'+plan['plan_id']+'/confirm',{'digest':plan['digest']})
        entry['confirmed']=result
        (RUN/'online-verification.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Confirm',result['status'],[r['status'] for r in result['results']],flush=True)
        return result

    if args.resume:
        first=evidence['rounds'][0]
        plans=first['plans']
    else:
        first,plans=run('冬季新品同款台灯电池模组，再开一张新工单，名称叫“D1真实验收小批试制”，需求日期2026-12-10。'
            '我想先创建6件草稿，再把同一张草稿改为5件，核对原仓账面材料够的话就下达。请准备这三个相互依赖的步骤，先不要执行。',True)
    actions=[step['action'] for step in plans[0]['steps']]
    assert [a for a in actions if a != 'check_materials']==['create_draft','edit_draft','release'], actions
    assert plans[0]['steps'][0]['draft']['quantity']==6 and plans[0]['steps'][1]['draft']['quantity']==5
    assert plans[0]['steps'][-1]['condition']=='warehouse_reference_sufficient'
    success=first.get('confirmed') or confirm(first,plans[0])
    assert success['status']=='succeeded' and success['results'][-1]['sn_count']==5
    followup,_=run('刚才建的那张真的下达了吗？现在实际有几道任务、多少个SN？有没有领料？',False)
    assert not followup['plans']
    shortage,plans=run('再给这个产品建一张300件的草稿，名称“D1真实验收缺料单”，需求日期是2026-12-20。'
        '如果原仓账面不够就留在草稿，不一定要下达。准备好让我确认。',True)
    assert plans[0]['steps'][0]['draft']['quantity']==300
    assert all(step.get('condition')=='warehouse_reference_sufficient' for step in plans[0]['steps'] if step['action']=='release')
    result=confirm(shortage,plans[0])
    assert result['results'][0]['status']=='succeeded'
    assert all(item['status']!='succeeded' for step,item in zip(result['steps'],result['results']) if step['action']=='release')
    print('D1 isolated online-model verification ended; evidence saved.',flush=True)


if __name__=='__main__':
    main()
