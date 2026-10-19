# T21 持久用量与预算快照

## 存储与迁移

SQLite `user_version` 从 3 升至 4。`runs.budget_json` 保存 run 生效预算，`runs.usage_json` 保存已知累计与完整性；`model_requests` 以 `(run_id, call_id)` 为主键，保存请求开始、状态、来源及供应商报告的 token。新 run 的空累计为已知零；旧库没有记录的 run 保留空列，读取时标为 `{"source":"legacy_unknown","complete":false}`，不推算历史用量。30 天事件清理只删 `events`，不会删除这些快照。

真实会话由 `QueryService.budget_snapshot` 在调度前写入预算。Provider 在网络请求前发出 `RequestStarted`，仓储在同一事务提交该事件和请求行；`model_usage_reported` 同事务更新请求、累计及事件。已报告的 usage 不因后续请求失败而变成 unknown。请求开始但无 usage 的记录为 unknown，未开始请求记 `not_started` 且为已知零。重复回放只读取数据；同一 `call_id` 的开始记录受主键约束，不会累计两次。

最终结算沿用一笔事务提交消息、结果摘要和最后的 `run_settled`。事件载荷使用该事务内读取的预算、累计和逐调用记录；GET run 从相同的独立快照读取。最终结算失败后，之前提交的用量仍可读取。恢复不重发模型请求，对未结算 run 保留已知用量，并让已开始且未报告 usage 的请求保持 unknown。写入事件或计量失败时 run 以 `USAGE_PERSISTENCE_FAILED` 停止；若数据库仍不可写，run 保持非终态待重启恢复，不声称失败写入已持久化。

## 公开 JSON 示例

`GET /agent-api/v1/runs/{rid}` 在原字段外增加：

```json
{
  "budget": {"max_model_turns": 8, "max_tool_calls": 12, "max_run_seconds": 90,
             "max_parallel_reads": 3, "max_total_tokens": 1000, "max_output_tokens": 1024},
  "usage": {"known_input_tokens": 17, "known_output_tokens": 5,
            "known_cached_input_tokens": 3, "known_total_tokens": 22,
            "unknown_request_count": 0, "complete": true},
  "model_requests": [{"call_id": "run-id:model:1", "turn": 1, "model": "model-id",
    "request_started": true, "status": "completed", "usage_source": "provider_reported",
    "input_tokens": 17, "output_tokens": 5, "cached_input_tokens": 3, "total_tokens": 22}]
}
```

`run_settled.payload` 增加相同的 `budget`、`usage`、`model_requests`。中途事件仍是 `model_request_started`、`model_usage_reported`、`model_request_settled`；公开事件信封未改变，`schema_version` 仍为 1。预算达到时原 `result_summary.limit_reason` 保留；用量缺失时原 `result_summary.error_code=MODEL_USAGE_UNAVAILABLE` 保留；未知业务写入仍优先将状态设为 `needs_reconciliation`。旧 `/query` 不创建持久 run。

## T22 读取约定

运行中直接显示 `usage` 已知小计和 `complete`；`unknown_request_count` 大于零时显示需核对用量。`budget.max_total_tokens` 为 null 表示未启用累计阈值。单次 `usage_source` 可为 `provider_reported`、`unknown`、`not_started`；历史 run 的 `usage.source=legacy_unknown` 表示无可追溯计量。错误码 `USAGE_PERSISTENCE_FAILED` 表示计量写入失败，`MODEL_USAGE_UNAVAILABLE` 表示供应商用量缺失。不要把 unknown 显示成零或把 token 当费用。
