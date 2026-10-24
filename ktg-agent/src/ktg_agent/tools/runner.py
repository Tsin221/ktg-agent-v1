"""Validated execution through the tool allow-list."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
import json
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.tools.catalog import CandidateSnapshot, CatalogSwitches, ToolCatalog
from ktg_agent.tools.permissions import PermissionDecision, evaluate_permission_policy
from ktg_agent.tools.registry import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    ToolChannel,
    RetryPolicy,
)


class BeforeToolHook(Protocol):
    def __call__(
        self,
        context: ToolContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> Awaitable[str | None]: ...


class AfterToolHook(Protocol):
    def __call__(
        self,
        context: ToolContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        result: ToolResult,
    ) -> Awaitable[ToolResult | None]: ...


@dataclass(frozen=True, slots=True)
class CallAuthorization:
    permissions: frozenset[str] | None
    switches: CatalogSwitches
    candidate: CandidateSnapshot | None = None
    active_names: frozenset[str] | None = None


type AuthorizationProvider = Callable[[ToolContext], Awaitable[CallAuthorization]]


class ToolRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        before_hooks: Sequence[BeforeToolHook] = (),
        after_hooks: Sequence[AfterToolHook] = (),
        catalog: ToolCatalog | None = None,
        authorization_provider: AuthorizationProvider | None = None,
    ) -> None:
        if (catalog is None) != (authorization_provider is None):
            raise ValueError("catalog and authorization provider must be configured together")
        self._registry = registry
        self._before_hooks = tuple(before_hooks)
        self._after_hooks = tuple(after_hooks)
        self._catalog = catalog
        self._authorization_provider = authorization_provider

    def can_run_batch_in_parallel(self, calls: Sequence[ToolCall]) -> bool:
        """Only independent, explicitly parallel reads may overlap."""
        if not calls:
            return False
        try:
            tools = tuple(self._registry.get(call.name) for call in calls)
        except KeyError:
            return False
        return all(
            tool.effect is ToolEffect.READ
            and tool.execution_mode is ExecutionMode.PARALLEL
            for tool in tools
        )

    async def run(self, call: ToolCall, context: ToolContext) -> ToolResult:
        if context.tool_call_id != call.id:
            return self._error(
                "Tool execution context does not match the call.",
                "CONTEXT_MISMATCH",
                context,
            )

        try:
            tool = self._registry.get(call.name)
        except KeyError:
            return self._error(
                "Tool is not available.",
                "TOOL_NOT_FOUND",
                context,
            )

        if context.channel is ToolChannel.MODEL and not tool.expose_to_model:
            return self._error("Tool is not available.", "TOOL_NOT_FOUND", context)
        if context.channel is ToolChannel.TRUSTED_CONFIRMATION and (
            tool.expose_to_model or context.identity is None
            or context.confirmation_id is None or context.operation_id is None
        ):
            return self._error("Confirmation is invalid.", "CONFIRMATION_INVALID", context)

        if self._catalog is not None and self._authorization_provider is not None:
            if context.identity is None:
                return self._error("Permission service is unavailable.", "PERMISSION_UNAVAILABLE", context)
            try:
                manifest = self._catalog.manifest(call.name)
            except KeyError:
                return self._error("Tool is not available.", "TOOL_NOT_FOUND", context)
            if manifest.version != tool.version:
                return self._error("Tool version changed.", "TOOL_VERSION_MISMATCH", context)
            if (context.channel is ToolChannel.TRUSTED_CONFIRMATION
                    and manifest.confirmation_policy != "trusted_confirmation"):
                return self._error("Confirmation is invalid.", "CONFIRMATION_INVALID", context)
            try:
                authorization = await self._authorization_provider(context)
            except Exception:
                return self._error("Permission service is unavailable.", "PERMISSION_UNAVAILABLE", context)
            if not isinstance(authorization, CallAuthorization) or not isinstance(authorization.switches, CatalogSwitches):
                return self._error("Permission service is unavailable.", "PERMISSION_UNAVAILABLE", context)
            if not manifest.expose_to_model and context.channel is not ToolChannel.TRUSTED_CONFIRMATION:
                return self._error("Tool is not available.", "TOOL_NOT_FOUND", context)
            if not authorization.switches.enabled(manifest):
                return self._error("Tool is disabled.", "TOOL_DISABLED", context)
            decision = evaluate_permission_policy(manifest.permission_policy, authorization.permissions)
            if decision is not PermissionDecision.ALLOWED:
                return self._error("Permission is unavailable." if decision is PermissionDecision.UNAVAILABLE
                                   else "Permission denied.", decision.value, context)
            if context.channel is ToolChannel.MODEL:
                candidate = authorization.candidate
                if candidate is None or candidate.run_id != context.run_id:
                    return self._error("Tool is not available.", "TOOL_NOT_FOUND", context)
                if candidate.catalog_version != self._catalog.catalog_version:
                    return self._error("Tool version changed.", "TOOL_VERSION_MISMATCH", context)
                if candidate.candidates.get(call.name) != manifest.version:
                    return self._error("Tool is not available.", "TOOL_NOT_FOUND", context)
                if authorization.active_names is None or call.name not in authorization.active_names:
                    return self._error("Tool is not active.", "TOOL_NOT_ACTIVE", context)

        validation_errors = list(islice(
            Draft202012Validator(tool.input_schema).iter_errors(call.arguments), 5,
        ))
        if validation_errors:
            errors = [{"path": error.json_path, "reason": error.message[:400]}
                      for error in validation_errors]
            properties = tool.input_schema.get("properties")
            # Diagnostic details are deliberately stripped from model messages.
            # Put the repair contract in content so the next request can use it.
            feedback = {
                "error_code": "VALIDATION_ERROR",
                "errors": errors,
                "required": tool.input_schema.get("required", []),
                "allowed_arguments": list(properties) if isinstance(properties, dict) else [],
                "next_step": "按当前工具 schema 修正参数后再调用；不要重复原参数。缺少用户信息时询问用户。",
            }
            return self._error(
                "Tool arguments are invalid. " + json.dumps(feedback, ensure_ascii=False),
                "VALIDATION_ERROR",
                context,
                details={
                    "path": validation_errors[0].json_path,
                    "reason": validation_errors[0].message,
                },
            )

        for before_hook in self._before_hooks:
            try:
                rejection = await before_hook(context, tool, call.arguments)
            except Exception as exc:
                return self._error(
                    "Tool pre-execution check failed.",
                    "PRE_HOOK_ERROR",
                    context,
                    details={
                        "exception_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                )
            if rejection is not None:
                return self._error(
                    "Tool execution was rejected by policy.",
                    "POLICY_REJECTED",
                    context,
                    details={"reason": rejection},
                )

        try:
            attempts = 2 if (
                tool.effect is ToolEffect.READ
                and tool.execution_mode is ExecutionMode.PARALLEL
                and tool.retry_policy is RetryPolicy.BOUNDED_READ
            ) else 1
            for attempt in range(attempts):
                try:
                    async with asyncio.timeout(tool.timeout_seconds):
                        result = await tool.execute(context, call.arguments)
                    break
                except TimeoutError:
                    if attempt + 1 == attempts:
                        raise
            if not isinstance(result, ToolResult):
                raise TypeError("tool execute must return ToolResult")
        except TimeoutError:
            result = (ToolResult(content="Tool result is unknown.", status=ToolStatus.UNKNOWN,
                                 error_code="RESULT_UNKNOWN", operation_id=context.operation_id)
                      if tool.effect is ToolEffect.WRITE else self._error(
                          "Tool execution timed out.", "TOOL_TIMEOUT", context))
        except Exception as exc:
            result = ToolResult(
                content="Tool execution failed.",
                status=ToolStatus.UNKNOWN if tool.effect is ToolEffect.WRITE else ToolStatus.FAILED,
                error_code="TOOL_EXECUTION_ERROR",
                operation_id=context.operation_id,
                details={
                    "exception_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
        else:
            result = self._validate_output(tool, result, context)

        for after_hook in self._after_hooks:
            replacement = await after_hook(context, tool, call.arguments, result)
            if replacement is not None:
                if not isinstance(replacement, ToolResult):
                    raise TypeError("after hook must return ToolResult or None")
                result = replacement
        return result

    def _validate_output(
        self,
        tool: ToolDefinition,
        result: ToolResult,
        context: ToolContext,
    ) -> ToolResult:
        if tool.output_schema is None or result.status is not ToolStatus.SUCCEEDED:
            return result
        validation_error = next(
            iter(Draft202012Validator(tool.output_schema).iter_errors(result.details)),
            None,
        )
        if validation_error is None:
            return result
        return self._error(
            "Tool returned an invalid result.",
            "INVALID_TOOL_RESULT",
            context,
            details={
                "path": validation_error.json_path,
                "reason": validation_error.message,
            },
        )

    @staticmethod
    def _error(
        content: str,
        error_code: str,
        context: ToolContext,
        *,
        details: JsonObject | None = None,
    ) -> ToolResult:
        return ToolResult(
            content=content,
            details=details,
            status=ToolStatus.FAILED,
            error_code=error_code,
            operation_id=context.operation_id,
        )
