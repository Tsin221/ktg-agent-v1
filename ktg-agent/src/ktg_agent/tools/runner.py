"""Validated execution through the tool allow-list."""

from __future__ import annotations

import asyncio
from itertools import islice
import json

from jsonschema import Draft202012Validator, ValidationError

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.tools.registry import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    RetryPolicy,
)


def _input_error_reason(error: ValidationError) -> str:
    branches = error.validator_value
    if error.validator == "oneOf" and isinstance(branches, list) and branches and all(
        isinstance(branch, dict) and branch.keys() == {"required"}
        and isinstance(branch["required"], list) and len(branch["required"]) == 1
        for branch in branches
    ):
        fields = " 或 ".join(branch["required"][0] for branch in branches)
        return f"必须且只能提供 {fields}。"
    return error.message[:400]


class ToolRunner:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

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

        if not tool.expose_to_model or tool.effect is not ToolEffect.READ:
            return self._error("Tool is not available.", "TOOL_NOT_FOUND", context)

        validation_errors = list(islice(
            Draft202012Validator(tool.input_schema).iter_errors(call.arguments), 5,
        ))
        if validation_errors:
            errors = [{"path": error.json_path, "reason": _input_error_reason(error)}
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

        try:
            attempts = 2 if (
                tool.execution_mode is ExecutionMode.PARALLEL
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
            result = self._error("Tool execution timed out.", "TOOL_TIMEOUT", context)
        except Exception as exc:
            result = ToolResult(
                content="Tool execution failed.",
                status=ToolStatus.FAILED,
                error_code="TOOL_EXECUTION_ERROR",
                operation_id=context.operation_id,
                details={
                    "exception_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
        else:
            result = self._validate_output(tool, result, context)

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
