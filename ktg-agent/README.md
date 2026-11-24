# KTG Agent

KTG Agent 的 Python 核心包，提供模型事件流、工具发现及受控执行、会话与任务持久化、上下文管理、运行限额和 HTTP/SSE 服务。

2026-11-24：旧业务工具已进入源码移除阶段，预删除 Git 检查点为 `80122c5`。现阶段仅保留产品总数及分仓现存量两项只读业务工具，其他生产能力须按新的 MES 主线另行建设。旧业务写入、探索、设备、维修、点检、工装等工具不能通过环境开关重新启用。历史部署和业务证据以对应 Git 版本与项目文档为准；MES 原生页面与后端不受这次 Agent 源码清理影响。

## 环境和开发验证

- Python 3.12、uv 0.11.21。
- 锁文件管理运行时 `httpx`、`jsonschema` 及开发依赖；FakeProvider 离线测试不调用真实模型或 MES。

```powershell
uv sync --locked
uv run --locked python -c "import ktg_agent; print(ktg_agent.__version__)"
uv run --locked python examples/fake_agent.py
uv run --locked pytest -q
uv run --locked mypy
```

核心事件协议和接口见 [`docs/core-contracts.md`](docs/core-contracts.md)。历史阶段的验收记录不代表现行新工具范围。

## 启动与隔离

在 `ktg_agent_v1/.env` 中配置服务端变量（真实密钥不进入 Git）：

```dotenv
MODEL_API_KEY=...
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_ID=...
KTG_BASE_URL=http://127.0.0.1:8080
AGENT_HOST=127.0.0.1
AGENT_PORT=8001
AGENT_SESSION_DB=E:/ktg_agent/ktg_agent_v1/.runtime/agent-readonly-sessions.sqlite3
```

从项目根目录启动 `scripts/start-agent.ps1`，或在本目录加载环境变量后运行 `uv run --locked python -m ktg_agent.server`。Agent 的 `KTG_BASE_URL` 必须明确对应希望访问的 MES 后端，正式演示环境另需独立状态文件和同源代理；不要将旧演示环境会话文件与新环境混用。模型凭据始终由 Python 服务持有，不进入浏览器。

可选运行总 token、模型轮次、工具数和上下文窗口配置遵循 `server.py` 的 Settings；未启用窗口时不作上下文裁切和压缩。所有口径必须按当前授权重新读事实，历史摘要或旧工具结果不能视作实时业务状态。

## 当前业务工具

| 工具 | 权威来源 | 权限 |
| --- | --- | --- |
| `ktg_count_products` | `GET /mes/md/mditem/list`，仅 PRODUCT 的真实总数 | `mes:md:mditem:list` |
| `ktg_query_stock_summary` | `GET /mes/wm/wmstock/summary`，按物料和仓库/单位的现存数量 | `mes:wm:wmstock:list` |

分仓**现存量**不是本工单可消耗量、预留量或齐套结论。没有匹配的库存记录时明确说明，不编造零数量。其他生产事实尚未提供 Agent 工具，不得用历史输出推断。

`search_tools`/`load_tools` 为目录发现与运行级加载；`create_task`/`update_task` 为 Agent 会话内部任务清单协议，不是 MES 工单写入。业务字典只加载上述两项；当前权限及运行时校验仍适用。配置旧业务工具开关不会恢复已删除的代码。已存在的历史会话应保持只读归档，不能恢复旧确认操作。

## HTTP 与会话

服务提供 `GET /health`、`GET /agent-api/v1/tools` 和 `POST /agent-api/v1/query`。业务请求必须使用当前 MES 用户的 `Authorization: Bearer <KTG token>`；用户身份与权限由真实 `/getInfo` 核对。模型不能指定 KTG 地址或任意接口。

会话入口包括 `POST/GET /agent-api/v1/sessions`、会话详情/消息/统计/压缩、`POST /agent-api/v1/sessions/{sid}/runs`、run 状态/事件/取消/输入及任务接口。SSE `after_seq` 为已收事件序号，断线续接不重放用户请求；事件历史不可用时读 run 快照和会话消息。持久化会话保持用户隔离和请求幂等，进程重启不自动重放不确定业务操作。旧业务专用确认和探索 HTTP 路由不再开放。

## 项目结构

```text
src/ktg_agent/
  core/       通用消息、生命周期、AgentLoop 和运行预算
  providers/  假模型与 OpenAI 兼容适配器
  context/    上下文窗口、摘要、计量和结果收纳
  tools/      定义、目录、运行时发现和权限执行
  ktg/        MES 客户端及两项保留业务查询
  sessions/   会话/任务仓储，运行调度及恢复
  service.py  组件装配与授权查询
  server.py   HTTP/SSE 服务和启动配置
```
