# T10：实现工装夹具类型基础Agent工具任务完成情况

状态：已完成。
创建日期：2026-10-27；完成日期：2026-10-28。
页面：`http://localhost:81/tm/tooltype`。

## 交付结果与分工

按用户选定范围，仿照现有设备管理工具，完成查询、详情、选项和需网页确认的新增/修改/删除六项基础能力。模型可见3个查询、3个写入预览，另有3个模型不可见的确认执行定义。

全部产品及测试代码由Luna（gpt-6-luna）实现；主任务负责任务/完成文档、代码审查、完整回归、本机启用与实际页面验收。保留既有未跟踪产物。用户在验收后明确要求提交Git，本轮代码、测试及任务/完成文档按该指令提交，未推送；T09提交不受影响。

## 工具与权限

下表中的权限均在服务端按当前MES登录身份检查；预览和确认执行使用同组业务权限。

| 能力 | 工具名 | MES权限 |
| --- | --- | --- |
| 分页查询 | `ktg_tm_tooltype_list` | `mes:tm:tooltype:list` |
| 按ID/精确编码读取详情 | `ktg_tm_tooltype_detail` | `mes:tm:tooltype:query`、`mes:tm:tooltype:list` |
| 真实字典与周期规则 | `ktg_tm_tooltype_options` | `mes:tm:tooltype:list` |
| 新增预览/确认 | `ktg_preview_tm_tooltype_create` / `ktg_tm_tooltype_create` | `mes:tm:tooltype:add`、`mes:tm:tooltype:list` |
| 修改预览/确认 | `ktg_preview_tm_tooltype_update` / `ktg_tm_tooltype_update` | `mes:tm:tooltype:edit`、`mes:tm:tooltype:query`、`mes:tm:tooltype:list` |
| 删除预览/确认 | `ktg_preview_tm_tooltype_delete` / `ktg_tm_tooltype_delete` | `mes:tm:tooltype:remove`、`mes:tm:tooltype:query`、`mes:tm:tooltype:list`、`mes:tm:tool:list` |

未开放模型直接执行增改删，网页通过`POST /agent-api/v1/tm-tooltype/confirm`确认，确认请求只接受`preview_id`。未扩大角色权限，未增加管理员旁路。

## 业务规则与确认机制

- 列表支持编码精确、名称模糊、编码管理和维护类型筛选，每页最多100条；详情只接受ID或精确编码之一，多命中要求明确ID。
- 编码管理Y时，维护类型为REGULAR/USAGE，周期须为1至2147483647整数；拒绝空、0、负数、小数、布尔值及溢出。N时维护类型及周期置空，卡片显示无。
- 新增要求明确业务编码，不调用消耗流水的自动编号。编码64、名称100、备注250字符上限。
- 修改先读取可信记录，仅合并白名单中明确提供的变更，保留其他字段及未提供的文本原值。编码/名称重复受预检和MES约束保护。
- 删除仅支持单条类型，预览和确认时都检查关联工装台账；有引用拒绝操作。
- 预览默认10分钟有效，绑定用户、动作、内容摘要及原记录快照，保存于独立SQLite；确认重新检查身份、权限、快照和业务约束。
- 重复确认复用持久回执，写请求不自动重试。响应或回读无法核实则为UNKNOWN，不伪报成功、不触发列表刷新、不自动重发。
- 删除后的成功核验使用精确编码列表查询，严格要求合法分页结果、总数0且记录为空。兼容MES在不存在记录时省略详情data字段的真实行为。

## 页面接入

新增中文列表、详情、选项、预览及结果卡片。修改卡片展示前后差异，周期单位分别依据变更前/后的维护类型；失败、UNKNOWN、确认中与已提交状态明确区分。

仅在`succeeded`且`verified=true`时派发类型变更事件并刷新页面。更新当前打开对象时回读详情，返回后复核对象和弹窗状态；删除当前对象时关闭弹窗；页面销毁时移除监听。前端页面上下文携带tm模块，服务端仅接受已知模块值。

## 配置与本机状态

- `.env.example`：`AGENT_ENABLE_TM_TOOLTYPE_WRITE=false`，新写入能力默认关闭。
- 本机已启用`AGENT_ENABLE_TM_TOOLTYPE_WRITE=true`。
- 本机独立预览库：`AGENT_TM_TOOLTYPE_DB=E:/ktg_agent/ktg_agent_v1/.runtime/agent-tm-tooltype.sqlite3`，不与会话、库存、工单或设备预览库共用。
- 配置库路径必须为绝对路径；未提供时按现有配置规则选择独立默认文件。查询随tm模块可用，写预览及确认受写开关控制。
- 8001端口Agent已加载最终实现，81端口页面已实际验收；本任务仅重启Agent，保留现有会话库，未重启MES、MySQL或Redis。真实`.env`仍为忽略文件，未输出凭据。

## 自动化验证

| 验证 | 最终结果 | 证据（仓库根目录output/T10） |
| --- | --- | --- |
| 完整Python回归 | 541项通过 | `pytest-full-accepted.log` |
| 工装工具及确认定向 | 22项通过 | `pytest-tm-tooltype-delete-readback.log` |
| 全部Python源码类型检查 | 49个源码文件无错误 | `mypy-src-final.log` |
| 新前端卡片、确认、上下文及刷新 | 通过 | `frontend-tm-tooltype-test.log` |
| 现有Agent会话前端回归 | 通过 | `frontend-agent-session-tests.log` |
| 原工装类型校验回归 | 通过 | `frontend-tooltype-tests.log` |
| Node 16生产构建 | 完成，包含构建警告 | `frontend-build-final.log` |

覆盖参数/分页/歧义、周期边界、N/Y、字段保留、身份/权限隔离、目录隐藏执行工具、库路径隔离、过期/篡改/快照变化、重复确认、异常响应UNKNOWN、回读不匹配、关联台账双次检查及页面异步切换保护。全仓旧测试文件的类型诊断未纳入“源码类型检查通过”的结论。

## 实际页面验收与数据清理

1. 正常模型对话返回列表、TT061详情及真实选项卡片。首次发现页面上下文module被旧白名单拒绝，已修复并补测。
2. 类型A：`T10-TOOLTYPE-20261028-A`，ID3。网页确认新增Y/REGULAR/2天，列表自动变为3条；修改为USAGE/3次及新备注，编码/名称/Y保留，卡片显示2天→3次，回读成功后自动刷新。
3. 删除A时暴露MES空详情不含data的真实形态，旧回执保守记录为UNKNOWN。没有重发删除；只读查询和页面搜索确认A已不存在、原记录保留。Luna修复核验并补测，原UNKNOWN回执保留为历史事实。
4. 类型B：`T10-TOOLTYPE-20261028-B`，ID4。N类型预览在Agent重启后仍能正常确认，维护类型/周期均为无，证明独立预览持久存储正常。再经网页确认删除，最终显示“已删除并回读核验”，列表自动恢复2条。
5. 最终原记录TT060（ID1、213、Y/REGULAR/2天、备注2）及TT061（ID2、233、Y/USAGE/2次、备注无）保留，两条T10测试类型均清理完成。

实际验收步骤及预览ID：`output/T10/browser-acceptance.md`；最终助手文本：`browser-final-assistant.txt`。截图：`01-created.png`、`02-updated.png`、`03-delete-verified-clean.png`。

## 范围与限制

未扩展台账写入、保养执行、批量操作、导入导出或自动编号。权限撤销、关联引用变化、异常响应和并发确认使用自动化测试覆盖；未逐项在真实多账号页面制造故障。关联检查和快照复核属于写入前读取，不等于MES数据库事务锁，外部并发写入仍受MES自身约束。进程在写入中异常终止时不会自动重放操作，需要核对MES实际数据后处理。

## Git交付

- 分支：`codex/t24-context-boundary`。
- `762e46b`：工装类型查询、受信确认写入、配置接入及Python回归测试。
- `3bed878`：中文卡片、网页确认、页面刷新及前端回归测试。
- 任务/完成文档随本次独立文档提交进入Git。
- 真实`.env`、SQLite运行库、构建输出、浏览器缓存及本机`output/T10`验收证据不纳入提交。原有无关未跟踪文件保留。未执行Git推送。

## 文档位置

任务和完成文档同步至用户指定的`E:\ktg_agent\docs\总体\任务`目录，并在仓库`docs/总体/任务`保留同名副本。
