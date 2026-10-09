# 阶段 A 核心契约与阶段 B 交接

本文描述当前代码已经实现并由测试约束的公开接口。阶段 A 核心之外，仓库还提供了 OpenAI 兼容 Provider、无状态 HTTP 查询入口，以及按当前 KTG 用户身份执行的三个固定只读工具。它们是可用的查询扩展，不代表阶段 B 的会话服务、持久化、SSE 或写操作恢复已经实现。

## 公开模块

当前只在 `ktg_agent` 包根导出 `__version__`。调用方应从下列模块导入阶段 A 接口；以下未列出的下划线成员和实现细节不是公开契约。

| 模块 | 公开职责与主要接口 |
| --- | --- |
| `ktg_agent.core.messages` | `SystemMessage`、`UserMessage`、`AssistantMessage`、`ToolResultMessage`、`TextBlock`、`ToolCall`、`StopReason`、`message_to_dict`、`message_from_dict`、`project_model_messages` |
| `ktg_agent.providers.base` | `Provider`、`ProviderRequest`、`ModelConfig`、`ToolSchema`、`TextDelta`、`ToolCallCompleted`、`ResponseCompleted`、`FinalResponse`、`Usage`、`ProviderError` |
| `ktg_agent.providers.fake` | `FakeProvider`、`ScriptedTurn` |
| `ktg_agent.providers.openai_compatible` | `OpenAICompatibleProvider`，把 OpenAI 兼容 chat completions 响应转换为核心 Provider 事件 |
| `ktg_agent.tools` | `ToolDefinition`、`ToolRegistry`、`ToolRunner`、`ToolContext`、`ToolResult`，以及 effect、执行方式、重试策略和状态枚举 |
| `ktg_agent.core.loop` | `AgentLoop`、`RunLimits`、`AgentRunResult`、`AgentLoopError` |
| `ktg_agent.core.events` | `AgentEventType`、`AgentEvent`、`EventPublisher`、`ToolProgressPublisher`、`EventSink` |
| `ktg_agent.core` | `Agent`、`AgentState`、`AgentBusyError`、`AgentNotRunningError`、`CancellationToken`、`InputMode`、`InputQueue` |
| `ktg_agent.ktg.client` | `KTGClient`、`KTGError`，使用请求携带的 Bearer token 调用固定 KTG 基址 |
| `ktg_agent.ktg.queries` | `build_query_tools`，注册产品、库存和工单三个固定只读工具 |
| `ktg_agent.service` | `QueryService`，验证当前用户并执行一次无状态查询 |
| `ktg_agent.server` | `Settings`、`create_service`、`AgentHTTPServer`，提供本地 health/query HTTP 入口 |

## 消息与 Provider

消息是不可变 dataclass，内容必须是非空 tuple。`ToolCall.arguments`、事件 payload 和工具结构化数据只能包含 JSON 值，其中数字必须是有限值，`NaN` 与正负无穷会被拒绝；出站 JSON 也禁止生成这些非标准数值。工具结果的 `tool_call_id` 必须引用之前出现且尚未完成的调用；重复 ID、未知结果或缺失结果会使 `project_model_messages` 失败。

`TextBlock.model_visible=False` 的文本不会进入 Provider 请求。`ToolResultMessage.details` 用于可信 UI 或记录，在模型上下文投影时总会移除；模型只看到 `content`。持久化层必须保存原始消息，不能用投影覆盖历史事实。

`Provider.stream(request)` 返回异步事件流。一个正常的流必须以且只能以一个 `ResponseCompleted` 完成。`StopReason.TOOL_CALL` 的最终消息必须至少包含一个完整 `ToolCall`；其它停止原因不能携带可执行工具调用。Provider 适配器应把供应商异常转换为 `ProviderError`。

### FakeProvider

`FakeProvider` 按构造时给出的 `ScriptedTurn` 顺序工作：一次 `stream` 调用消费一轮。每轮最后一个动作必须是 `ResponseCompleted` 或 `ProviderError`，终止动作之前可以包含 `TextDelta` 或 `ToolCallCompleted`。脚本耗尽后再次调用会明确抛出 `AssertionError`。

`requests` 以调用顺序保留收到的 `ProviderRequest`，适合断言下一轮是否包含预期的工具结果、历史和 schema；`remaining_turns` 可检查是否完全消费脚本。完整使用方法见 [`examples/fake_agent.py`](../examples/fake_agent.py)。

## 工具契约

`ToolRegistry` 是唯一执行白名单。`ToolDefinition` 使用严格的 JSON Schema object；未显式设置时会补上 `additionalProperties: false`。模型只接收工具名称、说明和输入 schema，不接收权限、身份、凭据、客户端或执行策略。

`ToolRunner` 的顺序是：查白名单、校验参数、运行 before hooks、执行并处理超时、校验成功结果的 output schema、运行 after hooks。未知工具、非法输入、策略拒绝和执行异常会归一化为失败的 `ToolResult`，而不是调用方异常。after hook 的异常会传播，供上层将记录失败视为运行失败。

`ToolContext` 中的 `identity`、`operation_id`、`cancellation`、`progress_publisher` 和 `ktg_client` 都由服务端注入，模型不能提供。`ToolResult.content` 是给模型的必要摘要；`details` 是结构化 UI 数据；`unknown` 状态用于未来无法确认外部写入结果的场景，不能降级成普通失败。

只有整个批次都标记为 `read + parallel` 时，`AgentLoop` 才按 `RunLimits.max_parallel_reads` 限量并行；只要含写工具或顺序工具，整批即按调用顺序串行。并行结束事件按实际完成顺序产生，写回模型上下文的结果仍保持调用顺序。

## AgentLoop 与 Agent

`AgentLoop.run` 接收 `run_id`、初始消息和可选的身份、取消信号、KTG 客户端、输入队列与事件发布器。它不拥有会话、不加载存储，也不验证身份。默认预算为 32 个模型轮次、128 个工具调用、300 秒和最多 4 个并行读工具；预算终止返回 `StopReason.LIMIT`，具体边界在 `AgentRunResult.limit_reason` 与 `agent_end.limit_reason` 中。

`Agent` 是有状态包装器：

- `prompt(..., run_id, history=(), identity=None, ktg_client=None)` 启动唯一活动 run，并把服务端准备好的历史、身份和客户端原样注入循环。
- 活动时再次普通 `prompt` 抛出 `AgentBusyError`，其代码为 `RUN_BUSY`。
- `steer` 和 `follow_up` 只在活动 run 接受；否则抛出 `AgentNotRunningError`，代码为 `RUN_NOT_ACTIVE`。
- steer 在当前已开始的工具批次和本轮 `turn_end` 之后进入历史，并触发下一轮模型调用；它不会撤销已经开始的工具。
- follow-up 仅在原运行即将以 `completed` 或 `length` 停止时消费，然后触发下一轮。错误、取消或预算结束不会消费 follow-up。
- `abort()` 返回是否接受取消。取消是协作式的：它在模型/工具边界阻止后续动作，不承诺中断或回滚已经开始的调用。
- `wait_for_idle()` 等待包装器完成最终清理。任何结束路径都会清空未消费输入、释放当前 token/publisher，并把状态恢复为 `idle`。

## 事件完成语义

每个阶段 A run 的事件从 1 开始按 `run_id` 单调递增；序号只在该 run 内有意义。典型文本运行顺序为 `agent_start` → `turn_start` → `message_start` → 若干 `message_update` → `message_end` → `turn_end` → `agent_end`。工具执行会在消息事件之间加入 `tool_execution_start/update/end` 和工具结果的 `message_start/end`；排队输入额外产生 `input_queued`。

| 信号 | 所有者 | 精确定义 | 不能据此推断 |
| --- | --- | --- | --- |
| `agent_end` | `AgentLoop` | 低层模型/工具循环已沿成功、失败、取消或预算路径结束；每个已启动循环最多发一次 | 事件、最终消息或外部写入已持久化；SSE 已发送；包装器已经 idle |
| `Agent.state == idle` / `wait_for_idle()` 返回 | `Agent` | `prompt` 的 `finally` 已执行，队列和 run 局部引用已清理，该 Agent 实例可接收新 prompt | 阶段 B 的 run 记录已经提交；业务写入结果已经核验；客户端已经收到终态 |
| `run_settled` | 未来的 `SessionService`，阶段 A 未实现 | run 的最终状态、最终消息、必需事件及操作记录已成功持久化，不会再自动继续；应是该 run 最后一个业务生命周期事件 | 所有外部操作都成功或被回滚；`cancelled` 代表已发写请求被撤销 |

因此阶段 B 的正确顺序是：观察 `agent_end` 或异常 → 等待 `Agent` 清理 → 把最终状态、消息、事件和操作结果提交到存储 → 发布持久化的 `run_settled`。如果持久化失败，不得仅因收到 `agent_end` 或 idle 就向 UI 宣称运行已完成。

监听器异常由 `Agent` 隔离，不会中断其他监听器或运行。用于可靠持久化的 sink 不应注册成普通监听器；应通过 `AgentLoop.event_sink` 或 SessionService 的事务流程处理，使失败能够传播或阻止 settled。

## 当前无状态只读查询扩展

`GET /health` 用于检查本地进程，`POST /agent-api/v1/query` 接受一次查询。每个查询请求必须携带当前网页用户的 Bearer token；`QueryService` 先调用 KTG `/getInfo` 验证当前身份，再创建短期客户端和 Agent。token 不写入消息、工具参数或持久化存储。

模型只能选择 `ktg_search_items`、`ktg_query_stock` 和 `ktg_list_workorders` 三个白名单工具；工具分别映射到固定的产品、库存和工单 GET 接口。模型不能指定 KTG 基址、HTTP 方法或任意路径。当前入口不保存会话和历史，不提供 run/input/cancel API，也没有 SSE、事件回放或写工具，因此不能当作阶段 B SessionService 使用。

## 当前已知限制

- 真实模型仅提供 OpenAI 兼容 chat completions 适配；当前适配器把完整响应转换为核心事件，不提供模型侧实时增量流。尚无重试编排或 token 用量预算。
- 真实 KTG 工具仅限三个固定只读查询；尚无写工具、细粒度工具权限策略或业务操作恢复。
- 没有 SessionService、Repository、数据库迁移、会话所有权或跨进程单写者保证。
- 事件只含 `run_id`、`seq`、`type` 和 payload；没有 `schema_version`、`session_id`、持久化时间、回放或保留期。
- 取消信号是进程内布尔 token；工具和 Provider 必须主动观察它，已开始的外部副作用不会自动撤销。
- `identity` 与 `ktg_client` 在核心层仍是不透明对象；当前无状态查询入口会调用 `/getInfo` 验证当前用户，但尚未实现会话所有权、长期凭据生命周期和完整权限编排。
- `ToolStatus.UNKNOWN` 和 operation 字段已有数据形状，但持久化意图、幂等键、回读核验与恢复状态机尚未实现。
- 当前只保证单个 `Agent` 实例单 run；阶段 B 仍要按会话管理实例、锁和 admission，不能依赖调用方自律。

## 阶段 B 注入边界

阶段 B 可以在不改写 `AgentLoop` 的情况下加入存储和身份：

1. SessionService 验证 Bearer token 与会话所有权，生成服务端身份上下文。
2. Repository 读取原始消息，并由 ContextBuilder 形成传给 `Agent.prompt(history=...)` 的模型上下文。
3. SessionService 按 session 管理单活动 run、request_id 幂等和 `Agent` 实例，把身份及短期 `KTGClient` 传给 `prompt`。
4. `AgentLoop` 已把 `identity`、`ktg_client`、run/tool call ID、取消信号和进度发布器放入 `ToolContext`；工具无需从模型参数取这些值。
5. SessionService/事件存储包装核心事件，补充 session、schema version、时间戳和持久化 seq；持久化完成后再对 SSE 发布。
6. Agent 返回或抛错后，SessionService 在事务中写最终消息、run 状态与操作摘要，随后生成 `run_settled`。

需要注意：若事件要求严格“先落库、再发布”，阶段 B 应把可靠存储 sink 放在 `AgentLoop.event_sink`，把 UI 订阅建立在已落库事件之上；不能依赖 `Agent.subscribe` 的尽力通知语义。

## 阶段 B 交接清单

### 持久化与会话

- [ ] 定义 `SessionRepository` / `RunRepository` 接口、SQLite 实现和迁移，覆盖 sessions、runs、messages、events、tool executions、business operations 与 context summaries。
- [ ] 实现按用户+会话范围的 `request_id` 幂等：同 ID 同请求复用，同 ID 不同请求返回 409。
- [ ] 实现每会话单活动 run 的 admission；多进程前补充租约、fencing 或明确的会话路由。
- [ ] 明确原始消息、模型上下文投影、文本 delta 和可恢复事件各自的保留策略。

### 身份与权限

- [ ] 在现有每请求 `/getInfo` 验证的基础上，把身份绑定到持久化会话及其所有权；token 不落库、不进提示词和普通日志。
- [ ] 会话读取、事件订阅、排队、取消和工具执行均校验所有权与权限；Java 端仍是最终权限判定。
- [ ] 定义短期凭据过期行为：停止后续写入，重新登录后先核验未决操作。

### HTTP 与 SSE

- [ ] 实现 `/agent-api/v1` 的 sessions、runs、cancel、inputs 和 events 端点及明确的 409/410 错误。
- [ ] 事件先持久化再发布，按 run_id+seq 去重；重连时从 `after_seq` 回放后无缝衔接实时流。
- [ ] SSE 使用请求头身份验证，解析完整帧，支持心跳、代理禁缓冲、保留期过期和最终快照回退。
- [ ] 由 SessionService 发布持久化的 `run_settled`，不能把低层 `agent_end` 直接转名。

### 未知写入与恢复

- [ ] 写操作执行前保存稳定 `operation_id`、幂等键、参数摘要、目标版本与 `prepared/executing` 状态。
- [ ] 发出请求后、结果落库前崩溃必须进入 `unknown` / `needs_reconciliation`，禁止自动重放。
- [ ] 优先用 Java 幂等键或操作回执核验；否则回读唯一业务编号与实际数据，无法唯一判断时保持 unknown 并要求人工处理。
- [ ] run 终态携带已完成、失败和未知步骤；失败或取消不得暗示已成功外部操作被回滚。

## 阶段 A 验证基线

在 `ktg-agent` 目录执行：

```powershell
uv sync --locked
uv run --locked python examples/fake_agent.py
uv run --locked pytest
uv run --locked mypy
```

验证记录应包含实际 Python/uv/锁定依赖版本、测试数量、类型检查与构建结果；命令或预期输出变化时须同步更新 README 和本文件。

2026-10-09 的独立目录离线复验使用 Windows、CPython 3.12.6 和 uv 0.11.21；锁定安装与包导入成功，示例产生 2 次 Provider 请求和 1 次工具调用，测试结果为 `92 passed`，严格类型检查结果为 `Success: no issues found in 33 source files`。

## A-08 完成记录

完成日期：2026-10-09。

实际修改文件：

- `README.md`：干净环境安装、命令、离线示例、当前查询能力和阶段边界。
- `docs/core-contracts.md`：公开接口、有限 JSON 数字约束、事件完成语义、当前只读查询扩展、已知限制及阶段 B 清单。
- `examples/fake_agent.py`：无网络的 Provider → 工具 → Provider 闭环。
- `tests/test_offline_example.py`：验证文档示例可由已安装包直接运行。

验证环境为 Windows、CPython 3.12.6、uv 0.11.21。锁定的直接依赖为 jsonschema 4.25.1；开发依赖为 pytest 9.0.3、mypy 1.18.2 和 types-jsonschema 4.26.0.20260518；构建后端为 hatchling 1.27.0。

在新建的独立目录和虚拟环境中执行离线锁文件安装、包导入、离线示例、完整测试和严格类型检查，结果为：包版本 `0.1.0`；示例产生 2 次 Provider 请求和 1 次工具调用；`92 passed`；`Success: no issues found in 33 source files`。本轮同时把 A-02 的有限 JSON 数字约束、A-06 的整批 deadline 约束，以及后续加入的真实只读查询能力纳入交接说明。下一阶段可按上方注入边界建立完整 SessionService。
