"""Strict JSON data and business-tool call validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypeAlias
from collections.abc import Mapping

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
JsonObject: TypeAlias = dict[str, JsonValue]


class MessageValidationError(ValueError):
    """Raised when a message cannot satisfy the core message contract."""


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MessageValidationError(f"{label} must be a non-empty string")
    return value


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MessageValidationError(
                f"{label} must contain only finite JSON numbers"
            )
        return value
    if isinstance(value, list):
        return [_json_value(item, label) for item in value]
    if isinstance(value, Mapping):
        converted: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MessageValidationError(f"{label} keys must be strings")
            converted[key] = _json_value(item, label)
        return converted
    raise MessageValidationError(f"{label} must contain only JSON values")


def _json_object(value: object, label: str) -> JsonObject:
    converted = _json_value(value, label)
    if not isinstance(converted, dict):
        raise MessageValidationError(f"{label} must be an object")
    return converted


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        _required_text(self.id, "tool call id")
        _required_text(self.name, "tool name")
        object.__setattr__(self, "arguments", _json_object(self.arguments, "tool arguments"))

