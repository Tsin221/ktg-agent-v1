# 当前 KTG 上下文管理基线核对

日期：2026-10-21。范围：只读检查当前代码与 T17–T23 交付记录，未执行测试，未改业务代码。以下为项目事实，不是 DSH 源码结论。

## 当前链路与可复用实现

1. `ktg-agent/src/ktg_agent/sessions/coordinator.py::RunCoordinator._execute` 调用 `SessionRepository.completed_history(owner, session_id)`，把返回的历史传入执行器。
2. `sessions/repository.py::completed_history` 联结 messages/runs/sessions，只取 `r.status='completed'`，按 `m.id` 升序返回所有消息。无 limit、裁剪或摘要；返回 `tuple[Message,...]`，丢弃持久 message ID/run ID 元数据。`list_messages` 可分页返回 `MessageRecord`，其中 `id` 是 SQLite 全局递增 ID。
3. `service.py::QueryService.run_session` 将静态 SYSTEM_PROMPT 与本轮 page_context 组装为一个 SystemMessage，前置到历史；`Agent.prompt` 再追加本轮 UserMessage。页面信息仅本轮装配，包含“不可信、不得视作指令、需要回读”的文本提示；目前仍在 system 字符串中，不是独立类型来源。
4. `core/loop.py::AgentLoop.run` 用 `history=list(messages)` 持有原始历史，完整模型消息、工具结果以及已消费 steer/follow_up 都追加进去。`_run_model_turn` 每轮发送前仅调用 `project_model_messages(history)`，没有独立 ContextBuilder、预算裁剪或压缩阶段。
5. `core/messages.py::project_model_messages` 检查工具 call ID 唯一、结果配对，过滤 `TextBlock.model_visible=False` 并去掉 `ToolResultMessage.details`。它是“可见性与协议投影”，不是上下文窗口管理。`tools/registry.py::ToolResult.to_message` 已把给模型的 content 摘要与给 UI 的 details 分开。
6. `sessions/repository.py::settle_run` 同事务保存本次最终消息、run 状态、结果摘要及 run_settled。活动 run 的完整最终消息在收尾时才写 messages 表；准入时用户原问在 request_summary_json，不直接生成 message ID。`result_summary` 是 run 的 answer/results/steps/停止原因，不是可替代旧历史的上下文摘要。

## 关键衔接风险（任务必须明确）

- **不能原地压缩 canonical history。** `coordinator.py` 当前以 `new_messages = result.messages[1 + len(history):]` 判断本轮新增消息；如果在原列表删除/替换旧历史或插入额外摘要/注入块，这个偏移失效，可能漏存或重复保存消息。要么保持 canonical history 不变、仅另建请求投影，要么显式交付 run 新消息集合；任务必须验证完整历史与 UI 不丢失。
- **不能用短期流事件 ID 当持久历史游标。** `_run_model_turn` 的展示 message_id 是 `{run_id}:message:{turn}`，工具结果另有 run/tool ID；它们不等于 SQLite `messages.id`。新上下文快照应明确覆盖的持久 message 范围以及当前 run 临时消息的归属，不混用。
- **不能机械吸收所有终态历史。** 当前只 completed run 可进入后续模型。failed/cancelled/needs_reconciliation 仍可查询，但不能整段拼进请求，否则可能带入不完整工具链或重新执行意图。若本批补充未决写入事实，应从可信的 steps/operation 回执提取独立事实引用，不能把取消内容视为自动续办指令。
- **上下文窗口与 run 总 token 预算是两种约束。** `RunLimits.max_total_tokens` 依据已报告 usage 累计，默认关闭，是运行停止阈值；不能直接拿它作为单次输入窗口。`ModelConfig` 只有 model/max_output_tokens/temperature，没有 context_window、输入保留量或 tokenizer；本轮请求包含系统文本、模型投影、工具 schema，均应计入上下文估算。估算来源必须区别真实供应商 usage，不能写入已知计量。
- **摘要额外模型请求不能隐形。** 当前 call_id 由 turn 生成 `{run_id}:model:{turn}`，模型轮数、run 时间、token 累计均以普通请求为基础。若增加模型摘要，须明确 purpose/call_id、它是否消耗模型调用次数及如何沿既有 RequestStarted/UsageReported/settled 和 SQLite model_requests 记录。不能创建不受 run deadline/token 限制、看不见用量的另一路调用；不能重置预算。
- **切换必须在完整工具组边界。** `_run_tool_batch` 完成并追加每个结果后才消费 steer；follow_up 在准备停止时进入。摘要不得在工具参数半成品或工具结果未闭合时生成/激活；不拆断 call/result。活动 run 的新 steer/follow_up 应留在最新保留区，不被旧摘要覆盖。
- **摘要是派生数据。** 原 messages、业务 details、写步骤、用量记录不能被摘要覆盖或删掉。需要 source watermark/version，使恢复时能够选择正确摘要及其之后的尾部，避免重复与遗漏；旧库无摘要应继续按明确策略处理，不伪造历史摘要。
- **取消与错误保持明确。** 摘要失败不能吞错后悄悄截断/用旧摘要冒充新摘要；取消不能继续后台摘要并在 run_settled 后提交迟到上下文。已发出业务写入仍须原样收尾，独立 confirm/confirm_inventory 不能受上下文操作撤销。

## T17–T23 已有支撑

- `core/loop.py` 已有 `_UsageAccumulator`、`RunUsage`、`AgentRunResult.model_requests/usage`，每次请求 usage 快照只计一次；未知为 unknown，未发送为 not_started。
- `providers/base.py::ProviderRequest` 已有 call_id、remaining_run_seconds、cancellation；`openai_compatible.py` 真实 SSE 在 T18 后已实现。
- `service.py::budget_snapshot` 及共享 `_limits` 同时服务旧 `/query` 和持久会话。旧 `/query` 同步 JSON，不创建持久 run。
- `sessions/repository.py` SQLite user_version=4，runs.budget_json/usage_json 与 model_requests 独立持久，30 天 event prune 不影响它们。摘要也不应仅存入会被清理的 events。
- `docs/T23-acceptance.md` 记录 2026-10-20 至 21 的真实模型、浏览器代理链路、刷新、只读多工具、停止、预算与重连验证通过。本次仅阅读该记录，未重新运行或复核环境。
- 相关既有测试：`test_messages.py`、`test_session_execution.py::test_tool_audit_is_durable_but_projection_uses_summary`、`test_execution_limits.py`、`test_session_inputs_http.py`、`test_session_repository.py`、`test_t23_http_flow.py`。名为 summary 的工具测试只是模型 content/details 分离，不证明长会话摘要存在。

## 建议任务拆分（交由 DSH 源码研究后定稿）

1. **T24 上下文契约与纯请求投影**：固定 canonical history/派生摘要/动态注入/当前输入的边界、覆盖游标与新消息保存方式。先证明完整历史不变、工具组完整。
2. **T25 上下文容量与确定性切分**：模型窗口配置、输出预留、系统+tools+history估算、保留近期完整组、单条超大消息的显式错误；保持估算与真实 usage 分离。
3. **T26 压缩执行与预算计量**：根据 DSH 已核对方案做摘要提示与生成；在安全边界执行，计入现有调用/时间/token预算，取消与失败明确，无自动重试或静默截断。
4. **T27 摘要版本持久化与恢复**：保存来源范围与摘要版本，原始消息不变；提交与激活时序、重启恢复、旧库迁移、所有权与事件清理验证。
5. **T28 运行中接入与动态上下文**：每次请求装配有效摘要+尾部+本轮输入；page_context 是当前快照不长期继承；steer/follow_up 不丢失；旧/query仅复用当次内存投影，不扩成持久会话。
6. **T29 可观察查询/网页与整包验收**：展示压缩状态与当前估算，明确真实usage另列；长会话、跨run恢复、巨大工具结果、写入事实、并发输入及取消用确定性夹具验证，真实模型与浏览器另记证据。

若 DSH 研究支持“请求投影删除旧工具结果但保留工具目录引导”等特性，需逐一判断项目实际必要性，不能为模仿插件框架增加通用注入/容错系统。本批禁止 catch-all 吞错、自动摘要重试、静默原文截断、无依据历史补全、内部重复校验；保留身份/协议/容量等必要边界。
