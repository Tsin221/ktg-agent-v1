"""Definitions and the allow-list registry for executable tools."""

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ktg_agent.core.messages import JsonObject, _json_object, _required_text


class ToolDefinitionError(ValueError):
    """Raised when a tool definition is unsafe or internally inconsistent."""


class DuplicateToolError(ToolDefinitionError):
    """Raised when two definitions claim the same stable tool name."""


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"


class ExecutionMode(StrEnum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class RetryPolicy(StrEnum):
    BOUNDED_READ = "bounded_read"
    RECONCILE_FIRST = "reconcile_first"
    NEVER = "never"


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Server-owned execution data that is never supplied by the model."""

    run_id: str
    tool_call_id: str
    operation_id: str | None = None
    identity: object | None = None
    ktg_client: object | None = None

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run id")
        _required_text(self.tool_call_id, "tool call id")
        if self.operation_id is not None:
            _required_text(self.operation_id, "operation id")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Normalized result with model summary separated from UI details."""

    content: str
    details: JsonObject | None = None
    status: ToolStatus = ToolStatus.SUCCEEDED
    error_code: str | None = None
    operation_id: str | None = None
    verification: JsonObject | None = None

    def __post_init__(self) -> None:
        _required_text(self.content, "tool result content")
        if not isinstance(self.status, ToolStatus):
            raise ToolDefinitionError("tool result status must be a ToolStatus")
        if self.details is not None:
            object.__setattr__(self, "details", _json_object(self.details, "tool details"))
        if self.verification is not None:
            object.__setattr__(
                self,
                "verification",
                _json_object(self.verification, "tool verification"),
            )
        if self.error_code is not None:
            _required_text(self.error_code, "tool error code")
        if self.operation_id is not None:
            _required_text(self.operation_id, "operation id")
        if self.status is ToolStatus.SUCCEEDED and self.error_code is not None:
            raise ToolDefinitionError("successful tool result cannot have an error code")
        if self.status is not ToolStatus.SUCCEEDED and self.error_code is None:
            raise ToolDefinitionError("non-success tool result requires an error code")

    @property
    def is_error(self) -> bool:
        return self.status is not ToolStatus.SUCCEEDED


class ToolExecute(Protocol):
    def __call__(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> Awaitable[ToolResult]: ...


def _strict_object_schema(value: object, label: str) -> JsonObject:
    schema = deepcopy(_json_object(value, label))
    if schema.get("type") != "object":
        raise ToolDefinitionError(f"{label} root type must be object")
    schema.setdefault("additionalProperties", False)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolDefinitionError(f"invalid {label}: {exc.message}") from exc
    return schema


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    label: str
    input_schema: JsonObject
    output_schema: JsonObject | None
    required_permissions: tuple[str, ...]
    effect: ToolEffect
    execution_mode: ExecutionMode
    timeout_seconds: float
    execute: ToolExecute
    retry_policy: RetryPolicy = RetryPolicy.NEVER
    expose_to_model: bool = True

    def __post_init__(self) -> None:
        _required_text(self.name, "tool name")
        _required_text(self.version, "tool version")
        _required_text(self.description, "tool description")
        _required_text(self.label, "tool label")
        object.__setattr__(
            self,
            "input_schema",
            _strict_object_schema(self.input_schema, "tool input schema"),
        )
        if self.output_schema is not None:
            object.__setattr__(
                self,
                "output_schema",
                _strict_object_schema(self.output_schema, "tool output schema"),
            )
        if not isinstance(self.required_permissions, tuple) or any(
            not isinstance(permission, str) or not permission.strip()
            for permission in self.required_permissions
        ):
            raise ToolDefinitionError(
                "required_permissions must be a tuple of non-empty strings"
            )
        if not isinstance(self.effect, ToolEffect):
            raise ToolDefinitionError("effect must be a ToolEffect")
        if not isinstance(self.execution_mode, ExecutionMode):
            raise ToolDefinitionError("execution_mode must be an ExecutionMode")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise ToolDefinitionError("retry_policy must be a RetryPolicy")
        if (
            not isinstance(self.timeout_seconds, int | float)
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ToolDefinitionError("timeout_seconds must be positive")
        if not callable(self.execute):
            raise ToolDefinitionError("execute must be callable")
        if not isinstance(self.expose_to_model, bool):
            raise ToolDefinitionError("expose_to_model must be a boolean")


class ToolRegistry:
    """The sole allow-list from which the runner resolves executable tools."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise DuplicateToolError(f"tool is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        return self._definitions[name]

