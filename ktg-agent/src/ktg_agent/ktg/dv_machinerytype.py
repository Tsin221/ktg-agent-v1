"""Read-only MES equipment classification (device type) tools.

The MES list endpoint returns a flat array (no rows/total/pagination), so this
module builds the tree, paths and totals locally and caps the returned rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf
from ktg_agent.tools.registry import (
    ExecutionMode,
    RetryPolicy,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolStatus,
)


TREE_NAME = "ktg_dv_machinerytype_tree"
DETAIL_NAME = "ktg_dv_machinerytype_detail"

_LIST_PATH = "/mes/dv/machinerytype/list"
_MACHINERY_PATH = "/mes/dv/machinery/list"
_REPAIR_PATH = "/mes/dv/repair/list"

_TREE_DESCRIPTION = (
    "查询 MES 设备分类（设备类型）树。可按类型名称模糊、类型编码、是否启用、父分类筛选，"
    "返回带完整路径与层级的扁平节点。适用于“设备分类有哪些/某分类下面有什么/某分类的完整路径/"
    "按启用状态统计”；不适用于“列出某分类下的设备”（请用 ktg_dv_machinerytype_detail）。"
    "不传筛选条件时返回全部分类，等同页面“重置”；total 为过滤后总数，truncated=true 表示还有更多。"
)
_DETAIL_DESCRIPTION = (
    "查询单个设备分类的详情与影响面：完整路径、父分类、直接子分类、子树内设备数量与样例、"
    "维修单引用数量，以及是否允许改名或停用。适用于“这个分类属于哪个大类/下面有多少设备/"
    "改名或停用有影响吗/为什么不能删除”。必须提供 ID、编码、名称三者之一；名称可能重名，"
    "重名时会返回候选，请让用户按 ID 或路径确认，不要自行猜测。"
)


def _tree_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "machinery_type_name": {
                "type": "string", "minLength": 1, "maxLength": 100,
                "description": "类型名称模糊匹配，等同页面“类型名称”输入框；不传=不按名称过滤",
            },
            "machinery_type_code": {
                "type": "string", "minLength": 1, "maxLength": 64,
                "description": "类型编码精确或前缀匹配；不传=不按编码过滤",
            },
            "enable_flag": {
                "type": "string", "enum": ["Y", "N"],
                "description": "Y=只看启用，N=只看停用；不传=全部（页面默认/重置行为）",
            },
            "parent_type_id": {
                "type": "integer", "minimum": 0,
                "description": "只看该父分类的直接子分类；0=根分类；不传=全树",
            },
            "max_depth": {
                "type": "integer", "minimum": 1, "maximum": 5, "default": 5,
                "description": "返回的最大层级深度",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 200, "default": 100,
                "description": "最多返回节点数",
            },
        },
        "required": [],
        "additionalProperties": False,
    }


def _detail_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "machinery_type_id": {
                "type": "integer", "minimum": 1,
                "description": "设备类型 ID（最精确，优先使用）",
            },
            "machinery_type_code": {
                "type": "string", "minLength": 1, "maxLength": 64,
                "description": "设备类型编码，如 M_TYPE_025",
            },
            "machinery_type_name": {
                "type": "string", "minLength": 1, "maxLength": 100,
                "description": "设备类型名称；允许模糊，但可能命中多个",
            },
        },
        "anyOf": [
            {"required": ["machinery_type_id"]},
            {"required": ["machinery_type_code"]},
            {"required": ["machinery_type_name"]},
        ],
        "additionalProperties": False,
    }


def _output_schema(fields: tuple[str, ...]) -> JsonObject:
    return {
        "type": "object",
        "properties": {key: {} for key in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


def _tree_definition() -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        client = _client(context)
        if client is None:
            return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
        try:
            details = await read_tree(client, arguments)
        except KTGError as exc:
            return _failure(str(exc), exc.code)
        rows = cast(list[JsonObject], details["rows"])
        summary = (
            f"按当前条件共 {details['total']} 个设备分类，本次返回 {details['returned']} 个"
            + ("（已截断）。" if details["truncated"] else "。")
        )
        lines = [summary, "筛选：" + _filter_text(cast(JsonObject, details["filters"]))]
        for row in rows[:20]:
            lines.append(
                f"- [{row['machineryTypeId']}] {row['path']}"
                f"（{row['machineryTypeCode']}，{'启用' if row['enableFlag'] == 'Y' else '停用'}）"
            )
        if details["truncated"]:
            lines.append(f"其余 {cast(int, details['total']) - len(rows)} 个未返回，请按名称/父分类缩小范围。")
        return ToolResult(content="\n".join(lines)[:4000], details=details)

    return ToolDefinition(
        name=TREE_NAME, version="1.0", label="查询设备分类树",
        description=_TREE_DESCRIPTION,
        input_schema=_tree_schema(),
        output_schema=_output_schema(("kind", "rows", "total", "returned", "truncated", "filters", "queried_at")),
        required_permissions=("mes:dv:machinerytype:list",),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=execute,
    )


def _detail_definition() -> ToolDefinition:
    async def execute(context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
        client = _client(context)
        if client is None:
            return _failure("KTG 查询客户端未配置。", "KTG_CLIENT_MISSING")
        try:
            details = await read_detail(client, arguments)
        except KTGError as exc:
            return _failure(str(exc), exc.code)
        node = cast(JsonObject, details["node"])
        children = cast(list[JsonObject], details["children"])
        device = cast(JsonObject, details["device"])
        sample = cast(list[JsonObject], device["sample"])
        repair_total = details["repairTotal"]
        repair_text = "未核验" if repair_total is None else f"{repair_total} 张"
        child_text = "；".join(str(item["machineryTypeName"]) for item in children[:10]) or "无"
        sample_text = "、".join(
            f"{item['machineryCode']} {item['machineryName']}" for item in sample[:5]
        ) or "无"
        content = (
            f"「{node['machineryTypeName']}」（{node['machineryTypeCode']}，ID {node['machineryTypeId']}）"
            f"路径：{node['path']}；父分类：{node['parentName'] or '无'}。"
            f"直接子分类 {len(children)} 个：{child_text}。"
            f"子树内设备 {device['subtreeTotal']} 台（样例：{sample_text}）。"
            f"维修单引用：{repair_text}。"
            "删除：已停用；父类型：不可修改；同级重名：保存时校验。"
        )
        return ToolResult(content=content[:4000], details=details)

    return ToolDefinition(
        name=DETAIL_NAME, version="1.0", label="查询设备分类详情与影响",
        description=_DETAIL_DESCRIPTION,
        input_schema=_detail_schema(),
        output_schema=_output_schema((
            "kind", "node", "children", "childrenTruncated", "device", "repairTotal",
            "repairStatus", "deleteBlocked", "updatePolicy", "checkPlanNote", "queried_at",
        )),
        required_permissions=("mes:dv:machinerytype:query", "mes:dv:machinery:list"),
        effect=ToolEffect.READ, execution_mode=ExecutionMode.PARALLEL,
        retry_policy=RetryPolicy.BOUNDED_READ, timeout_seconds=20, execute=execute,
    )


def build_dv_machinerytype_tools() -> tuple[ToolDefinition, ...]:
    return (_tree_definition(), _detail_definition())


def _factory(definition: ToolDefinition) -> ToolFactory:
    def factory() -> ToolDefinition:
        return definition

    return factory


def dv_machinerytype_factories() -> dict[str, ToolFactory]:
    return {
        f"dv:{definition.name}": _factory(definition)
        for definition in build_dv_machinerytype_tools()
    }


def dv_machinerytype_manifests() -> tuple[ToolManifest, ...]:
    tree = _tree_definition()
    detail = _detail_definition()
    return (
        ToolManifest(
            name=tree.name, version=tree.version, module="dv", domain="machinery_type",
            label=tree.label, description=tree.description,
            permission_policy=PermissionLeaf("mes:dv:machinerytype:list"),
            effect=tree.effect, execution_mode=tree.execution_mode, retry_policy=tree.retry_policy,
            timeout_seconds=tree.timeout_seconds, expose_to_model=True,
            confirmation_policy="none", proposal_store_effect=False, handler_ref=f"dv:{tree.name}",
        ),
        ToolManifest(
            name=detail.name, version=detail.version, module="dv", domain="machinery_type",
            label=detail.label, description=detail.description,
            permission_policy=PermissionAll((
                PermissionLeaf("mes:dv:machinerytype:query"),
                PermissionLeaf("mes:dv:machinery:list"),
            )),
            effect=detail.effect, execution_mode=detail.execution_mode, retry_policy=detail.retry_policy,
            timeout_seconds=detail.timeout_seconds, expose_to_model=True,
            confirmation_policy="none", proposal_store_effect=False, handler_ref=f"dv:{detail.name}",
        ),
    )


async def read_tree(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    """Read the flat MES type list and return filtered, path-enriched rows."""
    allowed = {"machinery_type_name", "machinery_type_code", "enable_flag",
               "parent_type_id", "max_depth", "limit"}
    if any(key not in allowed for key in arguments):
        raise KTGError("VALIDATION_ERROR", "包含未支持的筛选字段。")
    name = _optional_text(arguments, "machinery_type_name", 100)
    code = _optional_text(arguments, "machinery_type_code", 64)
    enable_flag = arguments.get("enable_flag")
    if enable_flag is not None and enable_flag not in {"Y", "N"}:
        raise KTGError("VALIDATION_ERROR", "enable_flag 只能是 Y 或 N。")
    parent_type_id = arguments.get("parent_type_id")
    if parent_type_id is not None and (
        not isinstance(parent_type_id, int) or isinstance(parent_type_id, bool) or parent_type_id < 0
    ):
        raise KTGError("VALIDATION_ERROR", "parent_type_id 必须是非负整数。")
    max_depth = arguments.get("max_depth", 5)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= 5:
        raise KTGError("VALIDATION_ERROR", "max_depth 必须在 1-5 之间。")
    limit = arguments.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise KTGError("VALIDATION_ERROR", "limit 必须在 1-200 之间。")

    nodes = await _load_nodes(client)
    by_id = {cast(int, node["machineryTypeId"]): node for node in nodes}
    child_counts: dict[int, int] = {}
    for node in nodes:
        parent = cast(int, node["parentTypeId"])
        if parent in by_id:
            child_counts[parent] = child_counts.get(parent, 0) + 1

    filtered: list[JsonObject] = []
    for node in nodes:
        path, depth = _path_of(node, by_id)
        if name is not None and name.casefold() not in str(node["machineryTypeName"]).casefold():
            continue
        if code is not None and not str(node["machineryTypeCode"]).casefold().startswith(code.casefold()):
            continue
        if enable_flag is not None and node["enableFlag"] != enable_flag:
            continue
        if parent_type_id is not None and node["parentTypeId"] != parent_type_id:
            continue
        if depth > max_depth:
            continue
        filtered.append({
            **node, "path": path, "depth": depth,
            "childCount": child_counts.get(cast(int, node["machineryTypeId"]), 0),
        })
    filtered.sort(key=lambda item: (cast(int, item["depth"]), cast(int, item["machineryTypeId"])))
    total = len(filtered)
    rows = filtered[:limit]
    filters: JsonObject = {}
    for key, value in (("machinery_type_name", name), ("machinery_type_code", code),
                       ("enable_flag", enable_flag), ("parent_type_id", parent_type_id),
                       ("max_depth", max_depth)):
        if value is not None and value != 5:
            filters[key] = cast(JsonValue, value)
    return {
        "kind": "dv_machinerytype_tree",
        "rows": cast(list[JsonValue], rows),
        "total": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "filters": filters,
        "queried_at": datetime.now(UTC).isoformat(),
    }


async def read_detail(client: KTGClient, arguments: Mapping[str, object]) -> JsonObject:
    """Resolve one type by id/code/name and enrich it with equipment/repair impact."""
    allowed = {"machinery_type_id", "machinery_type_code", "machinery_type_name"}
    if any(key not in allowed for key in arguments):
        raise KTGError("VALIDATION_ERROR", "包含未支持的字段。")
    type_id = arguments.get("machinery_type_id")
    code = _optional_text(arguments, "machinery_type_code", 64)
    name = _optional_text(arguments, "machinery_type_name", 100)
    if type_id is None and code is None and name is None:
        raise KTGError("VALIDATION_ERROR", "必须提供 ID、编码或名称之一。")

    nodes = await _load_nodes(client)
    by_id = {cast(int, node["machineryTypeId"]): node for node in nodes}
    target = _select_node(nodes, type_id, code, name)
    target_id = cast(int, target["machineryTypeId"])
    path, depth = _path_of(target, by_id)
    parent_id = cast(int, target["parentTypeId"])
    parent = by_id.get(parent_id)
    children = []
    for node in nodes:
        if node["parentTypeId"] != target_id:
            continue
        child_path, _ = _path_of(node, by_id)
        children.append({
            "machineryTypeId": node["machineryTypeId"],
            "machineryTypeCode": node["machineryTypeCode"],
            "machineryTypeName": node["machineryTypeName"],
            "enableFlag": node["enableFlag"],
            "path": child_path,
        })
    children.sort(key=lambda item: cast(int, item["machineryTypeId"]))

    device_total, device_sample = await _read_devices(client, target_id)
    repair_total, repair_status = await _read_repair_total(client, target_id)

    return {
        "kind": "dv_machinerytype_detail",
        "node": {
            **target,
            "path": path,
            "depth": depth,
            "parentName": parent["machineryTypeName"] if parent is not None else None,
        },
        "children": cast(list[JsonValue], children[:50]),
        "childrenTruncated": len(children) > 50,
        "device": {
            "subtreeTotal": device_total,
            "sample": cast(list[JsonValue], device_sample),
        },
        "repairTotal": repair_total,
        "repairStatus": repair_status,
        "deleteBlocked": {"allowed": False, "reason": "删除功能已停用"},
        "updatePolicy": {"parentChangeAllowed": False, "duplicateNameChecked": True},
        "checkPlanNote": "点检计划经设备间接关联，本次未统计",
        "queried_at": datetime.now(UTC).isoformat(),
    }


async def _load_nodes(client: KTGClient) -> list[JsonObject]:
    payload = await client.get(_LIST_PATH)
    data = payload.get("data")
    if not isinstance(data, list):
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类列表格式无效。")
    nodes: list[JsonObject] = []
    seen: set[int] = set()
    for item in data:
        node = _project_node(item)
        type_id = cast(int, node["machineryTypeId"])
        if type_id in seen:
            raise KTGError("KTG_INVALID_RESPONSE", "设备分类列表存在重复 ID。")
        seen.add(type_id)
        nodes.append(node)
    return nodes


def _project_node(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类数据不是对象。")
    type_id = value.get("machineryTypeId")
    if not isinstance(type_id, int) or isinstance(type_id, bool) or type_id <= 0:
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类 ID 无效。")
    parent_id = value.get("parentTypeId")
    if parent_id is None:
        parent_id = 0
    if not isinstance(parent_id, int) or isinstance(parent_id, bool) or parent_id < 0:
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类父 ID 无效。")
    code = value.get("machineryTypeCode")
    name = value.get("machineryTypeName")
    enable_flag = value.get("enableFlag")
    ancestors = value.get("ancestors")
    remark = value.get("remark")
    if not isinstance(code, str) or not code.strip():
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类编码无效。")
    if not isinstance(name, str) or not name.strip():
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类名称无效。")
    if enable_flag not in {"Y", "N"}:
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类启用状态无效。")
    if ancestors is not None and not isinstance(ancestors, str):
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类祖先链无效。")
    if remark is not None and not isinstance(remark, str):
        raise KTGError("KTG_INVALID_RESPONSE", "设备分类备注无效。")
    return {
        "machineryTypeId": type_id,
        "machineryTypeCode": code,
        "machineryTypeName": name,
        "parentTypeId": parent_id,
        "ancestors": ancestors or "",
        "enableFlag": enable_flag,
        "remark": remark or "",
    }


async def load_machinery_types(client: KTGClient) -> list[JsonObject]:
    """Public read used by the preview/write tools as well."""
    return await _load_nodes(client)


def path_of(node: JsonObject, by_id: Mapping[int, JsonObject]) -> tuple[str, int]:
    """Public path helper shared with the write tools."""
    return _path_of(node, by_id)


def _path_of(node: JsonObject, by_id: Mapping[int, JsonObject]) -> tuple[str, int]:
    names: list[str] = []
    current = node
    seen: set[int] = set()
    while True:
        current_id = cast(int, current["machineryTypeId"])
        if current_id in seen:
            break
        seen.add(current_id)
        names.append(str(current["machineryTypeName"]))
        parent_id = cast(int, current["parentTypeId"])
        if parent_id == 0 or parent_id not in by_id:
            break
        current = by_id[parent_id]
        if len(names) > 20:
            break
    names.reverse()
    return "/".join(names), len(names)


def _select_node(
    nodes: list[JsonObject], type_id: object, code: str | None, name: str | None
) -> JsonObject:
    if type_id is not None:
        if not isinstance(type_id, int) or isinstance(type_id, bool) or type_id <= 0:
            raise KTGError("VALIDATION_ERROR", "machinery_type_id 必须是正整数。")
        for node in nodes:
            if node["machineryTypeId"] == type_id:
                return node
        raise KTGError("TYPE_NOT_FOUND", f"未找到 ID 为 {type_id} 的设备分类，可先用 tree 按名称搜索。")
    if code is not None:
        matches = [node for node in nodes if str(node["machineryTypeCode"]).casefold() == code.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KTGError("AMBIGUOUS_MATCH", "该编码对应多个设备分类：" + _candidates_text(matches))
        raise KTGError("TYPE_NOT_FOUND", f"未找到编码为 {code} 的设备分类。")
    assert name is not None
    exact = [node for node in nodes if str(node["machineryTypeName"]).casefold() == name.casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise KTGError("AMBIGUOUS_MATCH", "存在多个同名设备分类：" + _candidates_text(exact))
    fuzzy = [node for node in nodes if name.casefold() in str(node["machineryTypeName"]).casefold()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise KTGError("AMBIGUOUS_MATCH", "名称命中多个设备分类：" + _candidates_text(fuzzy))
    raise KTGError("TYPE_NOT_FOUND", f"未找到名称包含“{name}”的设备分类。")


def _candidates_text(nodes: list[JsonObject]) -> str:
    return "；".join(
        f"[{node['machineryTypeId']}] {node['machineryTypeName']}（{node['machineryTypeCode']}）"
        for node in nodes[:10]
    )


async def _read_devices(client: KTGClient, type_id: int) -> tuple[int, list[JsonObject]]:
    payload = await client.get(_MACHINERY_PATH, {"machineryTypeId": type_id, "pageNum": 1, "pageSize": 5})
    total = payload.get("total")
    rows = payload.get("rows")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or not isinstance(rows, list):
        raise KTGError("KTG_INVALID_RESPONSE", "设备列表分页结果格式无效。")
    sample: list[JsonObject] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise KTGError("KTG_INVALID_RESPONSE", "设备数据不是对象。")
        sample.append({
            "machineryCode": row.get("machineryCode"),
            "machineryName": row.get("machineryName"),
            "status": row.get("status"),
        })
    return total, sample


async def _read_repair_total(client: KTGClient, type_id: int) -> tuple[int | None, str | None]:
    try:
        payload = await client.get(_REPAIR_PATH, {"machineryTypeId": type_id, "pageNum": 1, "pageSize": 1})
    except KTGError as exc:
        return None, exc.code
    total = payload.get("total")
    rows = payload.get("rows")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or not isinstance(rows, list):
        return None, "KTG_INVALID_RESPONSE"
    return total, None


def _optional_text(arguments: Mapping[str, object], key: str, max_length: int) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= max_length or not value.strip():
        raise KTGError("VALIDATION_ERROR", f"{key} 长度必须为 1-{max_length}。")
    return value.strip()


def _filter_text(filters: JsonObject) -> str:
    if not filters:
        return "无（等同页面重置，返回全部分类）"
    labels = {
        "machinery_type_name": "名称", "machinery_type_code": "编码",
        "enable_flag": "启用状态", "parent_type_id": "父分类", "max_depth": "最大深度",
    }
    return "；".join(f"{labels.get(key, key)}={value}" for key, value in filters.items())


def _client(context: ToolContext) -> KTGClient | None:
    return context.ktg_client if isinstance(context.ktg_client, KTGClient) else None


def _failure(message: str, code: str) -> ToolResult:
    return ToolResult(content=message, status=ToolStatus.FAILED, error_code=code)
