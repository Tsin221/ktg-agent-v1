"""Validated execution through the tool allow-list."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.tools.registry import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
    ToolStatus,
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


class ToolRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        before_hooks: Sequence[BeforeToolHook] = (),
        after_hooks: Sequence[AfterToolHook] = (),
    ) -> None:
        self._registry = registry
        self._before_hooks = tuple(before_hooks)
        self._after_hooks = tuple(after_hooks)

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
                f"Tool is not available: {call.name}",
                "TOOL_NOT_FOUND",
                context,
            )

        validation_error = next(
            iter(Draft202012Validator(tool.input_schema).iter_errors(call.arguments)),
            None,
        )
        if validation_error is not None:
            return self._error(
                "Tool arguments are invalid.",
                "VALIDATION_ERROR",
                context,
                details={
                    "path": validation_error.json_path,
                    "reason": validation_error.message,
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
            if tool.effect is ToolEffect.WRITE:
                result = await tool.execute(context, call.arguments)
            else:
                async with asyncio.timeout(tool.timeout_seconds):
                    result = await tool.execute(context, call.arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("tool execute must return ToolResult")
        except TimeoutError:
            result = self._error(
                "Tool execution timed out.",
                "TOOL_TIMEOUT",
                context,
            )
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
