# HTTP-02 候选基础

工作目录：E:/ktg_agent/ktg-agent-refactor/ktg-agent。此目录是旧 Python 源码的独立副本；正式演示仍使用 ktg_agent_v1，禁止将启动脚本提前切过来。

## 验证

在本目录执行（不要仅在总目录传 --project 给 pytest）：

~~~powershell
$env:UV_CACHE_DIR="E:/ktg_agent/ktg-agent-refactor/http01-probe/.uv-cache"
uv sync --locked --offline
uv run --locked --offline pytest -q --basetemp=.test-tmp-check
uv run --locked --offline mypy
~~~

基线 Python 3.12.6、uv 0.11.21；FastAPI 0.165.0、Uvicorn 0.54.0、Pydantic 2.14.4。保留 httpx 0.28.1；TestClient 有弃用警告但可用。

## 隔离候选启动

先确认回环端口 18992 未被使用。仅本地假配置，不调用真实模型/MES；数据库必须是独立候选目录的全新文件：

~~~powershell
$env:MODEL_API_KEY="fake-local-only"
$env:MODEL_ID="fake"
$env:MODEL_BASE_URL="http://model.invalid"
$env:KTG_BASE_URL="http://mes.invalid"
$env:AGENT_HOST="127.0.0.1"
$env:AGENT_PORT="18992"
$env:AGENT_SESSION_DB="E:/ktg_agent/ktg-agent-refactor/ktg-agent/http02-local.sqlite"
uv run --locked python -m ktg_agent.http_candidate
~~~

本阶段只有 /health 和原生 OpenAPI 文档，无业务路由。正式 server.py 保留旧实现。候选默认装配仅打开指定数据库并调用旧恢复接口，不创建 QueryService 或 RunCoordinator，不连接模型和 MES。应用导入及 create_app 不读取环境或创建数据库；lifespan 内才读取配置及装配。

## 后续接入

- 从 ktg_agent.http_app 导入 create_app(settings, resource_factory=...)。原生 app.include_router(router) 装配业务路由。
- factory(settings, stack) 为同步资源获取函数，返回后通过 app.state.resources 暴露；使用 Depends(get_resources) 获取。HTTP-03/04 可传入自有资源对象，不要求继承 Resources。
- 获取每个长期资源后立即 stack.callback(close) 或 stack.push_async_callback(aclose)，保证后续初始化异常也释放；后续异步运行关闭应自身实现有界退出。HTTP-04 如需异步初始化，可将 factory 接口扩展为原生异步上下文装配。
- 默认 open_resources 已调用 repository.recover_interrupted_runs() 和 operations.recover_interrupted() 各一次；替换装配时明确恢复责任，不要再调用旧 create_session_service 造成重复恢复/旧调度器装配。
- 当前旧 SQLite 使用操作级上下文连接，无长期关闭方法；默认不虚构 close。没有新增表设计或迁移脚本，仅复用已有仓库初始化。
- PublicError(status, code, message) 只接收经过审查的安全业务消息；不要直接传 str(内部异常)。KTGError 对已知认证/权限码映射 401/403，其余脱敏为 MES_ERROR 502；ProviderError 脱敏 502。具体资源、冲突异常由负责该路由的任务显式转为 PublicError，任意 KeyError/ValueError 保持 500。
- POST 体最多 64 KiB，空体/非法长度为 INVALID_REQUEST 400；非法 JSON 为 INVALID_JSON 400；Pydantic 校验为 VALIDATION_ERROR 400，不返回原输入。未匹配路由保留 HTTP-01 所记录的旧 {"error":"NOT_FOUND"} 404；其他错误使用 error.code/message。405 保留状态，具体业务兼容由后续任务核对。
- Cache-Control 默认 no-store，允许后续原生 SSE 明确覆盖；nosniff 全局保留。Uvicorn 固定 workers=1、reload=False、lifespan=on，关闭访问日志避免查询参数进入日志。
- /health 只在启动完成时返回 200 {"status":"ok"}，未启动为 503。HTTP-06 完整验证后才接入正式 python -m ktg_agent.server 命令。
