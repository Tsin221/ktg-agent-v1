"""Bounded traversal of reviewed showcase relations with per-operation grants."""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.exploration.catalog import ENTITIES, EntitySpec, RelationSpec, get_entity
from ktg_agent.exploration.engine import ReadEngine
from ktg_agent.ktg.client import KTGError


@dataclass(frozen=True, slots=True)
class RelatedRows:
    rows: tuple[JsonObject, ...]
    complete: bool
    state: str
    warnings: tuple[str, ...] = ()


async def related_rows(
    engine: ReadEngine, row: JsonObject, relation: RelationSpec, *, max_rows: int = 500,
) -> RelatedRows:
    """Read a registered edge; filtering is verified even if the API ignores it.

    This helper never authorizes the source by itself. Its caller must have
    freshly read/authorized that source operation. List summaries do not acquire
    detail permissions, and a later read of their record_ref still needs query.
    """
    engine.cancelled()
    target = get_entity(relation.target)
    operation = "detail" if relation.cardinality == "many_to_one" else "list"
    if not engine.allowed(target.permissions.get(operation, ())):
        return RelatedRows((), False, "无权限", ("当前角色无权读取该类关联记录。",))
    engine.authorize(target, operation)
    if relation.source_field not in row:
        return RelatedRows((), False, "未核实", ("来源未返回关联字段，不能据此判断无关联。",))
    value = row[relation.source_field]
    if value is None or value == 0:
        return RelatedRows((), True, "未查到关联记录")
    if type(value) is not int or value <= 0:
        return RelatedRows((), False, "未核实", ("来源关联字段格式无效。",))
    try:
        if operation == "detail":
            found = await engine.read(target.key, value)
            if found.get(relation.target_field) != value:
                return RelatedRows((), False, "未核实", ("关联详情与来源字段不一致。",))
            return RelatedRows((found,), True, "已查到")
        field = target.fields[relation.target_field]
        params = {field.list_filter: value} if field.list_filter and "eq" in field.operators else None
        scanned = await engine.scan(target.key, params=params, max_rows=max_rows)
        missing = any(relation.target_field not in candidate for candidate in scanned.rows)
        verified = tuple(
            candidate for candidate in scanned.rows
            if type(candidate.get(relation.target_field)) is int and candidate[relation.target_field] == value
        )
        warnings = scanned.warnings + (("部分来源缺少关联字段，不能证明关系已查全。",) if missing else ())
        return RelatedRows(verified, scanned.complete and not missing,
                           "已查到" if verified else "未核实" if missing else "未查到关联记录", warnings)
    except KTGError as exc:
        if exc.code in {"RUN_CANCELLED", "OUT_OF_SCOPE", "AUTH_REQUIRED", "CATALOG_CHANGED"}:
            raise
        if exc.code == "PERMISSION_DENIED":
            return RelatedRows((), False, "无权限", ("关联记录当前不可查看。",))
        # Do not forward backend diagnostics or IDs of inaccessible objects.
        return RelatedRows((), False, "未核实", ("关联来源暂不可用；未将读取失败当成无记录。",))


def _node(spec: EntitySpec, row: JsonObject) -> JsonObject:
    return {
        **row, "id": f"{spec.key}:{row['id']}", "record_id": row["id"], "entity": spec.key,
        "record_ref": row["record_ref"], "label": str(row.get("code") or row.get("name") or spec.label),
        "summary": str(row.get("name") or spec.label),
    }


async def follow(
    engine: ReadEngine, record_refs: Sequence[str], *, relations: tuple[str, ...] = (),
    max_depth: int = 2, max_nodes: int = 100,
) -> JsonObject:
    if (isinstance(record_refs, str) or not 1 <= len(record_refs) <= 10
            or not all(isinstance(ref, str) and ref for ref in record_refs)
            or type(max_depth) is not int or not 1 <= max_depth <= 4
            or type(max_nodes) is not int or not 1 <= max_nodes <= 200):
        raise KTGError("VALIDATION_ERROR", "请提供有效记录引用及有界的关系查询深度/节点数。")
    known = {r.key for entity in ENTITIES.values() for r in entity.relations}
    if any(not isinstance(key, str) or key not in known for key in relations):
        raise KTGError("VALIDATION_ERROR", "关系键未在当前展示目录登记。")
    selected = frozenset(relations)
    nodes: dict[str, JsonObject] = {}
    edges: list[JsonValue] = []
    gaps: list[JsonValue] = []
    warnings: list[JsonValue] = []
    queue: deque[tuple[EntitySpec, JsonObject, int, bool]] = deque()
    complete = True
    io_count = 0
    edge_keys: set[tuple[str, str, str]] = set()

    def add(spec: EntitySpec, row: JsonObject, depth: int) -> str | None:
        nonlocal complete
        node_id = f"{spec.key}:{row['id']}"
        if node_id in nodes:
            return node_id
        if len(nodes) >= max_nodes:
            complete = False
            return None
        needs_detail = "record_ref" not in row
        record = row if not needs_detail else engine.record(spec, row)
        nodes[node_id] = _node(spec, record)
        queue.append((spec, record, depth, needs_detail))
        return node_id

    for reference in dict.fromkeys(record_refs):
        spec, key = engine.resolve_record(reference)
        # Never fall back to list merely because a caller lacks query.
        row = await engine.read(spec.key, key)
        io_count += 1
        add(spec, row, 0)
    while queue:
        engine.cancelled()
        spec, row, depth, needs_detail = queue.popleft()
        if depth >= max_depth:
            continue
        applicable = tuple(edge for edge in spec.relations if not selected or edge.key in selected)
        if not applicable:
            continue
        if not engine.allowed(spec.permissions.get("detail", ())):
            complete = False
            gaps.append({"source": f"{spec.key}:{row['id']}", "entity": spec.key,
                         "reason": "无权限", "summary": "当前记录仅有列表权限，未展开详情或继续追踪。"})
            continue
        engine.authorize(spec, "detail")
        if needs_detail:
            if io_count >= 32:
                complete = False
                warnings.append("达到本次关系查询读取预算；可缩小关系范围继续查询。")
                break
            try:
                # A list-derived node may be displayed as an authorized list
                # summary, but expansion must also pass the actual detail API.
                io_count += 1
                row = await engine.read(spec.key, cast(int, row["id"]))
                nodes[f"{spec.key}:{row['id']}"] = _node(spec, row)
            except KTGError as exc:
                if exc.code in {"RUN_CANCELLED", "OUT_OF_SCOPE", "AUTH_REQUIRED", "CATALOG_CHANGED"}:
                    raise
                complete = False
                gaps.append({"source": f"{spec.key}:{row['id']}", "entity": spec.key,
                             "reason": "无权限" if exc.code == "PERMISSION_DENIED" else "未核实",
                             "summary": "该列表记录的详情不可核实，未继续展开关联。"})
                continue
        for relation in applicable:
            engine.cancelled()
            if io_count >= 32:
                complete = False
                warnings.append("达到本次关系查询读取预算；可缩小关系范围继续查询。")
                queue.clear()
                break
            related = await related_rows(engine, row, relation, max_rows=min(2000, max_nodes + 1))
            io_count += 1
            complete = complete and related.complete
            if not related.complete:
                # No target IDs, existence flags, or counts enter permission gaps.
                gaps.append({"source": f"{spec.key}:{row['id']}", "relation": relation.key,
                             "target_entity": relation.target, "reason": related.state,
                             "warnings": cast(JsonValue, list(related.warnings))})
            target = get_entity(relation.target)
            for candidate in related.rows:
                target_id = add(target, candidate, depth + 1)
                if target_id is None:
                    warnings.append("达到节点预算，未将已查到部分作为全量。")
                    break
                source_id = f"{spec.key}:{row['id']}"
                edge_key = source_id, target_id, relation.key
                if edge_key in edge_keys:
                    continue
                edge_keys.add(edge_key)
                edges.append({"source": source_id, "target": target_id, "relation": relation.key,
                              "evidence": {"kind": relation.evidence, "source_field": relation.source_field,
                                           "target_field": relation.target_field}, "note": relation.note})
    return engine.publish("exploration_relations", {
        "nodes": cast(JsonValue, list(nodes.values())), "edges": edges, "gaps": gaps,
        "headline": f"已查到 {len(nodes)} 个关联对象",
        "applied_query": {"relations": list(relations), "max_depth": max_depth, "max_nodes": max_nodes},
    }, complete=complete, warnings=list(dict.fromkeys(cast(list[str], warnings))))
