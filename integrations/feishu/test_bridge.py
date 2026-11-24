import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import Adapter, Store, parse_message, result_card, process_one


def event(mid='om_one', user='ou_a', chat='oc_a', **changes):
    message = {'message_id': mid, 'chat_id': chat, 'chat_type': 'p2p',
               'message_type': 'text', 'create_time': str(int(time.time()*1000)),
               'content': json.dumps({'text': '查询维修单 REP-001'})}
    message.update(changes)
    return {'header': {'app_id': 'cli_demo', 'tenant_key': 'tenant_a'},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': user}},
                      'message': message}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(__file__).resolve().parents[2]/'.runtime/feishu/tests'
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.path = Path(self.tmp.name)/'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        assert Path(self.tmp.name).resolve().is_relative_to(self.temp_root.resolve())
        self.tmp.cleanup()

    def test_rejects_wrong_app_group_bot_and_old_events(self):
        e=event(); self.assertIsNone(parse_message(e, 'another_app'))
        self.assertIsNone(parse_message(event(chat_type='group'), 'cli_demo'))
        self.assertIsNone(parse_message(event(create_time='1'), 'cli_demo'))
        e['event']['sender']['sender_type']='app'
        self.assertIsNone(parse_message(e, 'cli_demo'))

    def test_rejects_malformed_or_oversized_content(self):
        self.assertIsNone(parse_message(event(content='not JSON'), 'cli_demo'))
        self.assertIsNone(parse_message(event(content=json.dumps({'text':'x'*4001})), 'cli_demo'))

    def test_identity_is_separate_for_each_user_and_tenant(self):
        a=parse_message(event(), 'cli_demo')
        b=parse_message(event(user='ou_b'), 'cli_demo')
        e=event();e['header']['tenant_key']='tenant_b'
        c=parse_message(e,'cli_demo')
        self.assertEqual(len({a['owner'],b['owner'],c['owner']}),3)

    def test_event_dedup_survives_restart(self):
        job=parse_message(event(), 'cli_demo')
        self.assertTrue(self.store.enqueue(job))
        self.assertFalse(Store(self.path).enqueue(job))

    def test_queue_is_bounded(self):
        for n in range(100):self.assertTrue(self.store.enqueue(parse_message(event(mid=f'om_{n}',user=f'ou_{n}'),'cli_demo')))
        self.assertFalse(self.store.enqueue(parse_message(event(mid='om_full'),'cli_demo')))

    def test_model_failure_returns_safe_card(self):
        self.store.enqueue(parse_message(event(),'cli_demo'))
        sent=[]
        def query(*args):raise RuntimeError('SECRET_DO_NOT_DISCLOSE')
        process_one(self.store,query,lambda mid,card:sent.append(card),'http://example.test/experience')
        self.assertNotIn('SECRET_DO_NOT_DISCLOSE',json.dumps(sent))
        self.assertEqual(len(sent),1)

    def test_reply_retry_does_not_repeat_model_query(self):
        self.store.enqueue(parse_message(event(),'cli_demo'))
        queries=[];sent=[]
        def query(*args):queries.append(1);return {'answer':'REP-001 待处理','results':[]}
        def failed_reply(*args):raise RuntimeError('transport timeout')
        process_one(self.store,query,failed_reply,'http://example.test/experience')
        process_one(self.store,query,lambda mid,card:sent.append(card),'http://example.test/experience')
        self.assertEqual(len(queries),1);self.assertEqual(len(sent),1)

    def test_interrupted_queries_are_not_replayed_after_restart(self):
        self.store.enqueue(parse_message(event(),'cli_demo'));self.store.claim()
        restored=Store(self.path);calls=[];sent=[]
        process_one(restored,lambda *a:calls.append(1),lambda mid,card:sent.append(card),'http://example.test/experience')
        self.assertFalse(calls);self.assertEqual(len(sent),1)

    def test_card_cannot_turn_answer_into_actions_or_mentions(self):
        card=result_card({'answer':'<at id=all> [click](https://bad.example)','results':[]},'http://example.test/experience')
        def walk(value):
            if isinstance(value,dict):
                yield value
                for child in value.values():yield from walk(child)
            elif isinstance(value,list):
                for child in value:yield from walk(child)
        parts=list(walk(card))
        self.assertNotIn('markdown',[p.get('tag') for p in parts])
        self.assertEqual([p['url'] for p in parts if 'url' in p],['http://example.test/experience'])

    def test_business_repair_reply_shows_only_agent_conclusion(self):
        repair={'repairId':1,'repairCode':'REP-001','machineryCode':'DEV-001','machineryName':'一号装配机',
                'status':'FINISHED','repairResult':'SUCCESS','requireDate':'2026-11-11',
                'finishDate':'2026-11-12','confirmDate':'2026-11-13'}
        answer='维修完成，连接件已紧固。'
        card=result_card({'answer':answer,'results':[
            {'kind':'dv_repair_detail','repair':repair},
            {'kind':'dv_repair_lines','repair':repair,'rows':[{'repairId':1,'malfunction':'连接件异响','repairDes':'紧固后试运行正常'}],'total':1}]},'http://example.test')
        raw=json.dumps(card,ensure_ascii=False)
        self.assertEqual(card['elements'][0]['text']['content'],answer)
        self.assertNotIn('column_set',raw)
        self.assertNotIn('11-13',raw)
        self.assertIn('打开 MES',raw)

    def test_final_answer_is_not_overridden_by_empty_intermediate_query(self):
        answer='6204 轴承规格为 20×47×14 mm。'
        card=result_card({'answer':answer,'results':[
            {'kind':'product_list','rows':[],'total':0},
            {'kind':'exploration_search','records':[{'name':'6204'}]}]},'http://example.test')
        self.assertEqual(card['elements'][0]['text']['content'],answer)
        self.assertNotIn('未找到记录',card['header']['title']['content'])

    def test_long_answer_is_shortened_and_keeps_mes_link(self):
        answer='查询结论：'+'详细说明。'*100
        card=result_card({'answer':answer,'results':[]},'http://example.test')
        summary=card['elements'][0]['text']['content']
        self.assertTrue(summary.startswith('查询结论：'))
        self.assertLess(len(summary),len(answer))
        self.assertIn('…',summary)
        self.assertIn('打开 MES',json.dumps(card,ensure_ascii=False))

    def test_business_reply_does_not_append_raw_records(self):
        card=result_card({'answer':'维修成功，已验收','results':[{'kind':'dv_repair_detail','repair':{
            'repairId':1,'repairCode':'REP-001','status':'PREPARE'}}]},'http://example.test')
        self.assertEqual(card['elements'][0]['text']['content'],'维修成功，已验收')
        raw=json.dumps({'header':card['header'],'elements':card['elements'][1:]},ensure_ascii=False)
        self.assertNotIn('REP-001',raw)
        self.assertNotIn('维修成功',raw);self.assertNotIn('已验收',raw)

    def test_repair_lines_do_not_leak_between_records(self):
        repair={'repairId':1,'repairCode':'REP-001'}
        card=result_card({'results':[{'kind':'dv_repair_lines','repair':repair,'total':1,
            'rows':[{'repairId':2,'malfunction':'其他单据的故障'}]}]},'http://example.test')
        self.assertNotIn('其他单据的故障',json.dumps(card,ensure_ascii=False))

    def test_ordinary_lists_do_not_expose_raw_data(self):
        rows=[{'machineryCode':f'DEV-{n:03}','machineryName':f'设备{n}','status':'WORKING','password':'never expose'} for n in range(7)]
        card=result_card({'answer':'已查到设备。','results':[{'kind':'dv_machinery_list','rows':rows,'total':20}]},'http://example.test')
        raw=json.dumps(card,ensure_ascii=False)
        self.assertIn('已查到设备。',raw)
        self.assertNotIn('DEV-004',raw);self.assertNotIn('never expose',raw)

    def test_query_title_is_shared_by_completed_failed_and_preview_replies(self):
        complete=result_card({'answer':'查询完成','stop_reason':'completed'},'http://example.test')
        failed=result_card({'answer':'查询未完成','stop_reason':'error','error_code':'TEST_ERROR'},'http://example.test')
        preview=result_card({'answer':'已生成预览，未提交','results':[{'kind':'workorder_preview'}]},'http://example.test')
        self.assertEqual(complete['header'],failed['header'])
        self.assertEqual(complete['header'],preview['header'])
        self.assertEqual(failed['elements'][0]['text']['content'],'查询未完成')
        self.assertEqual(preview['elements'][0]['text']['content'],'已生成预览，未提交')

    def test_adapter_reuses_one_session_per_owner_without_confirm_endpoints(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        calls=[];tokens=[];sessions=[];runs={}
        def fake(url,body=None,token=None,timeout=20):
            calls.append((url,body,token))
            if url.endswith('/login'):
                t='fake-token-'+str(len(tokens));tokens.append(t);return {'code':200,'token':t}
            if url.endswith('/getInfo'):return {'code':200}
            if url.endswith('/agent-api/v1/sessions'):
                session_id='session-'+str(len(sessions));sessions.append((session_id,token))
                return {'session_id':session_id}
            if '/sessions/' in url and url.endswith('/runs'):
                session_id=url.split('/')[-2]
                self.assertIn((session_id,token),sessions)
                self.assertEqual(body['page_context'],{'response_style':'brief'})
                run_id='run-'+str(len(runs));runs[run_id]=(session_id,token)
                return {'run_id':run_id,'status':'queued'}
            if '/agent-api/v1/runs/' in url:
                run_id=url.rsplit('/',1)[-1];session_id,run_token=runs[run_id]
                self.assertEqual(token,run_token)
                return {'run_id':run_id,'session_id':session_id,'status':'completed',
                        'result_summary':{'answer':'ok','stop_reason':'completed','results':[{'kind':'dv_repair_list','rows':[]}]}}
            self.fail('Unexpected API: '+url)
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(self.store)
        with patch('bridge.http_json',side_effect=fake):
            first=adapter.query('user-a','查询','om_first')
            adapter.query('user-b','查询','om_second')
            adapter.query('user-a','继续问','om_third')
        self.assertEqual(first['answer'],'ok')
        self.assertEqual(first['results'][0]['kind'],'dv_repair_list')
        self.assertEqual(len(tokens),2)
        self.assertEqual(len(sessions),2)
        self.assertEqual([sid for sid,_ in runs.values()],['session-0','session-1','session-0'])
        self.assertNotEqual(self.store.account('user-a'),self.store.account('user-b'))
        self.assertFalse(any('confirm' in url or url.endswith('/query') for url,_,_ in calls))
        request_ids=[body['request_id'] for url,body,_ in calls if url.endswith('/runs')]
        self.assertEqual(len(set(request_ids)),3)

    def test_accepted_run_is_polled_after_restart_without_recreating_it(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        self.store.enqueue(parse_message(event(),'cli_demo'))
        self.store.claim()
        owner=parse_message(event(),'cli_demo')['owner']
        self.store.save_account(owner,'fake-token')
        self.store.save_agent_session(owner,'fake-token','session-1')
        self.store.save_agent_run('om_one',owner,'fake-token','session-1')
        self.store.set_agent_run_id('om_one','run-1')
        restored=Store(self.path)
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(restored)
        calls=[];sent=[]
        def fake(url,body=None,token=None,timeout=20):
            calls.append(url)
            if url.endswith('/getInfo'):return {'code':200}
            self.assertTrue(url.endswith('/runs/run-1'))
            return {'run_id':'run-1','session_id':'session-1','status':'completed',
                    'result_summary':{'answer':'原运行已完成','results':[]}}
        with patch('bridge.http_json',side_effect=fake):
            process_one(restored,adapter.query,lambda mid,card:sent.append(card),'http://example.test')
        self.assertEqual(len(calls),2)
        self.assertIn('原运行已完成',json.dumps(sent,ensure_ascii=False))

    def test_task_list_and_numbered_resume_use_saved_selection(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        self.store.save_account('user-a','fake-token')
        self.store.save_agent_session('user-a','fake-token','session-1')
        calls=[]
        tasks=[{'task_id':'task-a','title':'工单核查','current_version':2},
               {'task_id':'task-b','title':'库存核查','current_version':1}]
        tasks += [{'task_id':f'task-extra-{i}','title':f'旧任务{i}','current_version':1} for i in range(10)]
        def fake(url,body=None,token=None,timeout=20):
            calls.append((url,body))
            self.assertEqual(token,'fake-token')
            if url.endswith('/getInfo'):return {'code':200}
            if url.endswith('/tasks'):return {'items':tasks}
            if '/tasks/' in url:
                task_id=url.rsplit('/',1)[-1]
                task=next(t for t in tasks if t['task_id']==task_id)
                return {**task,'versions':[{'version':task['current_version'],'steps':[
                    {'step_id':'first','description':'查找工单','status':'completed'},
                    {'step_id':'second','description':'查询对应库存','status':'in_progress'}]}]}
            if url.endswith('/sessions/session-1/runs'):
                self.assertEqual(body['task_id'],'task-b')
                self.assertEqual(body['message'],'继续此任务')
                return {'run_id':'run-1','status':'queued'}
            if url.endswith('/runs/run-1'):
                return {'run_id':'run-1','session_id':'session-1','status':'completed',
                        'result_summary':{'answer':'续办完成','results':[]}}
            self.fail('Unexpected API: '+url)
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(self.store)
        with patch('bridge.http_json',side_effect=fake):
            listing=adapter.query('user-a','任务列表','om_list')
            self.assertFalse(any(url.endswith('/runs') for url,_ in calls))
            card=result_card(listing,'http://example.test')
            raw=json.dumps(card,ensure_ascii=False)
            self.assertIn('库存核查',raw)
            self.assertIn('1/2',raw)
            self.assertIn('查询对应库存',raw)
            self.assertNotIn('旧任务8',raw)
            self.assertIn('继续任务 1',raw)
            tasks.reverse()
            adapter.query('user-a','继续任务 2','om_resume')
        self.assertEqual(sum(url.endswith('/tasks') for url,_ in calls),1)

    def test_numbered_task_requires_current_owner_list(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        self.store.save_account('user-a','fake-token')
        self.store.save_agent_session('user-a','fake-token','session-1')
        self.store.save_task_list('user-b','other-token','other-session',['task-private'])
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(self.store)
        with patch('bridge.http_json',return_value={'code':200}) as api:
            result=adapter.query('user-a','继续任务 1','om_resume')
        self.assertIn('任务列表',result['answer'])
        self.assertEqual(api.call_count,1)

    def test_completed_task_is_not_resumed(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        self.store.save_account('user-a','fake-token')
        self.store.save_agent_session('user-a','fake-token','session-1')
        self.store.save_task_list('user-a','fake-token','session-1',['task-done'])
        def fake(url,body=None,token=None,timeout=20):
            if url.endswith('/getInfo'):return {'code':200}
            self.assertTrue(url.endswith('/tasks/task-done'))
            return {'task_id':'task-done','title':'已结束的任务','current_version':1,
                    'versions':[{'version':1,'steps':[{'status':'completed','description':'查询完成'}]}]}
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(self.store)
        with patch('bridge.http_json',side_effect=fake):
            result=adapter.query('user-a','继续任务 1','om_done')
        self.assertIn('已完成',result['answer'])
        self.assertIsNone(self.store.agent_run('om_done'))

    def test_token_rotation_does_not_reuse_previous_session_or_task_list(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        self.store.save_account('user-a','old-token')
        self.store.save_agent_session('user-a','old-token','old-session')
        self.store.save_task_list('user-a','old-token','old-session',['old-task'])
        def fake(url,body=None,token=None,timeout=20):
            if url.endswith('/getInfo'):return {'code':401}
            if url.endswith('/login'):return {'code':200,'token':'new-token'}
            self.assertEqual(token,'new-token')
            self.assertTrue(url.endswith('/agent-api/v1/sessions'))
            return {'session_id':'new-session'}
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(self.store)
        with patch('bridge.http_json',side_effect=fake):
            result=adapter.query('user-a','继续任务 1','om_new')
        self.assertEqual(self.store.agent_session('user-a','new-token'),'new-session')
        self.assertIn('任务列表',result['answer'])
        self.assertIsNone(self.store.agent_run('om_new'))


if __name__=='__main__':unittest.main()
