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
  providers/  Provider 协议与 FakeProvider
  tools/      工具定义、白名单注册表和受控执行器
examples/     无网络可执行示例
tests/        单元与回归测试
docs/         核心契约和阶段交接说明
```

## 阶段 A 边界

- `AgentLoop` 只处理模型/工具循环，不负责会话持久化、身份验证或 HTTP。
- `Agent` 保证单实例同一时刻只有一个活动 run；并发普通 `prompt` 返回 `RUN_BUSY`。
- `abort()` 只阻止尚未开始的后续动作，不回滚已经执行的工具或外部操作。
- 只有完整且最终停止原因为 `tool_call` 的工具调用会执行；截断输出不会执行流中出现的调用。
- 阶段 A 的 `agent_end` 是低层循环结束事件，`Agent` 随后在 `prompt` 的 `finally` 中转为 idle。未来阶段 B 的 `run_settled` 必须等最终状态、消息、事件和必要操作记录持久化后再由 SessionService 发布，三者不能互相替代。

阶段 B 尚需实现会话存储与幂等 admission、服务端身份上下文、HTTP/SSE 与事件回放，以及外部写入结果未知时的持久化和核验流程。详见 [`docs/core-contracts.md`](docs/core-contracts.md#阶段-b-交接清单)。
