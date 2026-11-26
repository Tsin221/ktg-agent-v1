"""Pydantic AI registration of the existing eight reviewed MES tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext, Tool, ToolReturn
from pydantic_ai.toolsets import AbstractToolset, DynamicToolset, FunctionToolset

from ktg_agent.core.messages import JsonObject, MessageValidationError, ToolCall
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.queries import STOCK_SUMMARY_TOOL
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.tools.catalog import CatalogSwitches, ToolManifest
from ktg_agent.tools.mes_catalog import build_mes_catalog, mes_definitions
from ktg_agent.tools.permissions import PermissionDecision, evaluate_permission_policy
from ktg_agent.tools.registry import ExecutionMode, ToolContext, ToolDefinition, ToolEffect, ToolRegistry, ToolResult, ToolStatus
from ktg_agent.tools.runner import ToolRunner


@dataclass(frozen=True, slots=True, repr=False)
class MesToolDeps:
    """Built only by the trusted HTTP/runtime layer, never from model arguments.

    The owner, session and run IDs must come from the same admitted RunRecord.
    """

    run_id: str
    session_id: str
    owner: str
    identity: JsonObject
    client: KTGClient
    task_id: str | None = None


async def _permissions(deps: MesToolDeps) -> frozenset[str] | None:
    try:
        if owner_from_identity(deps.identity) != deps.owner or not isinstance(deps.client, KTGClient):
            return None
        async with asyncio.timeout(15):
            # Demo visitor scope can separate users sharing the same business ID.
            # The existing client derives this scope from its own credential.
            if deps.identity.get('_agent_scope') and owner_from_identity(await deps.client.get_current_user()) != deps.owner:
                return None
            info = await deps.client.get('/getInfo')
        user, raw = info.get('user'), info.get('permissions')
        if (not isinstance(user, dict) or type(user.get('userId')) is not int
                or user['userId'] != deps.identity.get('userId')
                or not isinstance(raw, list | set | tuple) or any(not isinstance(p, str) for p in raw)):
            return None
        return frozenset(raw)
    except Exception:
        # Fail closed; upstream error text may contain request secrets.
        return None


def _decision(manifest: ToolManifest, permissions: frozenset[str] | None, switches: CatalogSwitches | None) -> str | None:
    if switches is None or permissions is None:
        return 'PERMISSION_UNAVAILABLE'
    if not manifest.expose_to_model or manifest.effect is not ToolEffect.READ:
        return 'TOOL_NOT_FOUND'
    if not switches.enabled(manifest):
        return 'TOOL_DISABLED'
    decision = evaluate_permission_policy(manifest.permission_policy, permissions)
    return None if decision is PermissionDecision.ALLOWED else decision.value


def _denied(error_code: str) -> ToolResult:
    message = {
        'PERMISSION_UNAVAILABLE': '无法核实当前账号业务权限。',
        'PERMISSION_DENIED': '当前账号不具备此业务权限。',
        'TOOL_NOT_FOUND': '工具不可用。',
        'TOOL_DISABLED': '工具已停用。',
        'CONTEXT_MISMATCH': '工具运行上下文不完整。',
    }[error_code]
    return ToolResult(message, status=ToolStatus.FAILED, error_code=error_code)


def _native_result(result: ToolResult, planning_evidence: JsonObject | None = None) -> ToolReturn:
    # ToolReturn.metadata stays in native history/events, not the provider's prompt.
    # https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/#advanced-tool-returns
    return ToolReturn(
        return_value=result.content if not result.error_code else f'[{result.error_code}] {result.content}',
        metadata={
            'status': result.status.value,
            'error_code': result.error_code,
            # Runner diagnostics can contain untrusted exception text. Only expose
            # business-validated success details; errors already have safe summaries.
            'details': result.details if result.status is ToolStatus.SUCCEEDED else None,
            'operation_id': result.operation_id,
            'verification': result.verification if result.status is ToolStatus.SUCCEEDED else None,
            **({'planning_evidence': planning_evidence} if planning_evidence is not None else {}),
        },
    )


def build_mes_toolset(
    operation_store: OperationPlanStore,
    *,
    switches_provider: Callable[[], CatalogSwitches] | None = None,
) -> AbstractToolset[MesToolDeps]:
    """Reuse business schemas, execution contracts and permission policies directly.

    No model-controlled search/load, activation snapshot, or confirmation tool.
    """
    definitions = mes_definitions(operation_store)
    catalog = build_mes_catalog(definitions)
    manifests = {definition.name: catalog.manifest(definition.name) for definition in definitions}
    defaults = CatalogSwitches({item.module: True for item in manifests.values()}, {name: True for name in manifests})
    provider = switches_provider or (lambda: defaults)
    # Reuse the existing JSON Schema validation, business errors, invocation timeouts
    # and narrowly bounded read retries. Native SDK owns tool selection and scheduling.
    runner = ToolRunner(ToolRegistry(definitions))

    def current_switches() -> CatalogSwitches | None:
        try:
            switches = provider()
            return switches if isinstance(switches, CatalogSwitches) else None
        except Exception:
            return None

    def register(definition: ToolDefinition) -> Tool[MesToolDeps]:
        manifest = manifests[definition.name]

        async def execute(ctx: RunContext[MesToolDeps], /, **arguments: Any) -> ToolReturn:
            deps = ctx.deps
            # Check again at the last responsible moment; visibility is not authorization.
            permissions = await _permissions(deps)
            if (reason := _decision(manifest, permissions, current_switches())) is not None:
                return _native_result(_denied(reason))
            if not ctx.tool_call_id:
                return _native_result(_denied('CONTEXT_MISMATCH'))
            identity = {**deps.identity, '_agent_session_id': deps.session_id}
            try:
                call = ToolCall(ctx.tool_call_id, definition.name, arguments)
            except MessageValidationError:
                return _native_result(ToolResult('工具参数必须符合 JSON，请修正后调用。',
                    status=ToolStatus.FAILED, error_code='VALIDATION_ERROR'))
            # Capture a stable, unique active Planning step across an actual
            # authorized stock query. Planning metadata is not model-supplied.
            # An absent or changing plan does not invalidate the business read.
            from ktg_agent.sessions.native_planning import active_stock_step, planning_task_id
            before = None
            if definition.name == STOCK_SUMMARY_TOOL:
                try:
                    before = await active_stock_step(str(operation_store.path), deps)
                except Exception:
                    pass
            result = await runner.run(call, ToolContext(
                run_id=deps.run_id, tool_call_id=ctx.tool_call_id, identity=identity, ktg_client=deps.client,
            ))
            evidence: JsonObject | None = None
            if before is not None and result.status is ToolStatus.SUCCEEDED and result.details is not None:
                try:
                    after = await active_stock_step(str(operation_store.path), deps)
                    if after == before and result.details.get('kind') == 'stock_summary':
                        evidence = {'task_id': planning_task_id(deps), 'step_id': before.id,
                                    'step_description': before.content}
                except Exception:
                    pass
            return _native_result(result, evidence)

        # from_schema intentionally bypasses native argument validation; runner
        # preserves the existing schema and repair feedback without duplicate types.
        # https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/#custom-tool-schema
        return Tool.from_schema(
            function=execute, name=definition.name, description=definition.description,
            json_schema=definition.input_schema, takes_ctx=True,
            sequential=definition.execution_mode is ExecutionMode.SEQUENTIAL,
        )

    native = FunctionToolset[MesToolDeps](tools=[register(definition) for definition in definitions], max_retries=0)

    async def visible(ctx: RunContext[MesToolDeps]) -> AbstractToolset[MesToolDeps]:
        # One permission request per framework step rather than one per visible tool.
        # https://pydantic.dev/docs/ai/tools-toolsets/toolsets/#dynamically-building-a-toolset
        permissions = await _permissions(ctx.deps)
        switches = current_switches()
        return native.filtered(lambda _, tool: _decision(manifests[tool.name], permissions, switches) is None)

    return DynamicToolset(visible, per_run_step=True, id='ktg-mes')
