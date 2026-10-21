# T24–T29 上下文整包验收记录

执行：Codex，2026-10-21，Windows 本地工作区。状态：**代码、离线验收与浏览器主路径通过**（当晚审查修复后已全量重跑自动测试；真实模型与浏览器未重跑）。本记录只写本次实际运行及已存在、随本次全套回归执行的测试证据；不含业务正文、访问令牌或模型回答。

## 已执行的证据

| 场景 | 证据与判定 |
| --- | --- |
| stats 字段、所有权与关闭语义 | `tests/unit/test_session_stats_http.py` 使用真实临时 SQLite 与本地 HTTP，验证消息/run/压缩数、按 kind 的供应商已知用量、估算来源、未配置时 `null`、401/404/400。通过（确定性）。 |
| 长会话 ≥3× 窗口 | `scripts/t29_real_context_probe.py` 在临时 SQLite 中准备 15648 估算 token 的历史（窗口 4096，约 3.82×），真实 `deepseek-flash` 用户 run `09697562…` 为 completed；最终 run 上下文估算 1471 ≤ 阈值 3276，实际保留消息数 2，供应商已知输入 1915、输出 12、合计 1927，未知请求 0，耗时 2.844 秒。既有历史为探针构造的已完成 run；本次只实际调用模型执行最后一轮。浏览器中另完成 9 轮真实用户 run，含一次成功压缩。 |
| 超大工具结果 | `tests/unit/test_tool_spill.py` 的落盘、首尾保留、原消息持久化及失败回退用例；`test_context_window.py` 的装配 spill 计数用例。本次全套回归通过（确定性）。 |
| 摘要后重启 | `test_context_assembly.py::test_assembly_uses_summary_and_canonical_tail_after_restart` 重建仓储后逐条比较；`test_context_summaries.py` 验证 active/superseded 持久化和清理后保留。本次全套回归通过（确定性）。 |
| 摘要后断线/刷新 | 前端 `npm run test:agent-session` 通过状态恢复与 SSE `after_seq` 续接自动测试。浏览器中摘要生成后刷新，重新打开助手，窗口 8192、阈值 6553、估算 2947、摘要前缀 `ae2c6515`、覆盖 2–10、spill 0 均与刷新前一致。又在一轮真实模型 run 显示运行中时刷新，恢复后同一轮完成，用量 4650；SQLite 中该 run 只有 1 次模型请求，事件序号 1–839 共 839 条且无重复。浏览器未单独抓取 Network 中的 `after_seq` 报文；该字段由自动测试覆盖。 |
| 取消压缩 | `test_context_compaction.py::test_cancel_compaction_records_failure_without_retry` 验证取消终态、失败事件和无重试。本次全套回归通过（确定性）。 |
| 摘要不更小 | `test_context_compaction.py::test_larger_summary_fails_and_preflight_user_run_continues` 验证 `SUMMARY_NOT_SMALLER`、`stage=validate` 和用户 run 裁切继续；`test_commit_rejects_not_smaller_summary_as_commit_stage` 覆盖 `commit_summary` 兜底拒绝时的 `stage=commit`。本次全套回归通过（确定性）。 |
| 未配置窗口 | `test_context_settings.py`、`test_context_window.py::test_unconfigured_loop_preserves_projection_and_events`，以及关闭状态 stats HTTP 测试；全套 328 项通过。原基线 217 项包含在全套中，未单独运行旧版本（确定性）。 |
| 真实模型 | 2026-10-21，`scripts/t23_real_model_probe.py` 单次真实流：首段 2.896 秒，最后一段 3.046 秒，完成 3.052 秒；输入 41、输出 166、缓存输入 0。另有上述长历史真实用户 run。只保留时序、长度、用量和匿名 run ID。 |
| 浏览器 | 21:18 在独立端口 18081/18001 与隔离 SQLite 中登录并新建会话；21:18–21:20 连续提交不含业务内容的合成文本，用户 run 完成；21:20 第一次预检压缩因摘要输出达到 256 token 测试上限而失败，用户 run 按窗口裁切后完成；提高隔离环境摘要上限至 1024 后，21:25 再次提交触发压缩，`compaction` run completed、摘要 active，源估算 5090 → 摘要估算 163，页面压力 7775 → 2947；随后刷新保持上述状态及本轮已知用量 3445。再做运行中刷新，页面恢复同一轮完成结果。**浏览器主路径通过**。 |

## 公开 JSON 样例与前端字段

`GET /agent-api/v1/sessions/{sid}/stats` 的结构为：

```json
{
  "session_id": "匿名会话", "message_count": 1,
  "messages_by_role": {"user": 1},
  "run_count": 2, "runs_by_status": {"completed": 1, "failed": 1},
  "runs_by_kind": {"user": 1, "compaction": 1},
  "summary_counts": {}, "compaction_count": 1,
  "known_usage": {"known_input_tokens": 10, "known_output_tokens": 2,
    "known_cached_input_tokens": 3, "known_total_tokens": 12,
    "unknown_request_count": 0, "complete": true},
  "known_usage_by_kind": {"user": {"known_input_tokens": 10, "known_output_tokens": 2,
    "known_cached_input_tokens": 3, "known_total_tokens": 12,
    "unknown_request_count": 0, "complete": true},
    "compaction": {"known_input_tokens": 0, "known_output_tokens": 0,
      "known_cached_input_tokens": 0, "known_total_tokens": 0,
      "unknown_request_count": 0, "complete": true}},
  "context": {"configured": true, "window_tokens": 8192, "reserve_tokens": 1024,
    "threshold_tokens": 6553, "estimated_tokens": 12,
    "estimate_kind": "estimated", "summary_id": null,
    "covers_start_message_id": null, "covers_end_message_id": null,
    "summary_token_estimate": null, "retained_message_count": 1,
    "spill_count": 0, "active_run_id": null}
}
```

样例数字来自单元测试的受控数据；`estimated_tokens` 会随系统提示与工具 schema 改变，示例值只展示类型与来源。前端“上下文状态”折叠区显示窗口、阈值、估算值及其“非已知用量”标注、摘要 ID 前缀与覆盖区间、最后装配的 spill 数。未配置窗口时隐藏。

## 自动命令与环境

- `uv run --locked pytest -q`：最终 **328 passed in 34.25s**（审查修复后全量重跑）。
- `uv run --locked mypy`：**Success: no issues found in 67 source files**。
- `npm run test:agent-session`：三个会话测试组通过；`eslint --no-ignore` 针对改动的三个前端源文件为 0 错误；`git diff --check` 通过。
- 真实模型使用本地 `.env` 中现有配置；探针使用独立临时 SQLite、窗口 4096、输出保留 1024、阈值 0.8；临时库随探针结束删除。浏览器使用独立端口 18081/18001 与 `.runtime/t29-browser-sessions.sqlite3`；只记录元数据，不保存页面正文截图。

## 浏览器复验步骤

1. 启动独立 Agent 和前端代理，使用隔离 SQLite、独立端口及已登录的测试账号，设置 `AGENT_CONTEXT_WINDOW` 与合适的输出保留值。记录本地时间、端口和匿名 run ID；不保存正文或带凭据的网络导出。
2. 在助手中新建会话，连续发送不含业务信息的长文本直到出现 `kind=compaction` run。检查每轮 `context_assembled.estimated_tokens ≤ threshold_tokens`，压缩 run 的摘要为 active，用户 run 已完成。
3. 在开发者工具 Network 观察 SSE `after_seq`。中断连接后续接，确认 seq 无重复、模型请求数不增加；刷新并重新打开会话，对比“上下文状态”、已知用量文字和 `GET /runs/{rid}.context` 的字段。最后核对 `/sessions/{sid}/stats` 与页面一致。

## 审查修复（2026-10-21）

当晚审查发现 6 处交付纪律与实现问题，代码已全部修复并全量重跑。**真实模型与浏览器证据未重跑**，上表记录继续有效；本节只补充修复事实、新增测试与探针结论。

| 编号 | 问题 | 修复 | 涉及文件 |
| --- | --- | --- | --- |
| M1 | `SUMMARY_NOT_SMALLER` 被错标为 `stage=commit` | 校验阶段归 `stage=validate`（失败收束默认 `validate`）；只有 `commit_summary` 兜底拒绝才标 `commit` | `sessions/coordinator.py`、`context/compaction.py` |
| M2 | 未配置窗口时手动 `/compact` 仍建 run | `admit_run` 之前判空策略 → 400 `VALIDATION_ERROR`，不建 run、不发 `compaction_*` | `sessions/service.py` |
| M3 | 被裁掉的旧超大组误报 `CONTEXT_OVERFLOW`；`threshold > available` 存在不裁切也不溢出的带 | 单组溢出只判保留组；`before > available` 即裁切 | `context/window.py` |
| M4 | spill 可能把模型可见文本改大 | 先预算 notice + 恢复路径，剩余预算分给首尾；`omitted_chars == 0`、已带标记、改写后不更短时返回原结果 | `context/spill.py` |
| M5 | 摘要请求自身溢出导致长历史无法压缩 | `prepare_compaction` 按摘要请求预算收窄覆盖，从最新被覆盖组整组回退 | `context/compaction.py` |
| P1 | 重放顺序、超时宽限、stats 口径、事件持久化字段 | `request_id` 先解析已存在 run；超时取消后 5s 宽限；`last_context` 只取 `kind='user'`；`context_assembled` 持久化追加 `summary_absent`/`covers_*` | `sessions/service.py`、`sessions/coordinator.py`、`sessions/repository.py` |

新增测试文件：`tests/unit/test_context_compaction_budget.py`（3 例）、`tests/unit/test_tool_spill_budget.py`（8 例）；扩充 `tests/unit/test_context_window.py`（+3）、`tests/unit/test_context_compaction.py`（+5，含 stage 断言修正）、`tests/unit/test_session_stats_http.py`（+1）。总数由 309 增至 328（+19）。

探针结论（本地受控环境，非真实模型）：

- 二次压缩：已有 active 摘要时再次手动 `/compact` 成功，覆盖区间 `1..155`，旧摘要转 `superseded`、新摘要 `active`。
- spill 近阈值不放大：`max_inline_tokens=64` + 130 token 中文结果返回原结果；`max_inline_tokens=200` + 1200 token → spill 后约 199 token。
- 硬预算裁切：`threshold_tokens > available_tokens` 时按 `available_tokens` 裁切，不再出现"既不裁切也不溢出"的失败带。
- 未配置窗口的手动压缩：400 `VALIDATION_ERROR`，`runs` 无新增、`events` 无 `compaction_*`。

口径同步：`uv run --locked pytest -q` → **328 passed in 34.25s**；`uv run --locked mypy` → **67 source files**（正文其余处的统计口径已同步为 328/67；上文的 309 仅作为本次“+19”的增量基线保留）。

## 任务衔接与边界

| 任务 | 已交付内容 | 下一环使用方式 |
| --- | --- | --- |
| T24 | `AgentRunResult.new_messages` 和新增事件/配置骨架 | 落库以 `new_messages` 为准。 |
| T25 | token 估算、工具结果 spill 与设置 | 提供窗口决策和 spill 计数来源。 |
| T26 | 完整工具组裁切、阈值和溢出事件 | 装配结果提供估算与实际保留数。 |
| T27 | 压缩 run、手动 `/compact`、不更小拒绝 | 摘要交 T28 持久化，失败按窗口裁切。 |
| T28 | schema v5、摘要表、摘要+尾部装配、run 上下文快照 | T29 只读统计与页面展示。 |
| T29 | stats、前端上下文状态、自动测试和本记录 | 后续沿用 `stats.context` 与 run 快照字段。 |

可选的 `/sessions/{sid}/export` **未实现**，总览契约正文未登记该路由。`stats` 只读，不触发压缩。已知用量仅取 `model_requests` 的供应商报告，估算值只在 `context`。浏览器主路径和运行中刷新已验证；`after_seq` 报文未单独抓包，依赖前端自动测试。初始窗口 4096 的合成输入曾触发 `CONTEXT_OVERFLOW`；窗口 8192、摘要上限 256 时压缩因输出达到上限而失败，均已保留为测试环境边界证据。上述两条浏览器/真实模型证据未在审查修复后重跑；审查修复 M3 起 `CONTEXT_OVERFLOW` 只对保留组（系统 + 最新完整组）判定，被裁掉的旧超大组不再误报，因此 4096 窗口那条证据复核时应按"保留组装不下硬上限"理解；M5 只改变摘要覆盖宽度，不改变"摘要必须更小"的拒绝逻辑。
