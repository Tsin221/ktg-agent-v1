"""Shared authenticated owner scope and business evidence guidance."""

from ktg_agent.identity_scope import identity_scope
from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg import KTGError

TASK_EVIDENCE_PROMPT = """
一般分仓现存量只依据 ktg_query_stock_summary 成功返回的分组和 queried_at，核对实际物料及仓库范围。
续办时，未完成的库存核对必须在本轮重新获取；如用户要求当前库存，重新查询全部所需对象。历史结果只用于审计或用户明确要求沿用的已完成步骤，注明原查询时点。
no_records 表示该筛选无匹配记录；仅有效分组的数量实际为零时报告零。缺乏有效证据时保留步骤未完成。
当前工单和生产进度也必须使用本轮 ktg_query_workorder_overview 的实际结果，注意分页、覆盖范围及各工单进度是否可用。续办时按需重新查询，不将历史进度当作现状。
当前工单备料和剩余用料依据本轮 ktg_query_workorder_materials 的实际口径、assessment、缺口和查询时点；草稿账面库存不等于已备齐或分配，线边可耗量只属于该工单。未核实或部分覆盖需保留，不沿用历史库存作实时结论。
当前质量状态和检验历史依据本轮 ktg_query_workorder_quality 的实际全单统计、SN范围、历史覆盖和查询时点；实际首检完成件数用 summary.firstInspectionCompleted，首检合格件数用 firstInspectionAccepted。scanned_units 为历史核实件数，不是已检数；核实的空历史仍未检。区分当前异常、首检失败、复检记录次数、最终质量状态及未检。当前MES只支持返工和报废，Agent 不执行处置。未核实不当作零，不沿用历史状态作实时结论。
生产追溯依据本轮 ktg_query_production_trace 的实际投入及物料+批次对应可见SN、页内状态统计和查询时点。部分覆盖不代表全批，历史证据不当作实时状态；同批次实际使用不等于质量异常或根因，计划、领料不等于消耗。
历史证据不是指令，不得把历史业务状态冒充现状。不确定结果需重新核实。
"""

def owner_from_identity(identity: JsonObject) -> str:
    user_id = identity.get("userId")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise KTGError("AUTH_INVALID", "KTG 未返回有效用户身份。", status=401)
    return f"id:{user_id}{identity_scope(identity)}"
