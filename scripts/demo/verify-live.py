"""Verify the retained read-only Agent catalogue and visitor isolation."""
import http.cookiejar
import json
from pathlib import Path
import urllib.request
import urllib.error

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
    names={item['name'] for item in tools.get('items',[])}
    check(names=={'ktg_count_products','ktg_query_stock_summary'},'exact retained Agent business catalogue')
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
    output=ROOT/'output/demo'
    output.mkdir(parents=True,exist_ok=True)
    (output/'live-verification.json').write_text(json.dumps({'checks':checks,'tool_count':len(names)},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
