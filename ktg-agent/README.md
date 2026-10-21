# KTG Agent

KTG Agent 的 Python 核心包。除阶段 A 的离线核心外，当前已提供 OpenAI 兼容模型适配器、本地 HTTP 查询入口，以及按当前 KTG 用户身份执行的产品、库存和工单三个只读查询工具。

核心接口、事件语义和阶段 B 交接项见 [`docs/core-contracts.md`](docs/core-contracts.md)。

## 环境要求

- Python 3.12（本阶段验证版本：3.12.6）
- uv 0.11.21

运行时依赖为 jsonschema 4.25.1。`uv.lock` 同时固定 pytest 9.0.3、mypy 1.18.2、types-jsonschema 4.26.0.20260518 和 hatchling 1.27.0。Node.js、Java、KTG 服务和网络访问都不是阶段 A 验证的依赖。

## 从干净环境开始

克隆仓库后进入 `ktg-agent` 目录。以下命令创建本地 `.venv`，并严格使用锁文件安装项目和开发依赖：

```powershell
uv sync --locked
```

只安装运行时依赖时使用：

```powershell
uv sync --locked --no-dev
```

验证包可导入：

```powershell
uv run --locked python -c "import ktg_agent; print(ktg_agent.__version__)"
```

预期输出为 `0.1.0`。

## 离线示例

运行 FakeProvider、假查询工具和 AgentLoop 的完整两轮闭环：

```powershell
uv run --locked python examples/fake_agent.py
```

预期输出：

```text
final: 仓库 WH-A 的可用库存为 8。
provider_requests: 2
tool_calls: 1
events: agent_start -> turn_start -> message_start -> message_end -> tool_execution_start -> tool_execution_end -> message_start -> message_end -> turn_end -> turn_start -> message_start -> message_update -> message_end -> turn_end -> agent_end
```

脚本完全由内存中的固定输入驱动，不读取环境变量，也不发起网络请求。`FakeProvider` 的每个 `ScriptedTurn` 必须以一个 `ResponseCompleted` 或 `ProviderError` 结束；每次 Provider 调用消费一轮，并可通过 `provider.requests` 检查 Agent 实际发送的上下文。

## 验证命令

```powershell
uv run --locked pytest
uv run --locked mypy
```

`pytest` 覆盖消息序列化与模型投影、FakeProvider 脚本、工具白名单与 schema 校验、工具调度和预算、事件顺序、取消、steer/follow-up 队列、监听器异常以及离线示例。`mypy` 对 `src` 和 `tests` 运行严格类型检查。

2026-10-09 在新建的独立目录和虚拟环境中使用锁文件离线复验：包版本为 `0.1.0`，示例产生 2 次 Provider 请求和 1 次工具调用，测试结果为 `92 passed`，类型检查结果为 `Success: no issues found in 33 source files`。

2026-10-16 本工作区执行 `uv run --locked pytest -q`：`132 passed`；执行 `uv run --locked mypy`：`Success: no issues found in 46 source files`。其中临时 SQLite 与 FakeProvider 的 HTTP 测试覆盖两轮会话、重启读回、遗留 run 不重放及第三轮历史输入；这不是外部模型或 MES 的联调结果。

2026-10-18 T16 离线整包验收：`uv run --locked pytest -q` 为 `164 passed`，`uv run --locked mypy` 为 `Success: no issues found in 50 source files`；前端 `npm run test:agent-session` 和 `npm run test:workorder` 均通过。含可控 HTTP 输入/取消竞争测试；真实浏览器、模型及只读 MES 仍待环境验收，详细矩阵记录在工作区文档 `docs/ktg_agent/任务/T16-运行控制与写入竞态整包验收.md`。

## 真实模型与网页查询

下面的代码与启动入口已经具备，但单元测试和 `/health` 只能证明本地链路可启动，不能证明外部模型或当前用户下的三个业务接口已经实际查询成功。只有配置真实模型、网页登录，并从悬浮窗分别完成产品、库存和工单查询后，才算端到端验收通过。

在工作区根目录 `.env` 中配置下列服务端变量，真实密钥不要写入 `.env.example` 或提交到 Git：

```dotenv
MODEL_API_KEY=...
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_ID=...
KTG_BASE_URL=http://127.0.0.1:8080
AGENT_HOST=127.0.0.1
AGENT_PORT=8001
# Optional positive integer. Stops later work after reported run tokens reach it.
# AGENT_MAX_TOTAL_TOKENS=5000
# AGENT_CONTEXT_WINDOW=32768
# AGENT_CONTEXT_RESERVE_TOKENS=1024
# AGENT_CONTEXT_THRESHOLD_RATIO=0.8
# AGENT_CONTEXT_RETAIN_RATIO=0.16
# AGENT_CONTEXT_SUMMARY_MAX_TOKENS=1024
# AGENT_SPILL_MAX_INLINE_TOKENS=4096
# AGENT_SPILL_DIR=E:/ktg_agent/ktg_agent_v1/.runtime/agent-spill
```

`AGENT_CONTEXT_WINDOW` 默认不设置，此时窗口估算、裁切和压缩关闭；设置后须为正整数。`AGENT_CONTEXT_RESERVE_TOKENS` 默认取模型输出上限（当前 1024），须小于窗口。阈值比例默认 0.8，近期保留比例默认 0.16，摘要输出上限默认 1024。`AGENT_SPILL_MAX_INLINE_TOKENS` 默认不设置，即工具结果 spill 关闭；启用时须同时给 `AGENT_SPILL_DIR` 一个绝对目录路径。非法配置会在启动时失败。上下文 token 是估算压力，不等于供应商报告的真实用量；摘要是派生且不可信的上下文，业务事实仍须用当前用户权限重新查询。

S3-08 库存操作默认关闭。启用前，先确认 Java 侧 S3-05～07 的执行与回执接口已经部署，配置可持久保存的绝对路径，并按动作分别开启：

```dotenv
AGENT_INVENTORY_DB=E:/ktg_agent/ktg_agent_v1/.runtime/inventory-operations.sqlite3
AGENT_ENABLE_ITEM_RECEIPT=true
AGENT_ENABLE_PRODUCTION_ISSUE=true
```

模型只获得两类单据的只读预览工具，参数限准确编号或 ID。确认由网页发送 `operation_id` 到 `POST /agent-api/v1/inventory-operations/confirm`；刷新或断线后通过 `GET /agent-api/v1/inventory-operations/{operation_id}` 查询。两条链路都使用当前登录用户身份。预览和操作绑定保存在 SQLite 文件中，需随服务备份和保留；Java 持久回执是执行成功的判据。没有真实业务单据与权限账号时，仅代表假 KTG 契约验证通过。

会话接口使用独立的持久化 SQLite 文件。启动 Agent 前设置绝对路径，且不能指向 `AGENT_INVENTORY_DB`：

```dotenv
AGENT_SESSION_DB=E:/ktg_agent/ktg_agent_v1/.runtime/agent-sessions.sqlite3
```

启动时会创建或打开该库，并在对外服务前收束上次进程留下的活动 run：`queued` 变为 `cancelled`，`running/cancelling` 变为 `needs_reconciliation`。`GET /runs/{rid}` 返回 `SERVICE_RESTARTED` 原因和摘要，持久事件以 `run_settled` 收尾；服务绝不自动重放模型或工具调用。已经完成的消息和事件保留，用户可继续新一轮。`needs_reconciliation` 表示外部写入结果可能未知，必须先核验业务回执。

当前 HTTP 会话入口为 `POST/GET /agent-api/v1/sessions`、`GET /agent-api/v1/sessions/{sid}`、`GET /agent-api/v1/sessions/{sid}/messages`、`GET /agent-api/v1/sessions/{sid}/stats`、`POST /agent-api/v1/sessions/{sid}/compact`、`POST /agent-api/v1/sessions/{sid}/runs`、`GET /agent-api/v1/runs/{rid}`、`GET /agent-api/v1/runs/{rid}/events?after_seq=N`、`POST /agent-api/v1/runs/{rid}/cancel`、`POST/GET /agent-api/v1/runs/{rid}/inputs`。请求均使用请求头中的当前 KTG Bearer token，不能把 token 放进 URL；创建 run 需提供 `request_id` 和 `message`。服务端保存最终消息和事件，并把已完成轮次作为下一轮的模型历史。相同用户和会话中，相同 `request_id` 与相同请求只返回已有 run；不同请求返回 409。同一会话同时只能有一个活动 run。现有 `/query` 仍是独立的单次查询入口。

取消和追加输入在会话服务校验身份与所有权，由仓储持久准入，协调器投递至运行中的 Agent。`cancel` 已接受返回 202，run 已终结的新取消返回 200 `already_settled`；取消只阻止后续动作，已发出的写入继续以真实结果收尾。`inputs` 的 `mode` 为 `follow_up` 或 `steer`，只在运行中接收；未就绪或已关闭接收门返回 409。`GET /runs/{rid}/inputs` 可查询 `queued`、`consumed`、`not_consumed` 及未消费原因，进程重启不自动重放。控制事件与核心事件使用同一持久 seq，`run_settled` 为最后事件。当前网页悬浮窗已接入停止、两种追加输入及输入状态恢复；停止、真实模型流、只读 MES 查询的浏览器验收见 T23 记录。工单和库存确认是独立 HTTP 路径，不受聊天 run 取消自动撤销。

事件入口返回 `text/event-stream`，默认 `after_seq=0`，先读取持久事件，之后每 0.5 秒检查新事件；SSE `id` 是业务 seq，`event` 是事件类型，JSON 信封按多行 `data:` 发送。15 秒无业务事件时发送 `: heartbeat`，不会占用 seq。`run_settled` 送达后连接结束。历史确实被清理时返回 410 `EVENT_HISTORY_EXPIRED`，应改读 run 快照和最终消息。并发订阅上限为 32，超限返回 503 `SSE_CAPACITY_EXCEEDED`。断开订阅不会取消 run。

创建 run 时可附带 `page_context`，例如 `{"route_name":"WmStock","filters":{"itemCode":"A"}}`。服务端只接受白名单字段，把它作为**本轮**定位提示交给模型；下一轮不会把旧页面状态当作当前状态。页面内容不可信，产品、库存和工单事实仍须通过当前用户权限下的工具查询。当前 Vue 助手已接入会话接口和页面上下文采集。

首次打开空文件时建立会话、运行、消息、事件、控制命令、`model_requests` 及 `context_summaries` 表，SQLite `user_version` 为 5；旧版 v1/v2/v3/v4 库会升级，未知已有表或更新版本会被拒绝。升级前备份数据库。备份时先停止 Agent，再复制 `AGENT_SESSION_DB` 指向的文件；若启用了库存操作，也复制 `AGENT_INVENTORY_DB` 指向的文件。分别保留两份文件、记录备份时间及对应部署版本，恢复时先停止 Agent、还原文件再启动，不要把两个数据库互换。若必须在线备份，使用 SQLite backup API 获取一致快照，不要在写入期间直接复制数据库文件。

读取详细回执还要求当前页面账号同时具备对应单据的 `query` 权限与库存查询权限（现有菜单 `mes:wm:wmstock:list`，或 `mes:wm:wmstock:query`）；仅有执行编辑权限不足以核实结果。S3-07 与 S3-08 已在独立测试库完成验收，正式环境仍需按环境配置并保持动作开关受控。

加载根目录环境变量后启动查询服务：

```powershell
. ..\use-local-secrets.ps1
uv run --locked python -m ktg_agent.server
```

服务提供 `GET /health` 和 `POST /agent-api/v1/query`。查询请求必须携带当前网页用户的 `Authorization: Bearer <KTG token>`；服务先调用 KTG `/getInfo` 验证身份。事实查询使用以下四个固定只读工具；模型还可使用工单草稿预览工具，库存预览工具按开关启用：

- `ktg_count_products` → `GET /mes/md/mditem/list`（只统计产品）
- `ktg_search_items` → `GET /mes/md/mditem/list`
- `ktg_query_stock` → `GET /mes/wm/wmstock/list`
- `ktg_list_workorders` → `GET /mes/pro/workorder/list`

模型 API Key 只保留在 Python 服务进程中，不发送到浏览器或 KTG。模型不能指定 KTG 地址、HTTP 方法或任意接口路径。

## 项目结构

```text
src/ktg_agent/
  core/       消息、生命周期事件、AgentLoop、Agent 和输入队列
  providers/  Provider 协议、FakeProvider 和 OpenAI 兼容适配器
  tools/      工具定义、白名单注册表和受控执行器
  ktg/        带身份验证的 KTG 客户端和四个固定只读查询工具
  service.py  无状态查询服务，负责身份验证与 Agent 组装
  sessions/  会话存储、身份隔离、运行调度与重启收束
  server.py   本地 HTTP 入口、启动配置和会话装配
examples/     无网络可执行示例
tests/        单元与回归测试
docs/         核心契约和阶段交接说明
```

## 阶段 A 边界

- `AgentLoop` 只处理模型/工具循环，不负责会话持久化、身份验证或 HTTP。
- `Agent` 保证单实例同一时刻只有一个活动 run；并发普通 `prompt` 返回 `RUN_BUSY`。
- `abort()` 只阻止尚未开始的后续动作，不回滚已经执行的工具或外部操作。
- 只有完整且最终停止原因为 `tool_call` 的工具调用会执行；截断输出不会执行流中出现的调用。
- 阶段 A 的 `agent_end` 是低层循环结束事件；会话运行的 `run_settled` 在最终状态和消息落库时一并提交。两者不能互相替代。

会话接口已实现持久化、用户隔离、幂等准入、多轮历史、重启收束、SSE 回放续接及 inputs/cancel 控制。真实模型使用 HTTP SSE 增量传输；每次请求和 run 的供应商用量、完整性及生效预算可从 `GET /runs/{rid}` 查询，`run_settled` 携带同一快照。`AGENT_MAX_TOTAL_TOKENS` 默认关闭；启用后根据已收到的真实用量停止后续工作，单轮可能越过阈值，这不是费用硬上限。缺失用量保留 unknown，不记为零。`/query` 共享核心预算政策，但不创建持久 run。写工具不暴露给模型；网页的独立确认接口可按既有授权和配置执行工单草稿或库存写入。

2026-10-20 至 2026-10-21 T23 本机验收记录见 [`docs/T23-acceptance.md`](docs/T23-acceptance.md)：受控 HTTP 跨层流、真实模型短探针、浏览器代理增量与断线续接、登录后只读 MES 多工具、follow_up、刷新、停止及预算阈值均已实测；T17–T22 原交付文件和独立复审已完成。
