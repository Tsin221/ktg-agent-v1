"""Fail-closed MES permission expressions for catalog and call-time checks."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


class PermissionPolicyError(ValueError):
    """An expression cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class PermissionLeaf:
    permission: str

    def __post_init__(self) -> None:
        if not isinstance(self.permission, str) or not self.permission.strip():
            raise PermissionPolicyError("permission must be a non-empty string")
        if "*" in self.permission and self.permission != "*:*:*":
            raise PermissionPolicyError("partial permission wildcards are unsupported")


@dataclass(frozen=True, slots=True)
class PermissionAll:
    all_of: tuple[PermissionPolicy, ...]

    def __post_init__(self) -> None:
        _validate_children(self.all_of, "all_of")


@dataclass(frozen=True, slots=True)
class PermissionAny:
    any_of: tuple[PermissionPolicy, ...]

    def __post_init__(self) -> None:
        _validate_children(self.any_of, "any_of")


PermissionPolicy: TypeAlias = PermissionLeaf | PermissionAll | PermissionAny


def _validate_children(children: object, label: str) -> None:
    if (
        not isinstance(children, tuple)
        or not children
        or any(not isinstance(child, PermissionLeaf | PermissionAll | PermissionAny) for child in children)
    ):
        raise PermissionPolicyError(f"{label} must contain permission policies")


def parse_permission_policy(value: object) -> PermissionPolicy:
    """Parse the contract-v1 JSON form, rejecting unknown or empty branches."""
    if not isinstance(value, Mapping) or len(value) != 1:
        raise PermissionPolicyError("permission policy must have exactly one operator")
    if "permission" in value:
        return PermissionLeaf(value["permission"])
    for key, policy_type in (("all_of", PermissionAll), ("any_of", PermissionAny)):
        if key in value:
            children = value[key]
            if not isinstance(children, list) or not children:
                raise PermissionPolicyError(f"{key} must be a non-empty list")
            return policy_type(tuple(parse_permission_policy(child) for child in children))
    raise PermissionPolicyError("unknown permission policy operator")


class PermissionDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "PERMISSION_DENIED"
    UNAVAILABLE = "PERMISSION_UNAVAILABLE"


def evaluate_permission_policy(
    policy: PermissionPolicy, granted: Collection[str] | None
) -> PermissionDecision:
    """A missing/failed permission lookup is distinct from a valid denial."""
    if granted is None or not isinstance(granted, Collection) or isinstance(granted, str):
        return PermissionDecision.UNAVAILABLE
    if any(not isinstance(permission, str) for permission in granted):
        return PermissionDecision.UNAVAILABLE
    permissions = frozenset(granted)
    if "*:*:*" in permissions:
        return PermissionDecision.ALLOWED

    def permits(node: PermissionPolicy) -> bool:
        if isinstance(node, PermissionLeaf):
            return node.permission in permissions
        if isinstance(node, PermissionAll):
            return all(permits(child) for child in node.all_of)
        return any(permits(child) for child in node.any_of)

    return PermissionDecision.ALLOWED if permits(policy) else PermissionDecision.DENIED


def legacy_permission_policy(required_permissions: tuple[str, ...]) -> PermissionPolicy:
    """Only explicitly non-empty legacy tuples can migrate as an AND policy."""
    if not required_permissions:
        raise PermissionPolicyError("empty legacy permissions require review")
    return PermissionAll(tuple(PermissionLeaf(item) for item in required_permissions))
