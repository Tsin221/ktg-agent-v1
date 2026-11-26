"""Static metadata and access rules for the reviewed MES tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ktg_agent.tools.permissions import (
    PermissionAll,
    PermissionAny,
    PermissionDecision,
    PermissionLeaf,
    PermissionPolicy,
    evaluate_permission_policy,
)
from ktg_agent.tools.registry import (
    DuplicateToolError,
    ExecutionMode,
    RetryPolicy,
    ToolDefinitionError,
    ToolEffect,
)


@dataclass(frozen=True, slots=True)
class ToolManifest:
    name: str
    version: str
    module: str
    domain: str
    label: str
    description: str
    permission_policy: PermissionPolicy
    effect: ToolEffect
    execution_mode: ExecutionMode
    retry_policy: RetryPolicy
    timeout_seconds: float
    expose_to_model: bool
    confirmation_policy: str
    proposal_store_effect: bool
    handler_ref: str

    def __post_init__(self) -> None:
        for field in ("name", "version", "module", "domain", "label", "description", "handler_ref"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ToolDefinitionError(f"manifest {field} must be non-empty")
        if not isinstance(self.permission_policy, PermissionLeaf | PermissionAll | PermissionAny):
            raise ToolDefinitionError("manifest permission_policy is required")
        if not isinstance(self.effect, ToolEffect) or not isinstance(self.execution_mode, ExecutionMode):
            raise ToolDefinitionError("invalid manifest effect or execution mode")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise ToolDefinitionError("invalid manifest retry policy")
        if (
            not isinstance(self.timeout_seconds, int | float)
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ToolDefinitionError("manifest timeout_seconds must be positive")
        if not isinstance(self.expose_to_model, bool) or not isinstance(self.proposal_store_effect, bool):
            raise ToolDefinitionError("manifest visibility and proposal effect must be boolean")
        if self.confirmation_policy not in ("none", "trusted_confirmation"):
            raise ToolDefinitionError("invalid confirmation policy")
        if self.confirmation_policy == "trusted_confirmation" and (
            self.expose_to_model or self.effect is not ToolEffect.WRITE
        ):
            raise ToolDefinitionError("trusted confirmation requires a hidden write tool")
        if self.proposal_store_effect and (
            self.effect is not ToolEffect.READ
            or self.execution_mode is not ExecutionMode.SEQUENTIAL
            or self.retry_policy is not RetryPolicy.NEVER
        ):
            raise ToolDefinitionError("proposal creation must be sequential and never retried")


@dataclass(frozen=True, slots=True)
class CatalogSwitches:
    modules: Mapping[str, bool]
    tools: Mapping[str, bool]
    emergency_disabled: bool = False

    def enabled(self, manifest: ToolManifest) -> bool:
        return (
            not self.emergency_disabled
            and self.modules.get(manifest.module) is True
            and self.tools.get(manifest.name) is True
        )


class ToolCatalog:
    """Static metadata for the native toolset and authorized HTTP directory."""

    def __init__(self, manifests: Iterable[ToolManifest]) -> None:
        self._manifests: dict[str, ToolManifest] = {}
        for manifest in manifests:
            if manifest.name in self._manifests:
                raise DuplicateToolError(f"tool is already registered: {manifest.name}")
            self._manifests[manifest.name] = manifest

    def available(self, permissions: frozenset[str] | None, switches: CatalogSwitches) -> dict[str, str]:
        return {
            name: manifest.version
            for name, manifest in sorted(self._manifests.items())
            if manifest.expose_to_model
            and switches.enabled(manifest)
            and evaluate_permission_policy(manifest.permission_policy, permissions)
            is PermissionDecision.ALLOWED
        }

    @property
    def catalog_version(self) -> str:
        entries = [(name, repr(item)) for name, item in sorted(self._manifests.items())]
        digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()[:16]
        return f"contract-v1:{digest}"

    def manifest(self, name: str) -> ToolManifest:
        return self._manifests[name]
