# MES-13 评测报告

## 离线结果

MES-07 独立 oracle 对 14 个固定合成案例通过校验；另对代表性预期作了 4 项断言（重复工序产量、报工追查 ID、25 条分页总数、权限拒绝）。案例 ID、输入和独立预期见 `tests/fixtures/mes_progress/cases.json`，校验器为 `tests/fixtures/mes_progress/validate_cases.py`，本评测入口输出见 `evals/mes_tool_granularity/offline-contract-result.json`。

真实模型评测未执行；没有真实 MES 连接或真实单据 ID，故无真实 A/B 逐题答案、模型工具选择/调用数、耗时、失败或明细追查观察。token 和成本未知，不记为零。合成案例结果不等于真实 MES 证据。

## 建议

进度视图继续默认关闭。离线合成 oracle 支持固定预期可复核，但不能评价真实模型答案质量或真实数据表现。获得授权账号、模型服务及 MES-07 确认的真实样本 ID 后，再按同题记录 A/B 事实正确性、工具调用、耗时/错误和追查能力；不从小样本推断统计显著性。

## 复现

在 `ktg-agent` 目录运行 `python evals/mes_tool_granularity/run_eval.py`。脚本先调用 MES-07 独立校验器，然后输出本评测结果 JSON；不修改环境开关，不发起网络请求。
