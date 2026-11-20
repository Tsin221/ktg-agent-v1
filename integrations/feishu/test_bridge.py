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

    def test_repair_card_uses_facts_table_and_recorded_date_flow(self):
        repair={'repairId':1,'repairCode':'REP-001','machineryCode':'DEV-001','machineryName':'一号装配机',
                'status':'FINISHED','repairResult':'SUCCESS','requireDate':'2026-11-11',
                'finishDate':'2026-11-12','confirmDate':'2026-11-13'}
        card=result_card({'answer':'不应显示的长篇说明'*200,'results':[
            {'kind':'dv_repair_detail','repair':repair},
            {'kind':'dv_repair_lines','repair':repair,'rows':[{'repairId':1,'malfunction':'连接件异响','repairDes':'紧固后试运行正常'}],'total':1}]},'http://example.test')
        raw=json.dumps(card,ensure_ascii=False)
        for text in ['REP-001','已完成','连接件异响','紧固后试运行正常','11-11','11-12','11-13','→']:
            self.assertIn(text,raw)
        self.assertNotIn('不应显示',raw)
        self.assertGreaterEqual(sum(e.get('tag')=='column_set' for e in card['elements']),3)

    def test_unqueried_repair_lines_are_not_invented(self):
        card=result_card({'answer':'维修成功，已验收','results':[{'kind':'dv_repair_detail','repair':{
            'repairId':1,'repairCode':'REP-001','status':'PREPARE'}}]},'http://example.test')
        raw=json.dumps(card,ensure_ascii=False)
        self.assertIn('草稿',raw);self.assertIn('未查询',raw)
        self.assertNotIn('维修成功',raw);self.assertNotIn('已验收',raw)

    def test_repair_lines_do_not_leak_between_records(self):
        repair={'repairId':1,'repairCode':'REP-001'}
        card=result_card({'results':[{'kind':'dv_repair_lines','repair':repair,'total':1,
            'rows':[{'repairId':2,'malfunction':'其他单据的故障'}]}]},'http://example.test')
        self.assertNotIn('其他单据的故障',json.dumps(card,ensure_ascii=False))

    def test_list_is_bounded_and_preserves_pagination_notice(self):
        rows=[{'machineryCode':f'DEV-{n:03}','machineryName':f'设备{n}','status':'WORKING','password':'never expose'} for n in range(7)]
        card=result_card({'results':[{'kind':'dv_machinery_list','rows':rows,'total':20}]},'http://example.test')
        raw=json.dumps(card,ensure_ascii=False)
        self.assertIn('DEV-004',raw);self.assertNotIn('DEV-005',raw)
        self.assertIn('20',raw);self.assertIn('5',raw);self.assertNotIn('never expose',raw)

    def test_adapter_uses_separate_demo_tokens_and_only_query_endpoint(self):
        account=Path(self.tmp.name)/'account.json'
        account.write_text(json.dumps({'username':'demo','password':'fake-test-password'}))
        calls=[];tokens=[]
        def fake(url,body=None,token=None,timeout=20):
            calls.append(url)
            if url.endswith('/login'):
                t='fake-token-'+str(len(tokens));tokens.append(t);return {'code':200,'token':t}
            if url.endswith('/getInfo'):return {'code':200}
            self.assertTrue(url.endswith('/agent-api/v1/query'))
            return {'answer':'ok'}
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_fake','FEISHU_APP_SECRET':'fake','KTG_DEMO_ACCOUNT':str(account)}):
            adapter=Adapter(self.store)
        with patch('bridge.http_json',side_effect=fake):
            adapter.query('user-a','查询');adapter.query('user-b','查询');adapter.query('user-a','查询')
        self.assertEqual(len(tokens),2)
        self.assertNotEqual(self.store.account('user-a'),self.store.account('user-b'))
        self.assertFalse(any('confirm' in url for url in calls))


if __name__=='__main__':unittest.main()
