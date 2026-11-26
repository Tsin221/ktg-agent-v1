# Agent 核心层框架接入契约

版本：V1.0；2026-11-26。依据 C-01 至 C-09，已真实安装并锁定框架；以下签名和行为取自官方文档、已安装源码、五项通过的原生行为探针及各独立实施报告。详细证据见 [CORE-01 实施报告](实施报告/CORE-01-框架验证与接入契约-报告.md)。

## 依赖与组合

- Python 3.12、`pydantic-ai-slim[openai]==2.75.0`、`pydantic-ai-harness==0.75.0`。不要安装全量 provider 或 durable execution 引擎；候选 `pyproject.toml` 和 `uv.lock` 为唯一版本依据。
- `Agent[MesToolDeps, str]` 由 CORE-05 负责组合。通过 `toolsets=[...]`、`capabilities=[...]` 装配独立模块，`deps=`、`message_history=`、`usage_limits=`、`model_settings=` 等为每 Run 输入。不创建第二套框架执行循环。
- 真实模型用 `OpenAIChatModel` 和 `DeepSeekProvider`；模型 ID、base URL、API key 由既有配置传入。环境未发现模型配置，DeepSeek thinking 字段、真实多轮和工具协议尚待联调；不要在测试配置中读取或打印密钥。

## 模块交接

| 任务 | 接入模块与签名 | 边界 |
| --- | --- | --- |
| CORE-02 | `ktg_agent.tools.native.MesToolDeps`（`run_id: str`, `session_id: str`, `owner: str`, `identity: JsonObject`, `client: KTGClient`, `task_id: str \| None = None`）；`build_mes_toolset(operation_store: OperationPlanStore, *, switches_provider: Callable[[], CatalogSwitches] \| None = None) -> AbstractToolset[MesToolDeps]` | 唯一共享运行依赖类型；只允许授权业务工具。`ToolReturn.metadata` 提供 `status,error_code,details,operation_id,verification`，不能直接发布任意 metadata，需选取可公开字段。 `from_schema` 须保留实际参数验证，`filtered` 每次决定候选工具，执行时再验证。 |
| CORE-03 | `ktg_agent.context.native_context.build_context_capabilities(archive, *, max_fraction=0.8, keep_tokens=20_000, context_window=None, summary_model=None)`；`serialize_messages(messages: Sequence[ModelMessage]) -> bytes`、`deserialize_messages(raw_json: bytes) -> list[ModelMessage]` | `archive` 为 async callback `(ctx: RunContext, raw_json: bytes) -> None`；返回有序 `(Hooks, FallbackCompaction)`，组合时保持顺序。压缩前归档原生历史，保存幂等/原始记录归属由持久化层负责。 |
| CORE-04 | `ktg_agent.sessions.native_planning.build_planning_capability(database: str) -> Planning[MesToolDeps]`；`planning_task_id(deps: MesToolDeps) -> str`；`read_plan(database: str, *, owner: str, session_id: str, task_id: str) -> Awaitable[list[PlanItem]]` | 同一个统一 SQLite 路径，使用 `agent_native_plan_items`；不使用 `:memory:`；`enable_subtasks=False`，使用稳定步骤 ID。原生计划创建、更新、删除事件用于刷新，最终读取最新快照。`SqlitePlanStore` 读改写不是跨写入原子事务，同一范围维持一活动 Run / 写入方。模型完成标记不代表业务成功；旧库存步骤证据关联尚待跨层适配。 |
| CORE-05 | `ktg_agent.core.native_execution.NativeExecution`（`run() -> NativeOutcome`、`stop()`、`input(request_id, mode, message) -> bool`）；`NativeOutcome` 含 `status/reason/summary/messages/new_messages` | 原生 `Agent.iter` 掌握执行；输入返回值是接受状态，`delivered` 回调确认实际交付。HTTP RunRuntime 拥有 asyncio task/终态。 |
| CORE-06 | `ktg_agent.core.native_core.build_deepseek_agent(settings, operation_store, *, instructions, switches_provider=None)`；`NativeCore(agent, history, *, deps_factory, prior_run, mark_consumed, publish, usage_limits=None, timeout=90)`；`ktg_agent.core.native_persistence.NativeStepHistory` | 通过 `NativeCore.prepare` 接入候选 HTTP；官方 StepPersistence/SqliteStepStore、原始消息归档及续办。旧应用 SQLite 表存在冲突，原生终态事务需实现 `NativeSettlementRepository.settle_native_run`，当前以同一隔离测试数据库验证。 |

## 原生运行与事件

- `async with agent.iter(...) as run`，再 `async for node in run`；可对于 `Agent.is_model_request_node(node)` 与 `Agent.is_call_tools_node(node)` 分别使用 `async with node.stream(run.ctx) as stream` 并遍历事件。推荐接入实际 `Hooks(event=callback)`，其中回调为 `async def callback(ctx: RunContext[Any], event: AgentStreamEvent)`、位置参数；原生图自动开流及推送事件（此时无需另行遍历每个 node.stream 发布，避免重复）。本地源码 `_deliver_leftover_events` 显示 deferred pause 末尾事件仅在有事件能力或 stream handler 时派发，故使用 Hook 作为唯一事件源更稳妥。`run.result` 在终止节点后读取；`run.all_messages()` / `run.new_messages()`、`run.usage` 用于原生消息和基础用量。
- 文字增量使用 `PartStartEvent` / `PartDeltaEvent` 中可公开的 `TextPart` / `TextPartDelta`，不输出思考内容。工具事件 `FunctionToolCallEvent`、`FunctionToolResultEvent` 用 `tool_call_id` 关联，`args_valid=False/None` 或 retry 事件不能宣称实际执行成功；只选取允许显示的工具信息。原生扩展事件（如 `Plan*Event`）也通过框架事件链路。HTTP SSE 负责序号、缓存、订阅和传输。
- `run.enqueue(user_text, priority='asap' | 'when_idle') -> str | None`；仅接受普通用户文本。非空 enqueue_id 表示入队成功，不表示已消费；`EnqueuedMessagesEvent.enqueue_id` 对应交付事实，`messages` 是进入历史的对象或工具返回同请求中附带的视图；交付并不证明模型已读取/遵从。Steer 为下一次模型决策机会到达前的优先排队，不是即时重写当前请求；Follow-up 在原运行即将结束时交付。处理暂停/取消后未交付输入时如实记录。入队后的早期终止不能自动重放。HTTP request_id 与原生 enqueue_id 分别归属应用和框架，建立一次对应映射。
- `run.cancel()` 可由外部 task 安全调用；在持续推进并退出 `iter` 上下文时，实际验证抛出 `RunCancelled`，已完成历史通过异常的 `all_messages()` 取回。不要主动 `break` / 提早离开迭代并假定捕获到异常：这可能只是 teardown。外部 task cancellation 保持 `asyncio.CancelledError`。对未知外部操作不推断撤销或成功。用于 `UsageLimits(request_limit=..., tool_calls_limit=...)` 的限制为框架定义轮次/成功工具调用数，不等价于供应商 HTTP 尝试/业务写入成功。原生 timeout、有限重试与使用量统计按 C-07 处理。
- 终态由 HTTP 在保存最终结果与状态后发布，不能以 `PartEndEvent` 或收到模型文字替代业务终态。模型、工具调用、执行异常均须保留真实 Outcome。

## 原始消息与续办

统一使用 `ModelMessage` / `ModelMessagesTypeAdapter`，原生消息 JSON 存储，不维持旧历史的平行权威协议。原始消息在任何 Compaction 前归档；压缩后使用派生上下文继续模型执行。框架的 StepPersistence 保存已结算边界，不是业务写入恰好一次方案。用户主动续办新 Run、新预算，读取历史、原生计划和必要业务回执；重启不自动续跑。StepStore 默认表 `runs` 等与候选旧存储存在冲突，CORE-06 和持久化负责人协调统一表名/结构。

## 测试约定及来源

使用实际安装框架的 `TestModel` / `FunctionModel`、临时 SQLite 与隔离业务替身。不得用编写的框架仿件冒充实测。存储和 API 带来的深层异常需真实记录；测试不访问正式 MES 或业务写入。

官方：[Agent](https://pydantic.dev/docs/ai/core-concepts/agent/)、[消息历史](https://pydantic.dev/docs/ai/core-concepts/message-history/)、[工具高级特性](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/)、[DeepSeek](https://pydantic.dev/docs/ai/models/deepseek/)、[Planning](https://pydantic.dev/docs/ai/harness/planning/)、[Compaction](https://pydantic.dev/docs/ai/harness/compaction/)、[Step Persistence](https://pydantic.dev/docs/ai/harness/step-persistence/)。
