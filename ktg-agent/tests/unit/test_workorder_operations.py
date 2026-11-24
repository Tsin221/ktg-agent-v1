from __future__ import annotations

import hashlib
import json
import asyncio
from collections.abc import Mapping

import pytest

from ktg_agent.operations.workorders import draft_digest, select_release_configuration, build_draft, edit_draft
from ktg_agent.operations.planner import prepare_steps
from ktg_agent.ktg.client import KTGClient


def test_draft_digest_matches_java_canonical_numeric_and_unicode_contract() -> None:
    draft = {'productId': 7, 'workorderName': '阅读灯补单', 'quantity': '10.00', 'remark': None}
    wire = '{"action":"CREATE_DRAFT","draft":{"productId":7,"quantity":"10","workorderName":"阅读灯补单"}}'
    assert draft_digest('CREATE_DRAFT', draft) == 'sha256:' + hashlib.sha256(wire.encode()).hexdigest()


def test_create_draft_does_not_invent_customer_or_received_material_facts() -> None:
    draft = build_draft({'product_code': 'LAMP', 'quantity': 50, 'request_date': '2026-11-30'},
                        {'itemId': 17, 'itemCode': 'LAMP', 'itemName': '阅读灯电池模组'}, code='WO-LAMP-50')
    assert draft['workorderCode'] == 'WO-LAMP-50'
    assert draft['productId'] == 17
    assert draft['quantity'] == 50
    assert draft['orderSource'] == 'STORE'
    assert 'clientId' not in draft and 'status' not in draft and 'version' not in draft
    with pytest.raises(ValueError, match='需求日期'):
        build_draft({'quantity': 50}, {'itemId': 17, 'itemCode': 'LAMP', 'itemName': '灯'}, code='WO-LAMP-50')


def config() -> dict[str, object]:
    return {'routes': [{'routeId': 6, 'routeCode': 'LAMP', 'routeName': '模组生产', 'steps': [
        {'routeProcessId': 11, 'executionKind': 'ASSEMBLY', 'processName': '装配', 'workstations': [{'workstationId': 21, 'workstationName': '装配工位'}], 'templates': []},
        {'routeProcessId': 12, 'executionKind': 'INSPECTION', 'processName': '检验', 'workstations': [{'workstationId': 22, 'workstationName': '检验工位'}], 'templates': [{'templateId': 31, 'templateName': '电压检验', 'qcTypes': ['FINAL']}]},
        {'routeProcessId': 13, 'executionKind': 'PACKING', 'processName': '包装', 'workstations': [{'workstationId': 23, 'workstationName': '包装工位'}], 'templates': []},
    ]}]}


def test_unique_configuration_is_resolved_from_real_server_options() -> None:
    selected = select_release_configuration(config(), {})
    assert selected['route_id'] == '6'
    assert selected['steps'][1]['template_id'] == '31'
    assert selected['steps'][1]['ipqc_type'] == 'FINAL'
    with pytest.raises(ValueError, match='路线'):
        select_release_configuration(config(), {'route_id': '404'})


def test_ambiguous_workstations_require_explicit_choice() -> None:
    source = json.loads(json.dumps(config()))
    source['routes'][0]['steps'][0]['workstations'].append({'workstationId': 99, 'workstationName': '另一工位'})
    with pytest.raises(ValueError, match='工位'):
        select_release_configuration(source, {})
    selected = select_release_configuration(source, {'workstation_ids': ['99', '22', '23']})
    assert selected['steps'][0]['workstation_id'] == '99'


def test_edit_merges_real_draft_fields_and_rejects_uncontrolled_state_changes() -> None:
    original = {'workorderId': 18, 'version': 2, 'status': 'PREPARE', 'workorderCode': 'WO-18',
                'workorderName': '客户补库', 'workorderType': 'SELF', 'orderSource': 'ORDER',
                'sourceCode': 'ORDER-1', 'clientId': 4, 'clientName': '张三', 'quantity': '10.00',
                'productId': 17, 'requestDate': '2026-11-30', 'remark': '旧备注'}
    result = edit_draft(original, {'quantity': 15, 'request_date': '2026-12-01'})
    assert result['quantity'] == 15 and result['requestDate'] == '2026-12-01'
    assert result['clientId'] == 4 and result['sourceCode'] == 'ORDER-1'
    assert result['remark'] == '旧备注' and 'status' not in result and 'version' not in result
    assert original['quantity'] == '10.00'
    with pytest.raises(ValueError, match='修改字段'):
        edit_draft(original, {'status': 'CONFIRMED'})


def test_prepare_multi_step_uses_resolved_future_order_and_does_not_write() -> None:
    calls: list[str] = []

    async def read(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        calls.append(url)
        if url.endswith('/getInfo'):
            return {'code': 200, 'user': {'userId': 1}, 'permissions': ['*:*:*']}
        if url.endswith('/mes/md/mditem/list'):
            return {'code': 200, 'total': 1, 'rows': [{'itemId': 17, 'itemCode': 'LAMP', 'itemName': '台灯',
                    'unitOfMeasure': 'PCS', 'itemOrProduct': 'PRODUCT', 'enableFlag': 'Y'}]}
        if url.endswith('/products/17/release-options'):
            return {'code': 200, 'data': config()}
        raise AssertionError(url)

    async def write(method: str, url: str, headers: Mapping[str, str], body: Mapping[str, object], timeout: float) -> dict[str, object]:
        raise AssertionError('preparing a plan cannot write')

    client = KTGClient(base_url='http://localhost:8080', authorization='Bearer test', transport=read, write_transport=write)
    result = asyncio.run(prepare_steps(client, {'userId': 1}, [
        {'action': 'create_draft', 'step_id': 'create', 'product_code': 'LAMP', 'quantity': 50,
         'request_date': '2026-11-30', 'workorder_code': 'WO-LAMP'},
        {'action': 'edit_draft', 'step_id': 'edit', 'target': {'from_step': 'create'}, 'changes': {'quantity': 40}},
        {'action': 'check_materials', 'step_id': 'check', 'targets': [{'from_step': 'edit'}]},
        {'action': 'release', 'step_id': 'release', 'target': {'from_step': 'edit'},
         'condition': 'warehouse_reference_sufficient', 'depends_on': ['check']},
    ]))
    assert result[1]['target']['expected_version'] == 0
    assert result[1]['depends_on'] == ['create']
    assert result[2]['depends_on'] == ['edit']
    assert result[3]['target']['quantity'] == 40 and result[3]['target']['expected_version'] == 1
    assert result[3]['depends_on'] == ['check', 'edit']
    assert result[3]['selection']['route_id'] == '6'
    assert all('autocode' not in call for call in calls)
