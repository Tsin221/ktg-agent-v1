"""Conclusion-first business blueprints, bounded by the showcase and role."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.exploration.catalog import EntitySpec, get_entity
from ktg_agent.exploration.engine import ReadEngine
from ktg_agent.exploration.relations import RelatedRows, related_rows
from ktg_agent.ktg.client import KTGError


def _node(
    key: str, label: str, state: str, summary: str, rows: tuple[JsonObject, ...] = (), *,
    evidence: list[JsonValue] | None = None, warnings: tuple[str, ...] = (),
) -> JsonObject:
    readable = [row for row in rows if row.get("can_read") is True]
    return {
        "id": key, "label": label, "state": state,
        "record_refs": [str(row["record_ref"]) for row in readable if isinstance(row.get("record_ref"), str)],
        "can_read": bool(readable), "available_actions": (["summary"] if rows else []) + (["read"] if readable else []),
        "records": list(rows[:20]), "record_count": len(rows),
        "records_truncated": len(rows) > 20,
        "summary": summary, "evidence": evidence or [], "warnings": list(warnings),
    }


def _state(row: JsonObject) -> str:
    status = row.get("status")
    if status == "FINISHED":
        return "已完成"
    if status in {"PREPARE", "CONFIRMED", "APPROVED", "APPROVING", "STARTED", "NORMAL"}:
        return "进行中"
    return "未核实"


async def _linked(engine: ReadEngine, spec: EntitySpec, row: JsonObject, relation: str) -> RelatedRows:
    edge = next((candidate for candidate in spec.relations if candidate.key == relation), None)
    if edge is None:
        return RelatedRows((), False, "关系未适配", ("当前展示目录未登记该关系。",))
    return await related_rows(engine, row, edge)


async def _production(engine: ReadEngine, row: JsonObject) -> JsonObject:
    spec = get_entity("pro.workorder")
    nodes: list[JsonValue] = [
        _node("workorder", "生产工单", _state(row), f"单据状态：{row.get('status') or '未提供'}", (row,),
              evidence=[{"record_ref": row["record_ref"], "field": "status", "value": row.get("status")}]),
    ]
    complete = True
    warnings: list[JsonValue] = []
    for key, label, relation in (
        ("bom", "工单物料需求", "workorder.bom"),
        ("tasks", "生产任务", "workorder.tasks"),
        ("issue", "生产领料", "workorder.issues"),
        ("feedback", "生产报工", "workorder.feedback"),
    ):
        engine.cancelled()
        result = await _linked(engine, spec, row, relation)
        complete = complete and result.complete
        warnings.extend(result.warnings)
        target = next((edge.target for edge in spec.relations if edge.key == relation), None)
        if target is None:
            records: tuple[JsonObject, ...] = ()
        else:
            records = tuple(engine.record(get_entity(target), candidate) for candidate in result.rows)
        state = result.state
        if records:
            state = "已完成" if all(_state(record) == "已完成" for record in records) else "进行中"
            if key == "bom":
                state = "未核实"
            summary = f"已查到 {len(records)} 条记录" + ("；未查全" if not result.complete else "")
            if key == "bom":
                summary += "；需求不是实际领料或齐套结论"
        elif state == "无权限":
            summary = "当前角色无权查看，未查询该环节的实际记录。"
        elif state in {"未核实", "关系未适配"}:
            summary = "该环节尚未核实，不能据此判定未开始。"
        else:
            summary = "在已查范围未找到关联记录，未据此判定未开始。"
        nodes.append(_node(key, label, state, summary, records,
                           evidence=[{"relation": relation, "coverage": "complete" if result.complete else "partial"}],
                           warnings=result.warnings))
    # Blueprint placeholders are static product knowledge, not query targets.
    # Never resolve, authorize, scan or count these out-of-scope entities.
    for key, label in (("ipqc", "过程检验"), ("produce", "独立产出"), ("receipt", "成品入库")):
        nodes.append(_node(key, label, "本演示未接入", "保留业务蓝图；本演示不查询此环节的实际状态。"))
    headline = f"{row.get('code') or row.get('name') or '生产工单'}：{_state(row)}"
    try:
        quantity, produced = Decimal(str(row.get("quantity"))), Decimal(str(row.get("quantity_produced")))
        if quantity.is_finite() and produced.is_finite() and quantity > 0 and produced >= 0:
            headline = f"已记录产量 {produced}/{quantity}{row.get('unit_of_measure') or ''}"
    except (InvalidOperation, ValueError):
        pass
    return engine.publish("exploration_flow", {
        "flow_type": "production", "root_ref": row["record_ref"], "headline": headline, "nodes": nodes,
        "edges": [
            {"source": "workorder", "target": "bom", "kind": "blueprint"},
            {"source": "workorder", "target": "tasks", "kind": "blueprint"},
            {"source": "tasks", "target": "issue", "kind": "blueprint"},
            {"source": "tasks", "target": "feedback", "kind": "blueprint"},
            {"source": "tasks", "target": "ipqc", "kind": "parallel_blueprint"},
            {"source": "feedback", "target": "produce", "kind": "blueprint"},
            {"source": "produce", "target": "receipt", "kind": "blueprint"},
        ],
        "blueprint_note": "流程边表示业务蓝图；实际单据关系另由授权记录和关系证据证明。",
    }, complete=complete, warnings=list(dict.fromkeys(cast(list[str], warnings))))


def _date_warnings(row: JsonObject) -> tuple[str, ...]:
    parsed: dict[str, date] = {}
    warnings: list[str] = []
    for key in ("require_date", "finish_date", "confirm_date"):
        value = row.get(key)
        if value:
            try:
                parsed[key] = date.fromisoformat(str(value))
            except ValueError:
                warnings.append("业务日期格式无效，需核实。")
    if ("require_date" in parsed and "finish_date" in parsed and parsed["finish_date"] < parsed["require_date"]
            or "finish_date" in parsed and "confirm_date" in parsed and parsed["confirm_date"] < parsed["finish_date"]):
        warnings.append("维修日期先后不一致，需核实。")
    if row.get("status") in {"PREPARE", "APPROVING"} and (row.get("finish_date") or row.get("confirm_date")):
        warnings.append("单据状态与完工/验收日期不一致，需核实。")
    if row.get("status") == "FINISHED" and not row.get("finish_date"):
        warnings.append("单据标为已完成，但未填写完工日期，需核实。")
    return tuple(warnings)


def _repair_result(
    engine: ReadEngine, row: JsonObject, *, additional_warnings: tuple[str, ...] = (),
    complete: bool = True, headline_override: str | None = None,
) -> JsonObject:
    date_warnings = _date_warnings(row)
    warnings = date_warnings + additional_warnings
    reference = row["record_ref"]
    finished = bool(row.get("finish_date"))
    accepted = bool(row.get("confirm_date"))
    repair_state = "未核实" if date_warnings or not finished else "已完成"
    acceptance_state = "未核实" if date_warnings else "已完成" if accepted else "未查到关联记录"
    outcome = row.get("repair_result")
    if date_warnings:
        headline = "维修记录存在不一致，状态待核实。"
    elif finished:
        headline = "已记录维修成功" if outcome == "SUCCESS" else f"已记录完工；维修结果：{outcome or '未填写'}"
        headline += "；验收日期已记录" if accepted else "；未填写验收日期"
    else:
        headline = "尚未记录完工日期；维修结果待核实。"
    if headline_override:
        headline = headline_override
    nodes: list[JsonValue] = [
        _node("reported", "报修", "已完成" if row.get("require_date") else "未核实",
              f"报修日期：{row.get('require_date') or '未填写'}", (row,),
              evidence=[{"record_ref": reference, "field": "require_date", "value": row.get("require_date")}]),
        _node("repair", "维修", repair_state,
              f"完工日期：{row.get('finish_date') or '未填写'}；维修结果：{outcome or '未填写'}", (row,),
              evidence=[{"record_ref": reference, "fields": {key: row.get(key) for key in ("status", "finish_date", "repair_result")}}],
              warnings=warnings),
        _node("acceptance", "验收", acceptance_state,
              f"验收日期：{row.get('confirm_date') or '未填写'}；没有独立验收结果，不能推断通过。", (row,),
              evidence=[{"record_ref": reference, "field": "confirm_date", "value": row.get("confirm_date")}],
              warnings=warnings),
    ]
    return engine.publish("exploration_flow", {
        "flow_type": "repair", "root_ref": reference, "headline": headline, "nodes": nodes,
        "edges": [{"source": "reported", "target": "repair"}, {"source": "repair", "target": "acceptance"}],
    }, complete=complete and not date_warnings, warnings=list(warnings))


def _empty_repair(engine: ReadEngine, root: JsonObject, state: str, warnings: tuple[str, ...]) -> JsonObject:
    return engine.publish("exploration_flow", {
        "flow_type": "repair", "root_ref": root["record_ref"], "headline": "维修情况尚无法确认。",
        "nodes": [
            _node(key, label, state, "未取得可核实的维修单；不推断实际状态。", warnings=warnings)
            for key, label in (("reported", "报修"), ("repair", "维修"), ("acceptance", "验收"))
        ],
        "edges": [{"source": "reported", "target": "repair"}, {"source": "repair", "target": "acceptance"}],
    }, complete=False, warnings=list(warnings))


async def business_flow(engine: ReadEngine, record_ref: str) -> JsonObject:
    engine.cancelled()
    spec, key = engine.resolve_record(record_ref)
    row = await engine.read(spec.key, key)
    if spec.key == "pro.workorder":
        return await _production(engine, row)
    if spec.key in {"pro.task", "pro.feedback", "pro.workorder_bom", "wm.issue"}:
        parent = row.get("workorder_id")
        if type(parent) is not int or parent <= 0:
            raise KTGError("NO_RECORD", "来源没有可核实的关联工单。")
        return await _production(engine, await engine.read("pro.workorder", parent))
    if spec.key == "dv.repair":
        return _repair_result(engine, row)
    if spec.key == "dv.repair_line":
        parent = row.get("repair_id")
        if type(parent) is not int or parent <= 0:
            raise KTGError("NO_RECORD", "内容行没有可核实的维修单关联。")
        return _repair_result(engine, await engine.read("dv.repair", parent))
    if spec.key == "dv.machinery":
        repair_spec = get_entity("dv.repair")
        if not engine.allowed(repair_spec.permissions.get("detail", ())):
            return _empty_repair(engine, row, "无权限", ("当前角色无权读取维修单详情。",))
        found = await _linked(engine, spec, row, "machinery.repairs")
        if not found.rows:
            return _empty_repair(engine, row, found.state, found.warnings)
        latest = max(found.rows, key=lambda repair: (str(repair.get("require_date") or ""), int(cast(int, repair["id"]))))
        detail = await engine.read("dv.repair", cast(int, latest["id"]))
        if not found.complete:
            return _repair_result(engine, detail, complete=False, additional_warnings=found.warnings,
                                  headline_override="已查范围内的维修记录；最近一次尚未核实。")
        return _repair_result(engine, detail)
    return engine.publish("exploration_flow", {
        "flow_type": "unadapted", "root_ref": row["record_ref"], "headline": "该展示对象暂未适配完整流程。",
        "nodes": [_node("record", spec.label, "关系未适配", "可继续使用已授权的记录或关联查询。", (row,))],
        "edges": [],
    }, complete=False, warnings=["仅生产工单与设备维修配置了本期完整流程蓝图。"])
