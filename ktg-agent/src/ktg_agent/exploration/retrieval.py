"""Bounded candidate search over the showcase and current account grants.

Results are not sliced here: the runtime pages the published snapshot. Related
fault evidence is fetched in bulk, with its own complete permission contract.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from typing import Any, cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.exploration.catalog import EntitySpec, get_entity
from ktg_agent.exploration.engine import ReadEngine, Scan
from ktg_agent.exploration.query import matches, order_rows, validate_filter
from ktg_agent.exploration.scope import SHOWCASE_ENTITIES, require_entity
from ktg_agent.exploration.search import infer_entities, parse_clues, rank_record, semantic_status
from ktg_agent.ktg.client import KTGError


MAX_SEARCH_ROWS = 2000
MAX_RESULT_BYTES = 2 * 1024 * 1024
_LOCAL = timezone(timedelta(hours=8))
_BUSINESS_DATES = {"dv.repair": "require_date", "pro.workorder": "request_date",
                   "pro.task": "start_time", "pro.feedback": "feedback_time",
                   "wm.item_receipt": "recpt_date", "wm.issue": "issue_date"}


def _now() -> datetime:
    return datetime.now(_LOCAL)


def _invalid(message: str) -> KTGError:
    return KTGError("VALIDATION_ERROR", message)


@dataclass
class _Reads:
    engine: ReadEngine
    remaining: int = MAX_SEARCH_ROWS
    complete: bool = True
    warnings: list[JsonValue] = field(default_factory=list)
    scans: dict[str, Scan] = field(default_factory=dict)

    async def get(self, key: str) -> Scan | None:
        require_entity(key)
        spec = get_entity(key)
        if key in self.scans:
            return self.scans[key]
        if not self.engine.allowed(spec.permissions["list"]):
            self.complete = False
            self.warnings.append(f"未检索{spec.label}：当前权限不足；未读取该类型记录。")
            return None
        if self.remaining <= 0:
            self.complete = False
            self.warnings.append(f"未继续检索{spec.label}：达到共享行数预算，请缩小范围。")
            return None
        result = await self.engine.scan(key, max_rows=self.remaining)
        self.remaining -= len(result.rows)
        self.complete = self.complete and result.complete
        self.warnings.extend(result.warnings)
        self.scans[key] = result
        return result


def _reject_related(node: object, depth: int = 0) -> None:
    if depth > 8:
        raise _invalid("条件嵌套过深，请拆分查询。")
    if isinstance(node, Mapping):
        if "related" in node:
            raise KTGError("UNSUPPORTED_FILTER", "搜索暂不执行关联条件；请使用组合查询或先搜索后沿关系读取。")
        for key in ("all", "any"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    _reject_related(child, depth + 1)


def _select(engine: ReadEngine, query: str, args: Mapping[str, Any]) -> list[EntitySpec]:
    explicit = args.get("entity_types")
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit or not all(isinstance(key, str) for key in explicit):
            raise _invalid("entity_types 必须为非空实体键数组。")
        for key in explicit:
            require_entity(key)
        selected = [get_entity(key) for key in dict.fromkeys(explicit)]
        if any(not engine.allowed(spec.permissions["list"]) for spec in selected):
            raise KTGError("PERMISSION_DENIED", "当前角色无权检索指定业务类型。")
        return selected
    visible = [get_entity(key) for key in sorted(SHOWCASE_ENTITIES)
               if engine.allowed(get_entity(key).permissions["list"])]
    if not visible:
        raise KTGError("PERMISSION_DENIED", "当前角色没有可检索的展示业务类型。")
    inferred = infer_entities(query, visible)
    return [get_entity(key) for key in inferred] if inferred else visible


def _filters(specs: Sequence[EntitySpec], args: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    common, per_entity = args.get("filters"), args.get("entity_filters")
    if common is not None and per_entity is not None:
        raise _invalid("filters 与 entity_filters 不能同时使用。")
    if per_entity is not None and not isinstance(per_entity, Mapping):
        raise _invalid("entity_filters 必须是实体键到条件树的映射。")
    allowed = {spec.key for spec in specs}
    if isinstance(per_entity, Mapping):
        for key in per_entity:
            if not isinstance(key, str):
                raise _invalid("实体条件键必须为字符串。")
            require_entity(key)
            if key not in allowed:
                raise _invalid("实体条件必须属于本次搜索类型，不能被静默忽略。")
    result: dict[str, Mapping[str, Any]] = {}
    for spec in specs:
        node = per_entity.get(spec.key) if isinstance(per_entity, Mapping) else common
        if node is not None:
            _reject_related(node)
            validate_filter(node, {key: value.type for key, value in spec.fields.items()})
            assert isinstance(node, Mapping)
            result[spec.key] = node
    return result


def _root_fields(spec: EntitySpec, row: JsonObject) -> dict[str, str]:
    # Contextual codes (e.g. workshop_code) must not satisfy a device prefix.
    return {(key if key in {"code", "name", "specification", "model"} else f"{spec.key}.{key}"): value
            for key, value in row.items() if key in spec.fields and spec.fields[key].searchable
            and isinstance(value, str)}


def _integer(row: JsonObject, key: str) -> int | None:
    value = row.get(key)
    return value if type(value) is int else None


@dataclass
class _Faults:
    heads: dict[int, JsonObject] = field(default_factory=dict)
    by_machine: dict[int, list[JsonObject]] = field(default_factory=dict)
    lines: dict[int, list[JsonObject]] = field(default_factory=dict)
    line_heads: dict[int, int] = field(default_factory=dict)

    def related(self, spec: EntitySpec, row: JsonObject) -> list[tuple[EntitySpec, JsonObject]]:
        if spec.key == "dv.machinery":
            heads = self.by_machine.get(cast(int, row["id"]), [])
        elif spec.key == "dv.repair":
            heads = [row]
        else:
            head = self.heads.get(_integer(row, "repair_id") or -1)
            # A content line cannot acquire a sibling's unrelated fault evidence.
            return [(get_entity("dv.repair"), head)] if head is not None else []
        result: list[tuple[EntitySpec, JsonObject]] = []
        for head in heads:
            result.append((get_entity("dv.repair"), head))
            result.extend((get_entity("dv.repair_line"), line)
                          for line in self.lines.get(cast(int, head["id"]), []))
        return result


async def _faults(reads: _Reads, specs: Sequence[EntitySpec], clues: JsonObject) -> _Faults:
    result = _Faults()
    if not any(spec.key in {"dv.machinery", "dv.repair", "dv.repair_line"} for spec in specs):
        return result
    # Prefix-only identity search does not require unrelated fault permissions.
    if not clues.get("terms") and not any(spec.key in {"dv.machinery", "dv.repair_line"}
                                          for spec in specs if clues.get("time_hint")):
        return result
    heads = await reads.get("dv.repair")
    if heads is None:
        return result
    for head in heads.rows:
        result.heads[cast(int, head["id"])] = head
        machine = _integer(head, "machinery_id")
        if machine is not None:
            result.by_machine.setdefault(machine, []).append(head)
    if clues.get("terms"):
        lines = await reads.get("dv.repair_line")
        if lines is not None:
            for line in lines.rows:
                repair = _integer(line, "repair_id")
                if repair is not None:
                    result.lines.setdefault(repair, []).append(line)
                    result.line_heads[cast(int, line["id"])] = repair
    return result


def _time_field(spec: EntitySpec, query: str) -> str | None:
    target = get_entity("dv.repair") if spec.key in {"dv.machinery", "dv.repair_line"} else spec
    if "验收" in query:
        return "confirm_date" if "confirm_date" in target.fields else None
    if any(word in query for word in ("完工", "修完")):
        return next((key for key in ("finish_date", "end_time") if key in target.fields), None)
    return _BUSINESS_DATES.get(target.key)


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=_LOCAL)).astimezone(_LOCAL)


def _time_bounds(clues: JsonObject, now: datetime, *,
                 date_only: bool = True) -> tuple[datetime | None, datetime | None]:
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    days = clues.get("relative_days")
    if type(days) is int:
        if not 1 <= days <= 3650:
            raise _invalid("相对日期范围须为 1 到 3650 天。")
        if not date_only:
            return now - timedelta(days=days), now
        return tomorrow - timedelta(days=days), tomorrow
    phrase = clues.get("time_text")
    if phrase in {"上周", "本周"}:
        monday = tomorrow - timedelta(days=now.weekday() + 1)
        return (monday - timedelta(days=7), monday) if phrase == "上周" else (monday, tomorrow if date_only else now)
    if phrase in {"上个月", "这个月"}:
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous = (first - timedelta(days=1)).replace(day=1)
        return (previous, first) if phrase == "上个月" else (first, tomorrow if date_only else now)
    return (None, tomorrow if date_only else now) if clues.get("time_hint") == "previous" else (None, None)


async def search_business(engine: ReadEngine, arguments: Mapping[str, Any]) -> JsonObject:
    """Search a bounded authorized scope; runtime handles result paging."""
    query = arguments.get("query")
    if not isinstance(query, str) or len(query) > 1024:
        raise _invalid("query 必须是长度不超过 1024 的线索文本。")
    specs = _select(engine, query, arguments)
    predicates = _filters(specs, arguments)
    sort = arguments.get("sort", [])
    if not isinstance(sort, list) or not all(isinstance(item, Mapping) for item in sort):
        raise _invalid("sort 必须是字段排序数组。")
    if sort and len(specs) != 1:
        raise KTGError("UNSUPPORTED_SORT", "跨类型候选不共用字段排序，请指定一个实体类型。")
    for spec in specs:
        order_rows([], sort, {key: value.type for key, value in spec.fields.items()})
    clues = parse_clues(query)
    now = _now().astimezone(_LOCAL)
    _time_bounds(clues, now)  # Validate relative intervals before any business request.
    reads = _Reads(engine, remaining=MAX_SEARCH_ROWS)
    root_scans = {spec.key: await reads.get(spec.key) for spec in specs}
    faults = await _faults(reads, specs, clues)
    candidates: list[tuple[JsonObject, float, float]] = []
    time_plans: JsonObject = {}
    result_bytes = 0
    for spec in specs:
        scan = root_scans[spec.key]
        if scan is None:
            continue
        types = {key: value.type for key, value in spec.fields.items()}
        time_field = _time_field(spec, query) if clues.get("time_hint") else None
        temporal_spec = get_entity("dv.repair") if spec.key in {"dv.machinery", "dv.repair_line"} else spec
        date_only = time_field is None or temporal_spec.fields[time_field].type not in {"datetime", "date-time"}
        lower, upper = _time_bounds(clues, now, date_only=date_only)
        if clues.get("time_hint"):
            time_plans[spec.key] = {
                "field": time_field, "source_entity": temporal_spec.key,
                "from": (lower.date().isoformat() if date_only else lower.isoformat()) if lower else None,
                "to": (upper.date().isoformat() if date_only else upper.isoformat()) if upper else None,
                "precision": "date" if date_only else "datetime", "timezone": "Asia/Shanghai"}
            if time_field is None:
                reads.complete = False
                reads.warnings.append(f"{spec.label}没有适配该业务时间，未将更新时间替代。")
                continue
        for row in scan.rows:
            engine.cancelled()
            predicate = predicates.get(spec.key)
            if predicate is not None and not matches(row, predicate, types):
                continue
            related = faults.related(spec, row) if spec.key in {"dv.machinery", "dv.repair", "dv.repair_line"} else []
            event_dates: dict[int, datetime] = {}
            if time_field is not None and spec.key in {"dv.machinery", "dv.repair_line"}:
                for related_spec, linked in related:
                    if related_spec.key != "dv.repair":
                        continue
                    stamp = _stamp(linked.get(time_field))
                    if stamp is None:
                        reads.complete = False
                        reads.warnings.append("部分维修记录未提供可核实的业务时间。")
                    elif not ((lower is not None and stamp < lower) or (upper is not None and stamp >= upper)):
                        event_dates[cast(int, linked["id"])] = stamp
                # Keep date and fault evidence attached to the SAME repair event.
                related = [(linked_spec, linked) for linked_spec, linked in related
                           if (linked["id"] in event_dates if linked_spec.key == "dv.repair"
                               else linked.get("repair_id") in event_dates)]
                if not event_dates:
                    continue
            fields = _root_fields(spec, row)
            origins: dict[str, tuple[str, int]] = {key: (spec.key, cast(int, row["id"])) for key in fields}
            for related_spec, linked in related:
                for key, value in linked.items():
                    if (key not in related_spec.fields or not related_spec.fields[key].searchable
                            or not isinstance(value, str)):
                        continue
                    field_key = f"{related_spec.key}.{linked['id']}.{key}"
                    fields[field_key] = value
                    origins[field_key] = (related_spec.key, cast(int, linked["id"]))
            stamps = [_stamp(row.get(time_field))] if time_field is not None and time_field in spec.fields else [
                _stamp(linked.get(time_field)) for related_spec, linked in related
                if related_spec.key == "dv.repair" and time_field is not None]
            timestamp = max((stamp for stamp in stamps if stamp is not None), default=None)
            if time_field is not None and timestamp is None:
                reads.complete = False
                reads.warnings.append(f"部分{spec.label}记录未提供可核实的业务时间。")
                continue
            if timestamp is not None and ((lower is not None and timestamp < lower)
                                          or (upper is not None and timestamp >= upper)):
                continue
            score, evidence = rank_record(query, fields)
            if not clues.get("terms") and not clues.get("code_prefix") and not clues.get("excluded_code_prefix"):
                score = 0.1
            if score <= 0:
                continue
            if clues.get("terms") and not any(item.get("method") not in {"prefix", "prefix_exclusion"} for item in evidence):
                continue  # A prefix match alone does not establish the other clue.
            matched_events: set[int] = set()
            for item in evidence:
                origin = origins.get(str(item.get("field")))
                if origin:
                    item.update(source_entity=origin[0], source_id=origin[1])
                    event = origin[1] if origin[0] == "dv.repair" else faults.line_heads.get(origin[1]) if origin[0] == "dv.repair_line" else None
                    if event in event_dates:
                        matched_events.add(event)
            if matched_events:
                timestamp = max(event_dates[event] for event in matched_events)
            weak = score < 0.9 or any(item.get("strength") == "weak" for item in evidence)
            record = {**engine.record(spec, row), "score": score, "match_evidence": cast(list[JsonValue], evidence),
                      "match_strength": "weak" if weak else "strong", "requires_confirmation": weak}
            if time_field is not None and timestamp is not None:
                record["business_time"] = {
                    "field": time_field, "source_entity": temporal_spec.key,
                    "value": timestamp.date().isoformat() if date_only else timestamp.isoformat(),
                    "precision": "date" if date_only else "datetime", "timezone": "Asia/Shanghai"}
            size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if result_bytes + size > MAX_RESULT_BYTES:
                reads.complete = False
                reads.warnings.append("候选证据达到本次结果大小预算，请缩小查询条件。")
                break
            result_bytes += size
            candidates.append((record, score, timestamp.timestamp() if timestamp else 0.0))
    candidates.sort(key=lambda item: (-item[2] if clues.get("time_hint") else 0,
                                      -item[1], str(item[0]["entity"]), cast(int, item[0]["id"])))
    records = [item[0] for item in candidates]
    if sort and specs:
        records = order_rows(records, sort, {key: value.type for key, value in specs[0].fields.items()})
    if any(row["requires_confirmation"] for row in records):
        reads.warnings.append("相近表达或线索不完整的结果需结合详情、真实关联核实，不能仅凭相似度认定对象或故障因果。可继续查询自行核实，仍无法分辨时再请用户选择。")
    headline = (f"本次已查范围找到 {len(records)} 条候选" if records else "本次已查范围未找到匹配记录")
    if not reads.complete:
        headline += "，部分范围尚未核实"
    return engine.publish("exploration_search", {
        "headline": headline, "records": cast(list[JsonValue], records),
        "total": len(records) if reads.complete else None, "next_offset": None,
        "searched_entities": [spec.key for spec in specs if root_scans[spec.key] is not None],
        "source_entities": [key for key in sorted(reads.scans)], "semantic_status": semantic_status(),
        "applied_query": {"query": query, "entity_types": [spec.key for spec in specs],
                          "filters": arguments.get("filters"), "entity_filters": arguments.get("entity_filters"),
                          "sort": sort, "clues": clues, "time": time_plans, "timezone": "Asia/Shanghai"},
    }, complete=reads.complete, warnings=list(dict.fromkeys(str(value) for value in reads.warnings)))
