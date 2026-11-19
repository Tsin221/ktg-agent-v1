"""Exercise the two existing inventory execution tools on the demo fixtures."""
import importlib.util
import json
from pathlib import Path
spec=importlib.util.spec_from_file_location('live',Path(__file__).with_name('verify-live.py'))
live=importlib.util.module_from_spec(spec);spec.loader.exec_module(live)
visitor=live.Visitor()
report=[]
def stock():
    _,data=visitor.call('/demo-api/mes/wm/wmstock/summary?itemCode=BRG-6204')
    return data
before=stock()
for code,kind in [('IN-001','物料入库'),('ISSUE-001','生产领料')]:
    resource='itemrecpt' if code=='IN-001' else 'issueheader'
    _,document=visitor.call('/demo-api/mes/wm/'+resource+'/1')
    if document.get('data',{}).get('status')=='FINISHED':
        print('Already verified and executed:',code,flush=True)
        report.append({'document':code,'already_executed':True})
        continue
    print('Requesting inventory preview:',code,flush=True)
    _,result=visitor.call('/agent-api/v1/query',{'message':f'请预览执行{kind}单 {code}，仅准备网页确认。'})
    candidates=[r for r in result.get('results',[]) if r.get('kind')=='inventory_preview']
    if not candidates: print(json.dumps(result,ensure_ascii=False),flush=True)
    assert candidates,code+' preview missing'
    _,result=visitor.call('/agent-api/v1/inventory-operations/confirm',{'operation_id':candidates[-1]['operation_id']})
    print('Inventory confirmation outcome:',result.get('result',{}).get('outcome'),flush=True)
    assert 'succeeded' in json.dumps(result),code+' did not succeed'
    report.append({'document':code,'result':result})
after=stock()
(live.ROOT/'output/demo/inventory-verification.json').write_text(json.dumps({'before':before,'after':after,'operations':report},ensure_ascii=False,indent=2),encoding='utf-8')
print('PASS receipt and production issue executed against demo inventory.',flush=True)
