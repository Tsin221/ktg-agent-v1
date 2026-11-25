"""Exercise the real write/receipt contracts only on the dedicated D1 clone."""
from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path
from uuid import uuid4

from ktg_agent.ktg.client import KTGClient
from ktg_agent.operations.executor import OperationExecutor
from ktg_agent.operations.materials import check_prospective_materials
from ktg_agent.operations.planner import prepare_steps
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.operations.workorders import get_order, get_product, release_options, release_request, select_release_configuration, selection_choices
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.sessions.task_tools import owner_from_identity

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / '.runtime' / 'mes-d1-validation'
BASE = 'http://127.0.0.1:18092'
DEMO = runpy.run_path(str(ROOT / 'scripts' / 'verify-mes-demo-agent.py'))


async def main() -> None:
    state = json.loads((RUN / 'service-pids.json').read_text(encoding='utf-8-sig'))
    assert state['database'] == 'ktg_mes_d1_validation_20261124' and state['backendPort'] == 18092
    auth = DEMO['login'](BASE)
    client = KTGClient(base_url=BASE, authorization=auth)
    identity = await client.get_current_user()
    owner = owner_from_identity(identity)
    assert identity['userName'] == 'lamp_leader'
    original = await get_order(client, 'WO-LAMP-20261130-WINTER')
    product = original['productCode']
    reference = await check_prospective_materials(client, await get_product(client, product), 5)
    assert reference['assessment'] == 'reference_sufficient' and reference['scope'] == 'prospective_product_bom_reference'
    options = await release_options(client, product_id=original['productId'])
    # For this demo use the real configured route, stations, template and FINAL type.
    route = options['routes'][0]
    choices = {'route_id': str(route['routeId']),
               'workstation_ids': [str(step['workstations'][0]['workstationId']) for step in route['steps']],
               'template_id': str(route['steps'][1]['templates'][0]['templateId']),
               'ipqc_type': 'FINAL'}
    choices = selection_choices(select_release_configuration(options, choices))
    repo = SessionRepository(RUN / f'direct-{uuid4().hex[:8]}.sqlite3')
    store = OperationPlanStore(repo)
    session = repo.create_session(owner)
    executor = OperationExecutor(store)
    suffix = uuid4().hex[:8].upper()

    async def apply(label: str, requests: list[dict], expected: str) -> dict:
        run = repo.create_run(owner, session.id, str(uuid4()), str(uuid4()), {'message': label})
        repo.update_run_status(owner, run.id, 'running')
        steps = await prepare_steps(client, identity, requests)
        plan = store.create(owner, run.id, steps)
        assert plan['status'] == 'pending'
        for step in steps:
            if step['action'] == 'create_draft':
                rows = await client.get('/mes/pro/workorder/list', {'workorderCode': step['target']['workorder_code'], 'pageNum': 1, 'pageSize': 10})
                assert rows['total'] == 0, 'Preparation changed MES!'
        repo.settle_run(owner, run.id, 'completed', 'completed', {}, ())
        result = await executor.confirm(client, identity, owner, session.id, plan['plan_id'], plan['digest'])
        (RUN / f'direct-{label}-{suffix}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(label, result['status'], [(step['action'], item['status'], item.get('message')) for step,item in zip(result['steps'],result['results'])])
        assert result['status'] == expected, result['results']
        same = await executor.confirm(client, identity, owner, session.id, plan['plan_id'], plan['digest'])
        assert same['results'] == result['results']
        return result

    def create(code: str, quantity: int, step_id: str = 'create') -> dict:
        return {'step_id': step_id, 'action': 'create_draft', 'product_code': product, 'quantity': quantity,
                'request_date': '2026-12-08', 'workorder_code': code, 'name': f'D1独立验收 · {code}'}

    def release(ref: str, step_id: str = 'release', depends: list[str] | None = None) -> dict:
        return {'step_id': step_id, 'action': 'release', 'target': {'from_step': ref}, 'selection': choices,
                'condition': 'warehouse_reference_sufficient', 'depends_on': depends or []}

    a = 'WO-D1-OK-' + suffix
    result = await apply('create-edit-release', [create(a, 12),
        {'action': 'edit_draft', 'step_id': 'edit', 'target': {'from_step': 'create'}, 'changes': {'quantity': 8, 'request_date': '2026-12-09'}},
        release('edit')], 'succeeded')
    actual = await get_order(client, a)
    assert actual['status'] == 'CONFIRMED' and int(actual['quantity']) == 8 and actual['requestDate'].startswith('2026-12-09')
    assert result['results'][2]['sn_count'] == 8 and result['results'][2]['task_count'] == 3
    b = 'WO-D1-SHORT-' + suffix
    result = await apply('shortage', [create(b, 300), release('create')], 'partial')
    assert (await get_order(client,b))['status'] == 'PREPARE' and result['results'][1]['status'] == 'skipped'
    # Guard the actual Java transaction against a BOM change after the read-only reference.
    draft = await get_order(client,b)
    old_options = await release_options(client, order_id=draft['workorderId'])
    bom = await client.get('/mes/pro/workorderbom/list', {'workorderId':draft['workorderId'],'pageNum':1,'pageSize':20})
    line = bom['rows'][0]
    await client.put('/mes/pro/workorderbom', {**line, 'quantity':int(line['quantity'])+1})
    latest = await release_options(client, order_id=draft['workorderId'])
    assert latest['bomFingerprint'] != old_options['bomFingerprint']
    stale = release_request(select_release_configuration(old_options,choices),str(uuid4()),int(draft['version']))
    stale['expectedBomFingerprint'] = old_options['bomFingerprint']
    rejected = await client.post_operation(f"/mes/pro/execution/workorders/{draft['workorderId']}/releases",stale)
    assert rejected['code']==409 and rejected['errorCode']=='BOM_CONFLICT',rejected
    assert (await get_order(client,b))['status']=='PREPARE'
    (RUN/f'bom-conflict-{suffix}.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding='utf-8')
    c,d = 'WO-D1-JOINT-A-'+suffix, 'WO-D1-JOINT-B-'+suffix
    result = await apply('joint-shortage', [create(c,50,'one'),create(d,50,'two'),
        {'action':'check_materials','step_id':'joint','targets':[{'from_step':'one'},{'from_step':'two'}]},
        release('one','releaseOne',['joint']),release('two','releaseTwo',['joint'])], 'partial')
    assert [r['status'] for r in result['results']] == ['succeeded','succeeded','skipped','skipped','skipped']
    assert (await get_order(client,c))['status'] == (await get_order(client,d))['status'] == 'PREPARE'
    e,f = 'WO-D1-JOINT-OK-A-'+suffix, 'WO-D1-JOINT-OK-B-'+suffix
    result = await apply('joint-success', [create(e,5,'one'),create(f,5,'two'),
        {'action':'check_materials','step_id':'joint','targets':[{'from_step':'one'},{'from_step':'two'}]},
        release('one','releaseOne',['joint']),release('two','releaseTwo',['joint'])], 'succeeded')
    assert result['results'][4]['sn_count'] == 5 and result['results'][4]['material_reference']['workorder_codes'] == [e,f]
    print('D1 direct isolated write and receipt verification passed.')


if __name__ == '__main__':
    asyncio.run(main())
