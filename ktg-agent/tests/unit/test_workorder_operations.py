from __future__ import annotations

import hashlib
import json

import pytest

from ktg_agent.operations.workorders import draft_digest, select_release_configuration, build_draft


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
