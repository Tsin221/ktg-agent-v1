"""Resolve a business identifier against existing authorized MES read endpoints."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.production_trace import _STATES, _checked_page, _id
from ktg_agent.tools import ExecutionMode, RetryPolicy, ToolContext, ToolDefinition, ToolEffect, ToolResult, ToolStatus

NAME = "ktg_resolve_business_reference"
PERMISSIONS = ("mes:pro:execution:trace", "mes:pro:execution:query", "mes:pro:workorder:list",
               "mes:md:mditem:list", "mes:wm:wmstock:list", "mes:wm:itemrecptline:list")
_PAGE_SIZE = 50
_MAX_PAGES = 4
_MAX_MATCHES = 30


@dataclass(slots=True)
class _Probe:
    coverage: str
    matches: list[JsonObject]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= 255 else None


def _auth_failure(exc: KTGError) -> None:
    if exc.code in {"AUTH_REQUIRED", "AUTH_EXPIRED"}:
        raise exc


async def _sn(client: KTGClient, code: str) -> _Probe:
    try:
        async with asyncio.timeout(8):
            payload = await client.get("/mes/pro/execution/units/trace", {"snCode": code})
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("snCode") != code:
            return _Probe("unavailable", [])
        trace_status = data.get("traceStatus")
        if not isinstance(trace_status, str) or trace_status not in {"TRACKED", "LEGACY_NO_RECORD"}:
            return _Probe("unavailable", [])
        tracked = trace_status == "TRACKED"
        match: JsonObject = {"type": "sn", "code": code, "sn_id": _id(data.get("snId")),
            "trace_status": trace_status, "unit_state": _text(data.get("unitState")),
            "workorder_id": _id(data.get("workorderId")) if tracked else None,
            "workorder_code": _text(data.get("workorderCode")), "workorder_name": _text(data.get("workorderName")),
            "item_code": _text(data.get("itemCode")), "item_name": _text(data.get("itemName"))}
        if tracked and (not match["workorder_code"] or not match["item_code"] or match["unit_state"] not in _STATES):
            return _Probe("unavailable", [])
        return _Probe("complete", [match])
    except KTGError as exc:
        _auth_failure(exc)
        return _Probe("complete" if exc.code == "KTG_NOT_FOUND" else "unavailable", [])
    except TimeoutError:
        return _Probe("unavailable", [])


def _project(kind: str, code: str, row: Mapping[str, object]) -> JsonObject:
    if kind == "workorder":
        return {"type": kind, "code": code, "workorder_id": _id(row.get("workorderId")),
                "workorder_name": _text(row.get("workorderName")), "status": _text(row.get("status")),
                "product_code": _text(row.get("productCode")), "product_name": _text(row.get("productName"))}
    common: JsonObject = {"type": "item" if kind == "item" else "batch", "code": code,
                         "item_id": _id(row.get("itemId")), "item_code": _text(row.get("itemCode")),
                         "item_name": _text(row.get("itemName"))}
    if not common["item_code"]:
        raise KTGError("KTG_INVALID_RESPONSE", "批次或物料身份缺少编码。")
    if kind == "item":
        common.update(item_or_product=_text(row.get("itemOrProduct")), unit=_text(row.get("unitOfMeasure")))
    else:
        common.update(batch_code=code, sources=["stock" if kind == "stock_batch" else "receipt_line"])
    return common


async def _list(client: KTGClient, code: str, kind: str, path: str, filter_field: str, id_field: str) -> _Probe:
    matches: list[JsonObject] = []
    seen: set[str] = set()
    total: int | None = None
    try:
        async with asyncio.timeout(8):
            for page in range(1, _MAX_PAGES + 1):
                result = await client.get(path, {filter_field: code, "pageNum": page, "pageSize": _PAGE_SIZE,
                    "orderByColumn": id_field, "isAsc": "asc", "reasonable": "false"})
                count, rows = _checked_page(result, page, _PAGE_SIZE)
                if total is not None and count != total:
                    return _Probe("partial", matches)
                total = count
                for row in rows:
                    row_id = _id(row.get(id_field))
                    if row_id in seen:
                        return _Probe("partial", matches)
                    seen.add(row_id)
                    if row.get(filter_field) == code:  # do not accept fuzzy or collation-only matches
                        projected = _project(kind, code, row)
                        if kind in {"stock_batch", "receipt_batch"} and projected in matches:
                            continue
                        if len(matches) >= _MAX_MATCHES:
                            return _Probe("partial", matches)
                        matches.append(projected)
                if len(seen) == total:
                    return _Probe("complete", matches)
            return _Probe("partial", matches)
    except KTGError as exc:
        _auth_failure(exc)
        return _Probe("partial" if matches else "unavailable", matches)
    except TimeoutError:
        return _Probe("partial" if matches else "unavailable", matches)


async def _execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    client, code = context.ktg_client, arguments.get("code")
    if not isinstance(client, KTGClient):
        return ToolResult("KTG 查询客户端不可用。", status=ToolStatus.FAILED, error_code="KTG_CLIENT_MISSING")
    if not isinstance(code, str) or not code.strip() or len(code) > 255 or any(ord(c) < 32 for c in code):
        return ToolResult("请提供完整业务编码。", status=ToolStatus.FAILED, error_code="VALIDATION_ERROR")
    kinds = ("sn", "workorder", "item", "stock_batch", "receipt_batch")
    try:
        results = await asyncio.gather(
            _sn(client, code),
            _list(client, code, "workorder", "/mes/pro/workorder/list", "workorderCode", "workorderId"),
            _list(client, code, "item", "/mes/md/mditem/list", "itemCode", "itemId"),
            _list(client, code, "stock_batch", "/mes/wm/wmstock/list", "batchCode", "materialStockId"),
            _list(client, code, "receipt_batch", "/mes/wm/itemrecptline/list", "batchCode", "lineId"),
        )
    except KTGError as exc:
        return ToolResult("登录状态已失效，请重新登录后查询。", status=ToolStatus.FAILED, error_code=exc.code)
    matches: list[JsonObject] = []
    batches: dict[tuple[str, str], JsonObject] = {}
    for probe in results:
        for match in probe.matches:
            if match["type"] != "batch":
                matches.append(match)
                continue
            key = (cast(str, match["item_id"]), cast(str, match["item_code"]))
            previous = batches.get(key)
            if previous is None:
                batches[key] = match
            else:
                previous["sources"] = list(dict.fromkeys(cast(list[str], previous["sources"]) + cast(list[str], match["sources"])))
    matches.extend(batches.values())
    complete = all(probe.coverage == "complete" for probe in results) and len(matches) <= _MAX_MATCHES
    resolution = ("ambiguous" if len(matches) > 1 else "matched" if matches and complete else
                  "matched_partial" if matches else "not_found" if complete else "unconfirmed")
    details: JsonObject = {"kind": "business_reference", "code": code, "resolution": resolution,
        "matches": cast(list[JsonValue], matches[:_MAX_MATCHES]), "coverage": "complete" if complete else "partial",
        "checks": {kind: probe.coverage for kind, probe in zip(kinds, results)}, "queried_at": datetime.now(UTC).isoformat()}
    return ToolResult("仅核实当前权限下的精确业务编码；无匹配不表示整个系统不存在，未核实不表示无记录。"
        "批次来自非零现存或入库单行，不证明已经入库、领料或使用。多个匹配须保留歧义；根据真实对象使用相应业务查询。"
        + json.dumps(details, ensure_ascii=False, separators=(",", ":")), details=details)


def build_business_reference_tool() -> ToolDefinition:
    return ToolDefinition(name=NAME, version="1.0", label="业务编码识别",
        description="当用户仅给编码、说查一下某编号，或编码类型不明时优先识别真实对象。按精确值同时核实成品单件SN、工单、物料/产品、原料批次（现存与入库单行），不猜编号前缀。返回真实匹配、歧义和未核实范围，并提供后续库存、质量、追溯等工具所需编码。已知对象类型时直接用对应业务工具。",
        input_schema={"type": "object", "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"\S", "description": "用户提供的完整业务编码，不添加前缀、不猜类型。"}}, "required": ["code"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"kind": {"const": "business_reference"}, "code": {"type": "string"},
            "resolution": {"enum": ["matched", "matched_partial", "ambiguous", "not_found", "unconfirmed"]},
            "matches": {"type": "array", "maxItems": _MAX_MATCHES, "items": {"type": "object", "required": ["type", "code"]}},
            "coverage": {"enum": ["complete", "partial"]}, "checks": {"type": "object"}, "queried_at": {"type": "string"}},
            "required": ["kind", "code", "resolution", "matches", "coverage", "checks", "queried_at"], "additionalProperties": False},
        required_permissions=PERMISSIONS, effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=_execute)
