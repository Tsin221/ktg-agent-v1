"""Verify the local administrator reset against running demo services."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
spec=importlib.util.spec_from_file_location('live',Path(__file__).with_name('verify-live.py'))
live=importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
visitor=live.Visitor()
_,session=visitor.call('/agent-api/v1/sessions',{'title':'恢复验证'})
root=Path(__file__).resolve().parents[2]
subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/'scripts/demo/stop.ps1')],check=True)
subprocess.run(['python',str(root/'scripts/demo/reset.py')],check=True)
subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/'scripts/demo/start.ps1')],check=True)
deadline=time.monotonic()+100
while True:
    try:
        fresh=live.Visitor()
        _,info=fresh.call('/demo-api/getInfo')
        if info.get('code')==200: break
    except Exception: pass
    if time.monotonic()>deadline: raise AssertionError('Demo did not restart')
    time.sleep(2)
_,after=fresh.call('/demo-api/mes/dv/repair/list')
assert after.get('total')==3
status,_=visitor.call('/agent-api/v1/sessions/'+session['session_id'])
assert status==401
_,sessions=fresh.call('/agent-api/v1/sessions')
assert not sessions['items']
report={'baseline_repairs':after['total'],'old_visitor_status':status,'old_sessions_removed':True}
(root/'output/demo/reset-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print('PASS manual reset: baseline restored, old login and sessions invalidated.',flush=True)
