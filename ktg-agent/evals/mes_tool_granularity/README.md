# MES-13 固定题目与离线评测入口

离线题目绑定 MES-07 仓库内合成案例（14 个固定 ID）；它们是合成数据，不代表真实 MES 单据 ID。真实运行时的真实单据 ID 需由 MES-07 确认记录提供。

A 组：基础工单详情、任务列表/详情、报工列表/详情查询。B 组：同候选再显式启用 `ktg_pro_workorder_progress`。两组权限、MES 数据和模型预算相同；试验开关保持默认关闭。

固定案例题目：
- `normal_two_processes`：工单已生产量应为 4，不能把两道工序的 4+4 当工单产量。
- `fractional_remaining`：核对小数剩余量与完成比例。
- `unfinished_process`：未完成工序不擅自推导业务结论。
- `trace_feedback`：用报工 ID 41、42 核对进度与报工明细追查。
- `large_pagination`：检查跨页 25 条报工，不能把首 20 条当全量。
- `draft_mixed`：未执行 PREPARE 草稿不计入已完成汇总。
- `zero_plan`：计划量为零时不计算比例。
- `overproduction`：超产比例可大于 100%，剩余量可为负。
- `missing_produced`：已生产量缺失不能当作零。
- `permission_denied`：无报工权限时整体不可用。
- `concurrent_change`：多次读取期间版本变化需识别。
- `partial_read_failure`：第二页失败需识别不完整读取。
- `unit_mismatch`：单位不一致时不换算。
- `correction_unknown`：缺少冲销链时标记修正历史未知。

```powershell
python evals/mes_tool_granularity/run_eval.py
```

脚本调用独立 MES-07 合成 oracle 校验器，并作代表性断言；不连接模型或 MES。真实模型质量评测仍需真实服务和 MES 环境。FakeProvider/契约检查不证明模型选择质量。
