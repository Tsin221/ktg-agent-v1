"""D1 browser/HTTP acceptance fixture. Real isolated MES, deterministic local model; no online calls."""
from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path
from uuid import uuid4

from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.operations.workorders import get_order, release_options, select_release_configuration, selection_choices
from ktg_agent.providers.base import FinalResponse, ModelConfig, ResponseCompleted, Usage
from ktg_agent.providers.fake import FakeProvider, ScriptedTurn
from ktg_agent.server import AgentHTTPServer, Settings, create_session_service
from ktg_agent.service import QueryService

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / '.runtime' / 'mes-d1-validation'
BASE = 'http://127.0.0.1:18092'
TOOL = 'ktg_prepare_workorder_operations'


def turn(*calls: ToolCall, answer: str | None = None) -> ScriptedTurn:
    content = (TextBlock(answer),) if answer is not None else calls
    return ScriptedTurn((ResponseCompleted(FinalResponse(AssistantMessage(content),
        StopReason.COMPLETED if answer is not None else StopReason.TOOL_CALL, Usage(1,1))),))


async def configured_product():
    demo = runpy.run_path(str(ROOT / 'scripts' / 'verify-mes-demo-agent.py'))
    client = KTGClient(base_url=BASE, authorization=demo['login'](BASE))
    original = await get_order(client, 'WO-LAMP-20261130-WINTER')
    options = await release_options(client, product_id=original['productId'])
    route = options['routes'][0]
    choice = {'route_id':str(route['routeId']), 'workstation_ids':[str(step['workstations'][0]['workstationId']) for step in route['steps']],
              'template_id':str(route['steps'][1]['templates'][0]['templateId']), 'ipqc_type':'FINAL'}
    return original['productCode'], selection_choices(select_release_configuration(options, choice))


def main() -> None:
    state = json.loads((RUN/'service-pids.json').read_text(encoding='utf-8-sig'))
    assert state['database'] == 'ktg_mes_d1_validation_20261124' and state['backendPort'] == 18092
    product, config = asyncio.run(configured_product())
    turns = []
    for _ in range(6):
        code = 'WO-D1-UI-' + uuid4().hex[:8].upper()
        steps = [{'step_id':'create','action':'create_draft','product_code':product,'quantity':6,'request_date':'2026-12-10',
                  'workorder_code':code,'name':'D1界面验收 · 充电台灯'},
                 {'step_id':'edit','action':'edit_draft','target':{'from_step':'create'},'changes':{'quantity':5}},
                 {'step_id':'release','action':'release','target':{'from_step':'edit'},'selection':config,'condition':'warehouse_reference_sufficient'}]
        turns.extend((turn(ToolCall(str(uuid4()),'search_tools',{'query':'工单操作准备'})),
                      turn(ToolCall(str(uuid4()),'load_tools',{'tools':[{'name':TOOL,'version':'1.0'}]})),
                      turn(ToolCall(str(uuid4()),TOOL,{'mode':'prepare','steps':steps})),
                      turn(answer='结论：已准备创建、修改和条件下达方案，尚未写入 MES。请在操作卡确认。')))
    settings = Settings(model_api_key='fixture-only', model_base_url='http://127.0.0.1:1', model_id='d1-local-fixture',
                        ktg_base_url=BASE, session_db_path=str(RUN/'fixture-sessions.sqlite3'), port=18093)
    service = QueryService(provider=FakeProvider(turns), model_config=ModelConfig('d1-local-fixture',512),
                           ktg_client_factory=lambda token: KTGClient(base_url=BASE,authorization=token))
    server = AgentHTTPServer(('127.0.0.1',18093),service,create_session_service(settings,service))
    print('D1 local deterministic fixture ready on port 18093', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
