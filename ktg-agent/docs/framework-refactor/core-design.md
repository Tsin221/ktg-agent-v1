# KTG Agent 核心层详细设计说明

版本：V1.0  
日期：2026-11-25  
状态：C-01 至 C-09 技术决策已确认，框架能力已依据公开官方文档核实。候选核心实施及隔离验收见[阶段交接说明](Agent核心层阶段交接说明-项目经理.md)，真实模型及统一持久化联调尚待完成。

本文承接 [Agent 框架化重构总体说明书 V1.2](../../Agent框架化重构方案.md) 和 [HTTP 服务层重构技术结论](../01-HTTP服务层重构技术结论.md)，供核心层、HTTP 层、持久化层共同设计与对接。本文不替代实施任务书，不规定最终依赖版本、数据库表结构、完整 API 字段或实施排期。D-01 至 D-14、H-01 至 H-07 继续有效。

## 一、目标与当前状态

KTG Agent 是面向 MES 生产主管的面试展示项目。重构的目标是采用已经成熟的 Agent 运行能力，减少自研通用基础设施，保持自主分析、工具组合、任务规划、多轮会话、实时交互及可信业务操作。

正式演示仍运行 `ktg_agent_v1`。现有 Python Agent 自行实现模型协议、模型与工具的执行循环、工具发现、消息历史、上下文压缩、任务预算、输入队列及事件系统。候选工程 `ktg-agent-refactor/ktg-agent` 的 HTTP 层已经独立开发并通过限定范围的验证，但尚未接入真实 Agent 核心和新持久化层。默认 `create_app` 没有真实 `run_core`，创建 Run 返回 `503 RUN_UNAVAILABLE` 是已知接入缺口，不能因此恢复旧核心作为正式实现。详见 [HTTP 层阶段交接说明](../HTTP服务层实施/HTTP服务层阶段交接说明-项目经理.md)。

本次设计遵循用户确认的原则：能够使用框架已提供的通用能力，就优先使用；只针对 KTG 的业务约束以及与现有应用的必要接缝做轻量适配。不建设第二套 Agent 框架，不扩展 MES 业务，不引入生产级分布式运行基础设施。

## 二、已确认的技术决策

| 编号 | 决策 | 主要理由 |
| --- | --- | --- |
| C-01 | Pydantic AI 拥有完整模型与工具执行循环 | 使用成熟通用运行能力，同时保留模型自主选择、组合工具与调整计划。 |
| C-02 | 官方 DeepSeek Provider + 原生工具注册，轻量复用八项现有业务工具 | 复用已验证的模型接入方向及业务契约，删除自研模型协议和工具搜索、加载、调度机制。 |
| C-03 | 采用原生 `Agent.iter()`、`AgentRun` 及单进程 asyncio；运行中交互采用框架原生语义 | 原生控制句柄能直接提供外部输入和取消，原生图仍掌握下一步。 |
| C-04 | 使用官方 Compaction 自动摘要，保存原始消息 | 简化上下文管理，保留多轮会话和长任务上下文连续性。 |
| C-05 | 使用官方 Planning，优先使用其 SQLite 计划存储 | 任务清单由模型自主维护，保留基本一致性与业务证据，删除专属复杂机制。 |
| C-06 | 框架原生事件经轻量映射进入现有 HTTP SSE | 复用已完成的实时传输、重连和终态同步，避免重复的事件系统。 |
| C-07 | 使用原生运行限制、超时、重试及基础用量统计 | 以统一 Run 级约束防止失控，不保留精细 Token 结算账本。 |
| C-08 | 优先采用官方运行过程记录，用户主动继续任务；进程重启不自动续跑 | 保存已完成工作和未知状态，满足演示需求，避免引入持久工作流引擎。 |
| C-09 | 明确 HTTP、Agent 核心、SQLite 持久化和 MES 业务工具的职责 | 复用现有 HTTP 接入点，不重复建立生命周期、历史、事件或业务执行体系。 |

## 三、核心架构与执行控制（C-01、C-03）

Pydantic AI 的 `Agent` 负责定义模型、指令、工具、输出及扩展能力；`AgentRun` 表示一次独立运行；`RunContext` 为工具和能力提供服务端依赖。`Conversation` 或原生消息历史承载跨 Run 的对话上下文。官方 Harness 的 Planning、Compaction、StepPersistence 是同一框架体系的扩展能力，按需采用，不安装无关能力，也不引入另一套 Agent 运行时。

```text
MES 前端
    ↕ HTTP / SSE
FastAPI + 应用 RunRuntime  ── SQLite：会话归属、Run 受理与终态
    ↕ 薄执行与事件适配
Pydantic AI Agent / AgentRun ── DeepSeek
    │ 原生 Tools / Toolsets、Planning、Compaction、过程保存
    ↕ 受信任的运行依赖
KTG 业务工具与受控操作机制 ── Java MES
    ↕
SQLite：原生消息、任务、关键工具状态、业务方案及回执
```

一次 Run 的概念流程：用户输入和必要历史进入原生 Agent，框架发起模型请求，模型决定回答或调用工具，框架执行工具并将结果交还模型，重复直到正常结束、暂停、停止或达到运行限制。底层由框架内部图推进。KTG 使用 `Agent.iter()` 的自动异步遍历，消费模型节点和工具节点产生的原生事件；不手工实现节点跳转、模型请求循环、工具批次调度或重规划状态机。

选择 `iter()` 的原因是 KTG 需要完整流式过程以及外部用户控制句柄。`run()` 和 `run_stream_events()` 也能够完成原生执行，但后者的事件句柄没有直接的外部 `enqueue()` 接口；普通 `run_stream()` 对可被判定为最终输出的文字可能提前结束，不适合直接作为自主多轮工具执行的主入口。正式迁移前验证 `iter()`、扩展能力、事件交付和最终结果的实际组合行为。

- Agent 定义与模型客户端可以复用，每次 Run 的依赖、消息、计划范围、取消状态和控制句柄独立；不能将用户身份或任务状态写入共享可变 Agent 配置。
- HTTP 层已有 `RunRuntime` 管理 `asyncio.create_task()`。同一会话最多一个活动 Run，不同会话可异步并发；共享资源的并发上限按演示测试配置。
- SSE 订阅不拥有后台 Run 的生命周期；禁止为每个 Run 创建独立线程池或事件循环。阻塞的 MES/SQLite I/O 仍需使用原有 `to_thread` 或相应异步适配。

### 3.1 运行中交互

| 交互 | 原生机制与生效时机 |
| --- | --- |
| Steer（调整方向） | `AgentRun.enqueue` 的 `asap` 优先级，在下一次模型决策机会交付；若原本即将结束，可触发额外模型请求。 |
| Follow-up（追加任务） | `when_idle` 优先级，当前工作即将正常结束时交付并继续本轮运行。 |
| Stop（停止） | 使用每轮独立的原生 `CancellationToken` 或 `AgentRun.cancel()`，停止后续工作并保存已完成的真实状态。 |

Steer 是优先交付的排队消息，不能直接改变已发出的 DeepSeek 请求或自动撤销已经调用的工具；不追求即时打断并改写当前模型生成的体验。需要立即改变方向时，用户可停止后提交新指令。停止不代表外部已发出的业务操作已经撤销。未执行或未知结果不得呈现为成功。框架的暂停机制不能被待处理消息绕过。

输入消息须以用户消息身份交付，不能作为系统指令。原生 `enqueue_id` 表示框架接受入队，`EnqueuedMessagesEvent` 表示内容进入运行历史，不能将两者混称为模型已读取或遵从。未交付消息保留真实状态，不在服务重启后自动重放。

候选 `RunRuntime.control()` 目前在全局锁内等待 `Execution.input()` 最多一秒，只有同步返回成功才记录 consumed。Follow-up 可能远晚于这个时限。真实接入时根据原生交付标识和事件异步确认，保证终态结算前正确归并；不得持锁等待模型，不能把已经交付的消息误报未消费。其具体契约由核心、HTTP 和持久化负责人共同确定。

## 四、模型与工具接入（C-02）

继续使用 `deepseek-flash`，优先 Pydantic AI 官方 `OpenAIChatModel` / `DeepSeekProvider` 的 Chat Completions 接入。保持现有 `MODEL_API_KEY`、`MODEL_BASE_URL`、`MODEL_ID` 等配置语义，由配置层装配官方 Provider，不维护自研 OpenAI 兼容协议。保留思考模式及自动工具选择，不强制所有最终回答产生 JSON。思考模式下 DeepSeek 的多轮工具调用要求必要推理内容正确回传，Provider 原生支持仍需通过实际 DeepSeek 回归验证。前端应展示用户可理解的进展和结果，不把未公开的模型内部推理或服务凭据直接暴露给用户。

八项业务工具的契约与业务逻辑继续复用：

| 工具 | 能力 |
| --- | --- |
| `ktg_count_products` | 产品统计 |
| `ktg_query_stock_summary` | 分仓现存量 |
| `ktg_query_workorder_overview` | 工单概览与生产进度 |
| `ktg_query_workorder_materials` | 工单备料及剩余用料 |
| `ktg_query_workorder_quality` | 工单与单件质量分析 |
| `ktg_query_production_trace` | 双向生产追溯 |
| `ktg_resolve_business_reference` | 业务编码识别 |
| `ktg_prepare_workorder_operations` | 工单受控操作方案准备 |

使用官方 `Tool` / `FunctionToolset` 等注册抽象，迁移优先采用 `Tool.from_schema()` 结合现有 JSON Schema 和异步执行函数，减少重写业务工具的风险。官方对 `from_schema()` 不自动执行 JSON Schema 参数校验，因此继续复用必要的现有输入、输出及业务校验，不仅依赖模型端 strict 参数。后续新增工具可使用框架原生 Python 类型与 Pydantic 模型。

根据当前用户权限直接提供可用工具，优先采用 `FilteredToolset` 等原生过滤机制；删除自研搜索、加载及候选/激活快照。服务端在实际执行时再次核验真实身份、当前权限、工具启用条件及对象范围。原有 `legacy_catalog.py` 中必要的权限规则不能因去掉工具发现而丢失。MES 凭据只通过服务端依赖用于调用与权限验证，不进入模型上下文、持久消息、日志或展示事件。页面信息（D-14）仅用于帮助理解用户当前业务对象，不能作为业务事实或权限依据。

模型负责自主调用，独立且明确允许并行的只读工具可由框架并行执行；必须串行的工具（例如具有方案存储副作用的操作方案准备）使用原生串行约束。现有业务结果摘要与 UI 详情的区分继续保留，可用 `ToolReturn` 的模型可见结果和应用 metadata 表达，避免将非必要 UI 详情发送给模型。实际 MES 写入仍由独立确认接口和现有受控执行机制完成，禁止向模型注册直接写入或确认执行工具。

## 五、消息历史、上下文与摘要（C-04）

使用 Pydantic AI 原生消息类型及序列化（如 `ModelMessagesTypeAdapter` / `Conversation`），不再维护旧通用消息协议作为第二套权威历史。原始用户和模型消息、必要的完整工具结果由持久化保存；前端展示格式在边界转换。

短对话直接传递必要上下文；长对话优先使用官方 Harness `SummarizingCompaction` 自动压缩较早历史，保留近期完整消息及工具调用与返回的配对。如果摘要失败或未有效缩小上下文，优先采用官方 `FallbackCompaction` / `SlidingWindowCompaction` 作合理回退，超出窗口仍无法继续时如实结束。触发阈值与保留量根据实际模型窗口、演示负载和测试调整。不再创建独立摘要 Run 的应用生命周期、不开放手动压缩、不采用工具结果落盘再提取。

官方上下文处理会改写当前运行历史，所以必须在压缩前保存原始消息记录。压缩后的模型上下文是派生视图，可保存必要摘要及引用以避免重复摘要，但不得作为完整对话或原始业务证据的唯一来源。不得为了保留原始消息而平行维护另一套旧消息表示。摘要需保留用户目标、关键业务对象、必要约束及未完成事项；摘要不是实时 MES 事实或业务回执，必要时重新查询核实。摘要的模型消耗按框架原生用量计入父 Run，不另建摘要结算机制。

## 六、Planning、任务清单与幂等（C-05）

采用官方 Harness `Planning`。框架提供计划写入、读取、任务新增、状态更新、批量更新和移除能力，模型可以主动创建并调整计划，简单请求不强制建清单。使用原生稳定步骤 ID 进行必要的更新，业务任务与会话归属由应用关联。优先使用 `SqlitePlanStore` 与按运行解析存储范围的能力，按所属用户、会话及任务隔离，使用户主动继续时能够读取原计划，而非共享全局清单或每次重建。

任务状态使用基础的 pending / in_progress / completed / cancelled 语义，暂不启用原生子任务、依赖树及额外调度能力；遇到无法执行事项保留未完成状态及必要阻塞原因。原生 Planning 事件映射为已有进度展示，前端可读取持久化的最新清单。保留稳定任务、步骤与必要工具证据的引用；模型自报 completed 不是业务操作成功或业务事实的凭证，现有需要证据的业务校验仍需保留。

不维护完整任务版本历史、每步骤调用配额、计划保存次数限制、专用收尾模型调用或专门重规划机制。计划本身不是第二套工作流调度器，不强制模型按固定步骤执行，也不额外启动模型完成清单。

幂等按风险分层：HTTP 的请求防重复继续使用已有 `request_id` 机制；Planning 使用步骤 ID、最新状态及必要的一致性校验，避免重复提交同一更新和盲目重放新增任务。普通 Planning 不承诺所有工具调用恰好一次；框架文档中的执行重放防重依赖可选持久执行引擎，本项目不引入。MES 写入的稳定操作标识、防重复执行、权限复核和未知回执核实继续由业务层严格保证，不能被任务 completed 或框架调用状态替代。

## 七、事件、交互展示和 HTTP 接入（C-06、C-09）

框架原生事件作为核心运行事实的来源；只映射实际前端需要的语义，无须新建通用事件总线或改变已完成的 SSE 传输机制。具体线协议字段按候选 HTTP 契约联调。

| 原生事实或事件 | 对外展示 |
| --- | --- |
| `PartStartEvent` / `PartDeltaEvent` / 对应完整消息 | 可展示文字的增量与完成状态 |
| `FunctionToolCallEvent` / `FunctionToolResultEvent` | 工具名称、开始、结束、结果或真实失败 |
| 业务工具的必要自定义进度事件 | 工具执行期间的可读工作进展 |
| `PlanCreatedEvent`、`PlanUpdatedEvent`、计划状态变化等 | 最新任务清单刷新与进度 |
| 原生输入入队标识及 `EnqueuedMessagesEvent` | 输入待处理、已交付或未消费状态 |
| 原生运行正常结束、取消和异常 | 供应用结算的真实 Outcome；不直接宣称 HTTP 已终态 |

框架事件由薄适配进入候选 `publish_progress()` / `RunEvents`；统一由 HTTP SSE 分配 Run 内递增序号，实时文字增量仅进入有界内存缓存，不逐条保存。任务、消息和关键工具状态等以 SQLite 为准。框架内部事件的字段和身份信息按需选取、转换和保护；不能把原生事件序列化后无条件推给前端。

候选前端目前仍按 `create_task` / `update_task` 工具完成事件触发清单刷新，需对官方 Planning 的变更事件或等价公共进度语义作最小适配，不恢复旧任务工具。不能在文字生成结束时提前发布 `run_settled`：应用应先提交最终消息与真实 Run 终态，再由已有 HTTP SSE 通知前端。连接中断、页面刷新与缓存不足均沿用 H-03 语义，不要求重放全部文字增量。

## 八、运行限制、错误与用量（C-07）

采用原生 `UsageLimits`、`RunUsage`、ModelSettings 和运行/工具超时机制；配置值以原有演示设置为参考，经真实测试调整，不在此锁定数值。

| 控制 | 处理方式与边界 |
| --- | --- |
| 最大模型轮次 | `UsageLimits.request_limit`，定义为原生模型响应轮次，不等于所有供应商 HTTP 尝试。 |
| 最大工具调用 | `tool_calls_limit`，限制框架定义的成功调用；模型轮次、工具有限重试及总时间共同约束失败循环。 |
| 单次模型输出及总用量 | 配置模型单次最大输出；可选简单累计 Token 上限，不建设精细结算。 |
| 运行时间 | 原生 Run `timeout`；达到后停止新工作，保存真实状态。 |
| 模型与工具超时 | 原生模型请求和工具的超时配置；工具集默认值须针对实际 Toolset 设置。 |
| 重试 | 原生参数/模型错误反馈和有限重试；保留业务只读查询的有限重试规则，避免叠加无界重试。 |

基本统计保留本轮模型轮次、成功工具调用次数、输入/输出及必要的缓存 Token 和运行耗时。框架的 `unmeasured_requests` 可指示缺失用量的响应；实际用量不完整时，显示已知部分及未知状态，不将未知当作零。跨 Run 续办使用新 Run 的预算，不将整个 Conversation 的累计用量误用作该 Run 预算。

参数或业务输入错误以可理解的反馈交给模型修正，普通只读工具异常可有限重试，未知业务写入不重试而先核实。框架超时、取消以及预算耗尽时，不得凭已有文字宣称完整成功；使用原生失败/取消历史捕获能力保存已产生的有效记录，映射真实终态。同步线程中的阻塞调用可能在取消后继续，其效果不能由 asyncio 取消状态推断，须保留业务回执核实机制。

## 九、过程保存与用户主动继续（C-08）

候选 HTTP 层已经负责活动 Run、终态结算和进程关闭恢复。核心层优先采用官方 Harness `StepPersistence` 的已结算步骤快照、关键执行状态和中断记录，优先验证 `SqliteStepStore` 与统一 SQLite 存储的组合。框架步骤保存是恢复点，并不自动执行下一步，也不能恢复到工具调用的中间指令。这里不引入 Temporal、DBOS 或其他持久工作流引擎。

| 场景 | 预期行为 |
| --- | --- |
| 网页刷新/断线 | 原后台 Run 继续执行，重新订阅和读取已有持久状态。 |
| 主动停止/超时/异常 | 停止后续工作，保留已完成消息、工具结果、清单最新状态与真实停止原因。 |
| 服务进程重启 | 遗留活动 Run 标记为中断，释放运行资格；不自动续跑，已保存的关键状态可查询。 |
| 用户主动继续 | 校验原任务归属，读取原生历史与有效工具结果和最新清单，创建新 Run 及新预算；由模型决定剩余工作。 |
| 业务操作结果未知 | 保留原业务操作标识，先通过现有机制核实真实回执，不盲目重发。 |

核心应保存足以支持续办的关键状态，不能只在成功结束后一次性写入。原生取消和失败捕获可提供已完成的消息；硬进程中断依赖此前已提交的过程记录。如果框架快照存在未完成工具调用，恢复前核实相应结果，必要时重新查询只读事实，不直接无条件重放未知操作。若仅使用原生 `continue_run` / 快照能力，其消息历史不包含全部 application capability 状态，任务清单和业务记录仍需按各自持久化来源读取。

`SqliteStepStore` 官方默认有 `runs`、`events`、`snapshots` 等表，与候选旧 `SessionRepository` 的表名存在接缝；结合 D-10 新架构无需沿用旧聊天数据库结构。由持久化设计确定如何在同一 Agent SQLite 中复用框架持久化能力与应用 Run 状态，避免表结构冲突、二套权威运行状态、重复存储完整历史或额外 Agent 数据库。对于默认媒体外置、快照保留策略等与 D-04/原始记录要求有关的配置，按已确认边界验证和必要约束；不把通用快照机制误用为工具结果落盘后二次提取。

## 十、分层职责与对接约束（C-09）

| 层级 | 负责 | 对接职责 |
| --- | --- | --- |
| HTTP / 应用运行层 | 认证、会话与 Run 接口、后台 asyncio 任务、请求防重、外部运行控制、SSE、资源启停 | 传递已验证身份、用户输入与页面信息；调用核心；统一发布进度与已结算终态。 |
| Agent 核心层 | 模型接入、工具注册、自主执行、消息上下文、Planning、运行限制、原生控制与事件 | 提供真实执行适配器，向持久层索取历史/任务并保存关键状态，输出可展示事件与真实 Outcome。 |
| SQLite 持久化层 | 会话归属、原生消息、完整历史、必要工具结果、最新任务、Run 状态与统计、方案确认/回执 | 原子受理防重、正确恢复中断、最终消息与终态一致提交；优先复用框架已有序列化与存储接口。 |
| MES 业务工具层 | 业务读取、数据解释、权限和对象校验、操作准备、确认、执行、回执核实与严格幂等 | 提供已经验证的异步业务实现及结构化结果；模型不能自行授权写入。 |

候选工程的 `http_app.create_app(run_core=..., run_events=...)`、`http_runs.Core.prepare()`、`Execution.run()/stop()/input()`、`Outcome` 和 `publish_progress()` 是既有跨层接入点。保持其职责，按真实框架能力调整内部消息类型、输入消费确认及必要返回结构，不要求原样保留旧数据模型。

核心、HTTP 和持久化负责人共同核实以下接缝：Run 与 Session 的标识映射及每轮独立上下文，真实结束与未完成状态映射，输入入队和交付确认，框架原始消息存储、计划/过程记录与统一 SQLite 的适配，关键状态一致提交及前端事件转换。HTTP 已完成的身份与 SSE 逻辑继续复用；不得让新 Agent 有独立的 HTTP 传输、会话认证或第二套 Run 生命周期。

## 十一、实施前能力验证边界

本设计明确技术选择，不代表候选工程已实现。正式切换前至少需要以下真实证据：

| 范围 | 验证要求 |
| --- | --- |
| 依赖 | 与候选 Python 3.12、FastAPI、Pydantic 和 Web 依赖兼容；锁定经过验证的 Pydantic AI 与 Harness 版本。Harness 仍为 0.x，应关注版本变化。 |
| 原生运行 | 真实 DeepSeek 多轮文字和工具调用、同一响应的文字与工具共存、原生 loop 和事件完整性。 |
| 工具 | 八项工具的现有参数、结果、权限、页面指代和跨工具分析；串行及独立只读并行；方案确认、回执和防重复执行。 |
| 上下文与 Planning | 自动摘要触发及失败回退、原始消息留存、DeepSeek 工具历史兼容、计划自主调整、步骤证据及持久化续办。 |
| 交互 | Steer 与 Follow-up 的实际生效时机（包括临近终态）、迟到交付确认、停止与中断状态、不会误报已消费或成功。 |
| 事件和 Run | 实时文字及工具/计划进度、重连、终态提交顺序；异常、限制、超时及基础用量缺失情况。 |
| 持久化 | 同一 Agent SQLite 下的消息、计划与过程记录及应用状态边界；进程重启后不自动续跑、用户主动继续、原业务回执可核实。 |

验证在候选工程及隔离环境进行，正式演示和业务数据不受影响；未经真实核心与持久化联调及隔离验收，不解除 HTTP-04、HTTP-06 的既有阻塞。本文件不规定任务书或正式切换授权。

## 十二、与总体决策的对应关系

| 总体决策 | 本设计中的落点 |
| --- | --- |
| D-01、D-02 | C-04 自动摘要归入正常上下文处理，不独立摘要 Run，不手动压缩。 |
| D-03 | C-07 原生基础用量和 Run 级限制，不维护精细结算。 |
| D-04 | C-02、C-04、C-08 控制工具回传大小，不做工具结果落盘后二次提取。 |
| D-05 | C-08 用户主动继续，进程重启不自动续跑，未知操作先核实。 |
| D-06 | C-02 直接提供授权工具，删除搜索与加载。 |
| D-07 | C-06 SSE 临时事件与 SQLite 关键状态分离。 |
| D-08 | C-05 简单模型驱动清单与最新状态，保留任务/步骤标识及必要证据。 |
| D-09 | C-08、C-09 新会话数据从空结构开始，保留必要旧业务确认、幂等和回执。 |
| D-10 | C-04、C-05、C-08、C-09 框架原生消息与统一 SQLite，应用保存必要状态。 |
| D-11 | C-01、C-02、C-03 由 Pydantic AI 接管核心并经真实模型验证。 |
| D-12 | C-03、C-06 原生 Steer、Follow-up、Stop，舍弃即时打断要求与复杂输入调度。 |
| D-13 | C-01、C-05 正常模型推理自主调整，不新增重规划机制。 |
| D-14 | C-02、C-09 传入 MES 页面信息，权限与业务事实由实际工具核实。 |

## 十三、技术依据

官方资料（2026-11-25 查阅）：

- Pydantic AI：[Agents 与原生执行循环](https://pydantic.dev/docs/ai/core-concepts/agent/)、[AgentRun API](https://pydantic.dev/docs/ai/api/pydantic-ai/agent/#agentrun)、[消息历史与运行中交互](https://pydantic.dev/docs/ai/core-concepts/message-history/)。
- Pydantic AI：[DeepSeek](https://pydantic.dev/docs/ai/models/deepseek/)、[Function Tools](https://pydantic.dev/docs/ai/tools-toolsets/tools/)、[Toolsets](https://pydantic.dev/docs/ai/tools-toolsets/toolsets/)、[Advanced Tool Features](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/)。DeepSeek：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
- Pydantic AI Harness：[Compaction](https://pydantic.dev/docs/ai/harness/compaction/)、[Planning](https://pydantic.dev/docs/ai/harness/planning/)、[Step Persistence](https://pydantic.dev/docs/ai/harness/step-persistence/)。
- Pydantic AI：[Timeouts](https://pydantic.dev/docs/ai/core-concepts/timeouts/)、[Retries](https://pydantic.dev/docs/ai/core-concepts/retries/)、[Usage](https://pydantic.dev/docs/ai/api/pydantic-ai/usage/)、[Persistence](https://pydantic.dev/docs/ai/core-concepts/persistence/)。
- 行业架构参考：[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/running_agents/)、[OpenAI Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/agent-loop)、[Google ADK](https://adk.dev/runtime/event-loop/)、[Microsoft Harness](https://learn.microsoft.com/en-us/agent-framework/concepts/harness)、[Microsoft Agent / Workflow 取舍](https://learn.microsoft.com/en-us/agent-framework/journey/workflows)。行业公开架构共同体现通用循环由框架、业务和交互由宿主应用承担，不用于推断闭源产品的内部实现。

现有源码审查依据：旧 [AgentLoop](../../../../ktg_agent_v1/ktg-agent/src/ktg_agent/core/loop.py)、[ToolRunner](../../../../ktg_agent_v1/ktg-agent/src/ktg_agent/tools/runner.py)、[权限目录](../../../../ktg_agent_v1/ktg-agent/src/ktg_agent/tools/legacy_catalog.py)、[任务工具](../../../../ktg_agent_v1/ktg-agent/src/ktg_agent/sessions/task_tools.py)、[受控操作执行器](../../../../ktg_agent_v1/ktg-agent/src/ktg_agent/operations/executor.py)，以及候选 [应用装配](../../../../ktg-agent-refactor/ktg-agent/src/ktg_agent/http_app.py)、[RunRuntime/执行接口](../../../../ktg-agent-refactor/ktg-agent/src/ktg_agent/http_runs.py)、[HTTP SSE](../../../../ktg-agent-refactor/ktg-agent/src/ktg_agent/http_sse.py)、[会话存储](../../../../ktg-agent-refactor/ktg-agent/src/ktg_agent/sessions/repository.py)。
