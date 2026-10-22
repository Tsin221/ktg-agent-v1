"""Lightweight tool catalog with explicit factory allow-list and lazy resolution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

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
    ToolDefinition,
    ToolDefinitionError,
    ToolEffect,
)


ToolFactory: TypeAlias = Callable[[], ToolDefinition]


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
class CatalogIdentity:
    """Permissions returned by a trusted identity service; None means unavailable."""

    permissions: frozenset[str] | None
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ToolDefinitionError("catalog identity requires a run id")
        if self.permissions is not None and (
            not isinstance(self.permissions, frozenset)
            or any(not isinstance(item, str) for item in self.permissions)
        ):
            raise ToolDefinitionError("catalog permissions must be a frozenset or unavailable")


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


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    run_id: str
    catalog_version: str
    candidates: Mapping[str, str]
    permission_status: PermissionDecision


class ToolCatalog:
    """Factories are supplied by trusted code and selected by static reference keys."""

    def __init__(self, factory_allowlist: Mapping[str, ToolFactory]) -> None:
        if any(
            not isinstance(key, str) or not key.strip() or not callable(factory)
            for key, factory in factory_allowlist.items()
        ):
            raise ToolDefinitionError("factory allow-list requires named callable factories")
        self._allowed = dict(factory_allowlist)
        self._manifests: dict[str, ToolManifest] = {}

    def register_factory(self, manifest: ToolManifest, factory: ToolFactory) -> None:
        if manifest.name in self._manifests:
            raise DuplicateToolError(f"tool is already registered: {manifest.name}")
        if self._allowed.get(manifest.handler_ref) is not factory:
            raise ToolDefinitionError("handler_ref is not bound to this allow-listed factory")
        self._manifests[manifest.name] = manifest

    def resolve_definition(self, name: str, version: str) -> ToolDefinition:
        manifest = self._manifests[name]
        if manifest.version != version:
            raise ToolDefinitionError("tool version does not match catalog")
        definition = self._allowed[manifest.handler_ref]()
        for field in (
            "name", "version", "label", "description", "effect", "execution_mode",
            "retry_policy", "timeout_seconds", "expose_to_model",
        ):
            if getattr(definition, field) != getattr(manifest, field):
                raise ToolDefinitionError(f"manifest/definition mismatch: {field}")
        return definition

    def candidate_snapshot(self, identity: object, switches: object) -> CandidateSnapshot:
        """Return only currently discoverable, enabled, authorized definitions."""
        permissions = identity.permissions if isinstance(identity, CatalogIdentity) else None
        if not isinstance(switches, CatalogSwitches):
            switches = CatalogSwitches({}, {})
        status = PermissionDecision.UNAVAILABLE if permissions is None else PermissionDecision.ALLOWED
        candidates = {
            name: manifest.version
            for name, manifest in sorted(self._manifests.items())
            if manifest.expose_to_model
            and switches.enabled(manifest)
            and evaluate_permission_policy(manifest.permission_policy, permissions)
            is PermissionDecision.ALLOWED
        }
        return CandidateSnapshot(
            run_id=identity.run_id if isinstance(identity, CatalogIdentity) else "",
            catalog_version=self.catalog_version,
            candidates=MappingProxyType(candidates),
            permission_status=status,
        )

    @property
    def catalog_version(self) -> str:
        entries = [(name, repr(item)) for name, item in sorted(self._manifests.items())]
        digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()[:16]
        return f"contract-v1:{digest}"

    def manifest(self, name: str) -> ToolManifest:
        return self._manifests[name]
