"""Write-preview contracts for the MES tool ledger."""
from __future__ import annotations
import asyncio
from collections.abc import Mapping
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
from threading import Thread
from typing import Any, cast
import pytest
from ktg_agent.core.messages import JsonObject, JsonValue, ToolCall
from ktg_agent.server import AgentHTTPServer, Settings, create_service
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.tools import CatalogIdentity, ToolDefinition, ToolResult
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.tm_tool_writes import (
    PREVIEW_CREATE, PREVIEW_UPDATE, PREVIEW_DELETE, APPLY_CREATE, APPLY_UPDATE, APPLY_DELETE,
    TmToolPreviewStore, build_tm_tool_write_tools,
)
from ktg_agent.tools import ToolContext, ToolStatus
from ktg_agent.tools.registry import ToolChannel

TYPE={"toolTypeId":2,"toolTypeCode":"TT2","toolTypeName":"独立编码","codeFlag":"Y"}
ROW={"toolId":7,"toolCode":"LUNA_TM_TOOL_20261106_OLD","toolName":"旧记录","brand":"B","spec":"S","toolTypeId":2,"toolTypeCode":"TT2","toolTypeName":"独立编码","codeFlag":"Y","quantity":1,"quantityAvail":1,"maintenType":"REGULAR","nextMaintenPeriod":None,"nextMaintenDate":"2026-12-01","status":"STORE","remark":"保留"}
class FakeMES:
    def __init__(self) -> None:
        self.types: dict[int, dict[str, object]] = {2: dict(TYPE)}
        self.rows: list[dict[str, object]] = [dict(ROW)]
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.permissions = {"mes:tm:tool:add","mes:tm:tool:edit","mes:tm:tool:remove","mes:tm:tool:list","mes:tm:tool:query","mes:tm:tooltype:list","mes:tm:tooltype:query"}
        self.unknown = False
        self.fail_delete_readback = False
    async def read(self,url: str,headers: Mapping[str, str],params: Mapping[str, object],timeout: float) -> object:
        if url.endswith('/getInfo'):
            return {"code":200,"user":{"userId":5,"userName":"luna-test"},"permissions":sorted(self.permissions)}
        if url.endswith('/mes/tm/tooltype/2'):return {"code":200,"data":self.types.get(2)}
        if url.endswith('/mes/tm/tool/7'):
            if self.fail_delete_readback and self.writes and self.writes[-1][0]=='DELETE':
                raise KTGError('KTG_INVALID_RESPONSE','bad delete readback')
            return {"code":200,"data":next((dict(r) for r in self.rows if r['toolId']==7),None)}
        if url.endswith('/mes/tm/tool/list'):
            rows=[dict(r) for r in self.rows if not params.get('toolCode') or r['toolCode']==params['toolCode']]
            return {"code":200,"rows":rows,"total":len(rows)}
        raise AssertionError(url)
    async def write(self,method: str,url: str,headers: Mapping[str, str],body: Mapping[str, object],timeout: float) -> dict[str, object]:
        self.writes.append((method,url,dict(body)))
        if method=='POST':self.rows.append({**body,'toolId':8})
        elif method=='PUT':next(r for r in self.rows if r['toolId']==body['toolId']).update(body)
        elif method=='DELETE':self.rows=[r for r in self.rows if r['toolId']!=7]
        if self.unknown: raise KTGError('KTG_CONNECTION_ERROR','lost response')
        return {"code":200}
    def client(self) -> KTGClient:return KTGClient(base_url='http://mes',authorization='Bearer fake',transport=self.read,write_transport=self.write)
def run(tool: ToolDefinition,mes: FakeMES,args: Mapping[str,Any],trusted: bool=False,user: int=5) -> ToolResult:
 async def call() -> ToolResult:return await tool.execute(ToolContext(run_id='r',tool_call_id='c',identity={'userId':user},ktg_client=mes.client(),channel=ToolChannel.TRUSTED_CONFIRMATION if trusted else ToolChannel.MODEL,operation_id=str(args.get('operation_id')) if trusted else None,confirmation_id='confirm' if trusted else None),args)
 return asyncio.run(call())
def tools(store: TmToolPreviewStore) -> dict[str, ToolDefinition]:return {x.name:x for x in build_tm_tool_write_tools(store)}
def create_args(**changes: JsonValue) -> JsonObject:return {"tool_code":"LUNA_TM_TOOL_20261106_NEW","tool_name":"测试台账","tool_type_id":2,"quantity":1,**changes}
def result_details(result: ToolResult) -> JsonObject:
    assert result.details is not None
    return result.details
def test_create_preview_and_hidden_confirm_are_verified_and_idempotent(tmp_path: Path) -> None:
 mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'previews.sqlite'))
 preview=run(ts[PREVIEW_CREATE],mes,create_args());assert preview.status is ToolStatus.SUCCEEDED and not mes.writes
 assert ts[APPLY_CREATE].expose_to_model is False
 result=run(ts[APPLY_CREATE],mes,{"operation_id":preview.operation_id},trusted=True)
 replay=run(ts[APPLY_CREATE],mes,{"operation_id":preview.operation_id},trusted=True)
 result_data = result_details(result)
 record = cast(JsonObject, result_data["verified_record"])
 assert result_data["outcome"]=="succeeded" and record["toolCode"]=="LUNA_TM_TOOL_20261106_NEW"
 assert replay.details==result.details and len(mes.writes)==1

def test_create_requires_exact_quantity_and_trusted_type_snapshot(tmp_path: Path) -> None:
 mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'p.sqlite'))
 assert run(ts[PREVIEW_CREATE],mes,create_args(quantity=2)).error_code=='VALIDATION_ERROR'
 preview=run(ts[PREVIEW_CREATE],mes,create_args());mes.types[2]['toolTypeName']='changed'
 result=run(ts[APPLY_CREATE],mes,{"operation_id":preview.operation_id},trusted=True)
 assert result_details(result)['outcome']=='failed' and not mes.writes

def test_create_usage_period_may_be_omitted_but_must_be_positive_when_given(tmp_path: Path) -> None:
 mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'p.sqlite'))
 without_period=run(ts[PREVIEW_CREATE],mes,create_args(mainten_type='USAGE'))
 assert without_period.status is ToolStatus.SUCCEEDED
 assert cast(JsonObject,result_details(without_period)['after'])['nextMaintenPeriod'] is None
 with_zero=run(ts[PREVIEW_CREATE],mes,create_args(mainten_type='USAGE',next_mainten_period=0))
 assert with_zero.error_code=='VALIDATION_ERROR'

def test_update_merges_null_clear_and_preserves_omitted_business_values(tmp_path: Path) -> None:
 mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'p.sqlite'))
 preview=run(ts[PREVIEW_UPDATE],mes,{"tool_id":7,"brand":"","next_mainten_date":None})
 assert preview.status is ToolStatus.SUCCEEDED
 result=run(ts[APPLY_UPDATE],mes,{"operation_id":preview.operation_id},trusted=True)
 assert result_details(result)['outcome']=='succeeded'
 body=mes.writes[0][2]
 assert body['toolTypeId']==2 and body['quantity']==1 and body['status']=='STORE'
 assert body['brand']=='' and mes.rows[0]['nextMaintenDate'] is None and mes.rows[0]['remark']=='保留'

def test_usage_maintenance_clears_date_and_delete_guard(tmp_path: Path) -> None:
 mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'p.sqlite'))
 preview=run(ts[PREVIEW_UPDATE],mes,{"tool_id":7,"mainten_type":"USAGE","next_mainten_period":12})
 assert cast(JsonObject,result_details(preview)['after'])['nextMaintenDate'] is None
 assert run(ts[PREVIEW_DELETE],mes,{"tool_id":7}).status is ToolStatus.SUCCEEDED
 mes.rows[0]['quantityAvail']=0
 assert run(ts[PREVIEW_DELETE],mes,{"tool_id":7}).error_code=='TOOL_NOT_DELETABLE'



def test_cross_identity_snapshot_stale_and_expiration_are_rejected(tmp_path: Path) -> None:
    mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'p.sqlite'))
    preview=run(ts[PREVIEW_UPDATE],mes,{"tool_id":7,"brand":"new"})
    wrong_user=run(ts[APPLY_UPDATE],mes,{"operation_id":preview.operation_id},trusted=True,user=99)
    assert wrong_user.status is ToolStatus.FAILED and not mes.writes
    preview2=run(ts[PREVIEW_UPDATE],mes,{"tool_id":7,"brand":"new"})
    mes.rows[0]['remark']='changed externally'
    stale=run(ts[APPLY_UPDATE],mes,{"operation_id":preview2.operation_id},trusted=True)
    assert result_details(stale)['outcome']=='failed' and not mes.writes
    expiring=tools(TmToolPreviewStore(ttl_seconds=0.001,path=tmp_path/'expire.sqlite'))
    pending=run(expiring[PREVIEW_UPDATE],mes,{"tool_id":7,"brand":"later"})
    import time
    time.sleep(0.01)
    expired=run(expiring[APPLY_UPDATE],mes,{"operation_id":pending.operation_id},trusted=True)
    assert expired.status is ToolStatus.FAILED and not mes.writes

def test_post_send_uncertainty_is_unknown_and_never_replayed(tmp_path: Path) -> None:
    mes=FakeMES();ts=tools(TmToolPreviewStore(path=tmp_path/'p.sqlite'))
    preview=run(ts[PREVIEW_CREATE],mes,create_args())
    mes.unknown=True
    result=run(ts[APPLY_CREATE],mes,{"operation_id":preview.operation_id},trusted=True)
    replay=run(ts[APPLY_CREATE],mes,{"operation_id":preview.operation_id},trusted=True)
    assert result_details(result)['outcome']=='unknown' and replay.details==result.details
    assert len(mes.writes)==1

def test_hidden_apply_tools_and_permission_requirements() -> None:
    definitions=build_tm_tool_write_tools(TmToolPreviewStore())
    by_name={item.name:item for item in definitions}
    assert by_name[APPLY_CREATE].expose_to_model is False
    assert {'mes:tm:tooltype:query','mes:tm:tool:list'} <= set(by_name[PREVIEW_CREATE].required_permissions)
    assert {'mes:tm:tool:query','mes:tm:tooltype:query'} <= set(by_name[PREVIEW_UPDATE].required_permissions)
    update_properties = cast(JsonObject, by_name[PREVIEW_UPDATE].input_schema['properties'])
    create_properties = cast(JsonObject, by_name[PREVIEW_CREATE].input_schema['properties'])
    assert update_properties.keys() == {'tool_id','tool_name','brand','spec','mainten_type','next_mainten_date','next_mainten_period','remark'}
    assert 'code_flag' not in create_properties
    assert run(by_name[APPLY_CREATE],FakeMES(),{'operation_id':'bogus'}).error_code=='CONFIRMATION_REQUIRED'


def test_preview_digest_tampering_is_rejected_before_write(tmp_path: Path) -> None:
    mes = FakeMES()
    store = TmToolPreviewStore(path=tmp_path / "tamper.sqlite")
    definitions = tools(store)
    preview = run(definitions[PREVIEW_UPDATE], mes, {"tool_id": 7, "brand": "tampered"})
    with sqlite3.connect(store.path) as database:
        database.execute(
            "UPDATE tm_tool_previews SET after_json='{}' WHERE preview_id=?",
            (preview.operation_id,),
        )

    result = run(
        definitions[APPLY_UPDATE], mes,
        {"operation_id": preview.operation_id}, trusted=True,
    )
    assert result.error_code == "PREVIEW_STALE"
    assert not mes.writes


def test_delete_readback_failure_is_unknown_and_not_replayed(tmp_path: Path) -> None:
    mes = FakeMES()
    definitions = tools(TmToolPreviewStore(path=tmp_path / "delete-unknown.sqlite"))
    preview = run(definitions[PREVIEW_DELETE], mes, {"tool_id": 7})
    mes.fail_delete_readback = True
    first = run(
        definitions[APPLY_DELETE], mes,
        {"operation_id": preview.operation_id}, trusted=True,
    )
    replay = run(
        definitions[APPLY_DELETE], mes,
        {"operation_id": preview.operation_id}, trusted=True,
    )
    assert result_details(first)["outcome"] == "unknown"
    assert replay.details == first.details
    assert len(mes.writes) == 1


def test_query_service_tool_runner_and_trusted_confirmation_chain(tmp_path: Path) -> None:
    async def scenario() -> None:
        mes = FakeMES()
        settings = Settings(
            model_api_key="fake",
            model_base_url="https://example.test/v1",
            model_id="fake",
            ktg_base_url="http://mes",
            session_db_path=str(tmp_path / "sessions.sqlite"),
            tm_tool_db_path=str(tmp_path / "tm-tool.sqlite"),
            enable_tm_tool_write=True,
        )
        service = create_service(settings)
        service._ktg_client_factory = lambda _authorization: mes.client()
        identity = {"userId": 5, "userName": "luna-test"}
        permissions = frozenset(mes.permissions)
        candidate = service._catalog.candidate_snapshot(
            CatalogIdentity(permissions, "tm-tool-run"), service._switches_provider(),
        )
        runner = service._tool_runner(candidate)
        client = mes.client()
        preview_call = ToolCall("preview-call", PREVIEW_CREATE, create_args())
        preview = await runner.run(
            preview_call,
            ToolContext(
                run_id="tm-tool-run", tool_call_id=preview_call.id,
                identity=identity, ktg_client=client,
            ),
        )
        assert preview.status is ToolStatus.SUCCEEDED
        assert not mes.writes

        hidden_call = ToolCall(
            "model-apply-call", APPLY_CREATE,
            {"operation_id": preview.operation_id},
        )
        denied = await runner.run(
            hidden_call,
            ToolContext(
                run_id="tm-tool-run", tool_call_id=hidden_call.id,
                operation_id=preview.operation_id, identity=identity,
                ktg_client=client,
            ),
        )
        assert denied.error_code == "TOOL_NOT_FOUND"
        assert not mes.writes

        confirmed = await service.confirm_tm_tool(
            preview_id=preview.operation_id or "", authorization="Bearer fake",
        )
        confirmed_result = cast(JsonObject, confirmed["result"])
        assert confirmed_result["outcome"] == "succeeded"
        assert confirmed_result["verified"] is True
        assert len(mes.writes) == 1

    asyncio.run(scenario())


def test_tool_preview_database_is_opt_in_and_path_isolated(tmp_path: Path) -> None:
    base = Settings(
        model_api_key="fake", model_base_url="https://example.test/v1", model_id="fake",
        ktg_base_url="http://mes", session_db_path=str(tmp_path / "sessions.sqlite"),
    )
    assert create_service(base)._tm_tool_store is None

    session_alias = Settings(
        model_api_key="fake", model_base_url="https://example.test/v1", model_id="fake",
        ktg_base_url="http://mes", session_db_path=str(tmp_path / "sessions.sqlite"),
        tm_tool_db_path=str(tmp_path / "sessions.sqlite"), enable_tm_tool_write=True,
    )
    with pytest.raises(RuntimeError, match="must differ"):
        create_service(session_alias)

    shared_path = str(tmp_path / "shared.sqlite")
    library_alias = Settings(
        model_api_key="fake", model_base_url="https://example.test/v1", model_id="fake",
        ktg_base_url="http://mes", session_db_path=str(tmp_path / "sessions.sqlite"),
        tm_tool_db_path=shared_path, enable_tm_tool_write=True,
        tm_tooltype_db_path=shared_path, enable_tm_tooltype_write=True,
    )
    with pytest.raises(RuntimeError, match="must differ"):
        create_service(library_alias)


def test_tm_tool_http_confirmation_route(tmp_path: Path) -> None:
    mes = FakeMES()
    service = create_service(Settings(
        model_api_key="fake", model_base_url="https://example.test/v1", model_id="fake",
        ktg_base_url="http://mes", session_db_path=str(tmp_path / "sessions.sqlite"),
        tm_tool_db_path=str(tmp_path / "tm-tool.sqlite"), enable_tm_tool_write=True,
    ))
    service._ktg_client_factory = lambda _authorization: mes.client()
    assert service._tm_tool_store is not None
    preview = run(
        tools(service._tm_tool_store)[PREVIEW_CREATE], mes, create_args(),
    )
    sessions = SessionService(
        SessionRepository(tmp_path / "http-sessions.sqlite"),
        lambda _authorization: mes.client(),
    )
    server = AgentHTTPServer(("127.0.0.1", 0), service, sessions)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST", "/agent-api/v1/tm-tool/confirm",
            body=json.dumps({"preview_id": preview.operation_id}),
            headers={"Authorization": "Bearer fake", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = cast(JsonObject, json.loads(response.read()))
        connection.close()
        confirmed = cast(JsonObject, payload["result"])
        assert response.status == 200
        assert confirmed["outcome"] == "succeeded" and confirmed["verified"] is True
        assert len(mes.writes) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
