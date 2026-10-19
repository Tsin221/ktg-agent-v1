# T20：run token 阈值与运行截止交付记录

实施日期：2026-10-19。

## 配置与边界

- `AGENT_MAX_TOTAL_TOKENS`：未设置时为 `None`，即关闭 token 停止阈值；设置时必须是正整数，空值、零、负数与非整数均使服务配置失败。浏览器请求不能修改它。
- `QueryService.query` 与 `run_session` 共用一份服务端 `RunLimits`：最多 8 次模型轮次、12 次工具启动、90 秒、3 个并行读。每次模型输出上限为 1024 token；Provider 单次绝对超时默认 60 秒。
- 90 秒从 Core 开始执行 run 时以单调时钟计；排队等待不计入。模型请求前重新计算剩余时间，传给 Provider；传输以剩余时间和 Provider 超时中较小者为绝对截止，持续收到流片段也不能延长。已发出的业务写入可在截止后按实际回执收尾，因此最终结算允许晚于 90 秒。

## 检查位置与停止原因

| 位置 | 检查 | 结果 |
| --- | --- | --- |
| 每轮模型调用前 | 取消、运行时间、模型轮数、工具启动数、已累计用量 | 取消为 `cancelled`；预算为 `limit`，并给出 `limit_reason`；启用 token 阈值且用量未知为 `error` 与 `MODEL_USAGE_UNAVAILABLE` |
| 发送模型请求前 | 剩余 run 时间 | 时间已到时不发送请求，计 `not_started`；否则传入剩余秒数 |
| 模型轮次正常结束后、工具启动前 | 取消、时间、token 用量 | 阻止全部未启动工具；完整 ToolCall 各补一个 `execution_status=not_executed` 结果，不发工具启动事件 |
| 顺序工具之间、并行读取得许可后 | 取消、时间 | 未开始的工具不运行；工具启动数沿 `tool_execution_start` 边界计，不按模型建议数计 |
| 消费 steer/follow_up 前 | 同一 run 的轮数、工具数、时间、token 用量 | 追加输入不重置预算；未消费输入在结算中持久标为 `not_consumed`，限额原因为 `limit`，用量缺失原因为 `run_failed` |

用量已知且累计值等于或超过阈值时，`StopReason.LIMIT` 与 `limit_reason=max_total_tokens`；已完成的纯文本和实际用量保留，即使恰好达到阈值也记录 limit。启用阈值且已开始请求的 usage 缺失时，`StopReason.ERROR` 与 `error_code=MODEL_USAGE_UNAVAILABLE`，保留完整文本、已完成模型消息与 `complete=false`。关闭阈值时允许继续，但用量仍为 incomplete。`length` 只表示单次输出截断，不作为 token 总预算停止原因。

同一检查点取消优先于预算；模型正常结束后时间优先于 token 用量原因。模型本身取消或失败时保留原始原因和已知/未知用量，不由用量政策覆盖；Provider 错误在会话结算中保留原始错误码。模型流的取消与截止关闭底层传输。工具已发出后的成功、失败或未知回执照实保留；未知写入使会话 run 成为 `needs_reconciliation`。旧 `/query` 同步 JSON 保留原字段，限额增加 `limit_reason`，用量缺失增加 `error_code`；不增加持久会话或 SSE。

`max_total_tokens` 根据模型已报告的真实用量做停止决策，不是硬账单上限。输入用量常到尾帧才出现，单次请求可能越过阈值；实际超额完整记录。

## 验收

`uv run pytest -q`：212 passed。`uv run mypy`：Success（50 source files）。覆盖阈值下方、恰好达到与越界、两个完整工具调用遇到未知 usage 的零启动及逐个未执行结果、旧 `/query` 映射、追加输入未消费持久化、模型停滞及持续增量时绝对截止、连接阶段取消、并行读许可、写入窗口成功与 unknown 的收尾。

未执行带真实模型凭据和业务环境的联调；该项由后续整包验收承担。T21 应继续将用量与预算事实持久化到独立快照，不能依赖可清理的事件。
