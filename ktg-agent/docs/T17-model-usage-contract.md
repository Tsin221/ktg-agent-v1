# T17：模型调用、用量与预算契约

决策日期：2026-10-19。此文档是本批新增决策；T18–T21 按此契约实现传输、累计、停止和持久化。当前 T17 只实现 Provider/Core 最小事件与调用标识，不声称 token 阈值已经生效。

## 调用与用量

Core 在每个 run 内为第 `turn` 次模型调用生成 `call_id = "{run_id}:model:{turn}"`，并放入 `ProviderRequest`。`turn` 从 1 开始；steer/follow_up 仍沿用同一 run 和递增 turn。Provider 在实际发出模型请求前发一次 `RequestStarted`；调用前取消不发此事件。Provider 可发 `UsageReported(Usage)`，它是该请求的**累计快照**，不是增量。最终 `FinalResponse.usage` 可空；如与已报告快照同时存在，二者必须相等。Core 只取该调用最后一个已知快照，按 `call_id` 最多计入 run 一次；绝不把快照或最终响应相加。即使随后协议错误、断流或取消，已经报告的快照仍保留。

| 字段 | 含义 |
| --- | --- |
| `call_id`, `turn`, `model` | run 内唯一调用标识、轮次、请求的模型标识 |
| `request_started` | Provider 是否开始发出该请求；调用前取消为 `false` |
| `status` | 调用结算：`completed`、`tool_call`、`length`、`cancelled`、`error` 或 `failed`；与用量来源分开 |
| `usage_source` | `provider_reported`、`unknown`、`not_started`；迁移旧记录时用 `legacy_unknown` |
| `input_tokens`, `output_tokens`, `cached_input_tokens`, `total_tokens` | 已知时为非负整数，未知时全为 `null`；`total_tokens=input_tokens+output_tokens`，缓存 token 已包含在 input 内 |

`not_started` 只用于确认尚未请求模型的调用，可明确记真实 0。已开始请求而没有有效 usage 为 `unknown`，所有 token 字段为 `null`。历史无逐调用用量记录的 run 为 `legacy_unknown`，不得回填 0。供应商 usage 字段缺失是 unknown；存在但类型、负值或缓存大于输入时是 `MODEL_INVALID_USAGE`，不伪装成 unknown 或零。已知 `Usage(0,0)` 是真实零。用量是 token 数，不代表费用。

Core 的 `model_request_started`、`model_usage_reported`、`model_request_settled` 事件携带 `call_id`；结算事件有 `request_started`、调用 `status` 和 `usage_source`。T19 用这些事实形成逐调用记录和 run 汇总，不能依赖 30 天后可清理的事件做最终存储。T18 的真实 SSE 解析必须在字节传输开始前发 `RequestStarted`，在收到完整用量帧时发 `UsageReported`，并在取消、断流或协议错误时关闭网络流。调用前取消只返回取消终态，不发开始通知。Core 的取消信号通过 `ProviderRequest.cancellation` 传入；T18 必须在流读取期间检查它并关闭底层传输。

## Run 汇总与预算

Run 快照包含 `known_input_tokens`、`known_output_tokens`、`known_cached_input_tokens`、`known_total_tokens`、`unknown_request_count` 和 `complete`。小计只累加 `provider_reported` 请求；已开始且缺失 usage 的请求使未知次数加一。`complete = unknown_request_count == 0` 且没有 `legacy_unknown`；它只表示用量统计完整，不表示 run 或业务成功。

新增配置名为 `max_total_tokens`，默认 `null`（关闭）；这是 T17 新决策，并非旧架构已规定。启用时必须为正整数。它是依据**已经收到的供应商用量**判断是否停止后续工作的阈值，不是调用前可保证的总量硬帽。每次 `max_output_tokens` 是输出请求上限；当前服务装配值为 1024，不等同总量阈值。输入 token 和尾帧延迟可能使真实累计超出阈值，仍须照实记录，不裁剪成阈值。已知小计达到或超过阈值后，停止下一次模型调用和未开始的工具，`StopReason.LIMIT`、`limit_reason=max_total_tokens`。

启用阈值而某次已开始的请求缺 usage 时，不再启动后续模型或工具，结算 `StopReason.ERROR`、错误码 `MODEL_USAGE_UNAVAILABLE`、`complete=false`。当前已经完成的文本可保留。关闭阈值时可继续，但 unknown 请求数与 `complete=false` 保留。若同时发生取消或网络错误，以原始取消/错误结算，并保留 unknown，不改写为用量错误或 token 用尽。已确定的写入结果保留；未知写入优先产生 `needs_reconciliation` run 终态。

已完整进入历史的 ToolCall 若被上述 token 停止原因拦下，必须逐个产生恰好一个对应 `ToolResultMessage`：沿用既有 `is_error=true`，在 `details` 中固定写 `execution_status="not_executed"` 与原始 `stop_reason`（用量缺失时再写 `error_code="MODEL_USAGE_UNAVAILABLE"`）。该消息只满足模型工具调用配对，不是业务执行成功或失败；不发工具启动事件、不计工具启动数。未完成参数不能成为可执行 ToolCall。T20 实施此调度规则；T17 不提前加入无效配置开关。

## 终态对照

| 场景 | 调用状态 | 用量来源 | Core / run 规则 |
| --- | --- | --- | --- |
| 正常文本 | `completed` | 已报告则 `provider_reported`，否则 `unknown` | `StopReason.COMPLETED`；业务成功另由工具事实决定 |
| 完整工具调用 | `tool_call` | 同上 | `StopReason.TOOL_CALL`；仅正常结束确认的完整调用可调度 |
| 输出截断 | `length` | 同上 | `StopReason.LENGTH`；部分工具参数不执行 |
| 调用前取消 | `cancelled` | `not_started`，真实 0 | `StopReason.CANCELLED`；无已发出请求 |
| 调用中取消 | `cancelled` | 有报告则已知，否则 `unknown` | `StopReason.CANCELLED`；已发出写入照实结算 |
| 断流/网络错误 | `failed` | 有报告则已知，否则 `unknown` | 原始错误；不得自动重试或补零 |
| 用量后协议错误 | `failed` | `provider_reported` | 保留最后快照，原始协议错误结算 |
| 无 usage 的正常响应且阈值开启 | `completed` | `unknown` | 保留文本，阻止后续工作，run 为 `MODEL_USAGE_UNAVAILABLE` |

`StopReason` 沿用 `completed/tool_call/length/error/cancelled/limit`。HTTP run 快照和 `run_settled` 沿用既有 run 状态 `completed/failed/cancelled/needs_reconciliation`；用量 `complete` 不映射成平行 run 状态。`agent_end` 携带停止原因及错误码或 `limit_reason`，`run_settled` 仍是最后生命周期事件。旧 `/query` 同步返回相同停止原因与预算政策；它不新增持久化生命周期。

## 固定 JSON 样例

```json
{
  "call": {"call_id": "run-7:model:2", "turn": 2, "model": "model-a", "request_started": true, "status": "failed", "usage_source": "provider_reported", "input_tokens": 120, "output_tokens": 18, "cached_input_tokens": 20, "total_tokens": 138},
  "usage": {"known_input_tokens": 120, "known_output_tokens": 18, "known_cached_input_tokens": 20, "known_total_tokens": 138, "unknown_request_count": 1, "complete": false},
  "budget": {"max_total_tokens": 500, "max_output_tokens": 1024, "known_total_tokens": 138},
  "stop_reason": "error",
  "error_code": "MODEL_INVALID_RESPONSE",
  "run_status": "failed"
}
```

这个样例中另一已开始调用没有 usage，故 unknown 次数为 1；本调用虽失败，用量仍已知。若预算关闭，`max_total_tokens` 为 `null`。若所有调用均未开始或有报告，unknown 次数为 0 且 `complete=true`。
