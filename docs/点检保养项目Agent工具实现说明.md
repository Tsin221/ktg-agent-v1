# 点检保养项目 Agent 工具

实现日期：2026-10-25。遵循设备台账/设备类型的基本 CRUD 模式，不涉及计划写入、导入导出、自动编号或批量删除。

## 工具与权限

| 工具 | 参数 | 权限（mes:dv:dvsubject: 前缀） |
|---|---|---|
| ktg_dv_subject_list | subject_code 精确、subject_name 模糊、subject_type、enable_flag、page/page_size | list |
| ktg_dv_subject_detail | subject_id 或 subject_code，只能二选一 | query + list |
| ktg_preview_dv_subject_create | subject_code、subject_type、subject_content 必填；其他允许字段可选 | add + list |
| ktg_preview_dv_subject_update | 明确 subject_id + 变更字段 | edit + query + list |
| ktg_preview_dv_subject_delete | 单个明确 subject_id | remove + query |

另有 ktg_dv_subject_create/update/delete 三项 hidden 执行工具，权限对应预览，模型候选和模型调用通道不可直接执行。

允许字段：subject_code（64）、subject_name（可空，100）、subject_type（CHECK/MAINTEN）、enable_flag（Y/N，新增默认Y）、subject_content（必填，250）、subject_standard（可空，255）、remark（可空，500）。长度按页面与数据库字段约束，类型/启用代码使用 mes_dvsubject_type 与 sys_yes_no；卡片复用这两个界面字典。分页上限100。按编码命中多条（包括结果被分页截断）报歧义并提示ID，不选择首条。用户保留的历史同码项目仍正常按ID区分；其原编码与其他ID冲突时拒绝修改，不偷偷换码。

## 确认链路

- 预览仅查询MES并写独立SQLite，不调用MES新增、修改、删除或自动编码接口。
- 预览绑定用户、动作和不可变前后快照，默认TTL600秒，摘要校验内容，执行前核对身份与旧快照、复查编码。
- 前端轻卡片 → POST /agent-api/v1/dv-subject/confirm → 服务重新从MES获取当前身份与权限 → trusted_confirmation通道 → hidden执行。
- HTTP请求中的userId、permissions、channel不能代替服务器身份或授权；模型伪造确认参数也不能直接执行hidden工具。
- SQLite begin事务锁定一次执行；重复确认返回已缓存结果，不重发写入。请求失败、结果不明时前端不自动重提。
- 成功必须回读核验。后端业务错误不是成功；写后回读失败记unknown，避免把可能已完成的写入再次执行。更新核对旧快照是读前检查，后端没有版本号条件更新，不能声称跨系统原子乐观锁。
- 删除预览显示明确ID、编码名称及关联警示。已有计划快照不会自动同步，删除源项目可能留下引用；工具没有凭空承诺无关联，也未新增全局禁止规则。

## 配置与当前状态

写工具默认关闭。需要配置后重启Agent才注册：

```text
AGENT_ENABLE_DV_SUBJECT_WRITE=true
AGENT_DV_SUBJECT_DB=E:/ktg_agent/ktg_agent_v1/.runtime/agent-dv-subject.sqlite3
```

数据库路径必须绝对且不同于会话、工单、库存、设备台账和设备类型预览库。未显式指定时，使用会话库同目录的 agent-dv-subject.sqlite3，无会话路径则在用户 .ktg-agent 目录。只读工具不依赖写开关。

初始源码交付时未启用。随后用户于2026-10-25明确授权开启并重启：根.env仅精确设置AGENT_ENABLE_DV_SUBJECT_WRITE=true，原设备台账开关保持true；已隐藏重启项目Agent，8001唯一监听PID4944且/health返回ok。相同环境加载器下隔离工厂验证5个可见工具、3个hidden执行工具均注册，hidden仍不入模型候选，原设备/类型工具保留。MES8080与前端81进程不变，未对真实MES写入，未提交Git，保留数据未动。

## 验证

- 新增 mock/临时SQLite测试先RED（缺实现），再实现。后端定向回归103项通过，含新增17项及旧设备台账、设备类型、目录和配置回归。
- 用真实本地HTTP handler、mockMES验证确认路由：伪造身份无效、可信重授权、重复确认只写一次。另验证过滤分页、编码歧义、创建必填与白名单、预览无MES副作用、旧快照冲突、TTL、业务错误、写后回读失败为unknown且不重复POST。
- Python：`.venv/Scripts/python.exe -m pytest tests/unit/test_dv_subject.py tests/unit/test_dv_machinery.py tests/unit/test_dv_machinery_writes.py tests/unit/test_dv_machinerytype.py tests/unit/test_dv_machinerytype_writes.py tests/unit/test_server_settings.py tests/unit/test_catalog_business_integration.py -q`
- mypy：新读写模块及service.py/server.py四文件通过。
- 前端：`node tests/agent-dv-subject.test.js` 和 `npm run test:agent-session`通过；定向ESLint通过；`npm run build:stage`通过，仅本次构建设置NODE_OPTIONS=--openssl-legacy-provider，现有体积警告保留。
- 未对真实MES做新增、修改或删除验收，未重跑无关Java全量测试。代码差异与敏感配置边界做最终检查。

## 主要文件

- ktg-agent/src/ktg_agent/ktg/dv_subject.py、dv_subject_writes.py
- ktg-agent/src/ktg_agent/service.py、server.py
- ktg-mes-ui/src/components/AgentAssistant/DvSubjectCard.vue、index.vue
- ktg-mes-ui/src/api/agent.js、src/views/mes/dv/subject/index.vue
- ktg-agent/tests/unit/test_dv_subject.py、ktg-mes-ui/tests/agent-dv-subject.test.js
