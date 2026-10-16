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
```

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

当前 HTTP 会话入口为 `POST/GET /agent-api/v1/sessions`、`GET /agent-api/v1/sessions/{sid}`、`GET /agent-api/v1/sessions/{sid}/messages`、`POST /agent-api/v1/sessions/{sid}/runs` 和 `GET /agent-api/v1/runs/{rid}`。请求均使用当前 KTG Bearer token；创建 run 需提供 `request_id` 和 `message`。服务端保存最终消息和事件，并把已完成轮次作为下一轮的模型历史。相同用户和会话中，相同 `request_id` 与相同请求只返回已有 run；不同请求返回 409。同一会话同时只能有一个活动 run。现有 `/query` 仍是独立的单次查询入口。

首次打开空文件时建立 `sessions`、`runs`、`messages`、`events` 和索引，SQLite `user_version` 为 1；未知已有表或更新版本会被拒绝。升级前备份数据库。备份时先停止 Agent，再复制 `AGENT_SESSION_DB` 指向的文件；若启用了库存操作，也复制 `AGENT_INVENTORY_DB` 指向的文件。分别保留两份文件、记录备份时间及对应部署版本，恢复时先停止 Agent、还原文件再启动，不要把两个数据库互换。若必须在线备份，使用 SQLite backup API 获取一致快照，不要在写入期间直接复制数据库文件。

读取详细回执还要求当前页面账号同时具备对应单据的 `query` 权限与库存查询权限（现有菜单 `mes:wm:wmstock:list`，或 `mes:wm:wmstock:query`）；仅有执行编辑权限不足以核实结果。S3-07 与 S3-08 已在独立测试库完成验收，正式环境仍需按环境配置并保持动作开关受控。

加载根目录环境变量后启动查询服务：

```powershell
. ..\use-local-secrets.ps1
uv run --locked python -m ktg_agent.server
```

服务提供 `GET /health` 和 `POST /agent-api/v1/query`。查询请求必须携带当前网页用户的 `Authorization: Bearer <KTG token>`；服务先调用 KTG `/getInfo` 验证身份，然后只允许模型使用以下三个固定只读工具：

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
  ktg/        带身份验证的 KTG 客户端和三个固定只读查询工具
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

会话接口已实现持久化、用户隔离、幂等准入、多轮历史和重启收束；仍未提供 SSE、事件回放 HTTP 入口、inputs/cancel 接口以及会话运行中的业务写工具。这里只完成离线 FakeProvider 和 HTTP 验证，真实模型及 MES 联调需要另行验收。详见 [`docs/core-contracts.md`](docs/core-contracts.md#阶段-b-交接清单)。
