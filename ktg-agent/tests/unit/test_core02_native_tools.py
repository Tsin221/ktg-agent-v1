from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.queries import build_query_tools
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.operations.tool import build_workorder_operation_tool
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.sessions.native_planning import build_planning_capability, stock_step_evidence
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.tools.catalog import CatalogSwitches
from ktg_agent.tools import native
from ktg_agent.tools.native import MesToolDeps, build_mes_toolset
from ktg_agent.tools.registry import ToolContext, ToolDefinition, ToolResult


NAMES = {
    'ktg_count_products', 'ktg_query_stock_summary', 'ktg_query_workorder_overview',
    'ktg_query_workorder_materials', 'ktg_query_workorder_quality',
    'ktg_query_production_trace', 'ktg_resolve_business_reference', 'ktg_prepare_workorder_operations',
}


class FakeMES:
    def __init__(self) -> None:
        self.permissions: list[str] | None = ['*:*:*']
        self.user_id = 1
        self.business_reads: list[str] = []
        self.permission_reads = 0
        self.raise_on_query = False

    async def read(self, url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
        if url.endswith('/getInfo'):
            self.permission_reads += 1
            return {'code': 200, 'user': {'userId': self.user_id}, 'permissions': self.permissions}
        self.business_reads.append(url)
        if self.raise_on_query:
            raise RuntimeError('private diagnostic, not model or UI content')
        if url.endswith('/mes/md/mditem/list'):
            return {'code': 200, 'total': 1, 'rows': [{'itemId': 17, 'itemCode': 'LAMP', 'itemName': '台灯',
                    'unitOfMeasure': 'PCS', 'itemOrProduct': 'PRODUCT', 'enableFlag': 'Y'}]}
        if url.endswith('/mes/wm/wmstock/summary'):
            return {'code': 200, 'data': {'itemCode': params['itemCode'], 'warehouseId': None,
                'groups': [], 'hasRecords': False, 'queriedAt': '2026-11-26T00:00:00+00:00'}}
        raise AssertionError(url)

    async def write(self, method: str, url: str, headers: Mapping[str, str], body: Mapping[str, object], timeout: float) -> dict[str, object]:
        raise AssertionError('model tools cannot write MES')


def setup(tmp_path: Path, mes: FakeMES) -> tuple[OperationPlanStore, MesToolDeps]:
    repository = SessionRepository(tmp_path / 'native.sqlite3')
    session = repository.create_session('id:1')
    run = repository.create_run('id:1', session.id, 'request', 'hash', {})
    repository.update_run_status('id:1', run.id, 'running')
    client = KTGClient(base_url='http://test.invalid', authorization='Bearer fake', transport=mes.read,
                       write_transport=mes.write)
    return OperationPlanStore(repository), MesToolDeps(run.id, session.id, 'id:1', {'userId': 1}, client)


def returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [part for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, ToolReturnPart)]


def test_registration_schemas_and_native_multiple_model_turns(tmp_path: Path) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = {tool.name: tool for tool in info.function_tools}
        assert tools.keys() == NAMES
        assert tools['ktg_query_stock_summary'].parameters_json_schema['required'] == ['item_code']
        assert 'client' not in str(tools['ktg_count_products'].parameters_json_schema)
        assert tools['ktg_prepare_workorder_operations'].sequential
        assert not tools['ktg_count_products'].sequential
        results = returns(messages)
        if not results:
            return ModelResponse(parts=[ToolCallPart('ktg_count_products', {}, 'count')])
        if len(results) == 1:
            assert results[0].content == '按当前用户权限查询，产品记录总数为 1 条（不含物料）。'
            assert results[0].metadata['details']['kind'] == 'product_count'
            return ModelResponse(parts=[ToolCallPart('ktg_query_stock_summary', {'item_code': 'LAMP'}, 'stock')])
        return ModelResponse(parts=[TextPart('done')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    result = asyncio.run(agent.run('query', deps=deps))
    assert result.output == 'done'
    parts = returns(result.all_messages())
    assert [part.tool_call_id for part in parts] == ['count', 'stock']
    assert parts[-1].metadata['details']['no_records'] is True
    assert parts[0].metadata['status'] == 'succeeded'
    assert len(mes.business_reads) == 2


def test_verified_stock_result_links_only_a_stable_active_plan_step(tmp_path: Path) -> None:
    store, deps = setup(tmp_path, FakeMES())
    actions = iter([
        ToolCallPart('write_plan', {'items': [{'id': 'a', 'content': '核实台灯库存', 'status': 'in_progress'}]}, 'p1'),
        ToolCallPart('ktg_query_stock_summary', {'item_code': 'LAMP'}, 'stock1'),
        ToolCallPart('write_plan', {'items': [
            {'id': 'a', 'content': '核实台灯库存', 'status': 'completed'},
            {'id': 'b', 'content': '检查库存步骤二', 'status': 'in_progress'},
            {'id': 'c', 'content': '检查库存步骤三', 'status': 'in_progress'}]}, 'p2'),
        ToolCallPart('ktg_query_stock_summary', {'item_code': 'LAMP'}, 'stock2'),
    ])

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        action = next(actions, None)
        return ModelResponse(parts=[action if action is not None else TextPart('done')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)],
                  capabilities=[build_planning_capability(str(store.path))])
    result = asyncio.run(agent.run('query stock', deps=deps, run_id=deps.run_id, conversation_id=deps.session_id))
    parts = [part for part in returns(result.all_messages()) if part.tool_name == 'ktg_query_stock_summary']
    assert len(parts) == 2 and 'planning_evidence' in parts[0].metadata
    assert 'planning_evidence' not in parts[1].metadata
    evidence = stock_step_evidence(result.all_messages())
    assert set(evidence) == {'a'} and evidence['a'][0]['tool_call_id'] == 'stock1'
    assert evidence['a'][0]['no_records'] is True and evidence['a'][0]['queried_at'] == '2026-11-26T00:00:00+00:00'
    repository = SessionRepository(store.path)
    repository.settle_native_run(deps.owner, deps.run_id, 'completed', 'completed', {}, tuple(result.all_messages()))
    task = repository.get_task(deps.owner, deps.session_id, deps.run_id)
    assert 'stock1' in str(task) and 'stock2' not in str(task)


def test_permission_visibility_then_revocation_before_actual_execution(tmp_path: Path) -> None:
    mes = FakeMES()
    mes.permissions = ['mes:md:mditem:list']
    store, deps = setup(tmp_path, mes)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = returns(messages)
        if results:
            assert not info.function_tools
            assert results[-1].metadata['error_code'] == 'PERMISSION_DENIED'
            return ModelResponse(parts=[TextPart('denied')])
        assert {tool.name for tool in info.function_tools} == {'ktg_count_products'}
        mes.permissions = []
        return ModelResponse(parts=[ToolCallPart('ktg_count_products', {}, 'revoked')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    result = asyncio.run(agent.run('query', deps=deps))
    assert result.output == 'denied'
    assert not mes.business_reads


def test_permission_failure_and_emergency_switch_hide_tools(tmp_path: Path) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.function_tools
        return ModelResponse(parts=[TextPart('unavailable')])

    mes.permissions = None
    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    assert asyncio.run(agent.run('query', deps=deps)).output == 'unavailable'
    mes.permissions = ['*:*:*']
    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[
        build_mes_toolset(store, switches_provider=lambda: CatalogSwitches({}, {}, emergency_disabled=True))])
    assert asyncio.run(agent.run('query', deps=deps)).output == 'unavailable'
    assert not mes.business_reads


def test_owner_mismatch_and_switch_between_selection_and_execution(tmp_path: Path) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)
    switched = False

    def switches() -> CatalogSwitches:
        return CatalogSwitches({'md': True}, {'ktg_count_products': True}, emergency_disabled=switched)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal switched
        results = returns(messages)
        if results:
            assert results[-1].metadata['error_code'] == 'TOOL_DISABLED'
            assert not info.function_tools
            return ModelResponse(parts=[TextPart('disabled')])
        if not info.function_tools:
            return ModelResponse(parts=[TextPart('unavailable')])
        switched = True
        return ModelResponse(parts=[ToolCallPart('ktg_count_products', {}, 'disabled')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store, switches_provider=switches)])
    assert asyncio.run(agent.run('query', deps=replace(deps, owner='id:2'))).output == 'unavailable'
    assert asyncio.run(agent.run('query', deps=deps)).output == 'disabled'
    assert not mes.business_reads


def test_same_business_user_different_visitor_scope_is_denied(tmp_path: Path) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)
    first = KTGClient(base_url='http://test.invalid', authorization='Bearer test-first', scope_by_token=True,
                      transport=mes.read, write_transport=mes.write)
    second = KTGClient(base_url='http://test.invalid', authorization='Bearer test-second', scope_by_token=True,
                       transport=mes.read, write_transport=mes.write)
    identity = asyncio.run(first.get_current_user())

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.function_tools
        return ModelResponse(parts=[TextPart('wrong scope')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    assert asyncio.run(agent.run('query', deps=replace(deps, identity=identity,
        owner=owner_from_identity(identity), client=second))).output == 'wrong scope'
    assert not mes.business_reads


def test_model_can_call_independent_reads_concurrently(tmp_path: Path) -> None:
    class ConcurrentMES(FakeMES):
        async def read(self, url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
            if not url.endswith('/getInfo'):
                in_progress.append(url)
                if len(in_progress) == 2:
                    both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=1)
            return await super().read(url, headers, params, timeout)

    mes = ConcurrentMES()
    store, deps = setup(tmp_path, mes)
    both_entered = asyncio.Event()
    in_progress: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart('ktg_count_products', {}, 'count'),
                ToolCallPart('ktg_query_stock_summary', {'item_code': 'LAMP'}, 'stock')])
        return ModelResponse(parts=[TextPart('done')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    result = asyncio.run(agent.run('query both', deps=deps))
    assert len(in_progress) == 2
    assert all(part.metadata['status'] == 'succeeded' for part in returns(result.all_messages()))


def test_schema_validation_and_safe_runtime_failure(tmp_path: Path) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = returns(messages)
        if not results:
            return ModelResponse(parts=[ToolCallPart('ktg_count_products', {'bogus': 1}, 'invalid')])
        if len(results) == 1:
            assert results[0].metadata['error_code'] == 'VALIDATION_ERROR'
            assert 'allowed_arguments' in str(results[0].content)
            assert not mes.business_reads
            mes.raise_on_query = True
            return ModelResponse(parts=[ToolCallPart('ktg_count_products', {}, 'fail')])
        assert results[-1].metadata['error_code'] == 'TOOL_EXECUTION_ERROR'
        assert 'private diagnostic' not in str(results[-1].metadata)
        assert 'private diagnostic' not in str(results[-1].content)
        return ModelResponse(parts=[TextPart('done')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    result = asyncio.run(agent.run('query', deps=deps))
    assert result.output == 'done'
    assert len(mes.business_reads) == 1


def test_preparation_preserves_pending_plan_and_ui_metadata(tmp_path: Path) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)
    args: dict[str, Any] = {'mode': 'prepare', 'steps': [{'action': 'create_draft', 'step_id': 'create',
        'product_code': 'LAMP', 'quantity': 3, 'request_date': '2026-11-30', 'workorder_code': 'WO-LAMP'}]}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = returns(messages)
        if not results:
            return ModelResponse(parts=[ToolCallPart('ktg_prepare_workorder_operations', args, 'prepare')])
        assert '尚未写入 MES' in str(results[-1].content)
        return ModelResponse(parts=[TextPart('confirm')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    result = asyncio.run(agent.run('prepare', deps=deps))
    metadata = returns(result.all_messages())[-1].metadata
    assert metadata['details']['kind'] == 'workorder_operation_preview'
    assert metadata['operation_id'] == metadata['details']['plan_id']
    plan = store.get(deps.owner, deps.session_id, metadata['operation_id'])
    assert plan['status'] == 'pending'
    assert len(store.list_plans(deps.owner, deps.session_id)) == 1


def test_output_validation_and_finite_retry_respect_side_effect_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mes = FakeMES()
    store, deps = setup(tmp_path, mes)
    attempts = {'read': 0, 'proposal': 0}

    async def invalid_read(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        attempts['read'] += 1
        if attempts['read'] == 1:
            raise TimeoutError
        return ToolResult('invalid', details={'kind': 'wrong-fields'})

    async def timed_out_proposal(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        attempts['proposal'] += 1
        raise TimeoutError

    def query_tools() -> tuple[ToolDefinition, ...]:
        return tuple(replace(tool, execute=invalid_read) if tool.name == 'ktg_count_products' else tool
                     for tool in build_query_tools())

    def proposal_tool(operation_store: OperationPlanStore) -> ToolDefinition:
        return replace(build_workorder_operation_tool(operation_store), execute=timed_out_proposal)

    from ktg_agent.tools import mes_catalog
    monkeypatch.setattr(mes_catalog, 'build_query_tools', query_tools)
    monkeypatch.setattr(mes_catalog, 'build_workorder_operation_tool', proposal_tool)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart('ktg_count_products', {}, 'invalid-return'),
                ToolCallPart('ktg_prepare_workorder_operations', {'mode': 'inspect', 'product_code': 'LAMP'}, 'timeout')])
        return ModelResponse(parts=[TextPart('done')])

    agent = Agent(FunctionModel(model), deps_type=MesToolDeps, toolsets=[build_mes_toolset(store)])
    result = asyncio.run(agent.run('query', deps=deps))
    parts = {part.tool_call_id: part for part in returns(result.all_messages())}
    assert parts['invalid-return'].metadata['error_code'] == 'INVALID_TOOL_RESULT'
    assert parts['timeout'].metadata['error_code'] == 'TOOL_TIMEOUT'
    assert attempts == {'read': 2, 'proposal': 1}
