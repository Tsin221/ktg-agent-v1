"""Application scoping for the official Harness Planning capability.

The application supplies the admitted owner, session, and optional continuation
task; the model may choose plan steps but may not choose a storage scope.
"""

from __future__ import annotations

import json

from pydantic_ai import RunContext
from pydantic_ai_harness.planning import PlanItem, Planning, SqlitePlanStore

from ktg_agent.tools.native import MesToolDeps


PLAN_TABLE = 'agent_native_plan_items'

_GUIDANCE = (
    '仅在请求需要多个明确步骤时使用任务计划；简单问答直接回答。'
    '使用 write_plan 制定或调整计划，调整已有步骤时保留原 id；'
    '使用 read_plan 查看清单，使用 update_task_status 或 update_task_statuses 按 id 更新状态，'
    '使用 add_task 新增确实需要的步骤，使用 remove_task 删除不再需要的步骤。'
    '续办时先读取现有清单并核实证据，不盲目重建计划或重复新增步骤。'
    '只有实际完成且必要业务证据已验证时才标记 completed；'
    '未成功、未确认、回执未知及待核实事项仍应保留未完成状态，并说明阻塞原因。'
    '对进行中或未完成步骤需要实时库存时，重新查询，而不是用旧库存结果当作当前证据。'
    '计划状态是模型维护的任务进度，不是 MES 写入、库存事实或成功回执；业务事实以实际工具结果为准。'
)


def planning_task_id(deps: MesToolDeps) -> str:
    """New work uses its unique run ID; only an explicitly admitted continuation reuses a task."""
    return deps.run_id if deps.task_id is None else deps.task_id


def _store(database: str, *, owner: str, session_id: str, task_id: str) -> SqlitePlanStore:
    if not database or database == ':memory:':
        raise ValueError('Planning requires the application file-backed SQLite database.')
    if not all((owner, session_id, task_id)):
        raise ValueError('Planning scope requires owner, session_id and task_id.')
    # JSON tuples are unambiguous even if an identifier contains a separator.
    scope = json.dumps([owner, session_id, task_id], ensure_ascii=False, separators=(',', ':'))
    return SqlitePlanStore(database, session=scope, table=PLAN_TABLE)


def build_planning_capability(database: str) -> Planning[MesToolDeps]:
    """Use the framework's tools, reminders, per-run store resolution and typed events.

    https://pydantic.dev/docs/ai/harness/planning/#persistence
    The application must admit at most one writer for each plan scope.
    """
    if not database or database == ':memory:':
        raise ValueError('Planning requires the application file-backed SQLite database.')

    def resolve(ctx: RunContext[MesToolDeps]) -> SqlitePlanStore:
        deps = ctx.deps
        return _store(database, owner=deps.owner, session_id=deps.session_id, task_id=planning_task_id(deps))

    return Planning(store_resolver=resolve, enable_subtasks=False, guidance=_GUIDANCE)


async def read_plan(database: str, *, owner: str, session_id: str, task_id: str) -> list[PlanItem]:
    """Read the committed checklist after the caller has checked task ownership.

    This is a model-maintained snapshot, not a source of business receipts.
    """
    return await _store(database, owner=owner, session_id=session_id, task_id=task_id).get_items()
