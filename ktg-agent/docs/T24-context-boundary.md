# T24 上下文装配边界与新增消息集合

## 四层边界

| 层 | 载体 | 变更规则 | 写入者 |
|---|---|---|---|
| canonical | SQLite `messages` 与 `completed_history` | 只追加；不因装配或摘要而修改、删除 | `settle_run` |
| 派生摘要 | 后续任务的 `context_summaries.active` | 可被 supersede；不能替换 canonical | T27/T28 |
| 动态注入 | 每轮装配前缀：系统提示、`page_context`、未来摘要与警告 | 每轮可变；不落库 | 装配器 |
| 当前输入 | 当前 user、steer、follow_up | 属于 `AgentRunResult.new_messages`；落库后成为 canonical | `AgentLoop` |

`AgentRunResult.messages` 保留完整本地历史，`new_messages` 是其后缀，也是本轮落库、审计步骤和业务结果的唯一消息来源。`RunCoordinator` 不再根据前缀长度或 `completed_history` 长度推断新增消息。`page_context` 已使前缀内容随 run 改变，后续摘要装配还会改变前缀长度。

## 实现与验证说明

- `AgentLoop.run` 接收显式的 `new_input_count`，默认值为 1；`Agent.prompt` 明确传入 1。当前 user 消息在调用 `run` 前已附加到 `messages`，所以单纯在 `run` 入口把 `len(history)` 当基线会漏掉它。基线为 `len(history) - new_input_count`，此后 loop 追加的 assistant、tool、steer 和 follow_up 消息都进入后缀。`new_input_count=0` 允许尚无新增输入就取消的情形。
- 新事件只登记并校验 payload；既有事件 payload 与公开信封版本不变。新增配置只在 `Settings.from_env` 解析和校验，未接入 `create_service`。
- 正常文本、工具消息顺序、steer、follow_up、取消、limit、用量不可得和错误终态通过运行结果测试。executor 异常仍以空消息结算；前缀漂移测试使用临时 SQLite，重开后通过 `completed_history` 证明仅保存显式 `new_messages`。
- `_write_steps` 的既有成功、失败、未知三桶与取消后 `needs_reconciliation` 测试保持通过；它和 `business_results` 现在读取同一 `new_messages` 元组。
- 新增 `tests/unit/test_context_settings.py` 是任务书允许的测试范围扩展，用于隔离验证环境变量映射和启动拒绝非法配置。

实现位置：`core/loop.py::AgentRunResult` 与 `AgentLoop.run` 及三个终态辅助方法；`core/agent.py::Agent.prompt`；`sessions/coordinator.py::_execute`；`core/events.py::AgentEventType`、`AgentEvent.__post_init__`；`server.py::Settings.from_env`。旧偏移推断 `result.messages[1 + len(history):]` 已从 `coordinator.py` 删除。

最终验证：`uv run pytest -q` 为 **260 passed**；`uv run mypy` 为 **52 source files，无错误**。相对 217 基线新增 43 个测试用例；mypy 文件数多 1 来自新增的配置测试文件。

给 T25 的估算接口入参示例：`messages: tuple[Message, ...]` 可取当前模型可见的装配序列，`tools: tuple[ToolDefinition, ...]` 可取当前 `ToolRegistry` 的定义集合；估算值仅用于窗口决策，不写入实际用量字段。
