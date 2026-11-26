# KTG Agent 候选工程

Pydantic AI / FastAPI 框架化重构候选，面向 MES 生产主管演示。当前八项业务工具及业务权限、确认、回执、幂等规则保持原口径。正式工程与数据库仍独立运行。

## 候选配置及启动

只使用独立目录和经确认的隔离 MES。Windows PowerShell 示例：

```powershell
$env:MODEL_API_KEY = '<隔离模型凭据>'
$env:MODEL_ID = 'deepseek-flash'
$env:MODEL_BASE_URL = '<DeepSeek API 地址>'
$env:KTG_BASE_URL = '<明确隔离 MES 地址>'
$env:AGENT_HOST = '127.0.0.1'
$env:AGENT_PORT = '18991'
$env:AGENT_SESSION_DB = 'E:/ktg_agent/ktg-agent-refactor/.runtime/agent-sqlite-candidate.sqlite3'
uv sync --locked
uv run --locked python -m ktg_agent.server
```

候选启动在 readiness 前完成独立 SQLite、官方 StepStore 和 Planning 初始化及中断状态恢复。正常运行使用原生核心。候选已由 `ktg_agent.server` 统一启动 FastAPI/Uvicorn，单 Worker，禁用 reload；`http_candidate` 仅保留启动实现，不再作为独立命令。候选不得指向 ktg_agent_v1 的演示库。本次候选入口接管不改变正在运行的正式本地或云端演示，实际服务切换需要另行批准。

## 开发验证

```powershell
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m mypy src tests
uv lock --check --offline --python .venv/Scripts/python.exe
```

离线测试使用真实安装的 Pydantic AI TestModel/FunctionModel、临时 SQLite 与 MES 替身。真实 MES 写入只能在明确授权、隔离的验收环境进行。新会话库不迁移旧聊天；必要历史业务记录由显式只读导入旧库副本保留，不能重放未知操作。

## 结构

- sessions：统一 SQLite、正式原生会话历史、Run 状态和官方 Planning。
- core：Pydantic AI 原生执行、控制、StepPersistence 和恢复。
- context：Pydantic AI 原生自动摘要与原始消息归档。
- tools：八项 MES 业务工具的静态目录、权限过滤和业务参数校验；原生工具集负责模型工具选择及调度。
- operations：确定性业务方案、确认、执行与回执核实。
- http_app / http_runs / http_rest / http_sse：FastAPI 装配、Run 受理、会话 DTO 和实时传输。

旧的自研 Agent 循环、消息协议、模型适配器、工具搜索与加载、独立摘要、结果落盘、任务专属预算和输入队列已从候选运行源码移除。旧环境里的结果落盘、工具激活上限、独立摘要输出上限与预留上下文 Token 配置不再被候选服务消费。

详细设计与验收记录见 `docs/Agent建设工程/框架化重构详细设计/SQLite持久化层实施/`（工作区根目录）。
