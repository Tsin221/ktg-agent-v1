# HTTP 服务层重构技术结论

版本：V1.0  
日期：2026-11-25  
状态：主要技术决策已确认，尚未实施或完成框架能力验证。

本文承接 [Agent 框架化重构总体说明书 V1.2](../Agent框架化重构方案.md)，记录 HTTP 服务层的技术结论及取舍。总体说明书中的 D-01 至 D-14 继续有效。本文不制定实施任务、排期、最终 API 字段、数据库表结构或依赖锁定版本；与其他层相关的实现细节留待各层详细设计。

## 一、重构目标与判断依据

KTG Agent 是面向 MES 生产主管的面试展示 Demo。HTTP 层重构的目标是减少通用服务代码、降低维护难度，保持现有演示交互及业务安全边界。

现有 `server.py` 使用 Python 标准库 `ThreadingHTTPServer` 和 `BaseHTTPRequestHandler`，手动处理路由、JSON、参数校验、错误响应、SSE 帧与心跳、连接断开等，还负责装配 Agent 和会话服务。`RunCoordinator` 另行维护线程池、事件循环和跨线程调度。现有 Vue 前端已具备按 Run ID 订阅事件、按序号重连、查询持久化终态的能力。

本次使用 FastAPI、Uvicorn、Pydantic 和 Python 原生 asyncio 接管或简化通用 HTTP 与运行设施。保留八项 MES 工具、实际权限、会话归属、用户确认、执行与回执核实；不重构 Java MES，不新增生产级通用 Agent 平台设施。

## 二、已确认的七项决策

| 编号 | 技术结论 | 取舍理由 |
| --- | --- | --- |
| H-01 | HTTP 服务采用薄适配层，使用 FastAPI + Uvicorn + Pydantic | 让成熟框架承担 HTTP 协议、路由、数据校验与响应；业务规则及运行状态由相应层负责。 |
| H-02 | 保留 `/agent-api/v1` 下仍有用的接口地址及主要通信格式，只维护一套 API | 降低现有前端改动，不为已退役功能建设兼容机制，不追求重新命名接口。 |
| H-03 | FastAPI 原生 SSE + Run 级内存事件缓存 + SQLite 关键状态持久化 | 复用当前事件编号、订阅与重连方式，避免逐条文字增量写入 SQLite，也减少额外快照协议的改造。 |
| H-04 | 使用 FastAPI 原生认证接入能力，继续复用实际 MES 身份与权限体系 | Agent 无需另建登录、用户或角色系统；保留会话归属及访客隔离。 |
| H-05 | 单进程异步架构，以 asyncio 管理后台 Run | 使运行脱离单次 HTTP 请求、支持运行中交互，减少现有线程池及跨事件循环协调。 |
| H-06 | 保留现有服务启动入口，内部由 Uvicorn 启动 FastAPI，通过 lifespan 管理应用资源 | 降低本地、云端部署改动，统一启动、恢复与退出逻辑。 |
| H-07 | 独立环境验证，通过后再切换正式演示 | 不影响现有演示，验证真实能力和异常情况，保留可回退的旧版本。 |

## 三、职责边界与框架接管范围（H-01）

```text
现有 MES 前端 / 同源代理
          │ HTTP / SSE
Uvicorn + FastAPI / Pydantic
          │ 已校验请求、已核实的身份
应用运行层（Session、Run、任务状态、进程内事件缓存）
          │
Pydantic AI（模型、消息、工具调用、事件及运行中交互）
          │
KTG 业务工具与受控执行层 ── 现有 Java MES

SQLite 保存会话、消息、必要运行状态、任务及业务操作记录。
```

| 现有通用工作 | 重构处理 |
| --- | --- |
| HTTP 监听、连接及协议处理 | Uvicorn 接管，删除自研 HTTPServer。 |
| 手写 URL 分发、请求/响应 JSON、字段校验及接口文档 | FastAPI 路由、Pydantic 数据模型及原生 OpenAPI。 |
| 各分支重复处理异常 | 使用 FastAPI 原生异常机制和统一的必要业务错误映射。 |
| 手拼 SSE 帧、心跳、传输响应头 | 优先使用 FastAPI 原生 `EventSourceResponse` / `ServerSentEvent`。 |
| 用户身份取得及共享依赖 | 使用 FastAPI `HTTPBearer`、`Depends`，调用现有 MES 身份核验能力。 |
| 资源启停 | 使用 FastAPI `lifespan`，由应用负责必要的业务状态恢复。 |

HTTP 层只做协议适配，不在路由内组织模型执行循环、编排 MES 写入或直接管理 SQLite 表。运行、持久化和业务层之间用清晰的函数与数据结构衔接，无需为此建立 Controller、通用事件总线、多层接口抽象或新的 Agent 框架。

## 四、API 保留与简化（H-02）

当前前端通过 `agent.js` 和 `agentSession.js` 使用 REST + SSE。继续采用先创建 Run、取得 `run_id`、再订阅 SSE 的方式；保留 `session_id`、`request_id`、`run_id`、`page_context` 等核心语义。`request_id` 继续用于防止重试重复创建运行，服务端验证归属和业务条件，不能只依赖前端防重。

| 接口族（现有地址） | 处理原则 |
| --- | --- |
| `GET /health`、`GET /agent-api/v1/tools` | 保留健康检查、按实际权限返回工具目录。 |
| `GET/POST /agent-api/v1/sessions`、会话详情及 `/title` | 保留会话创建、查询和重命名。 |
| `.../sessions/{session_id}/messages` | 保留会话历史分页，使用框架原生消息表示与必要的前端适配。 |
| `.../sessions/{session_id}/stats` | 保留基础统计，按 D-01、D-03 删除独立摘要和精细结算相关结构。 |
| `.../sessions/{session_id}/runs`、`.../runs/{run_id}` | 保留创建运行、查询状态、结果与必要统计。 |
| `.../runs/{run_id}/events` | 保留 SSE 订阅地址与当前 `after_seq` 重连方式。 |
| `.../runs/{run_id}/cancel`、`.../runs/{run_id}/inputs` | 保留停止、Steer、Follow-up 及显示所需的输入状态，按 D-12 简化，具体语义以框架验证为准。 |
| `.../sessions/{session_id}/tasks`、任务详情 | 保留最新任务清单、步骤及主动续办所需信息，按 D-08 去除完整版本历史与专属机制。 |
| `.../sessions/{session_id}/operations` 及方案详情、`confirm`、`cancel`、`reconcile` | 保留受控业务操作接口、确认及回执语义，不能删减既有授权、条件校验及防重复执行能力。 |
| `.../sessions/{session_id}/compact` | 根据 D-02 删除用户手动上下文压缩；自动上下文处理保留，独立摘要 Run 按 D-01 退役。 |
| `POST /agent-api/v1/query` | 旧同步单轮查询；当前 MES 主界面和现有飞书 Bridge 均走会话 Run。作为待核实调用依赖后退役的候选，非本次额外确认的删除决定。 |

请求/响应采用 Pydantic 模型及 FastAPI 原生校验。保留当前前端依赖的关键字段、HTTP 状态语义和 `error.code`、`error.message` 业务错误结构，统一处理请求校验、身份失败、资源不存在、冲突及服务失败等异常。他人会话的访问继续按现有归属规则拒绝；500 响应不暴露内部异常。

不兼容已删除功能的旧数据结构。简化任务详情等对外字段时，应检查实际调用者，包括现有飞书集成（其任务详情当前依赖版本结构，应根据后续实际使用范围确定必要的兼容调整）。不为旧结构建立长期双版本服务。

## 五、SSE、断线重连与终态同步（H-03）

FastAPI 负责 SSE 传输；应用层负责 Run 的临时可展示事件及持久化状态。沿用每个 Run 有序递增的事件编号，默认只在进程内为活动 Run 保留有界的事件列表；不引入 Redis、额外消息队列或持久化全量文本增量。

| 场景 | 预期处理 |
| --- | --- |
| 正常运行 | Agent 事件追加到临时缓存，SSE 按序发送；完整消息、必要工具记录、任务状态及运行终态写入 SQLite。 |
| 网络短暂断开 | Run 独立于 SSE 连接，继续运行；重连携带最后接收序号，补发仍在缓存中的遗漏事件，随后继续实时订阅，不再次提交问题。 |
| 网页刷新 | 重新验证会话及 Run 归属，恢复持久化消息及可展示的活动 Run 进度；页面已失去原临时文字时，应从可用的起点重建显示，不能仅根据旧游标跳过尚未展示的数据。 |
| 活动 Run 的缓存不足 | 通过现有状态读取能力恢复已持久化的进度，恢复后续订阅；允许部分临时文字无法完整补发，不将其误认为最终答案。 |
| Run 进入终态 | 先持久化完整消息、关键状态及终态，再发送结束通知（沿用 `run_settled` 语义）。前端立即读取最终状态及消息，不能等待 SSE 连接关闭；REST 先发现终态时也走同一最终同步流程。 |
| 服务进程重启 | 内存事件可丢失，原活动 Run 标记为中断终态，不自动续跑。用户可根据已保存的消息、任务和必要结果主动继续。未知业务操作结果必须核实原回执。 |

完整消息和持久化终态是最终事实来源；事件是实时展示和刷新状态的通知。缓存必须在运行结束后适时释放，且释放时机不能使已提交的最终结果不可获取。缓存上限、过期游标处理和准确的事件字段留待 HTTP 契约及框架事件适配验证。总体方案不要求精确回放每个历史文字增量。

## 六、身份、会话归属与业务权限（H-04）

用户继续在 MES 登录，前端现有 Bearer Token 通过请求头传给 Agent。FastAPI 使用统一认证依赖提取凭据，并通过现有 `KTGClient` 调用 MES `/getInfo` 核实身份；不能把请求中的用户 ID、页面信息或模型输出当作授权依据。

- 每次受保护的普通请求和 SSE 订阅、重连均验证真实身份并检查具体会话、Run、任务或操作方案归属。沿用现有本地按 MES 用户、云端体验可按令牌派生范围隔离会话的做法，访客共享演示业务库不等于共享会话。
- 实际业务查询和操作继续基于当前用户权限和对象范围校验；用户确认后才可执行受控 MES 写入，必要时重新核实权限、条件及回执。工具是否对模型可见不得代替实际执行授权。
- Token 仅用于服务端认证及授权 MES 调用，不进入历史消息、模型上下文、事件或持久化记录，也不写入日志。Agent 不另建登录、用户表、角色及权限体系。

## 七、后台运行与会话保留（H-05）

Uvicorn 使用单 Worker，HTTP 与后台 Run 在同一进程的异步事件循环中协作。应用运行层使用 `asyncio.create_task()` 管理后台 Run，保留运行任务引用，使停止等控制能够定位当前 Run，并处理任务完成、异常、停止和资源清理。路由收到请求后触发运行并返回 ID，SSE 只是订阅，不拥有或控制 Run 的生命周期。无需沿用现有自研的 Run 线程池、每请求/每 Run 事件循环和跨线程消息调度。

FastAPI 的 `BackgroundTasks` 适合响应后的简单工作；本项目 Run 需要被其他接口查询、控制并接受运行中输入，因此选择可由应用持续管理的 asyncio 任务。阻塞的 MES 客户端或 SQLite I/O 需要避免长时间占用事件循环（现有 MES 客户端已对部分同步网络调用使用 `asyncio.to_thread`），按后续实际验证作必要适配，不为此建设分布式运行系统。

Session、Run 与上下文仍各有职责：一个 Session 包含多轮 Run；应用按会话读取和保存历史，Pydantic AI 利用原生消息历史与上下文能力处理本轮及必要历史，SQLite 保存完整消息和关键状态。保持多轮对话、任务续办、自动摘要和页面感知；进程重启不自动续跑，旧 Agent 聊天历史不迁移，必要业务回执和幂等记录不随会话重置丢失（D-05、D-09、D-10、D-14）。

Steer、Follow-up 和停止优先采用 Pydantic AI 经验证的原生语义。停止运行不等于撤销已经完成的 MES 操作，也不得因下游调用中断、结果未知而盲目重新执行。

## 八、服务启动和生命周期（H-06）

保留现有启动入口 `uv run --locked python -m ktg_agent.server`，由简短入口调用原生 `uvicorn.run()` 启动 FastAPI；继续使用现有的环境配置入口、服务地址、代理路径、模型及 MES 配置方式。正式演示保持单 Worker，不需要多进程或自动热重载。

FastAPI 的 `lifespan` 统一初始化应用资源、打开独立的 Agent 持久化环境、处理遗留活动 Run 及业务操作状态，并在服务退出时妥善结束或中断活动任务、记录真实状态后释放资源。Uvicorn 负责 HTTP 服务的正常启动和关闭；应用仍需对自己的后台 Run 做有界的退出处理，并对异常退出后遗留状态进行恢复标记。

保留本地启动脚本、云端 Compose 容器及既有同源 Nginx/Node 代理方式的基本结构。SSE 代理仍需正确转发认证请求头、保证不缓冲和合理读取超时。不会因为替换 Python Web 框架而要求改造 Java MES 或引入新的网关体系。

## 九、验证与演示切换原则（H-07）

新服务在独立端口、独立 Agent SQLite 和隔离的业务写入验收环境中验证；正式演示继续运行。优先复用现有有效的行为测试，已确认退役的内部机制及其测试不作为新架构必须通过的旧实现约束。

| 验证范围 | 主要验收效果 |
| --- | --- |
| HTTP 接口 | 使用 FastAPI `TestClient`、依赖替换和生命周期测试验证路由、校验、响应、鉴权、会话隔离、幂等及资源启停。 |
| 实时交互 | 在真实 Uvicorn HTTP 连接和浏览器中验证首段文字实时到达、进度、刷新、断线重连、缓存不足、终态同步、停止、Steer、Follow-up 及服务重启。 |
| Agent 与 MES | 实际模型多轮对话、八项现有业务工具、页面感知、任务清单和主动续办；隔离环境内的受控业务确认、权限、回执和防重复执行。 |

验证通过后，才切换本地及云端演示。保留旧程序/镜像、旧 Agent SQLite 及启动和代理配置作为回退依据。切换或回退前处理、保留必要的已确认业务操作及幂等、回执记录，核实未知结果；不得通过恢复聊天旧库回滚或重做已实际写入 MES 的业务。演示切换后检查健康接口、关键用户交互及错误日志。

## 十、尚需后续验证的细节

七项技术方向已经确认。以下事项取决于 Agent 核心、持久化和接口进一步设计或实际验证，不应为填满文档提前决定：

- FastAPI、Uvicorn、Pydantic 和 Pydantic AI 的兼容版本。FastAPI 官方文档标明原生 SSE 自 0.135.0 引入，实际依赖待验证后锁定。
- 最终 API 模型、各类校验错误状态码兼容方式、精确事件类型与字段、缓存容量及重连回退细节；旧 `/query` 的外部依赖核实。
- Pydantic AI 在 DeepSeek 下的流式事件、运行中输入与停止能力，消息持久化和自动上下文处理的具体实现，以及长任务停止和未知业务结果的处理边界。
- 新 SQLite 数据结构、具体应用模块、测试环境配置及实施步骤。

## 十一、依据

本次源码审查涉及 [HTTP 入口 server.py](../../../ktg_agent_v1/ktg-agent/src/ktg_agent/server.py)、[会话服务](../../../ktg_agent_v1/ktg-agent/src/ktg_agent/sessions/service.py)、[运行协调器](../../../ktg_agent_v1/ktg-agent/src/ktg_agent/sessions/coordinator.py)、[MES 客户端](../../../ktg_agent_v1/ktg-agent/src/ktg_agent/ktg/client.py)、[前端 REST 接口](../../../ktg_agent_v1/ktg-mes-ui/src/api/agent.js)、[前端 SSE 客户端](../../../ktg_agent_v1/ktg-mes-ui/src/api/agentSession.js)、[飞书 Bridge](../../../ktg_agent_v1/integrations/feishu/bridge.py)、[启动脚本](../../../ktg_agent_v1/scripts/start-agent.ps1) 和 [云端发布与结算同步修复记录](../07-云端演示发布.md)。现有 [SSE 测试](../../../ktg_agent_v1/ktg-agent/tests/unit/test_session_sse.py)、[前端会话测试](../../../ktg_agent_v1/ktg-mes-ui/tests/agent-session.test.js) 与 [D1 隔离验收入口](../../../ktg_agent_v1/scripts/tests/d1-isolated-start.ps1) 为后续行为验证提供依据。

官方技术参考：

- FastAPI：[请求体](https://fastapi.tiangolo.com/tutorial/body/)、[响应模型](https://fastapi.tiangolo.com/tutorial/response-model/)、[异常处理](https://fastapi.tiangolo.com/tutorial/handling-errors/)、[SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/)。
- FastAPI：[认证工具](https://fastapi.tiangolo.com/reference/security/#fastapi.security.HTTPBearer)、[依赖注入](https://fastapi.tiangolo.com/tutorial/dependencies/)、[BackgroundTasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)、[lifespan](https://fastapi.tiangolo.com/advanced/events/)、[测试](https://fastapi.tiangolo.com/tutorial/testing/)。
- Uvicorn：[启动方式及配置](https://uvicorn.dev/settings/)；Python：[asyncio 任务](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)。
- Pydantic AI：[消息与会话历史](https://pydantic.dev/docs/ai/core-concepts/message-history/)。
