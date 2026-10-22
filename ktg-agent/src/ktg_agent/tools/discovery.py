"""Run-local discovery and request-scoped activation of catalog definitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
import json
import re

from jsonschema import Draft202012Validator

from ktg_agent.context.tokens import estimate_tools_tokens
from ktg_agent.core.messages import JsonObject, JsonValue, ToolCall
from ktg_agent.providers.base import ToolSchema
from ktg_agent.tools.catalog import CandidateSnapshot, CatalogSwitches, ToolCatalog
from ktg_agent.tools.permissions import PermissionDecision, evaluate_permission_policy
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolDefinition, ToolEffect, ToolRegistry,
    ToolContext, ToolDefinitionError, ToolResult, ToolStatus,
)


MODULES = ("md", "wm", "pro", "qc", "dv", "cal", "tm", "report", "system")


def _search_terms(query: str) -> set[str]:
    """Bounded lexical matching without adding a tokenizer dependency."""
    terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", query))
    for term in tuple(terms):
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            terms.update(term[index:index + 2] for index in range(len(term) - 1))
    for alias, canonical in (("仓储", "库存"), ("stock", "库存"), ("inventory", "库存")):
        if alias in query:
            terms.add(canonical)
    return terms


SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "maxLength": 200, "description": "业务关键词；空字符串用于查看当前用户可用能力。"},
        "module": {"type": "string", "enum": list(MODULES)},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    }, "anyOf": [{"required": ["query"]}, {"required": ["module"]}],
}
LOAD_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {"tools": {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False,
                  "properties": {"name": {"type": "string"}, "version": {"type": "string"}},
                  "required": ["name", "version"]},
    }}, "required": ["tools"],
}


async def _not_directly_executable(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult("Directory tools require a run-local controller.",
                      status=ToolStatus.FAILED, error_code="CHANNEL_FORBIDDEN")


def directory_definitions() -> tuple[ToolDefinition, ToolDefinition]:
    return tuple(ToolDefinition(
        name=name, version="1.0", label=label, description=description,
        input_schema=schema, output_schema=None, required_permissions=(),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        retry_policy=RetryPolicy.NEVER, timeout_seconds=10,
        execute=_not_directly_executable,
    ) for name, label, description, schema in (
        ("search_tools", "搜索工具", "查找当前用户可用工具，返回准确名称、版本和功能边界。询问能力时用 query=空字符串，一次总览即可回答，不必加载或执行业务工具。模块只接受 md/wm/pro/qc/dv/cal/tm/report/system。", SEARCH_SCHEMA),
        ("load_tools", "加载工具", "仅复制 search_tools 返回的准确名称和版本，下一次模型请求才可调用。禁止猜名称、改版本；只加载完成当前任务所需工具。", LOAD_SCHEMA),
    ))  # type: ignore[return-value]


type CurrentAccess = Callable[[], Awaitable[tuple[frozenset[str] | None, CatalogSwitches]]]


@dataclass(frozen=True, slots=True)
class ActiveToolSet:
    request_id: str
    names: frozenset[str]
    schemas: tuple[ToolSchema, ...]


class RunToolDiscovery:
    """Only this object mutates one run's activated names; snapshots stay immutable."""

    def __init__(
        self, catalog: ToolCatalog, candidate: CandidateSnapshot,
        registry: ToolRegistry, current_access: CurrentAccess, *,
        max_active_tools: int = 8, max_schema_tokens: int = 6000,
        page_module: str | None = None,
    ) -> None:
        if max_active_tools <= 0 or max_schema_tokens <= 0:
            raise ValueError("activation budgets must be positive")
        self.catalog = catalog
        self.candidate = candidate
        self.registry = registry
        self.current_access = current_access
        self.max_active_tools = max_active_tools
        self.max_schema_tokens = max_schema_tokens
        self.page_module = page_module
        self._active: set[str] = set()
        self._snapshots: dict[str, ActiveToolSet] = {}
        for definition in directory_definitions():
            registry.register(definition)

    def begin_request(self, request_id: str) -> ActiveToolSet:
        names = frozenset(("search_tools", "load_tools", *self._active))
        schemas = tuple(self.registry.get(name).model_schema() for name in sorted(names))
        snapshot = ActiveToolSet(request_id, names, schemas)
        self._snapshots[request_id] = snapshot
        return snapshot

    def active_names(self, request_id: str | None) -> frozenset[str] | None:
        snapshot = self._snapshots.get(request_id or "")
        return snapshot.names if snapshot is not None else None

    async def execute(self, call: ToolCall) -> ToolResult:
        schema = SEARCH_SCHEMA if call.name == "search_tools" else LOAD_SCHEMA
        error = next(iter(Draft202012Validator(schema).iter_errors(call.arguments)), None)
        if error is not None:
            if call.name == "search_tools" and "module" in call.arguments and call.arguments["module"] not in MODULES:
                return self._error("VALIDATION_ERROR", "module must be one of: " + ", ".join(MODULES) + ". Use query for Chinese business terms.")
            return self._error("VALIDATION_ERROR", "Tool arguments are invalid.")
        try:
            permissions, switches = await self.current_access()
        except Exception:
            return self._error("PERMISSION_UNAVAILABLE", "Permission service is unavailable.")
        if permissions is None:
            return self._error("PERMISSION_UNAVAILABLE", "Permission service is unavailable.")
        if self.catalog.catalog_version != self.candidate.catalog_version:
            return self._error("TOOL_VERSION_MISMATCH", "Catalog version changed.")
        if call.name == "search_tools":
            return self._search(call.arguments, permissions, switches)
        return self._load(call.arguments, permissions, switches)

    def _available(self, name: str, permissions: frozenset[str], switches: CatalogSwitches) -> bool:
        if name not in self.candidate.candidates:
            return False
        manifest = self.catalog.manifest(name)
        return (manifest.expose_to_model and switches.enabled(manifest)
                and evaluate_permission_policy(manifest.permission_policy, permissions)
                is PermissionDecision.ALLOWED)

    def _search(self, args: Mapping[str, Any], permissions: frozenset[str],
                switches: CatalogSwitches) -> ToolResult:
        query = str(args.get("query", "")).strip().casefold()
        module = args.get("module")
        ranked: list[tuple[int, str]] = []
        available: list[tuple[int, str]] = []
        terms = _search_terms(query)
        for name in self.candidate.candidates:
            if not self._available(name, permissions, switches):
                continue
            manifest = self.catalog.manifest(name)
            if module and manifest.module != module:
                continue
            fields = (name.casefold(), manifest.label.casefold(), manifest.description.casefold())
            available.append((0 if self.page_module == manifest.module else 1, name))
            score = sum(weight for term in terms for field, weight in zip(fields, (8, 6, 1)) if term in field)
            if query and not score:
                continue
            rank = -score
            if self.page_module == manifest.module:
                rank -= 1
            ranked.append((rank, name))
        fallback = bool(query and not ranked and available)
        if fallback:
            ranked = available
        ranked.sort()
        limit = args.get("limit", 10)
        assert isinstance(limit, int)
        items: list[JsonValue] = [{"name": name, "version": self.candidate.candidates[name],
                  "label": self.catalog.manifest(name).label[:80],
                  "description": self.catalog.manifest(name).description[:400]}
                 for _, name in ranked[:limit]]
        details: JsonObject = {"catalog_version": self.candidate.catalog_version,
                   "items": items, "truncated": len(ranked) > limit,
                   "match_type": "available_alternatives" if fallback else "matched" if query else "overview"}
        hint = ("No matching tool; these are available alternatives, not evidence that the requested operation is supported. "
                if fallback else "")
        if not items:
            hint = "No available tool for this scope. Explain the limitation; do not guess tool names. "
        # details is UI-only and removed by model projection. The model MUST receive
        # the reviewed names, exact versions and capability boundaries as text too.
        return ToolResult(content=hint + json.dumps(details, ensure_ascii=False), details=details)

    def _load(self, args: Mapping[str, Any], permissions: frozenset[str],
              switches: CatalogSwitches) -> ToolResult:
        requested = args["tools"]
        assert isinstance(requested, list)
        names = [item["name"] for item in requested]
        if len(set(names)) != len(names):
            return self._error("VALIDATION_ERROR", "Duplicate tool names are invalid.")
        pending_names: list[tuple[str, str]] = []
        already: list[JsonValue] = []
        for index, item in enumerate(requested):
            name, version = item["name"], item["version"]
            if not self._available(name, permissions, switches):
                return self._error("TOOL_NOT_FOUND", f"tools[{index}] is unavailable. No tools in this batch were activated. Use search_tools and copy its names; do not guess.")
            expected = self.candidate.candidates[name]
            if version != expected:
                return self._error("TOOL_VERSION_MISMATCH", f"tools[{index}]: {name} requires exact version {expected}. No tools in this batch were activated.")
            if name in self._active:
                already.append({"name": name, "version": version})
            else:
                pending_names.append((name, version))
        if len(self._active) + len(pending_names) > self.max_active_tools:
            return self._error("ACTIVE_LIMIT_EXCEEDED", "Active tool limit exceeded.")
        pending: list[tuple[str, str, ToolDefinition]] = []
        for name, version in pending_names:
            try:
                definition = self.catalog.resolve_definition(name, version)
            except (KeyError, ToolDefinitionError):
                return self._error("TOOL_NOT_FOUND", "Tool is not available.")
            pending.append((name, version, definition))
        schemas = [self.registry.get(name).model_schema() for name in self._active]
        schemas += [definition.model_schema() for _, _, definition in pending]
        if estimate_tools_tokens(tuple(schemas)) > self.max_schema_tokens:
            return self._error("ACTIVE_LIMIT_EXCEEDED", "Tool schema budget exceeded.")
        for name, _, definition in pending:
            self.registry.register(definition)
            self._active.add(name)
        activated: list[JsonValue] = [{"name": name, "version": version} for name, version, _ in pending]
        details: JsonObject = {"catalog_version": self.candidate.catalog_version,
                   "activated": activated,
                   "already_active": already, "effective_request": "next"}
        return ToolResult(content=json.dumps(details, ensure_ascii=False),
                          details=details)

    @staticmethod
    def _error(code: str, message: str) -> ToolResult:
        return ToolResult(message, status=ToolStatus.FAILED, error_code=code)
