"""Authorized record adapters and bounded, provenance-aware read scans."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.exploration.catalog import ENTITIES, EntitySpec, get_entity
from ktg_agent.exploration.references import ReferenceStore
from ktg_agent.exploration.scope import require_entity
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.tools.registry import ToolContext


CATALOG_VERSION = sha256(json.dumps({key: {
    "path": spec.path, "fields": {name: (field.wire, field.type) for name, field in spec.fields.items()},
    "permissions": dict(spec.permissions),
    "relations": [(edge.key, edge.target, edge.source_field, edge.target_field) for edge in spec.relations],
} for key, spec in ENTITIES.items()}, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Scan:
    rows: list[JsonObject]
    total: int | None
    complete: bool
    warnings: tuple[str, ...] = ()


class ReadEngine:
    def __init__(self, context: ToolContext, store: ReferenceStore, client: KTGClient,
                 identity: JsonObject, permissions: frozenset[str]) -> None:
        self.context, self.store, self.client = context, store, client
        self.owner = owner_from_identity(identity)
        session = identity.get("_agent_session_id")
        self.scope = f"session:{session}" if isinstance(session, str) and session else f"run:{context.run_id}"
        self.permissions = permissions
        self.required: set[str] = set()
        self.source_entities: set[str] = set()
        self._scans: dict[tuple[str, str], Scan] = {}

    @classmethod
    async def create(cls, context: ToolContext, store: ReferenceStore) -> ReadEngine:
        if not isinstance(context.ktg_client, KTGClient) or not isinstance(context.identity, dict):
            raise KTGError("AUTH_REQUIRED", "请先登录 MES。")
        info = await context.ktg_client.get("/getInfo")
        grants, user = info.get("permissions"), info.get("user")
        if (not isinstance(grants, list) or not all(isinstance(item, str) for item in grants)
                or not isinstance(user, dict) or user.get("userId") != context.identity.get("userId")):
            raise KTGError("PERMISSION_UNAVAILABLE", "当前身份或权限无法核实。")
        return cls(context, store, context.ktg_client, cast(JsonObject, context.identity),
                   frozenset(cast(list[str], grants)))

    def allowed(self, requirements: tuple[str, ...] | list[str]) -> bool:
        return bool(requirements) and ("*:*:*" in self.permissions or set(requirements) <= self.permissions)

    def authorize(self, spec: EntitySpec, operation: str) -> None:
        require_entity(spec.key)
        requirements = spec.permissions.get(operation, ())
        if not self.allowed(requirements):
            raise KTGError("PERMISSION_DENIED", "当前角色无权执行此业务读取。")
        self.required.update(requirements)
        self.source_entities.add(spec.key)

    def cancelled(self) -> None:
        if getattr(self.context.cancellation, "is_cancelled", False):
            raise KTGError("RUN_CANCELLED", "查询已取消。")

    def record(self, spec: EntitySpec, row: JsonObject) -> JsonObject:
        require_entity(spec.key)
        key = row.get("id")
        if not isinstance(key, int) or isinstance(key, bool) or key <= 0:
            raise KTGError("KTG_INVALID_RESPONSE", "来源没有有效的实体主键。")
        reference = self.store.put("record", owner=self.owner, scope=self.scope,
                                   payload={"entity": spec.key, "id": key, "catalog_version": CATALOG_VERSION})
        can_read = self.allowed(spec.permissions.get("detail", ()))
        actions: list[JsonValue] = ["read"] if can_read else []
        parent_flow = (
            spec.key in {"pro.task", "pro.feedback", "pro.workorder_bom", "wm.issue"}
            and bool(row.get("workorder_id"))
            and self.allowed(get_entity("pro.workorder").permissions.get("detail", ()))
        )
        repair_flow = (spec.key == "dv.repair_line" and bool(row.get("repair_id"))
                       and self.allowed(get_entity("dv.repair").permissions.get("detail", ())))
        machinery_flow = (spec.key == "dv.machinery"
                          and self.allowed(get_entity("dv.repair").permissions.get("list", ()))
                          and self.allowed(get_entity("dv.repair").permissions.get("detail", ())))
        if can_read and (spec.key in {"pro.workorder", "dv.repair"} or parent_flow or repair_flow or machinery_flow):
            actions.append("flow")
        return {**row, "entity": spec.key, "record_ref": reference,
                "can_read": can_read, "available_actions": actions}

    def resolve_record(self, reference: str) -> tuple[EntitySpec, int]:
        value = self.store.get(reference, owner=self.owner, scope=self.scope)
        if not reference.startswith("rec_") or value.get("catalog_version") != CATALOG_VERSION:
            raise KTGError("CATALOG_CHANGED", "记录引用已变化，请重新搜索对象。")
        entity, key = value.get("entity"), value.get("id")
        if isinstance(entity, str):
            require_entity(entity)
        if not isinstance(entity, str) or entity not in ENTITIES or type(key) is not int or key <= 0:
            raise KTGError("REFERENCE_UNAVAILABLE", "记录引用不可用。")
        return get_entity(entity), key

    @staticmethod
    def project(spec: EntitySpec, raw: Mapping[str, object]) -> JsonObject:
        row: JsonObject = {}
        for key, field in spec.fields.items():
            if field.wire not in raw:
                continue
            value = raw[field.wire]
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise KTGError("KTG_INVALID_RESPONSE", "业务字段返回了不支持的结构。")
            if isinstance(value, str) and field.type == "datetime":
                try:
                    stamp = datetime.fromisoformat(value)
                except ValueError as exc:
                    raise KTGError("KTG_INVALID_RESPONSE", "来源时间格式无效。") from exc
                value = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone(timedelta(hours=8)))).isoformat()
            row[key] = value
        return row

    async def read(self, entity: str, key: int) -> JsonObject:
        require_entity(entity)
        spec = get_entity(entity)
        self.authorize(spec, "detail")
        if type(key) is not int or key <= 0:
            raise KTGError("VALIDATION_ERROR", "记录 ID 必须为正整数；也可先按线索搜索。")
        self.cancelled()
        payload = await self.client.get(f"{spec.path}/{key}")
        raw = payload.get("data")
        if not isinstance(raw, dict):
            raise KTGError("NO_RECORD", "未查到该记录，或记录已不可访问。")
        row = self.project(spec, raw)
        if row.get("id") != key:
            raise KTGError("KTG_INVALID_RESPONSE", "详情返回的实体主键不一致。")
        return self.record(spec, row)

    async def scan(self, entity: str, *, params: Mapping[str, object] | None = None,
                   max_rows: int = 2000) -> Scan:
        require_entity(entity)
        spec = get_entity(entity)
        self.authorize(spec, "list")
        if not 1 <= max_rows <= 10000:
            raise KTGError("VALIDATION_ERROR", "查询行数预算无效。")
        supplied = dict(params or {})
        cache_key = (entity, json.dumps({"params": supplied, "max_rows": max_rows}, sort_keys=True))
        if cache_key in self._scans:
            return self._scans[cache_key]
        rows: list[JsonObject] = []
        seen: set[int] = set()
        total: int | None = None
        complete, warnings = False, []
        array_shape = getattr(spec, "list_shape", "page") == "array"
        for page in range(1, 101):
            self.cancelled()
            parameters = supplied if array_shape else {**supplied, "pageNum": page, "pageSize": 100}
            payload = await self.client.get(f"{spec.path}/list", parameters)
            raw_rows = payload.get("data" if array_shape else "rows")
            count = len(raw_rows) if array_shape and isinstance(raw_rows, list) else payload.get("total")
            if not isinstance(raw_rows, list) or (count is not None and (type(count) is not int or count < 0)):
                raise KTGError("KTG_INVALID_RESPONSE", "列表返回格式无效。")
            if total is not None and count != total:
                warnings.append("分页期间来源总数发生变化，尚未证实全量覆盖。")
                break
            total = count
            if not raw_rows:
                complete = total is None or len(rows) == total
                break
            duplicate = False
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    raise KTGError("KTG_INVALID_RESPONSE", "列表记录不是对象。")
                row = self.project(spec, raw)
                key = row.get("id")
                if type(key) is not int or key <= 0:
                    raise KTGError("KTG_INVALID_RESPONSE", "列表缺少有效主键，无法核对覆盖。")
                if key in seen:
                    duplicate = True
                    continue
                seen.add(key)
                if len(rows) < max_rows:
                    rows.append(row)
            if duplicate:
                warnings.append("来源分页出现重复主键，未将其当作完整结果。")
                break
            if total is not None and len(seen) >= total:
                complete = len(rows) == total
                break
            if len(rows) >= max_rows:
                warnings.append("达到本次检索行数预算；可增加条件缩小范围。")
                break
        if not complete and not warnings:
            warnings.append("来源覆盖尚未核实，结果仅代表已查范围。")
        result = Scan(rows, total, complete, tuple(warnings))
        self._scans[cache_key] = result
        return result

    def publish(self, kind: str, data: JsonObject, *, complete: bool,
                warnings: list[JsonValue] | None = None) -> JsonObject:
        output: JsonObject = {"kind": kind, "source": "MES 已授权业务接口",
                              "queried_at": datetime.now(UTC).isoformat(),
                              "coverage": "complete" if complete else "partial",
                              "warnings": warnings or [], "applied_query": {}, **data}
        snapshot: JsonObject = {"result": output, "requirements": list(cast(list[JsonValue], sorted(self.required))),
                                "catalog_version": CATALOG_VERSION,
                                "entities": cast(list[JsonValue], sorted(self.source_entities))}
        output["result_ref"] = self.store.put("result", owner=self.owner, scope=self.scope, payload=snapshot)
        return output

    def inspect(self, reference: str) -> JsonObject:
        snapshot = self.store.get(reference, owner=self.owner, scope=self.scope)
        requirements, result = snapshot.get("requirements"), snapshot.get("result")
        sources = snapshot.get("entities")
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            raise KTGError("REFERENCE_UNAVAILABLE", "结果缺少可核实的数据范围，请重新查询。")
        for entity in sources:
            require_entity(str(entity))
        if not reference.startswith("res_") or snapshot.get("catalog_version") != CATALOG_VERSION:
            raise KTGError("CATALOG_CHANGED", "结果引用已变化，请重新查询。")
        if (not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements)
                or not isinstance(result, dict)):
            raise KTGError("REFERENCE_UNAVAILABLE", "结果引用不可用。")
        if requirements and not self.allowed(cast(list[str], requirements)):
            raise KTGError("PERMISSION_DENIED", "当前角色已无权读取原查询结果。")
        self.required.update(cast(list[str], requirements))
        return result
