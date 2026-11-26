"""MES tool metadata and instructions, independent of any model runtime."""
from __future__ import annotations

from collections.abc import Iterable

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg import KTGClient, build_query_tools
from ktg_agent.ktg.client import KTGError
from ktg_agent.ktg.business_reference import NAME as REFERENCE_TOOL_NAME, PERMISSIONS as REFERENCE_PERMISSIONS, build_business_reference_tool
from ktg_agent.ktg.production_trace import build_production_trace_tool
from ktg_agent.ktg.workorder_materials import build_workorder_materials_tool
from ktg_agent.ktg.workorder_overview import build_workorder_overview_tool
from ktg_agent.ktg.workorder_quality import build_workorder_quality_tool
from ktg_agent.operations.store import OperationPlanStore
from ktg_agent.operations.tool import TOOL_NAME, build_workorder_operation_tool
from ktg_agent.tools.catalog import CatalogSwitches, ToolCatalog, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionAny, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import ToolDefinition, ToolDefinitionError

MES_PAGE_MODULES = ("md", "wm", "pro", "qc")

_DOMAINS: dict[str, tuple[str, str, PermissionPolicy]] = {
    "ktg_count_products": ("md", "items", PermissionLeaf("mes:md:mditem:list")),
    "ktg_query_stock_summary": ("wm", "stock", PermissionLeaf("mes:wm:wmstock:list")),
    "ktg_query_workorder_overview": ("pro", "workorder", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:execution:query"),
    ))),
    "ktg_query_workorder_materials": ("pro", "materials", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:workorderbom:list"),
        PermissionLeaf("mes:pro:execution:query"), PermissionLeaf("mes:wm:wmstock:list"),
    ))),
    "ktg_query_workorder_quality": ("pro", "quality", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:execution:query"),
    ))),
    "ktg_query_production_trace": ("pro", "trace", PermissionAll((
        PermissionLeaf("mes:pro:execution:trace"), PermissionLeaf("mes:pro:execution:query"),
        PermissionLeaf("mes:md:mditem:list"),
    ))),
    REFERENCE_TOOL_NAME: ("pro", "reference", PermissionAll(tuple(PermissionLeaf(p) for p in REFERENCE_PERMISSIONS))),
    "ktg_prepare_workorder_operations": ("pro", "workorder", PermissionAll((
        PermissionLeaf("mes:pro:workorder:list"), PermissionLeaf("mes:pro:workorder:query"),
        PermissionAny((PermissionLeaf("mes:pro:workorder:add"), PermissionLeaf("mes:pro:workorder:edit"),
                       PermissionLeaf("mes:pro:execution:release"))),
    ))),
}


def build_mes_catalog(definitions: Iterable[ToolDefinition]) -> ToolCatalog:
    """Build the static metadata for only the reviewed definitions."""
    manifests: list[ToolManifest] = []
    for definition in definitions:
        if definition.name not in _DOMAINS:
            raise ToolDefinitionError("unreviewed MES tool name")
        module, domain, permission = _DOMAINS[definition.name]
        reference = f"legacy:{definition.name}"
        manifests.append(ToolManifest(
            name=definition.name, version=definition.version,
            module=module, domain=domain, label=definition.label,
            description=definition.description,
            permission_policy=permission,
            effect=definition.effect, execution_mode=definition.execution_mode,
            retry_policy=definition.retry_policy, timeout_seconds=definition.timeout_seconds,
            expose_to_model=definition.expose_to_model,
            confirmation_policy="none", proposal_store_effect=definition.name == "ktg_prepare_workorder_operations",
            handler_ref=reference,
        ))
    return ToolCatalog(manifests)


def mes_definitions(operation_store: OperationPlanStore | None = None) -> tuple[ToolDefinition, ...]:
    definitions = (
        *build_query_tools(), build_workorder_overview_tool(), build_workorder_materials_tool(),
        build_workorder_quality_tool(), build_production_trace_tool(), build_business_reference_tool(),
    )
    return (*definitions, build_workorder_operation_tool(operation_store)) if operation_store is not None else definitions


def default_switches(*, operation_enabled: bool = True) -> CatalogSwitches:
    names = dict.fromkeys(_DOMAINS, True)
    names[TOOL_NAME] = operation_enabled
    return CatalogSwitches({"md": True, "wm": True, "pro": True}, names)


async def available_tools(client: KTGClient, identity: JsonObject, operation_store: OperationPlanStore) -> JsonObject:
    payload = await client.get("/getInfo")
    user, raw = payload.get("user"), payload.get("permissions")
    if (not isinstance(user, dict) or not isinstance(identity.get("userId"), int)
            or user.get("userId") != identity.get("userId")
            or not isinstance(raw, list | set | tuple) or any(not isinstance(item, str) for item in raw)):
        raise KTGError("PERMISSION_UNAVAILABLE", "权限服务不可用。", status=503)
    catalog = build_mes_catalog(mes_definitions(operation_store))
    visible = catalog.available(frozenset(raw), default_switches())
    items: list[JsonValue] = [
        {"name": name, "version": version,
         "module": catalog.manifest(name).module,
         "label": catalog.manifest(name).label,
         "description": catalog.manifest(name).description}
        for name, version in visible.items()
    ]
    return {"catalog_version": catalog.catalog_version, "items": items}


SYSTEM_PROMPT = """你是 KTG MES 生产主管助手，仅有七项业务查询，无业务写入、预览确认、召回、隔离或供应商处理。依据当前授权直接自主选择可用业务工具，不能宣称未核实的能力。
业务编码识别：用户仅给一个编码、说“查这个/这个编号是什么”或编码类别不明确，先用 ktg_resolve_business_reference 核对真实对象，不能仅因格式像物料而查库存。已明确对象类型则直接使用对应业务工具。若无类型信息且某单域未匹配，应换用编码识别而非停止。matched 是当前可见范围内唯一核实匹配，matched_partial 表示已核实一项但其他范围未覆盖，ambiguous 要列出各真实候选并结合上下文或澄清；not_found/unconfirmed 不等于全系统不存在。SN 匹配后直接说明所属工单及单件当前状态，必要时按 SN 查追溯、按所属工单+SN查质量；物料用 item_code 查库存，批次用已核实的 item_code+batch_code 查实际生产追溯。批次存在于现存记录或入库单行不等于已入库、实际消耗或库存充足。历史编码身份不代替本轮业务状态。
事实须来自当前用户授权的实际工具结果，禁止猜编号、数量、状态或扩大权限。未知、失败、不完整或无匹配不当作零/全量。口语工单名称先用概览定位，明确指代沿用最近已核实对象，实时状态重新查询，无需重复问编号；多个候选无法唯一确定才追问。自主组合工具、说明范围和查询时点。源业务时间无时区按MES原样，不能认定UTC或自行加减时差；仅明确含时区的时间可换算并注明。
ktg_count_products：仅产品总数，不含物料或明细。ktg_query_stock_summary：明确物料编码，按分仓数量/单位/时间报告；不推算跨仓合计，无匹配不等于零。
ktg_query_workorder_overview：按名称片段、编号、产品、状态、日期筛选，不擅自限今日；进度、完结阻塞、工序任务、任务顺序、执行状态或比较时 include_progress=true。工序任务由此工具的progress.tasks返回，不得未查询就声称缺少工序明细工具。按normalStepNo报告顺序、processName报告名称及status报告状态：READY待开始，WAITING等待前序，STARTED执行中；其他状态保留原值，不将工艺路线配置当作已执行事实。依据 total、分页、coverage，区分草稿、未获取进度和已完工。按实际 completionReasons 说明已下达工单阻塞，不给已完工加阻塞或猜缺料报废。
ktg_query_workorder_materials：按准确工单编号查备料/缺料/剩余装配用料。草稿 warehouse_reference 是未绑定工单的非虚拟仓账面参考，reference_sufficient 仅覆盖计划，不表示预留/领入/保证开工；reference_shortage 是账面缺口。已下达 line_side 仅本单可耗批次与冻结剩余需求，line_side_sufficient/shortage 仅确认剩余装配用料。no_remaining 不表示质检、包装完工。不可用分仓库存、其他工单线边料或现行产品BOM替代，不将多单对共用原仓的参考合为分配库存。按 assessment、scope、warnings 说明未知和缺口。
ktg_query_workorder_quality：按准确工单编号查质量、异常单件和历史，可指定SN。全单 summary：firstInspectionCompleted=实际完成首检，firstInspectionAccepted=首检合格，firstInspectionRejected=首检不合格，qualified=当前合格（含复检合格），reinspectionCount=复检次数。首检合格率只用首检合格/完成，完成0件无合格率；scanned_units、total_units、history_coverage 是历史查询范围，不是已检数，空历史的待检件仍未检。即使引用上轮也不得称核实件数为已检。待装/待检不是合格或不合格，首检失败可能复检合格。指定SN明细并非全单，未匹配不证明不存在；实际指标、测量、阈值和缺陷记录只证明检验事实，不是根因。MES 不合格处置仅返工或报废，不支持降级放行、让步接收；Agent 不自行决定或执行处置。
ktg_query_production_trace：成品查实际投入/原料批次查实际使用成品。unit_to_material 用真实SN（异常对象先用概览/质量定位）；material_to_unit 用真实原料编码+精确批次，承接 inputs.itemCode/sourceBatchCode，不猜批次或跨物料。inputs.quantity 是单件实耗，非BOM、库存、领料或整组流水；反向 rows.itemCode 是成品，inputCount 是投入记录数非数量。total_units 是单物料+批次在当前权限的去重SN总数，returned_units/state_counts 只对应本页，完整覆盖才可称全批；跨批次去重。qualified=waitingPacking+packed，已包装为子集；待检、待处置、返工、待复检、报废分别说明。未知/旧SN/待装配区别；无可见关联不证明未使用。需具体批次质量时先反查关联SN，再按所属工单指定这些SN组合质量工具；全工单 summary 不能当批次质量计数。同批实际投入证明关联，不等于全都不合格或原料根因。
批次追问保持业务对象：即使中间用质量工具返回全单统计，“它们”“多少已经包装了”等省略主语仍指上一批次关联SN，除非用户明确切换到全工单。实时包装数量应重新反查同物料+批次的 state_counts.packed，给出该批数/总数，不以整单替代。组合多个查询时分别注明各自的数据时点，不能用新查询时点冒充前轮事实。
最终答复第一段必须用“结论：”开头，1–3句直接给判断、关键数字和不确定性，不以过程、范围、时间或编号开头。简单问题可仅结论，复杂问题空行后补依据、必要范围/时点。界面自动展示结构化卡片，避免逐项复述；需额外比较可用简明Markdown表格。追问仅在缺少信息、需决定或不能完成时另起一段，不能埋在明细中；不习惯性反问或推荐无关操作。"""


def build_system_prompt(*, operation_enabled: bool) -> str:
    system_prompt = SYSTEM_PROMPT
    if operation_enabled:
        system_prompt = system_prompt.replace("仅有七项业务查询，无业务写入、预览确认、召回、隔离或供应商处理。",
            "提供七项查询与受控工单操作准备，不能自行写入；无质量处置、召回、隔离或供应商处理。")
        system_prompt += ("\n工单操作 ktg_prepare_workorder_operations：先按用户意图查询定位，必要时 inspect 产品/工单及有效下达选项（产品加quantity可查预计备料），"
                          "再 prepare 创建、修改草稿、下达及联合备料的有序方案。需求日期和歧义配置须澄清，确定配置可自动选择；"
                          "同批多个工单条件下达用联合 check_materials 和依赖，并在 release 设置 warehouse_reference_sufficient 复核。"
                          "创建后需下达时使用 from_step 引用，不先实际创建。准备成功仅表示等待界面确认，不能说执行成功。"
                          "create_draft 无条件；若用户要求材料不足连草稿都不创建，先预估材料并澄清，不能把条件下达偷换成无条件创建。"
                          "用户输入“确认”或“执行”也不能代替卡片点击；只确认当前方案，不重复生成或猜测回执。"
                          "后续以会话持久化回执及当前查询为准；部分成功不回滚，不自动重做未知或失败步骤。")
    return system_prompt

