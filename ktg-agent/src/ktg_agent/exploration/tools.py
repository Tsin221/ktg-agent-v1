"""Declarations for enabled showcase tools; execution authorization belongs to runtime.

These definitions require their explicit manifests. The legacy permission tuple
cannot represent the candidate policy's OR of complete operation conjunctions.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from ktg_agent.core.messages import JsonObject
from ktg_agent.exploration.catalog import ENTITIES
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionAny, PermissionLeaf
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolDefinitionError,
    ToolEffect, ToolExecute, ToolResult,
)


class ExplorationRuntime(Protocol):
    enabled_names: frozenset[str]

    async def invoke(self, name: str, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult: ...


_SPECS = (
    ("ktg_describe_business", "查看演示业务能力", "发现当前账号可用的演示实体、字段、关系和审核读API。字典、指标和SQL视图暂不提供，会明确标注不支持；内部键从本工具取得。"),
    ("ktg_search_business", "按线索查找业务对象", "用不完整名称、编号前缀、型号、故障与时间线索找候选，不要求用户先给编号。可改词或组合条件；返回真实引用与命中证据，弱相似不当确定对象。"),
    ("ktg_query_business", "组合查询与统计", "在演示范围和当前权限内组合条件、业务时间、排序和根实体粒度聚合。字段从业务目录取得，关系存在性不会重复累计根记录；部分结果不得冒充全量。"),
    ("ktg_read_record", "查看对象与内容", "使用已查得的record_ref，或准确实体键和真实ID读取授权记录、所选内容。缺少编号时先搜索，不要求用户记住内部ID；列表权限不自动取得详情权限。"),
    ("ktg_follow_relations", "追踪关联单据", "从已找到的记录沿登记关系正向或反向追查。起点与目标均检查演示范围和权限，返回真实关联证据；不以同产品或同车间猜关联。"),
    ("ktg_get_business_flow", "查看业务流程与结论", "以记录为起点返回结论、完整流程蓝图和已核实节点。未接入、未查询、无权限与未开始分别表达；流程图不限定查询工具调用顺序。"),
    ("ktg_inspect_result", "继续查看结果与证据", "用本会话result_ref读取后续记录、字段和来源证据。引用不代表永久授权；分页、时点和覆盖范围如实保留，不能用首屏长度代替总量。"),
    ("ktg_readonly_sql", "受控只读业务SQL", "在当前账号有权的演示视图上自主组合SELECT、JOIN、子查询和统计，或解释查询。使用:name命名参数；数据库与授权视图由服务端固定，不接受原始表或跨库访问。"),
    ("ktg_call_read_api", "调用审核读接口", "调用业务目录提供的纯读endpoint_id及其准确参数，继承当前身份和完整操作权限。不接受URL、HTTP方法或headers；GET自动编号等副作用接口不在此入口开放。"),
    ("ktg_analyze_result", "隔离分析已查结果", "对当前会话已授权result_refs进行临时分组、比较、连接和计算。代码定义analyze(datasets)，返回summary/tables/artifacts/warnings；无网络或宿主数据，保留输入时点与覆盖。"),
)
_NAMES = frozenset(spec[0] for spec in _SPECS)
_HIGH = frozenset({"ktg_readonly_sql", "ktg_call_read_api", "ktg_analyze_result"})


def _text(maximum: int = 128) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _strings(maximum: int = 32) -> dict[str, Any]:
    return {"type": "array", "items": _text(), "minItems": 1, "maxItems": maximum, "uniqueItems": True}


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _condition_definitions() -> dict[str, Any]:
    scalar = {"type": ["string", "number", "boolean", "null"]}
    leaf = _object({"field": _text(), "op": {"enum": ["eq", "ne", "in", "not_in", "contains", "starts_with",
        "gt", "gte", "lt", "lte", "between", "is_null"]},
        "value": {"anyOf": [scalar, {"type": "array", "items": scalar, "minItems": 1, "maxItems": 200}]}},
        ("field", "op", "value"))
    children = {"type": "array", "items": {"$ref": "#/$defs/condition"}, "minItems": 1, "maxItems": 64}
    related = _object({"related": _object({"relation": _text(), "where": {"$ref": "#/$defs/condition"}},
                                         ("relation", "where"))}, ("related",))
    return {"condition": {"oneOf": [leaf, _object({"all": children}, ("all",)),
                                              _object({"any": children}, ("any",)), related]}}


def _sort() -> dict[str, Any]:
    return {"type": "array", "maxItems": 8, "items": _object(
        {"field": _text(), "direction": {"enum": ["asc", "desc"]}}, ("field", "direction"))}


def _map(values: dict[str, Any], maximum: int = 64) -> dict[str, Any]:
    return {"type": "object", "propertyNames": _text(), "additionalProperties": values, "maxProperties": maximum}


def _input(name: str) -> JsonObject:
    paging = {"offset": {"type": "integer", "minimum": 0, "default": 0},
              "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}}
    scalar = {"type": ["string", "number", "boolean", "null"]}
    if name == "ktg_describe_business":
        schema = _object({"query": {"type": "string", "maxLength": 2000}, "entity_types": _strings(22),
            "include": {"type": "array", "items": {"enum": ["fields", "dictionaries", "relations", "metrics", "apis", "sql_views"]},
                        "minItems": 1, "maxItems": 6, "uniqueItems": True}, **paging})
    elif name == "ktg_search_business":
        schema = _object({"query": _text(2000), "entity_types": _strings(22),
            "filters": {"$ref": "#/$defs/condition"},
            "entity_filters": _map({"$ref": "#/$defs/condition"}, 22), "sort": _sort(), **paging}, ("query",))
        schema["not"] = {"required": ["filters", "entity_filters"]}
    elif name == "ktg_query_business":
        aggregate = {"oneOf": [
            _object({"op": {"const": "count_records"}, "as": _text()}, ("op", "as")),
            _object({"op": {"enum": ["sum", "avg", "min", "max"]}, "as": _text(), "field": _text()},
                    ("op", "as", "field"))]}
        schema = _object({"entity": _text(), "filters": {"$ref": "#/$defs/condition"}, "fields": _strings(),
            "sort": _sort(), "group_by": {"type": "array", "items": _text(), "maxItems": 8, "uniqueItems": True},
            "aggregates": {"type": "array", "items": aggregate,
            "minItems": 1, "maxItems": 16}, **paging}, ("entity",))
    elif name == "ktg_read_record":
        schema = _object({"record_ref": _text(256), "entity": _text(), "record_id": {"type": "integer", "minimum": 1},
                          "sections": _strings(16)})
        schema["oneOf"] = [{"required": ["record_ref"], "not": {"anyOf": [
            {"required": ["entity"]}, {"required": ["record_id"]}]}},
            {"required": ["entity", "record_id"], "not": {"required": ["record_ref"]}}]
    elif name == "ktg_follow_relations":
        schema = _object({"record_refs": _strings(20), "relations": _strings(32),
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
            "max_nodes": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}}, ("record_refs",))
    elif name == "ktg_get_business_flow":
        schema = _object({"record_ref": _text(256)}, ("record_ref",))
    elif name == "ktg_inspect_result":
        schema = _object({"result_ref": _text(256), "fields": _strings(), **paging}, ("result_ref",))
    elif name == "ktg_readonly_sql":
        schema = _object({"sql": _text(40000), "parameters": _map(scalar),
            "mode": {"enum": ["explain", "execute"], "default": "execute"}}, ("sql",))
    elif name == "ktg_call_read_api":
        schema = _object({"endpoint_id": _text(), "path_params": _map(scalar, 16),
            "query_params": _map({"anyOf": [scalar, {"type": "array", "items": scalar, "maxItems": 200}]})},
            ("endpoint_id",))
    elif name == "ktg_analyze_result":
        schema = _object({"result_refs": _strings(8), "code": _text(65536)}, ("result_refs", "code"))
    else:
        raise ToolDefinitionError("Unknown exploration tool")
    if name in {"ktg_search_business", "ktg_query_business"}:
        schema["$defs"] = _condition_definitions()
    return cast(JsonObject, schema)


def _output() -> JsonObject:
    # Variant records/catalog/aggregate/flow/analysis payloads remain extensible.
    return cast(JsonObject, {"type": "object", "properties": {
        "kind": {"type": "string"}, "result_ref": {"type": "string"},
        "source": {"type": ["string", "object"]}, "applied_query": {"type": "object"},
        "coverage": {"enum": ["complete", "partial", "unknown"]}, "warnings": {"type": "array"}},
        "required": ["kind", "result_ref", "source", "applied_query", "coverage", "warnings"],
        "additionalProperties": True})


def _handler(runtime: ExplorationRuntime, name: str) -> ToolExecute:
    async def execute(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return await runtime.invoke(name, arguments, context)
    return execute


def build_exploration_tools(runtime: ExplorationRuntime) -> tuple[ToolDefinition, ...]:
    if not isinstance(runtime.enabled_names, frozenset) or not runtime.enabled_names <= _NAMES:
        raise ToolDefinitionError("Unknown or invalid enabled exploration tool names")
    return tuple(ToolDefinition(
        name=name, version="1.0", label=label, description=description,
        input_schema=_input(name), output_schema=_output(), required_permissions=(),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.SEQUENTIAL,
        timeout_seconds=30 if name in _HIGH else 60,
        retry_policy=RetryPolicy.NEVER if name in {"ktg_readonly_sql", "ktg_analyze_result"} else RetryPolicy.BOUNDED_READ,
        execute=_handler(runtime, name),
    ) for name, label, description in _SPECS if name in runtime.enabled_names)


def _candidate_policy() -> PermissionAny:
    operations = sorted({tuple(sorted(permissions)) for entity in ENTITIES.values()
                         for operation, permissions in entity.permissions.items()
                         if operation in {"list", "detail"} and permissions})
    if not operations:
        raise ToolDefinitionError("No reviewed exploration read permissions")
    return PermissionAny(tuple(PermissionAll(tuple(PermissionLeaf(permission) for permission in operation))
                               for operation in operations))


def _check_definitions(definitions: tuple[ToolDefinition, ...]) -> None:
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)) or not set(names) <= _NAMES:
        raise ToolDefinitionError("Unreviewed or duplicate exploration definitions")


def exploration_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
    _check_definitions(definitions)
    if not definitions:
        return ()
    policy = _candidate_policy()
    return tuple(ToolManifest(
        name=definition.name, version=definition.version, module="system", domain="exploration",
        label=definition.label, description=definition.description, permission_policy=policy,
        effect=definition.effect, execution_mode=definition.execution_mode, retry_policy=definition.retry_policy,
        timeout_seconds=definition.timeout_seconds, expose_to_model=definition.expose_to_model,
        confirmation_policy="none", proposal_store_effect=False, handler_ref="exploration:" + definition.name,
    ) for definition in definitions)


def _factory(definition: ToolDefinition) -> ToolFactory:
    return lambda: definition


def exploration_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
    _check_definitions(definitions)
    return {"exploration:" + definition.name: _factory(definition) for definition in definitions}
