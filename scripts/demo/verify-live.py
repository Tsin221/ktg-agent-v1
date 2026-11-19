"""Exercise real local authentication, isolation and a model-backed write."""
import http.cookiejar
import json
from pathlib import Path
import urllib.request
import urllib.error
from datetime import date
from uuid import uuid4

ORIGIN='http://127.0.0.1:4180'
ROOT=Path(__file__).resolve().parents[2]


class Visitor:
    def __init__(self):
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call('/demo-session',{})
    def call(self,path,body=None):
        request=urllib.request.Request(ORIGIN+path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Origin':ORIGIN})
        try:
            with self.opener.open(request,timeout=180) as response: return response.status,json.load(response)
        except urllib.error.HTTPError as error: return error.code,json.load(error)


def main():
    a,b=Visitor(),Visitor()
    checks=[]
    def check(ok,label):
        checks.append({'passed':bool(ok),'label':label})
        print(('PASS ' if ok else 'FAIL ')+label,flush=True)
        if not ok: raise AssertionError(label)
    _,info=a.call('/demo-api/getInfo')
    check(info['user']['userName']=='demo','dedicated demo account')
    status,profile=a.call('/demo-api/system/user/profile')
    check(status==200 and profile.get('code')==200,'current employee profile readable for business pages')
    _,tools=a.call('/agent-api/v1/tools')
    print('Available tools:',len(tools.get('items',[])),flush=True)
    for endpoint in ['dv/machinery','dv/repair','dv/checkplan','tm/tool','pro/workorder','wm/wmstock']:
        _,result=a.call('/demo-api/mes/'+endpoint+'/list')
        check(result.get('code')==200 and len(result.get('rows',[]))>0,endpoint+' real seeded records')
    status,_=a.call('/demo-api/system/user/list')
    check(status==403,'system administration denied')
    _,session=a.call('/agent-api/v1/sessions',{'title':'接入验收'})
    sid=session['session_id']
    _,own=a.call('/agent-api/v1/sessions/'+sid)
    check(own.get('session_id')==sid,'visitor can reopen own session')
    status,_=b.call('/agent-api/v1/sessions/'+sid)
    check(status==404,'other visitor cannot read session')
    code='TRY-'+uuid4().hex[:8].upper()
    prompt=f'请为设备 DEV-001 新建维修单草稿，编号 {code}，名称 一号装配机体验维修，报修日期 {date.today()}。只生成待确认预览。'
    print('Requesting real model repair preview…',flush=True)
    _,result=a.call('/agent-api/v1/query',{'message':prompt})
    previews=[item for item in result.get('results',[]) if isinstance(item,dict) and item.get('preview_id')]
    if not previews:
        print(json.dumps(result,ensure_ascii=False),flush=True)
    check(bool(previews),'real model returned a business preview')
    preview=previews[-1]['preview_id']
    _,denied=b.call('/agent-api/v1/dv-repair/confirm',{'preview_id':preview})
    check(denied.get('outcome')!='succeeded' and denied.get('status')!='succeeded','other visitor cannot confirm preview')
    _,before=a.call('/demo-api/mes/dv/repair/list?repairCode='+code)
    check(before.get('total')==0,'preview did not write a repair')
    _,confirmed=a.call('/agent-api/v1/dv-repair/confirm',{'preview_id':preview})
    _,after=a.call('/demo-api/mes/dv/repair/list?repairCode='+code)
    if after.get('total')!=1: print(json.dumps(confirmed,ensure_ascii=False),flush=True)
    check(after.get('total')==1,'confirmed repair persisted in MES')
    _,shared=b.call('/demo-api/mes/dv/repair/list?repairCode='+code)
    check(shared.get('total')==1,'business data shared across visitors')
    a.call('/agent-api/v1/dv-repair/confirm',{'preview_id':preview})
    _,again=a.call('/demo-api/mes/dv/repair/list?repairCode='+code)
    check(again.get('total')==1,'repeat confirmation does not create a duplicate')
    output=ROOT/'output/demo'
    output.mkdir(parents=True,exist_ok=True)
    (output/'live-verification.json').write_text(json.dumps({'checks':checks,'created_repair':code,'tool_count':len(tools.get('items',[]))},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
