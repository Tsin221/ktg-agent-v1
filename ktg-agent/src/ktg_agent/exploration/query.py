"""Typed conditions and root-grain calculations shared by exploration tools."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from typing import Any
import unicodedata

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGError


OPS = frozenset({"eq", "ne", "in", "not_in", "contains", "starts_with", "gt", "gte",
                 "lt", "lte", "between", "is_null"})


def normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _invalid(message: str) -> KTGError:
    return KTGError("VALIDATION_ERROR", message)


def _value(value: object, kind: str) -> Any:
    if kind in {"integer", "decimal", "number", "numeric"}:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise _invalid("数值条件必须是有限数值，不能使用布尔值。")
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise _invalid("数值条件格式无效。") from exc
        if not result.is_finite() or (kind == "integer" and result != result.to_integral_value()):
            raise _invalid("数值条件类型无效。")
        return result
    if kind == "date":
        if not isinstance(value, str):
            raise _invalid("日期必须使用 yyyy-MM-dd。")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _invalid("日期必须使用 yyyy-MM-dd。") from exc
    if kind in {"datetime", "date-time"}:
        if not isinstance(value, str):
            raise _invalid("时间必须是带时区的 ISO 时间。")
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise _invalid("时间格式无效。") from exc
        if stamp.tzinfo is None:
            raise _invalid("时间必须包含时区。")
        return stamp
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _invalid("布尔字段需要 true 或 false。")
        return value
    if not isinstance(value, str):
        raise _invalid("文本字段需要字符串。")
    return normalized(value)


def validate_filter(node: object, fields: Mapping[str, str], *, _depth: int = 0,
                    _budget: list[int] | None = None) -> None:
    budget = _budget if _budget is not None else [64]
    budget[0] -= 1
    if budget[0] < 0 or _depth > 8:
        raise _invalid("条件过多或嵌套过深，请拆分查询。")
    if not isinstance(node, Mapping):
        raise _invalid("筛选条件需要对象。")
    for operator in ("all", "any"):
        if operator in node:
            children = node[operator]
            if set(node) != {operator} or not isinstance(children, list) or not children:
                raise _invalid("all/any 只能包含非空条件数组。")
            for child in children:
                validate_filter(child, fields, _depth=_depth + 1, _budget=budget)
            return
    if "related" in node:
        relation = node["related"]
        if (set(node) != {"related"} or not isinstance(relation, Mapping)
                or set(relation) != {"relation", "where"} or not isinstance(relation["relation"], str)
                or not isinstance(relation["where"], Mapping)):
            raise _invalid("关联条件需要登记的 relation 和 where。")
        # The engine validates target fields against the related entity's catalog.
        return
    if set(node) != {"field", "op", "value"}:
        raise _invalid("条件只能包含 field、op、value。")
    field, op, value = node["field"], node["op"], node["value"]
    if not isinstance(field, str) or field not in fields or not isinstance(op, str) or op not in OPS:
        raise KTGError("UNSUPPORTED_FILTER", "不支持的字段或操作符；请查看业务目录。")
    kind = fields[field]
    if op == "is_null":
        if not isinstance(value, bool):
            raise _invalid("is_null 的 value 必须是布尔值。")
        return
    if value is None:
        raise _invalid("空值只能使用 is_null 条件。")
    if op in {"contains", "starts_with"} and kind not in {"string", "text"}:
        raise KTGError("UNSUPPORTED_FILTER", "只有文本字段支持包含或前缀查询。")
    if op in {"in", "not_in", "between"}:
        if not isinstance(value, list) or not value or len(value) > 100:
            raise _invalid("集合条件需要 1 至 100 个同类型值。")
        values = [_value(item, kind) for item in value]
        if op == "between" and (len(values) != 2 or values[0] >= values[1]):
            raise _invalid("between 需要递增的半开区间 [起点,终点)。")
    else:
        _value(value, kind)


def matches(row: Mapping[str, object], node: Mapping[str, Any], fields: Mapping[str, str]) -> bool:
    if "all" in node:
        return all(matches(row, child, fields) for child in node["all"])
    if "any" in node:
        return any(matches(row, child, fields) for child in node["any"])
    if "related" in node:
        raise KTGError("UNRESOLVED_RELATION", "关联条件须由已授权关系适配器处理。")
    field, op, expected = node["field"], node["op"], node["value"]
    if field not in row:
        raise KTGError("FIELD_UNAVAILABLE", "结果未读取所需筛选字段。")
    raw = row[field]
    if op == "is_null":
        return bool((raw is None) == expected)
    if raw is None:
        return False
    actual = _value(raw, fields[field])
    if op in {"in", "not_in"}:
        found = actual in [_value(item, fields[field]) for item in expected]
        return found if op == "in" else not found
    if op == "between":
        return bool(_value(expected[0], fields[field]) <= actual < _value(expected[1], fields[field]))
    target = _value(expected, fields[field])
    if op == "contains":
        return str(target) in str(actual)
    if op == "starts_with":
        return str(actual).startswith(str(target))
    if op == "eq":
        return bool(actual == target)
    if op == "ne":
        return bool(actual != target)
    if op == "gt":
        return bool(actual > target)
    if op == "gte":
        return bool(actual >= target)
    if op == "lt":
        return bool(actual < target)
    return bool(actual <= target)


def order_rows(rows: Sequence[JsonObject], sort: Sequence[Mapping[str, object]],
               fields: Mapping[str, str]) -> list[JsonObject]:
    orders: list[tuple[str, str]] = []
    for item in sort:
        field, direction = item.get("field"), item.get("direction")
        if (set(item) != {"field", "direction"} or not isinstance(field, str) or field not in fields
                or not isinstance(direction, str) or direction not in {"asc", "desc"}):
            raise KTGError("UNSUPPORTED_FILTER", "排序字段或方向无效。")
        orders.append((str(field), str(direction)))
    if "id" in fields and all(field != "id" for field, _ in orders):
        orders.append(("id", "asc"))

    def compare(left: JsonObject, right: JsonObject) -> int:
        for field, direction in orders:
            a, b = left.get(field), right.get(field)
            if a is None or b is None:
                result = 0 if a is b else (1 if a is None else -1)
            else:
                av, bv = _value(a, fields[field]), _value(b, fields[field])
                result = (av > bv) - (av < bv)
                if direction == "desc":
                    result = -result
            if result:
                return result
        return 0

    return sorted(rows, key=cmp_to_key(compare))


def aggregate_rows(rows: Sequence[JsonObject], group_by: Sequence[str],
                   aggregates: Sequence[Mapping[str, object]], fields: Mapping[str, str]) -> list[JsonObject]:
    if not aggregates or any(not isinstance(field, str) or field not in fields for field in group_by):
        raise KTGError("UNSUPPORTED_FILTER", "分组或聚合条件无效。")
    deduplicated: dict[str, JsonObject] = {}
    for row in rows:
        if row.get("id") is None:
            raise KTGError("AMBIGUOUS_AGGREGATION", "缺少根实体主键，无法确定统计粒度。")
        key = str(row["id"])
        if key in deduplicated and deduplicated[key] != row:
            raise KTGError("AMBIGUOUS_AGGREGATION", "同一主键有不同明细，统计粒度不明确。")
        deduplicated[key] = row
    groups: dict[tuple[object, ...], list[JsonObject]] = {(): []} if not group_by else {}
    for row in deduplicated.values():
        groups.setdefault(tuple(row.get(key) for key in group_by), []).append(row)
    output: list[JsonObject] = []
    for group, members in groups.items():
        result: JsonObject = {field: members[0].get(field) for field in group_by}
        aliases = set(group_by)
        for aggregate in aggregates:
            op, alias, field = aggregate.get("op"), aggregate.get("as"), aggregate.get("field")
            if not isinstance(alias, str) or not alias or alias in aliases:
                raise _invalid("聚合别名必须非空且不能重复。")
            aliases.add(alias)
            if op == "count_records" and set(aggregate) <= {"op", "as"}:
                result[alias] = len(members)
                continue
            if (not isinstance(op, str) or op not in {"sum", "avg", "min", "max"}
                    or not isinstance(field, str) or field not in fields
                    or set(aggregate) != {"op", "as", "field"}
                    or fields[str(field)] not in {"integer", "decimal", "number", "numeric"}):
                raise KTGError("AMBIGUOUS_AGGREGATION", "请选择可用数值字段、聚合方式和根实体粒度。")
            values = [_value(row[str(field)], fields[str(field)]) for row in members if row.get(str(field)) is not None]
            value = (sum(values, Decimal(0)) if op == "sum" and values else
                     sum(values, Decimal(0)) / len(values) if op == "avg" and values else
                     min(values) if op == "min" and values else max(values) if op == "max" and values else None)
            result[alias] = format(value, "f") if value is not None else None
        output.append(result)
    return output
