from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.operations.executor import OperationExecutor
from ktg_agent.operations.planner import prepare_steps
from ktg_agent.operations.store import OperationPlanStore, Step
from ktg_agent.sessions.repository import SessionRepository


class FakeMES:
    def __init__(self, stock: int) -> None:
        self.stock = stock
        self.orders: dict[str, Step] = {}
        self.receipts: dict[str, Step] = {}
        self.sent: list[Step] = []
        self.lost = False
        self.hide_receipts = False
        self.permissions = ['*:*:*']
        self.stock_after_first_release: int | None = None
        self.client = KTGClient(base_url='http://localhost:8080', authorization='Bearer test',
                                transport=self.read, write_transport=self.write)

    def options(self) -> Step:
        return {'bomFingerprint': 'sha256:' + 'a'*64, 'routes': [{'routeId': 6, 'routeName': '台灯', 'steps': [
            {'routeProcessId': 11, 'executionKind': 'ASSEMBLY', 'processName': '装配', 'workstations': [{'workstationId': 21, 'workstationName': 'A'}], 'templates': []},
            {'routeProcessId': 12, 'executionKind': 'INSPECTION', 'processName': '检验', 'workstations': [{'workstationId': 22, 'workstationName': 'B'}], 'templates': [{'templateId': 31, 'templateName': '检验', 'qcTypes': ['FINAL']}]},
            {'routeProcessId': 13, 'executionKind': 'PACKING', 'processName': '包装', 'workstations': [{'workstationId': 23, 'workstationName': 'C'}], 'templates': []},
        ]}]}

    async def read(self, url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        path = url.removeprefix('http://localhost:8080')
        if path == '/getInfo':
            return {'code': 200, 'user': {'userId': 1}, 'permissions': self.permissions}
        if path == '/mes/md/mditem/list':
            rows = [{'itemId': 17, 'itemCode': 'LAMP', 'itemName': '台灯', 'unitOfMeasure': 'PCS',
                     'itemOrProduct': 'PRODUCT', 'enableFlag': 'Y'}]
            return {'code': 200, 'total': 1, 'rows': rows}
        if path.endswith('/release-options'):
            return {'code': 200, 'data': self.options()}
        if '/operations/' in path:
            receipt = self.receipts.get(path.rsplit('/', 1)[1])
            if self.hide_receipts or receipt is None:
                raise KTGError('NOT_FOUND', 'not found', status=404)
            return {'code': 200, 'data': receipt}
        if path == '/mes/pro/workorder/list':
            row = self.orders.get(str(params['workorderCode']))
            return {'code': 200, 'total': int(row is not None), 'rows': [row] if row else []}
        if path == '/mes/pro/workorderbom/list':
            order = next(o for o in self.orders.values() if str(o['workorderId']) == str(params['workorderId']))
            rows = [{'lineId': order['workorderId'], 'workorderId': order['workorderId'], 'itemId': 42,
                     'itemCode': 'BAT', 'itemName': '电池', 'unitOfMeasure': 'PCS', 'itemOrProduct': 'ITEM', 'quantity': order['quantity']}]
            return {'code': 200, 'total': 1, 'rows': rows}
        if path == '/mes/wm/wmstock/list':
            return {'code': 200, 'total': 1, 'rows': [{'materialStockId': 99, 'itemId': 42, 'warehouseCode': 'RAW',
                     'warehouseId': 1, 'warehouseName': '原料仓', 'workorderId': None, 'workorderCode': None,
                     'unitOfMeasure': 'PCS', 'quantityOnhand': self.stock, 'batchCode': 'B1'}]}
        if path.startswith('/mes/pro/execution/workorders/'):
            order = next(o for o in self.orders.values() if str(o['workorderId']) == path.rsplit('/', 1)[1])
            frozen = json.dumps([{'line_id': order['workorderId'], 'workorder_id': order['workorderId'], 'item_id':42,
                                   'item_code':'BAT','item_name':'电池','unit_of_measure':'PCS','item_or_product':'ITEM','quantity':order['quantity']}])
            return {'code': 200, 'data': {'order': {**order, 'plannedUnits': order['quantity'], 'routeId': 6, 'materialRequirementsSnapshot': frozen},
                    'summary': {'allocated': order['quantity']}, 'tasks': [{'taskId': i} for i in [101, 102, 103]],
                    'units': [{'snId': i+1} for i in range(order['quantity'])]}}
        if path.startswith('/mes/pro/workorder/'):
            order = next(o for o in self.orders.values() if str(o['workorderId']) == path.rsplit('/', 1)[1])
            return {'code': 200, 'data': order}
        raise AssertionError(path)

    async def write(self, method: str, url: str, headers: Mapping[str, str], body: Mapping[str, object], timeout: float) -> dict[str, object]:
        request: Step = dict(body)
        self.sent.append(request)
        if request.get('action') in {'CREATE_DRAFT', 'EDIT_DRAFT'}:
            draft = request['draft']
            code = draft['workorderCode']
            existing = self.orders.get(code)
            version = existing['version'] + 1 if existing else 0
            order_id = existing['workorderId'] if existing else len(self.orders) + 51
            self.orders[code] = {**draft, 'version': version, 'workorderId': order_id, 'status': 'PREPARE',
                                 'productCode': 'LAMP', 'productName': '台灯'}
            receipt = {'operationId': request['operationId'], 'action': request['action'], 'ownerId': 1,
                       'requestDigest': request['requestDigest'], 'expectedVersion': request.get('expectedVersion'),
                       'workorderId': order_id, 'workorderCode': code, 'workorderVersion': version,
                       'outcome': 'succeeded', 'committedAt': 'now'}
        else:
            order_id = int(url.split('/')[-2])
            order = next(o for o in self.orders.values() if o['workorderId'] == order_id)
            order.update(status='CONFIRMED', version=order['version']+1)
            if self.stock_after_first_release is not None:
                self.stock = self.stock_after_first_release
                self.stock_after_first_release = None
            receipt = {'operationId': request['operationId'], 'actionType': 'RELEASE', 'workorderId': order_id,
                       'resultRef': {'version': order['version'], 'state': 'CONFIRMED', 'taskCount': 3,
                                     'taskIds': [101,102,103], 'snCount': order['quantity'], 'allocated': order['quantity']}, 'committedAt': 'now'}
        self.receipts[request['operationId']] = receipt
        if self.lost:
            raise KTGError('KTG_CONNECTION_ERROR', 'lost')
        return {'code': 200, 'data': receipt}


def plan_requests() -> list[Step]:
    return [
        {'action': 'create_draft', 'step_id': 'create', 'product_code': 'LAMP', 'quantity': 50,
         'request_date': '2026-11-30', 'workorder_code': 'WO-LAMP'},
        {'action': 'edit_draft', 'step_id': 'edit', 'target': {'from_step': 'create'}, 'changes': {'quantity': 40}},
        {'action': 'release', 'step_id': 'release', 'target': {'from_step': 'edit'}, 'condition': 'warehouse_reference_sufficient'},
    ]


async def create_plan(tmp_path: Path, mes: FakeMES, requests: list[Step]) -> tuple[OperationPlanStore, Step]:
    repo = SessionRepository(tmp_path / 'sessions.sqlite3')
    session = repo.create_session('id:1')
    run = repo.create_run('id:1', session.id, 'req', 'hash', {})
    repo.update_run_status('id:1', run.id, 'running')
    store = OperationPlanStore(repo)
    plan = store.create('id:1', run.id, await prepare_steps(mes.client, {'userId': 1}, requests))
    repo.settle_run('id:1', run.id, 'completed', 'completed', {}, ())
    return store, plan


async def confirm(store: OperationPlanStore, plan: Step, mes: FakeMES) -> Step:
    return await OperationExecutor(store).confirm(mes.client, {'userId': 1}, 'id:1', plan['session_id'], plan['plan_id'], plan['digest'])


def test_confirmed_create_edit_and_conditional_release_read_back_and_never_repeat(tmp_path: Path) -> None:
    async def inner() -> None:
        mes = FakeMES(100)
        store, plan = await create_plan(tmp_path, mes, plan_requests())
        assert not mes.sent and not mes.orders
        result = await confirm(store, plan, mes)
        assert result['status'] == 'succeeded', result['results']
        assert [r['status'] for r in result['results']] == ['succeeded']*3
        assert mes.orders['WO-LAMP']['quantity'] == 40 and mes.orders['WO-LAMP']['status'] == 'CONFIRMED'
        assert mes.sent[-1]['expectedBomFingerprint'] == 'sha256:'+'a'*64
        assert result['results'][-1]['sn_count'] == 40
        assert (await confirm(store, plan, mes))['status'] == 'succeeded'
        assert len(mes.sent) == 3
    asyncio.run(inner())


def test_shortage_keeps_created_edited_draft_without_sending_release(tmp_path: Path) -> None:
    async def inner() -> None:
        mes = FakeMES(30)
        store, plan = await create_plan(tmp_path, mes, plan_requests())
        result = await confirm(store, plan, mes)
        assert result['status'] == 'partial', result['results']
        assert result['results'][-1]['status'] == 'skipped'
        assert result['results'][-1]['material_reference']['assessment'] == 'reference_shortage'
        assert len(mes.sent) == 2 and mes.orders['WO-LAMP']['status'] == 'PREPARE'
    asyncio.run(inner())


def test_lost_response_unknown_never_resends_and_reconciles_original_receipt(tmp_path: Path) -> None:
    async def inner() -> None:
        mes = FakeMES(100)
        store, plan = await create_plan(tmp_path, mes, plan_requests())
        mes.lost = True
        mes.hide_receipts = True
        result = await confirm(store, plan, mes)
        assert result['status'] == 'needs_reconciliation', result['results']
        assert [r['status'] for r in result['results']] == ['unknown', 'skipped', 'skipped']
        assert len(mes.sent) == 1
        mes.hide_receipts = False
        result = await OperationExecutor(store).reconcile(mes.client, {'userId': 1}, 'id:1', plan['session_id'], plan['plan_id'])
        assert result['status'] == 'partial' and result['results'][0]['status'] == 'succeeded'
        assert len(mes.sent) == 1
    asyncio.run(inner())


def test_joint_condition_does_not_double_count_inventory(tmp_path: Path) -> None:
    async def inner() -> None:
        mes = FakeMES(75)
        a = plan_requests()[0]
        b = {**a, 'step_id': 'second', 'workorder_code': 'WO-SECOND'}
        requests = [a, b, {'action': 'check_materials', 'step_id': 'check', 'targets': [{'from_step': 'create'}, {'from_step': 'second'}]},
                    {'action': 'release', 'step_id': 'releaseA', 'target': {'from_step': 'create'}, 'depends_on': ['check']},
                    {'action': 'release', 'step_id': 'releaseB', 'target': {'from_step': 'second'}, 'depends_on': ['check']}]
        store, plan = await create_plan(tmp_path, mes, requests)
        result = await confirm(store, plan, mes)
        assert result['results'][2]['material_reference']['materials'][0]['planned_required'] == '100'
        assert [r['status'] for r in result['results']] == ['succeeded','succeeded','skipped','skipped','skipped']
        assert len(mes.sent) == 2
    asyncio.run(inner())


def test_joint_condition_rechecks_all_requirements_after_first_release(tmp_path: Path) -> None:
    async def inner() -> None:
        mes = FakeMES(100)
        mes.stock_after_first_release = 75
        a = plan_requests()[0]
        b = {**a, 'step_id': 'second', 'workorder_code': 'WO-SECOND'}
        requests = [a, b, {'action': 'check_materials', 'step_id': 'check', 'targets': [{'from_step': 'create'}, {'from_step': 'second'}]},
                    {'action': 'release', 'step_id': 'releaseA', 'target': {'from_step': 'create'}, 'depends_on': ['check']},
                    {'action': 'release', 'step_id': 'releaseB', 'target': {'from_step': 'second'}, 'depends_on': ['check']}]
        store, plan = await create_plan(tmp_path, mes, requests)
        result = await confirm(store, plan, mes)
        assert [r['status'] for r in result['results']] == ['succeeded','succeeded','succeeded','succeeded','skipped'], result['results']
        assert result['results'][-1]['material_reference']['materials'][0]['planned_required'] == '100'
        assert len(mes.sent) == 3 and mes.orders['WO-SECOND']['status']=='PREPARE'
    asyncio.run(inner())


def test_permission_switch_stops_subsequent_confirmed_writes(tmp_path: Path) -> None:
    async def inner() -> None:
        mes = FakeMES(100)
        store, plan = await create_plan(tmp_path, mes, plan_requests())
        executor=OperationExecutor(store, lambda: not mes.sent)
        result=await executor.confirm(mes.client, {'userId': 1}, 'id:1', plan['session_id'], plan['plan_id'], plan['digest'])
        assert [r['status'] for r in result['results']]==['succeeded','failed','skipped']
        assert len(mes.sent)==1
    asyncio.run(inner())
