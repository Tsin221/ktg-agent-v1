"""Real HTTP closure verification. No mutations via SQL.

Run after the separately approved fixture/runtime setup. Every business
action uses ordinary-role auth, is recorded without credentials, and is
verified against new workorder-specific database facts. A failed run is
preserved: choose a new unique --run-id only after auditing it.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
import hashlib
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from execution_closure_fixture import RUN, TARGET, sql

BASE = 'http://127.0.0.1:18088'
EX = '/mes/pro/execution'
ITEM = 33
PRODUCT = 32


class Case:
    def __init__(self, run_id, branch, resume=False):
        self.branch = branch
        self.code = f'B06-{run_id}-{branch}'
        self.batches = [f'{self.code}-B0{i}' for i in (6, 7, 8)]
        self.file = RUN / f'{self.code}-evidence.jsonl'
        if resume:
            assert self.file.exists()
        else:
            self.file.touch(exist_ok=False)
        self.credentials = json.loads((RUN / 'credentials.json').read_text(encoding='utf-8'))
        self.tokens = {}
        self.user_ids = {}
        self.leader = 'leader_r' if branch == 'R' else 'leader_s'
        launch=json.loads((RUN/'backend-pid.json').read_text(encoding='utf-8-sig'))
        jar=Path(launch['jar'])
        assert jar.is_file()
        self.record({'runtimeJar':jar.name,'sha256':hashlib.sha256(jar.read_bytes()).hexdigest()})

    def configure_sop(self):
        sops=self.req(f'/mes/md/sop/list?itemId={PRODUCT}',self.leader)['rows']
        if not any(x['sopTitle']=='B06 acceptance assembly SOP' for x in sops):
            self.req('/mes/md/sop',self.leader,{'itemId':PRODUCT,'orderNum':1,'processId':68,
                     'processCode':'B02-LIVE-1','processName':'B02 ASSEMBLY','sopTitle':'B06 acceptance assembly SOP',
                     'sopDescription':'Verify part batch before assembly; record the actual batch and SN group for each piece.'})

    def record(self, value):
        with self.file.open('a', encoding='utf-8') as out:
            out.write(json.dumps(value, ensure_ascii=False) + '\n')

    def req(self, path, role=None, body=None, method=None, expected=200, headers=None):
        h = {'Content-Type': 'application/json', **(headers or {})}
        if role:
            h['Authorization'] = 'Bearer ' + self.tokens[role]
        req = urllib.request.Request(BASE + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as error:
            resp = error
        with resp as f:
            result = json.load(f)
        if path not in ('/login', '/getInfo'):
            self.record({'role': role, 'method': req.get_method(), 'path': path,
                         'request': body, 'operationHeaders': headers, 'response': result})
        if not isinstance(expected, tuple):
            expected = (expected,)
        assert result.get('code') in expected, (path, result)
        return result

    def login(self, role):
        body = self.credentials['accounts'][role]
        self.tokens[role] = self.req('/login', body=body)['token']
        info = self.req('/getInfo', role)
        assert '*:*:*' not in info['permissions'] and 'mes:pro:execution:manageAll' not in info['permissions'], role
        self.user_ids[role] = info['user']['userId']
        self.record({'role': role, 'userId': self.user_ids[role], 'permissions': info['permissions']})

    def action(self, path, role, body=None, expected=200, op_id=None):
        payload = {'operationId': op_id or str(uuid.uuid4()), **(body or {})}
        return payload, self.req(EX + path, role, payload, expected=expected)

    def detail(self):
        return self.req(EX + f'/workorders/{self.order}', self.leader)['data']

    def progress(self, **expected):
        detail = self.detail()
        for field, value in expected.items():
            assert detail['progress'][field] == value, (field, detail['progress'])
        return detail

    def readback(self, stage):
        o = self.order
        batch_set = ','.join("'" + b + "'" for b in self.batches)
        statements = {
            'workorder': f'SELECT workorder_id,status,version,quantity_scheduled,quantity_produced FROM pro_workorder WHERE workorder_id={o}',
            'tasks': f'SELECT t.task_id,e.step_kind,e.task_kind,t.status,e.assigned_user_id,e.started_by,e.actual_started_at,e.ended_by,e.actual_ended_at FROM pro_task t JOIN pro_execution_task e ON t.task_id=e.task_id WHERE t.workorder_id={o} ORDER BY t.task_id',
            'units': f'SELECT u.sn_id,s.sn_code,u.unit_state,u.version FROM pro_execution_unit u JOIN wm_sn s ON u.sn_id=s.sn_id WHERE u.workorder_id={o} ORDER BY u.sn_id',
            'inputs': f'SELECT i.sn_id,i.item_id,i.source_batch_code,i.quantity,i.consume_line_id,i.feedback_id FROM pro_execution_input i JOIN pro_execution_unit u ON i.sn_id=u.sn_id WHERE u.workorder_id={o} ORDER BY i.sn_id',
            'inspection': f'SELECT i.sn_id,i.round_no,i.ipqc_id,q.check_result,i.source_disposition_id,i.produce_line_id FROM pro_execution_inspection i JOIN qc_ipqc q ON i.ipqc_id=q.ipqc_id JOIN pro_execution_unit u ON i.sn_id=u.sn_id WHERE u.workorder_id={o} ORDER BY i.sn_id,i.round_no',
            'disposition': f'SELECT d.* FROM pro_execution_disposition d JOIN pro_execution_unit u ON d.sn_id=u.sn_id WHERE u.workorder_id={o}',
            'pack': f'SELECT p.* FROM pro_execution_pack p JOIN pro_execution_unit u ON p.sn_id=u.sn_id WHERE u.workorder_id={o} ORDER BY p.sn_id',
            'transactions': f'SELECT transaction_id,transaction_type,item_id,batch_code,transaction_quantity,warehouse_code,source_doc_id,source_doc_line_id FROM wm_transaction WHERE workorder_id={o} ORDER BY transaction_id',
            'stock': f'SELECT material_stock_id,item_id,batch_code,quantity_onhand,warehouse_id FROM wm_material_stock WHERE (item_id={ITEM} AND batch_code IN ({batch_set})) OR (item_id={PRODUCT} AND batch_code=\'{self.code}-FG\') ORDER BY material_stock_id',
            'operations': f'SELECT action_type,COUNT(*) FROM pro_execution_operation WHERE workorder_id={o} GROUP BY action_type ORDER BY action_type',
        }
        facts = {k: sql(TARGET, v + ';').splitlines() for k, v in statements.items()}
        self.record({'stage': stage, 'database': TARGET, 'facts': facts})
        return facts

    def new_draft(self):
        for role in self.credentials['accounts']:
            self.login(role)
        self.configure_sop()
        existing = self.req('/mes/pro/workorder/list?workorderCode=' + self.code, self.leader)
        assert existing['total'] == 0
        self.req('/mes/pro/workorder', self.leader, {
            'workorderCode': self.code, 'workorderName': self.code + ' isolated case',
            'workorderType': 'SELF', 'orderSource': 'STORE', 'productId': PRODUCT,
            'batchCode': self.code + '-FG', 'quantity': 10,
            'requestDate': (date.today() + timedelta(days=7)).isoformat()})
        draft = self.req('/mes/pro/workorder/list?workorderCode=' + self.code, self.leader)['rows'][0]
        self.order = draft['workorderId']
        assert draft['status'] == 'PREPARE' and draft['version'] == 0
        # Opening balances through the actual receipt workflow, not stock CRUD.
        template=self.req('/mes/wm/itemrecpt/930001',self.leader)['data']
        receipt_code=self.code+'-RECEIPT'
        location={'warehouseId':930001,'locationId':930001,'areaId':930001}
        header={k: template.get(k) for k in ['vendorId','vendorCode','vendorName','vendorNick']}
        self.req('/mes/wm/itemrecpt',self.leader,{**header,**location,'recptCode':receipt_code,
                 'recptName':receipt_code,'recptDate':date.today().isoformat()+' 00:00:00','status':'PREPARE'})
        receipt=self.req('/mes/wm/itemrecpt/list?recptCode='+receipt_code,self.leader)['rows'][0]['recptId']
        for batch, amount in zip(self.batches, (7, 8, 10)):
            self.req('/mes/wm/itemrecptline',self.leader,{**location,'recptId':receipt,'itemId':ITEM,
                     'itemCode':'B02-LIVE-M','itemName':'B02 material','unitOfMeasure':'EA','unitName':'EA',
                     'quantityRecived':amount,'batchCode':batch,'iqcCheck':'N'})
        ep=f'/mes/wm/itemrecpt/{receipt}'
        for state in ('APPROVING','APPROVED'):
            current=self.req(ep,self.leader)['data']
            self.req('/mes/wm/itemrecpt',self.leader,{'recptId':receipt,'version':current['version'],'status':state},method='PUT')
        preview=self.req(ep+'/execution-preview',self.leader)['data']
        assert len(preview['additions'])==3
        version=self.req(ep,self.leader)['data']['version']
        self.req(ep,self.leader,method='PUT',headers={'X-Operation-Id':str(uuid.uuid4()),'X-Document-Version':str(version)})
        assert self.req(ep,self.leader)['data']['status']=='FINISHED'
        before = self.readback('before-release')
        assert not before['units'] and not before['transactions'] and len(before['stock']) == 3
        self.req(EX + f'/workorders/{self.order}/release-options', self.leader)
        payload = {'expectedVersion': 0, 'routeId': 28, 'steps': [
            {'routeProcessId': 79, 'workstationId': 81},
            {'routeProcessId': 80, 'workstationId': 82, 'templateId': 27, 'ipqcType': 'FINAL'},
            {'routeProcessId': 81, 'workstationId': 83}]}
        self.action(f'/workorders/{self.order}/releases', self.leader,
                    {**payload, 'steps': [{**payload['steps'][0], 'workstationId': 82}, *payload['steps'][1:]]}, (400,409))
        op = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.action(f'/workorders/{self.order}/releases', self.leader, payload, op_id=op)[1], range(2)))
        assert results[0] == results[1]
        detail = self.detail()
        assert detail['order']['status'] == 'CONFIRMED'
        assert len(detail['tasks']) == 3 and len(detail['units']) == 10
        assert all(x['unitState']=='WAIT_ASSEMBLY' for x in detail['units'])
        self.tasks = {t['stepKind']: t for t in detail['tasks']}
        self.sns = sorted(detail['units'], key=lambda x: x['snCode'])
        self.ids = [x['snId'] for x in self.sns]
        self.bad = self.ids[2]
        after = self.readback('release')
        assert after['stock'] == before['stock'] and after['transactions'] == before['transactions']

    def assign(self, task, role):
        options = self.req(EX + f'/tasks/{task}/assignment-options', self.leader)['data']
        assert any(x['userId'] == self.user_ids[role] for x in options)
        self.action(f'/tasks/{task}/assignments', self.leader, {'assignedUserId': self.user_ids[role]})

    def issue_and_report(self):
        assembly = self.tasks['ASSEMBLY']['taskId']
        header = {'issueCode': self.code + '-ISSUE', 'issueName': self.code, 'workorderId': self.order,
                  'workorderCode': self.code, 'taskId': assembly, 'taskCode': self.tasks['ASSEMBLY']['taskCode'],
                  'workstationId': 81, 'status': 'PREPARE', 'warehouseId': 930001, 'locationId': 930001,
                  'areaId': 930001, 'issueDate': date.today().isoformat()}
        self.req('/mes/wm/issueheader', self.leader, header)
        issued = self.req('/mes/wm/issueheader/list?issueCode=' + header['issueCode'], self.leader)['rows']
        assert len(issued) == 1
        issue = issued[0]['issueId']
        stock = self.req(f'/mes/wm/wmstock/list?itemId={ITEM}&pageSize=100', self.leader)['rows']
        for batch, qty in zip(self.batches, [2, 3, 5]):
            rows = [x for x in stock if x['batchCode'] == batch and x['warehouseId'] == 930001]
            assert len(rows)==1
            self.req('/mes/wm/issueline', self.leader, {'issueId':issue, 'materialStockId':rows[0]['materialStockId'],
                     'itemId':ITEM, 'itemCode':'B02-LIVE-M', 'itemName':'B02 material','unitOfMeasure':'EA',
                     'batchCode':batch, 'quantityIssued':qty, 'warehouseId':930001,'locationId':930001,'areaId':930001})
        ep = f'/mes/wm/issueheader/{issue}'
        for state in ('APPROVING','APPROVED'):
            detail = self.req(ep, self.leader)['data']
            self.req('/mes/wm/issueheader', self.leader, {'issueId':issue,'issueCode':header['issueCode'],
                     'version':detail['version'],'status':state}, method='PUT')
        self.req(ep + '/execution-preview', self.leader)
        version = self.req(ep,self.leader)['data']['version']
        self.req(ep,self.leader,method='PUT',headers={'X-Operation-Id':str(uuid.uuid4()),'X-Document-Version':str(version)})
        assert self.req(ep,self.leader)['data']['status']=='FINISHED'
        self.assign(assembly,'assembler')
        self.assign(self.tasks['INSPECTION']['taskId'],'inspector')
        self.assign(self.tasks['PACKING']['taskId'],'packer')
        before = self.readback('issued')
        self.action(f'/tasks/{assembly}/starts','assembler')
        self.report(before)

    def resume_after_issue(self, reported=False):
        for role in self.credentials['accounts']:
            self.login(role)
        self.configure_sop()
        matches=self.req('/mes/pro/workorder/list?workorderCode='+self.code,self.leader)['rows']
        assert len(matches)==1
        self.order=matches[0]['workorderId']
        detail=self.detail()
        assert detail['order']['status']=='CONFIRMED' and detail['progress']['assembled']==(10 if reported else 0)
        self.tasks={t['stepKind']:t for t in detail['tasks']}
        assert self.tasks['ASSEMBLY']['status']=='STARTED'
        self.sns=sorted(detail['units'],key=lambda x:x['snCode'])
        self.ids=[x['snId'] for x in self.sns];self.bad=self.ids[2]
        before=self.readback('resume-after-issue')
        if reported:
            assert len(before['transactions'])==9 and len(before['inputs'])==10 and not before['inspection']
            self.action(f"/tasks/{self.tasks['ASSEMBLY']['taskId']}/completions",'assembler')
            self.progress(assembled=10,qualified=0,waitingInspection=10)
        else:
            assert len(before['transactions'])==6 and not before['inputs']
            self.report(before)

    def report(self,before):
        assembly=self.tasks['ASSEMBLY']['taskId']
        task=self.req(EX+f'/tasks/{assembly}','assembler')['data']
        assert any(x['sopTitle']=='B06 acceptance assembly SOP' for x in task['productSops'])
        rows = self.req(EX+f'/tasks/{assembly}/available-materials?pageSize=100','assembler')['rows']
        groups = [{'snIds': self.ids[a:b], 'materialStockId': next(r['materialStockId'] for r in rows if r['sourceBatchCode']==batch),
                   'quantityPerUnit': 1} for a,b,batch in [(0,2,self.batches[0]),(2,5,self.batches[1]),(5,10,self.batches[2])]]
        payload, result = self.action(f'/tasks/{assembly}/reports','assembler',{'snIds':self.ids,'inputs':groups})
        assert self.req(EX+f'/tasks/{assembly}/reports','assembler',payload)==result
        self.action(f'/tasks/{assembly}/reports','assembler',{'snIds':self.ids,'inputs':groups},(404,409))
        self.action(f'/tasks/{assembly}/completions','assembler')
        self.progress(assembled=10, qualified=0, waitingInspection=10)
        after = self.readback('reported')
        assert len(after['inputs'])==10 and len(after['inspection'])==0
        assert [x.split('\t')[2] for x in after['inputs']] == [self.batches[0]]*2+[self.batches[1]]*3+[self.batches[2]]*5
        assert len([x for x in after['transactions'] if '\tITEM_CONSUME\t' in x])==3
        assert not [x for x in after['transactions'] if '\tPRODUCT_PRODUCE\t' in x]
        assert [x for x in after['stock'] if x.endswith('\t930001')]==[x for x in before['stock'] if x.endswith('\t930001')]

    def inspect_and_pack(self):
        inspect = self.tasks['INSPECTION']['taskId']; pack = self.tasks['PACKING']['taskId']
        self.action(f'/tasks/{inspect}/starts','inspector')
        for sn in self.ids:
            idx = {'indexId':1,'value':12 if sn==self.bad else 10}
            if sn==self.bad: idx.update(defectId=1,reason='Measurement outside 9..11 mm limits')
            body = {'taskId':inspect,'indices':[idx]}
            self.action(f'/units/{sn}/inspections','inspector',body)
        self.progress(qualified=9, pendingDisposition=1, packed=0, scrapped=0)
        initial = self.readback('first-inspections')
        assert len(initial['inspection'])==10 and len([x for x in initial['transactions'] if '\tPRODUCT_PRODUCE\t' in x])==9
        self.action(f'/tasks/{pack}/starts','packer')
        nine = [sn for sn in self.ids if sn!=self.bad]
        self.action(f'/tasks/{pack}/packages','packer',{'snIds':[self.bad,nine[0]]},409)
        first, result = self.action(f'/tasks/{pack}/packages','packer',{'snIds':nine})
        assert self.req(EX+f'/tasks/{pack}/packages','packer',first)==result
        self.action(f'/tasks/{pack}/packages','packer',{'snIds':nine},409)
        version=self.detail()['order']['version']
        self.action(f'/workorders/{self.order}/completions',self.leader,{'version':version},409)
        self.action(f'/tasks/{pack}/completions','packer',expected=409)
        packed=self.readback('packed-nine-and-rejections')
        assert initial['stock']==packed['stock'] and initial['transactions']==packed['transactions']
        history = self.req(EX+f'/units/{self.bad}/inspections',self.leader)['data']
        assert len(history)==1
        self.action(f'/units/{self.bad}/inspections','inspector',{'taskId':inspect,'indices':[{'indexId':1,'value':10}]},409)
        decision={'inspectionId':history[0]['inspectionId'],'decision':'REWORK' if self.branch=='R' else 'SCRAP','reason':'B06 controlled decision'}
        _, response = self.action(f'/units/{self.bad}/dispositions',self.leader,decision)
        self.action(f'/units/{self.bad}/dispositions',self.leader,decision,409)
        self.finish(response['data']['resultRef'].get('createdTaskId'))

    def resume_rework(self):
        for role in self.credentials['accounts']:
            self.login(role)
        matches=self.req('/mes/pro/workorder/list?workorderCode='+self.code,self.leader)['rows']
        assert len(matches)==1
        self.order=matches[0]['workorderId'];detail=self.detail()
        assert detail['order']['status']=='CONFIRMED' and detail['progress']['packed']==9
        self.tasks={t['stepKind']:t for t in detail['tasks'] if t['taskKind']=='NORMAL'}
        self.sns=sorted(detail['units'],key=lambda x:x['snCode'])
        self.ids=[x['snId'] for x in self.sns];self.bad=self.ids[2]
        assert self.sns[2]['unitState']=='REWORK_IN_PROGRESS'
        facts=self.readback('resume-rework')
        assert len(facts['inspection'])==10 and len(facts['disposition'])==1 and len(facts['pack'])==9
        rework=next(t['taskId'] for t in detail['tasks'] if t['taskKind']=='REWORK' and t['status']=='READY')
        self.finish(rework)

    def finish(self,rework):
        inspect = self.tasks['INSPECTION']['taskId']; pack = self.tasks['PACKING']['taskId']
        if self.branch=='R':
            self.action(f'/units/{self.bad}/inspections','inspector',{'taskId':inspect,'indices':[{'indexId':1,'value':10}]},(404,409))
            self.assign(rework,'reworker')
            self.action(f'/tasks/{rework}/starts','reworker')
            self.action(f'/tasks/{rework}/completions','reworker')
            self.action(f'/units/{self.bad}/inspections','inspector',{'taskId':inspect,'indices':[{'indexId':1,'value':10}]})
            self.action(f'/units/{self.bad}/inspections','inspector',{'taskId':inspect,'indices':[{'indexId':1,'value':10}]},(404,409))
            self.action(f'/tasks/{pack}/packages','packer',{'snIds':[self.bad]})
        else:
            self.action(f'/tasks/{pack}/packages','packer',{'snIds':[self.bad]},409)
        self.action(f'/tasks/{inspect}/completions','inspector')
        self.action(f'/tasks/{pack}/completions','packer')
        version=self.detail()['order']['version']
        first,result=self.action(f'/workorders/{self.order}/completions',self.leader,{'version':version})
        assert self.req(EX+f'/workorders/{self.order}/completions',self.leader,first)==result
        good=10 if self.branch=='R' else 9
        final=self.progress(packed=good,qualified=good,scrapped=10-good,pendingDisposition=0)
        assert final['order']['status']=='FINISHED' and all(t['status']=='FINISHED' for t in final['tasks'])
        facts=self.readback('final')
        assert len(facts['pack'])==good and len(facts['inspection'])==(11 if self.branch=='R' else 10)
        assert len(facts['disposition'])==1 and len(facts['inputs'])==10
        assert len([x for x in facts['transactions'] if '\tPRODUCT_PRODUCE\t' in x])==good
        self.record({'passed':True,'case':self.branch,'workorderId':self.order,'sn':self.sns,'batches':self.batches})
        print(self.code, self.order, 'PASSED closure')

    def trace(self, exact_case=False):
        for role in (self.leader,'observer','leader_s' if self.branch=='R' else 'leader_r','packer'):
            self.login(role)
        self.order=self.req('/mes/pro/workorder/list?workorderCode='+self.code,self.leader)['rows'][0]['workorderId']
        detail=self.detail()
        assert detail['order']['status']=='FINISHED'
        sns=sorted(detail['units'],key=lambda x:x['snCode'])
        sn=sns[2]['snCode']
        q=urllib.parse.urlencode({'snCode':sn})
        unit=EX+'/units/trace?'+q
        traced=self.req(unit,self.leader)['data']
        assert traced['traceStatus']=='TRACKED' and traced['snCode']==sn
        assert len(traced['inputs'])==1 and traced['inputs'][0]['sourceBatchCode']==self.batches[1]
        assert len(traced['inspections'])==(2 if self.branch=='R' else 1)
        assert traced['inspections'][0]['roundNo']==1 and traced['inspections'][0]['checkResult']=='REJECT'
        assert len(traced['dispositions'])==1
        assert traced['dispositions'][0]['decision']==('REWORK' if self.branch=='R' else 'SCRAP')
        if self.branch=='R':
            assert traced['inspections'][1]['roundNo']==2 and traced['inspections'][1]['checkResult']=='ACCEPT'
            assert traced['packing'] and traced['inspections'][1]['produceTransactions']
        else:
            assert traced['packing'] is None and not traced['inspections'][0]['produceTransactions']
        assert traced['inputs'][0]['transactions'] and traced['assemblyFeedback']
        def material(role,item=ITEM,batch=None,page=1,size=2):
            query=urllib.parse.urlencode({'itemId':item,'batchCode':batch or self.batches[1],'pageNum':page,'pageSize':size})
            return self.req(EX+'/materials/trace?'+query,role)
        one=material(self.leader);two=material(self.leader,page=2)
        assert one['total']==two['total']==3 and len(one['rows'])==2 and len(two['rows'])==1
        assert {x['snCode'] for x in one['rows']+two['rows']}=={x['snCode'] for x in sns[2:5]}
        assert material(self.leader,item=PRODUCT)['total']==0
        if exact_case:
            assert material(self.leader,batch=self.batches[1].lower())['total']==0
        assert material('observer')['total']==0
        other='leader_s' if self.branch=='R' else 'leader_r'
        assert material(other)['total']==0
        self.req(unit,'observer',expected=404);self.req(unit,other,expected=404)
        self.req(unit,'packer',expected=403)
        self.req(EX+'/units/trace?snCode=NO-SUCH-B06-SN',self.leader,expected=404)
        self.record({'tracePassed':True,'workorderId':self.order,'case':self.branch,'snCode':sn,
                     'itemId':ITEM,'batchCode':self.batches[1],'affected':[x['snCode'] for x in sns[2:5]]})
        print(self.code, self.order, 'PASSED trace')

    def legacy(self):
        self.login('leader_r');self.login('leader_s')
        batch='B06-LEGACY-IDENTITY'
        old=self.req('/mes/wm/sn/list?batchCode='+batch,'leader_r')['rows']
        if not old:
            self.req('/mes/wm/sn','leader_r',{'itemId':PRODUCT,'itemCode':'B02-LIVE-P',
                     'itemName':'B02 product','unitOfMeasure':'EA','batchCode':batch,'snNum':1})
            old=self.req('/mes/wm/sn/list?batchCode='+batch,'leader_r')['rows']
        assert len(old)==1 and old[0]['batchCode']==batch
        sn=old[0]['snCode']
        unit=EX+'/units/trace?'+urllib.parse.urlencode({'snCode':sn})
        row=self.req(unit,'leader_r')['data']
        assert row['traceStatus']=='LEGACY_NO_RECORD' and not row['inputs'] and not row['inspections'] and row['packing'] is None
        self.req(unit,'leader_s',expected=404)
        self.record({'legacyPassed':True,'snCode':sn,'withoutExecution':True})
        print('Legacy SN generated by old API, no fabricated execution history, scope verified.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-isolated-b06',action='store_true',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--case',choices=['R','S'],required=True)
    parser.add_argument('--resume-report',action='store_true')
    parser.add_argument('--resume-after-report',action='store_true')
    parser.add_argument('--resume-rework',action='store_true')
    parser.add_argument('--trace-only',action='store_true')
    parser.add_argument('--exact-batch-case',action='store_true')
    parser.add_argument('--legacy-only',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch('[A-Z0-9]{3,12}',args.run_id): raise ValueError('unsafe run identifier')
    if sql(TARGET,'SELECT DATABASE();')!=TARGET: raise RuntimeError('unexpected database')
    case=Case(args.run_id,args.case,args.resume_report or args.resume_after_report or args.resume_rework or args.trace_only or args.legacy_only)
    if args.legacy_only:
        case.legacy();return
    if args.trace_only:
        case.trace(args.exact_batch_case);return
    if args.resume_rework:
        case.resume_rework();return
    if args.resume_report or args.resume_after_report:
        case.resume_after_issue(reported=args.resume_after_report)
    else:
        case.new_draft();case.issue_and_report()
    case.inspect_and_pack()


if __name__=='__main__':
    main()
