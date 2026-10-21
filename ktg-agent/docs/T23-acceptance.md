# T23 真实模型流与预算整包验收记录

执行：Codex，2026-10-20 至 2026-10-21，Windows 本地工作区。状态：**T23 整包验收通过**。本记录只写实际运行的证据；T17–T22 的既有勾选不自动继承为 T23 通过。

## 已执行的证据

| 层与场景 | 证据 | 结果 |
| --- | --- | --- |
| Provider HTTP 字节流 → Core → SQLite → Agent SSE → GET 快照 | `tests/unit/test_t23_http_flow.py` 通过。模型端使用本地真实 HTTP chunked SSE；尾帧由闩锁控制。在尾帧放行前，Agent SSE 已送达“第一段”，GET run 仍为 running；放行后，重连只见剩余事件，模型端请求数为 1，尾帧 7 输入 + 3 输出与 `run_settled`、GET run、重新打开 SQLite 后的 10 token 一致。 | 通过（受控端点） |
| 供应商真实流 | 真实配置中的 OpenAI 兼容端点，模型 `deepseek-flash`。`scripts/t23_real_model_probe.py` 只输出时序/长度/用量，不输出正文或密钥。第二次运行：首个 `TextDelta` 3.345 秒，21 个 delta 至 3.430 秒，usage 尾帧 3.430 秒，完成 3.434 秒；输入 41、输出 223、缓存输入 0 token。首段早于响应结束约 0.089 秒。 | Provider 真实流通过；尚未证明浏览器代理链路 |
| 首次真实模型短上限探针 | 输出上限 64 时返回 usage（输入 41、输出 64），没有可展示正文，Provider 明确报 `MODEL_INVALID_RESPONSE`。随后用 512 上限获得上述正常流。 | 失败记录保留；未当作流通过 |
| 纯文本、工具交错、`length`、空 choices usage、缺 usage、已知 usage 后断流、取消关闭与模型截止 | `test_openai_compatible_provider.py`、`test_agent_loop.py` 覆盖协议及 Core；本次全套测试通过。 | 离线通过 |
| 四类预算、完整工具参数之外零启动、follow_up/steer 共用预算 | `test_execution_limits.py`、`test_agent_lifecycle.py`、`test_session_inputs_http.py`。两个完整调用遇 token 阈值或缺 usage 各有未执行结果，工具启动为 0；未消费输入持久化。 | 离线通过 |
| SSE 重连、重启与事件过期后快照 | 新增贯通测试及 `test_session_sse.py`、`test_session_repository.py`、`test_session_http.py`：断开订阅不重新调度；事件清理后 run 用量来自独立快照；逐调用只累计一次。 | 本地 HTTP/SQLite 通过 |
| 已发出写入的成功、已知失败、未知与取消/截止竞态 | `test_session_execution.py::test_cancel_waits_for_started_write_and_preserves_outcome` 用闩锁逐一覆盖三种结果，实际调用各一次；未知保持 `needs_reconciliation`。`test_run_deadline_after_issued_write_preserves_unknown_reconciliation` 验证截止不覆盖未知。仅使用假写工具，没有真实 MES 写入。 | 受控测试通过 |
| 网页会话及工单展示 | `npm run test:agent-session` 与 `npm run test:workorder` 各三组测试通过。 | 自动测试通过 |
| 真实浏览器代理链路增量 | 2026-10-21，北京时间约 09:40，Chromium 155.0.8059.85；`http://127.0.0.1:81/` 登录后新建会话。浏览器时间轴：第一段临时文本 256771.7 ms，`/agent-api/v1/runs/.../events` 的 Resource Timing `responseEnd` 257822.2 ms，正式消息 257977.1 ms。首段早于 SSE 结束约 1050.5 ms；1 次 SSE、1 条正式答复。run `1203aec4…` 为 completed，`deepseek-flash` 1 次请求，供应商报告 2253 输入 + 190 输出 = 2443 token，`run_settled` 与 SQLite 快照一致。 | 浏览器真实模型链路通过 |
| 浏览器刷新恢复 | 同一 run 刷新后重新打开助手；最终消息文本与用量显示均与刷新前完全相等。只比较长度与相等性，不保存业务正文。 | 通过 |
| 浏览器只读 MES 多工具 | run `e232b477…`：浏览器提问后第 1 轮并发执行 `ktg_search_items(item_code=A-01)`、`ktg_query_stock(item_code=A-01)`、`ktg_list_workorders(product_code=A-01)`，三者均 `succeeded`；第 2 轮模型生成答复。三项结果分别返回 1、49、2 条（库存本页 10 条），无业务写入；模型两次调用合计 7367 token，run completed。同一登录浏览器直接 GET 三个 MES 只读列表接口，HTTP 和业务状态码均 200，总数及分页条数逐项相同。 | 真实只读调用、多轮和 MES 接口交叉核对通过 |
| 浏览器停止窗口 | run `97aa2ffa…` 在临时文本出现后点击“停止后续执行”；页面为“部分文本 · 未完成”“本轮已由用户取消”，正式答案区未出现该消息。持久 run 为 cancelled，`cancel_requested` 后 `run_settled`，模型尾帧未到，`unknown_request_count=1`、`complete=false`。 | 通过；用量未知如实保留 |
| 浏览器预算阈值 | 隔离页面 `http://127.0.0.1:18081/` 代理到独立 Agent `:18001` 和独立 SQLite。run `d761bf4e…` 展示阈值 100 token，真实 `deepseek-flash` 单轮供应商报告 2253 输入 + 176 输出 = 2429 token；run `stop_reason=limit`、`limit_reason=max_total_tokens`，仅 1 次模型请求。 | 通过；真实单轮超额 2329 token，非费用硬上限 |
| 浏览器 SSE 断线续接 | `http://127.0.0.1:28081/` → `:28001` Agent → 本地受控慢流模型 `:28000`。首段 3 字显示后用 `window.stop()` 中断正在读取的 SSE，浏览器后续请求 `after_seq=5`（初次为 0），再释放模型尾帧。最终 6 字，没有重复；受控模型请求数 1，run `e203f5d2…` completed、供应商报告 7 输入 + 3 输出 = 10 token，与持久快照一致。独立复审从 SQLite 核实仅有两条各 3 字的 text delta，最终持久文本恰等于按 seq 拼接的两个 delta。浏览器 `setOffline(true)` 未可靠终止已建立的 SSE；该次未计为断线通过，改用明确的请求中断完成验收。 | 受控慢流浏览器续接通过 |
| 浏览器 follow_up 共用计量 | 在受控慢流的首段可见、尾帧被闩锁阻塞时提交 `follow_up`，随后释放尾帧。run `6cda993b…` 的追加输入持久状态为 consumed，两次模型请求各 10 token，合计输入 14、输出 6、总计 20；页面有两条正式答复、无临时残留。刷新后用量文本完全一致。 | T22 前置浏览器缺口通过 |
| 浏览器终态快照一致性独立复核 | 独立复审对上述 6 个浏览器 run 的 SQLite `run_settled` 与 `runs` 行逐一比较 `budget`、`usage`、`status`、`stop_reason`，均相等；未输出正文或凭据。 | 通过 |
| 独立 agent 复审 | 2026-10-21 独立审查 Provider/Core/Session/UI 实现、T17–T22 交接、六个浏览器 run 快照和 T23 受控 HTTP 测试；在 `ktg-agent` 设置工作区 `UV_CACHE_DIR` 后执行 `uv run --locked pytest -q tests/unit/test_t23_http_flow.py tests/unit/test_execution_limits.py tests/unit/test_session_execution.py`，结果 **38 passed in 4.61s**。默认沙箱直接用 `.venv/Scripts/python.exe -m pytest` 曾停滞并被中断，未计入通过。发现只读工具数、模型写工具边界及断线复验步骤的文档误述，均已修正。审查未发现阻碍关闭 T23 的实质问题。完整 217 项回归及真实浏览器操作依据本次主执行记录，独立审查未重复运行。 | 通过，范围与局限已记明 |

## 自动命令与环境

- 2026-10-20 09:41–09:52（北京时间），CPython 3.12.6、uv 0.11.21：`uv run --locked pytest -q` 最终 **217 passed in 24.12s**；`uv run --locked mypy` 为 **Success: no issues found in 51 source files**。uv 缓存改设为工作区 `.runtime/uv-cache`；Windows 沙箱内 asyncio 测试停滞，正常本机权限下运行。
- 同日 Node.js 16.20.2、npm 8.19.4：`npm run test:agent-session`、`npm run test:workorder` 通过；`npm run build:stage` 成功，有 2 条现有资源体积警告。系统 Node 22 的构建因旧版 webpack/OpenSSL 不兼容失败，按项目 `use-node16.ps1` 重跑成功。
- 首次加入贯通测试后的全套回归与 Java/前端并发编译时，已有 `test_read_timeout_and_cancellation_close_connection` 的 2 秒本地连接等待超时：**1 failed, 216 passed**。服务稳定后该用例与新增测试一起 **2 passed**；全套复跑为上述 **217 passed**。不把第一次失败记作通过。
- MySQL、Redis 容器健康；本地迁移脚本完成。Java 后端 `:8080/captchaImage`、常规前端 `:81/`、常规 Agent `:8001/health` 均为 HTTP 200；`:81/agent-api/v1/runs/nonexistent` 经前端代理返回 Agent 401，证明代理目标可达，但不证明无整包缓冲。

## 浏览器复验步骤

常规服务地址 `http://localhost:81/`，模型 Agent `:8001`，MES 后端 `:8080`。本次另曾启动独立预算环境 `http://localhost:18081/`，代理到 `:18001` Agent；该 Agent 使用独立 `.runtime/t23-acceptance-sessions.sqlite3`、`AGENT_MAX_TOTAL_TOKENS=100`，库存写入执行开关关闭。受控慢流页面使用 `http://localhost:28081/`，Agent `:28001`，模型 `:28000`，以及独立 `.runtime/t23-browser-reconnect.sqlite3`。三套页面都使用当前 MES 登录账号，不需要把令牌复制出来。以下步骤可供再次复验，上一节是本次已完成结果；隔离验收服务需按上述配置另行启动。

1. 在 `http://localhost:81/` 登录有产品、库存、工单只读权限的测试账号。打开开发者工具 Network，勾选 Preserve log，筛选 `agent-api`。不要导出含 Authorization 的 HAR，也不要分享业务正文截图。
2. 打开右下角“智能查询”，新建会话，发送一个不涉及业务的短文本问题。观察 `POST /agent-api/v1/sessions/{sid}/runs` 为 202，随后 `GET /agent-api/v1/runs/{rid}/events?after_seq=0` 为 `text/event-stream`。记录第一段文字出现在页面的时间和 SSE 请求结束时间；第一段必须更早出现，最终文本不能重复。只记录时间、事件类型、seq、用量和状态。
3. 用本地受控慢流启动一个 run，等首段出现而模型尾帧仍被闩锁阻塞时，执行 `window.stop()` 中断浏览器当前 SSE 请求；确认自动续接的 events URL 带非零 `after_seq`，再释放尾帧。页面最终片段不能重复，GET run 的 `model_requests` 数量和 token 总数不能因重连增加。单纯切换 Offline/Online 在本次 Chromium 中未终止已建立的 SSE，不能将其当作通过证据。
4. 刷新页面并重新打开该会话。最终消息、逐调用用量、已知总量、完整性与刷新前一致。停止一次运行，确认部分文本显示未完成且不会进入完成答案；停止后不自动续跑。对于快速完成的模型，请使用受控慢流复验停止窗口。
5. 用准确且可公开的测试数据分别询问产品、库存、工单只读信息；在 Agent 事件中确认工具调用与后续模型轮次，在 MES 页面或只读接口核对结果。记录工具名称、轮数、成功/失败及权限结果，不记录业务敏感正文。不得点击工单或库存确认按钮。
6. 在 `http://localhost:18081/` 登录同一测试账号，发送纯文本问题。该隔离环境的 run 预算应显示 `max_total_tokens=100`；若供应商完整响应的真实已知 token 达到阈值，run 应显示 `limit_reason=max_total_tokens`，保留实际超额数值，不产生后续模型或工具启动。若本次用量低于 100，换用稍长但不涉及业务的提示重新运行，不把未达到阈值记作失败。此项与常规环境使用不同会话库。
7. 用开发者工具核对最后 `run_settled` 与 GET run 的 `budget`、`usage`、`model_requests` 和停止原因；令牌只留在浏览器，不复制到验收记录。记录浏览器版本、北京时间、匿名 run ID、关键截图或无敏感信息的时间表。

## 未完成、边界与分层核对

- **前置交接核对**：2026-10-21 已找到并阅读 T17–T22 六份原交付任务文件。T17–T21 均标记已完成且各有测试、类型检查及关键场景记录；T22 原标记代码完成，两个浏览器勾选项留待 T23。本次已覆盖首段、停止、重连、刷新、真实模型、多轮及 follow_up 用量；T22 的页面证据可据此更新。
- **验收边界**：只读 MES 已按相同筛选参数对三项接口逐一比对数量，不导出业务正文。独立审查没有重复执行全套 217 项或真实浏览器交互，按上表列明的证据范围复核；T23 整包可以关闭，不代表生产环境上线验收。
- `max_total_tokens` 依据供应商已报告用量停止**后续**工作，单轮可超额；不提供金额或严格总量保证。真实模型 64-token 探针失败说明过小的单次输出上限可能没有正文。
- Provider 处理供应商协议和真实流；AgentLoop 累计调用用量、决定四类预算及工具调度；Session 层持久化、所有权、SSE 与恢复；页面只展示快照和事件。符合本地 pi 分层目标。没有引入非流式回退、自动模型重试、伪造 usage、费用估算或通用容错框架。
- 长会话压缩、业务恢复与独立确认回执的正式环境验收、价格表/金额限制、多进程协调属后续包；本轮受控假写入不代表真实 MES 写入验收。
