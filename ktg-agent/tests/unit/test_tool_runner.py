from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from ktg_agent.core.messages import (
    AssistantMessage,
    JsonObject,
    MessageValidationError,
    ToolCall,
    ToolResultMessage,
    project_model_messages,
)
from ktg_agent.tools import (
    ExecutionMode,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolExecute,
    ToolDefinitionError,
    DuplicateToolError,
    ToolRegistry,
    ToolResult,
    ToolRunner,
    ToolStatus,
)
from ktg_agent.tools.registry import ToolChannel, RetryPolicy


INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"item_id": {"type": "integer"}},
    "required": ["item_id"],
}


class CountingTool:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def __call__(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        item_id = arguments["item_id"]
        return ToolResult(
            content=f"Item {item_id} has 12 units",
            details={"item_id": item_id, "quantity": 12},
            operation_id=context.operation_id,
        )


def definition(execute: ToolExecute) -> ToolDefinition:
    return ToolDefinition(
        name="ktg.query_stock",
        version="1.0",
        description="Find current stock",
        label="Query stock",
        input_schema=INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "quantity": {"type": "integer"},
            },
            "required": ["item_id", "quantity"],
            "additionalProperties": False,
        },
        required_permissions=("stock:read",),
        effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL,
        timeout_seconds=5.0,
        execute=execute,
    )


def run(runner: ToolRunner, call: ToolCall) -> ToolResult:
    return asyncio.run(
        runner.run(
            call,
            ToolContext(
                run_id="run-1",
                tool_call_id=call.id,
                operation_id="op-1",
            ),
        )
    )


def test_registry_exposes_only_registered_tools_to_the_model() -> None:
    registry = ToolRegistry([definition(CountingTool())])

    assert registry.get("ktg.query_stock").name == "ktg.query_stock"
    assert registry.model_schemas()[0].input_schema["additionalProperties"] is False


def test_registry_rejects_duplicate_names() -> None:
    tool = definition(CountingTool())

    with pytest.raises(DuplicateToolError, match="already registered"):
        ToolRegistry([tool, tool])


def test_definition_rejects_invalid_json_schema() -> None:
    with pytest.raises(ToolDefinitionError, match="invalid tool input schema"):
        ToolDefinition(
            name="ktg.invalid",
            version="1.0",
            description="Invalid test tool",
            label="Invalid",
            input_schema={"type": "object", "required": "not-an-array"},
            output_schema=None,
            required_permissions=(),
            effect=ToolEffect.READ,
            execution_mode=ExecutionMode.PARALLEL,
            timeout_seconds=5,
            execute=CountingTool(),
        )


def test_unregistered_tool_does_not_execute_any_registered_callable() -> None:
    execute = CountingTool()
    runner = ToolRunner(ToolRegistry([definition(execute)]))

    result = run(runner, ToolCall("call-1", "python.eval", {"item_id": 42}))

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "TOOL_NOT_FOUND"
    assert execute.calls == 0


def test_hidden_tool_requires_server_confirmation_context() -> None:
    execute = CountingTool()
    hidden = ToolDefinition(
        name="ktg.hidden", version="1.0", description="Hidden action", label="Action",
        input_schema=INPUT_SCHEMA, output_schema=None, required_permissions=("stock:write",),
        effect=ToolEffect.WRITE, execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=1, execute=execute, expose_to_model=False,
    )
    runner = ToolRunner(ToolRegistry([hidden]))
    call = ToolCall("call-1", "ktg.hidden", {"item_id": 42, "confirmed": True})
    model_result = asyncio.run(runner.run(call, ToolContext(
        run_id="run-1", tool_call_id=call.id, operation_id="guessed")))
    assert model_result.error_code == "TOOL_NOT_FOUND"
    assert execute.calls == 0

    invalid = asyncio.run(runner.run(
        ToolCall("call-2", "ktg.hidden", {"item_id": 42}),
        ToolContext(run_id="run-1", tool_call_id="call-2", operation_id="guessed",
                    channel=ToolChannel.TRUSTED_CONFIRMATION),
    ))
    assert invalid.error_code == "CONFIRMATION_INVALID"
    assert execute.calls == 0

    valid = asyncio.run(runner.run(
        ToolCall("call-3", "ktg.hidden", {"item_id": 42}),
        ToolContext(run_id="run-1", tool_call_id="call-3", operation_id="op-1",
                    identity={"userId": 7}, channel=ToolChannel.TRUSTED_CONFIRMATION,
                    confirmation_id="confirmation-1"),
    ))
    assert valid.status is ToolStatus.SUCCEEDED
    assert valid.operation_id == "op-1"
    assert execute.calls == 1


def test_write_timeout_is_unknown_and_not_retried() -> None:
    class SlowWrite:
        calls = 0

        async def __call__(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
            self.calls += 1
            await asyncio.sleep(0.02)
            return ToolResult(content="done")

    execute = SlowWrite()
    base = definition(execute)
    write = ToolDefinition(
        name=base.name, version=base.version, description=base.description,
        label=base.label, input_schema=base.input_schema, output_schema=None,
        required_permissions=base.required_permissions, effect=ToolEffect.WRITE,
        execution_mode=ExecutionMode.SEQUENTIAL, timeout_seconds=0.001,
        execute=execute, retry_policy=RetryPolicy.RECONCILE_FIRST,
    )
    result = run(ToolRunner(ToolRegistry([write])), ToolCall("call-1", write.name, {"item_id": 42}))
    assert result.status is ToolStatus.UNKNOWN
    assert result.error_code == "RESULT_UNKNOWN"
    assert execute.calls == 1


def test_bounded_read_retries_once_but_sequential_preview_does_not() -> None:
    class FailsOnce:
        calls = 0

        async def __call__(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return ToolResult(content="read succeeded")

    read_execute = FailsOnce()
    base = definition(read_execute)
    bounded = ToolDefinition(
        name=base.name, version=base.version, description=base.description,
        label=base.label, input_schema=base.input_schema, output_schema=None,
        required_permissions=base.required_permissions, effect=ToolEffect.READ,
        execution_mode=ExecutionMode.PARALLEL, timeout_seconds=1,
        execute=read_execute, retry_policy=RetryPolicy.BOUNDED_READ,
    )
    result = run(ToolRunner(ToolRegistry([bounded])), ToolCall("call-1", bounded.name, {"item_id": 42}))
    assert result.status is ToolStatus.SUCCEEDED
    assert read_execute.calls == 2

    preview_execute = FailsOnce()
    preview = ToolDefinition(
        name=base.name, version=base.version, description=base.description,
        label=base.label, input_schema=base.input_schema, output_schema=None,
        required_permissions=base.required_permissions, effect=ToolEffect.READ,
        execution_mode=ExecutionMode.SEQUENTIAL, timeout_seconds=1,
        execute=preview_execute, retry_policy=RetryPolicy.BOUNDED_READ,
    )
    result = run(ToolRunner(ToolRegistry([preview])), ToolCall("call-2", preview.name, {"item_id": 42}))
    assert result.status is ToolStatus.FAILED
    assert preview_execute.calls == 1


def test_missing_required_argument_does_not_execute() -> None:
    execute = CountingTool()
    runner = ToolRunner(ToolRegistry([definition(execute)]))

    result = run(runner, ToolCall("call-1", "ktg.query_stock", {}))

    assert result.error_code == "VALIDATION_ERROR"
    assert execute.calls == 0


def test_wrong_argument_type_does_not_execute() -> None:
    execute = CountingTool()
    runner = ToolRunner(ToolRegistry([definition(execute)]))

    result = run(
        runner,
        ToolCall("call-1", "ktg.query_stock", {"item_id": "forty-two"}),
    )

    assert result.error_code == "VALIDATION_ERROR"
    assert execute.calls == 0


def test_extra_argument_is_rejected_even_when_schema_omits_the_policy() -> None:
    execute = CountingTool()
    runner = ToolRunner(ToolRegistry([definition(execute)]))

    result = run(
        runner,
        ToolCall(
            "call-1",
            "ktg.query_stock",
            {"item_id": 42, "url": "https://untrusted.invalid"},
        ),
    )

    assert result.error_code == "VALIDATION_ERROR"
    assert execute.calls == 0


def test_before_hook_can_reject_without_executing_tool() -> None:
    execute = CountingTool()

    async def deny_write_like_request(
        context: ToolContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> str | None:
        return "permission denied"

    runner = ToolRunner(
        ToolRegistry([definition(execute)]), before_hooks=(deny_write_like_request,)
    )

    result = run(
        runner, ToolCall("call-1", "ktg.query_stock", {"item_id": 42})
    )

    assert result.error_code == "POLICY_REJECTED"
    assert result.details == {"reason": "permission denied"}
    assert execute.calls == 0


def test_after_hook_observes_normalized_success_result() -> None:
    execute = CountingTool()
    observed: list[ToolResult] = []

    async def observe(
        context: ToolContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        result: ToolResult,
    ) -> None:
        observed.append(result)

    runner = ToolRunner(ToolRegistry([definition(execute)]), after_hooks=(observe,))

    result = run(
        runner, ToolCall("call-1", "ktg.query_stock", {"item_id": 42})
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert observed == [result]
    assert execute.calls == 1


def test_tool_exception_becomes_error_result_and_preserves_call_id() -> None:
    execute = CountingTool(error=RuntimeError("backend unavailable"))
    runner = ToolRunner(ToolRegistry([definition(execute)]))

    result = run(
        runner, ToolCall("call-7", "ktg.query_stock", {"item_id": 42})
    )
    message = result.to_message("call-7")

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "TOOL_EXECUTION_ERROR"
    assert isinstance(message, ToolResultMessage)
    assert message.tool_call_id == "call-7"
    assert message.is_error is True
    assert execute.calls == 1


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_tool_result_rejects_non_finite_details(value: float) -> None:
    with pytest.raises(MessageValidationError, match="finite JSON numbers"):
        ToolResult(content="invalid", details={"quantity": value})


def test_invalid_structured_output_is_normalized() -> None:
    class InvalidOutputTool:
        async def __call__(
            self, context: ToolContext, arguments: Mapping[str, Any]
        ) -> ToolResult:
            return ToolResult(content="Incomplete", details={"item_id": 42})

    runner = ToolRunner(ToolRegistry([definition(InvalidOutputTool())]))

    result = run(
        runner, ToolCall("call-1", "ktg.query_stock", {"item_id": 42})
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "INVALID_TOOL_RESULT"


def test_before_hook_exception_is_normalized_without_executing_tool() -> None:
    execute = CountingTool()

    async def broken_hook(
        context: ToolContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> str | None:
        raise RuntimeError("policy service unavailable")

    runner = ToolRunner(ToolRegistry([definition(execute)]), before_hooks=(broken_hook,))

    result = run(
        runner, ToolCall("call-1", "ktg.query_stock", {"item_id": 42})
    )

    assert result.error_code == "PRE_HOOK_ERROR"
    assert execute.calls == 0


def test_model_projection_keeps_summary_but_removes_structured_details() -> None:
    result = ToolResult(
        content="12 units",
        details={"warehouse": "A", "quantity": 12},
    )
    message = result.to_message("call-1")

    projected = project_model_messages(
        (
            # Projection validates that every result has a matching call.
            AssistantMessage(
                content=(ToolCall("call-1", "ktg.query_stock", {"item_id": 42}),)
            ),
            message,
        )
    )

    projected_result = projected[1]
    assert isinstance(projected_result, ToolResultMessage)
    assert projected_result.content[0].text == "12 units"
    assert projected_result.details is None
    assert message.details == {"warehouse": "A", "quantity": 12}
