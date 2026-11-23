"""Independent B06 evidence audit from captured SQL rows and finalized API returns."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from execution_closure_fixture import RUN, ROOT, SOURCE, TARGET, sql


def audit_case(branch):
    code=f'B06-FINAL1-{branch}'
    file=RUN/f'{code}-evidence.jsonl'
    records=[json.loads(s) for s in file.read_text(encoding='utf-8').splitlines()]
    stages={r['stage']:r['facts'] for r in records if 'stage' in r}
    passed=next(r for r in records if r.get('passed'))
    assert any(r.get('tracePassed') for r in records)
    batches=passed['batches'];good=10 if branch=='R' else 9
    final=stages['final']
    def fields(rows): return [s.split('\t') for s in rows]
    assert stages['before-release']['stock']==stages['release']['stock']
    assert not stages['release']['transactions']
    assert stages['first-inspections']['stock']==stages['packed-nine-and-rejections']['stock']
    assert stages['first-inspections']['transactions']==stages['packed-nine-and-rejections']['transactions']
    input_rows=fields(final['inputs'])
    assert all(Decimal(r[3])==1 for r in input_rows)
    assert Counter(r[2] for r in input_rows)==dict(zip(batches,[2,3,5]))
    assert len({r[0] for r in input_rows})==10 and len({r[5] for r in input_rows})==1
    tx=fields(final['transactions'])
    for j,b in enumerate(batches):
        amounts=[2,3,5]
        assert [(r[1],Decimal(r[4])) for r in tx if r[3]==b]==[
            ('ITEM_ISSUE_OUT',-amounts[j]),('ITEM_ISSUE_IN',amounts[j]),('ITEM_CONSUME',-amounts[j])]
    outputs=[r for r in tx if r[1]=='PRODUCT_PRODUCE']
    assert len(outputs)==good and all(Decimal(r[4])==1 for r in outputs)
    assert len({r[7] for r in outputs})==good
    stocks=fields(final['stock'])
    assert len(stocks)==7
    assert {r[2]:Decimal(r[3]) for r in stocks if r[4]=='930001'}==dict.fromkeys(batches,Decimal(5))
    assert {r[2]:Decimal(r[3]) for r in stocks if r[4]=='200' and r[1]=='33'}==dict.fromkeys(batches,Decimal(0))
    assert len([r for r in stocks if r[1]=='32'])==1 and Decimal([r for r in stocks if r[1]=='32'][0][3])==good
    assert fields(final['workorder'])[0][1]=='FINISHED'
    assert all(r[3]=='FINISHED' and r[6]!='NULL' and r[8]!='NULL' and r[6]<=r[8] for r in fields(final['tasks']))
    assert Counter(r[2] for r in fields(final['units']))==({'PACKED':10} if branch=='R' else {'PACKED':9,'SCRAPPED':1})
    sn=passed['sn'][2]['snId'];ins=fields(final['inspection'])
    bad=[r for r in ins if r[0]==str(sn)]
    assert [(r[1],r[3]) for r in bad]==([('1','REJECT'),('2','ACCEPT')] if branch=='R' else [('1','REJECT')])
    assert bad[0][5]=='NULL' and (branch!='R' or bad[1][5]!='NULL')
    assert len(final['pack'])==good and len(final['disposition'])==1
    result={'code':code,'workorderId':passed['workorderId'],'badSn':passed['sn'][2]['snCode'],
            'materials':dict(zip(batches,[2,3,5])),'sourceAfterIssue':[5,5,5],'lineSideAfterReport':[0,0,0],
            'qualified':good,'packed':good,'scrapped':10-good,'inspections':len(ins),'reworkTasks':len(final['tasks'])-3,
            'affectedSNs':[r['snCode'] for r in passed['sn'][2:5]],'sha256Evidence':hashlib.sha256(file.read_bytes()).hexdigest()}
    return result


def audit_source():
    # Compare historic copied B05 order facts with the unchanged source after B06.
    statements={
        'workorder':'SELECT * FROM pro_workorder WHERE workorder_id=991000058',
        'task':'SELECT * FROM pro_task WHERE workorder_id=991000058 ORDER BY task_id',
        'execution_order':'SELECT * FROM pro_execution_order WHERE workorder_id=991000058',
        'execution_task':'SELECT * FROM pro_execution_task WHERE workorder_id=991000058 ORDER BY task_id',
        'unit':'SELECT * FROM pro_execution_unit WHERE workorder_id=991000058 ORDER BY sn_id',
        'input':'SELECT i.* FROM pro_execution_input i JOIN pro_execution_unit u ON i.sn_id=u.sn_id WHERE u.workorder_id=991000058 ORDER BY i.input_id',
        'inspection':'SELECT i.* FROM pro_execution_inspection i JOIN pro_execution_unit u ON i.sn_id=u.sn_id WHERE u.workorder_id=991000058 ORDER BY i.inspection_id',
        'disposition':'SELECT i.* FROM pro_execution_disposition i JOIN pro_execution_unit u ON i.sn_id=u.sn_id WHERE u.workorder_id=991000058 ORDER BY i.disposition_id',
        'pack':'SELECT i.* FROM pro_execution_pack i JOIN pro_execution_unit u ON i.sn_id=u.sn_id WHERE u.workorder_id=991000058 ORDER BY i.sn_id',
        'transaction':'SELECT * FROM wm_transaction WHERE workorder_id=991000058 ORDER BY transaction_id',
    }
    hashes={}
    for key,statement in statements.items():
        source=sql(SOURCE,statement+';');target=sql(TARGET,statement+';')
        assert source==target,key
        hashes[key]={'rows':len(source.splitlines()),'sha256':hashlib.sha256(source.encode()).hexdigest()}
    return hashes


if __name__=='__main__':
    report={'cases':[audit_case('R'),audit_case('S')],
            'historicCopiedB05OrderFactsEqualSourceAfterB06':audit_source(),
            'sourceDatabase':SOURCE,'targetDatabase':TARGET,
            'note':'Historic workorder facts compared to read-only source after B06; not an assertion that all preexisting tables have an opening snapshot.'}
    assert set(report['cases'][0]['affectedSNs']).isdisjoint(report['cases'][1]['affectedSNs'])
    out=ROOT.parent/'docs'/'ktg完善文档'/'证据'/'MES-B06'
    out.mkdir(parents=True,exist_ok=True)
    (out/'closure-database-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Both evidence cases and copied original order independently verified.')
