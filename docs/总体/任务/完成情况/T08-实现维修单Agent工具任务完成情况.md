# T08任务完成情况：实现维修单Agent工具

完成日期：2026-10-26
状态：代码实现、隔离回归、前端构建、浏览器检查和审查已完成；3个本地Git提交及维修写入启用、服务重启已完成。后续完成45轮真实MES助手对话及8次成功网页确认，覆盖主单与维修内容6类增改删，并清理专用测试数据。原有维修单查询字段与明细不变，未推送Git；体验问题及未覆盖条件场景见独立实测报告。

## 分工与成果

所有代码由 Sol 完成：主实施任务负责工具、服务集成、必要后端守卫、前端卡片及相关测试；另一个同为 Sol 的测试任务编写Java隔离事务测试，随后主Sol补充最后的守卫断言并复验。

主任务编写任务文档和本完成文档，复核源码、权限、验证输出，并使用内置浏览器检查真实组件编译的隔离卡片页面。

本次新增18个工具定义：6个查询工具、6个模型可见操作预览、6个模型不可见的确认执行工具。关闭写入开关时，只注册维修查询工具；开启后才注册对应预览与内部执行工具，执行仍需网页可信确认及当前用户权限。

## 工具清单

### 查询工具

| 工具 | 用途 | 必需权限 |
| --- | --- | --- |
| `ktg_dv_repair_list` | 按编号、名称、设备、结果、状态分页查询 | `mes:dv:repair:list` |
| `ktg_dv_repair_detail` | 按ID或准确编号定位详情；重码时要求按ID消歧 | `mes:dv:repair:list`、`mes:dv:repair:query` |
| `ktg_dv_repair_lines` | 查询指定维修单的维修内容并校验归属 | `mes:dv:repair:query`、`mes:dv:repairline:list` |
| `ktg_dv_repair_options` | 读取真实维修结果、状态字典及支持范围 | `mes:dv:repair:list` |
| `ktg_dv_repair_machinery_candidates` | 分页查询设备候选 | `mes:dv:machinery:list` |
| `ktg_dv_repair_subject_candidates` | 分页查询项目候选 | `mes:dv:dvsubject:list` |

查询可读取各状态；写入仅维护草稿。设备候选不擅加设备状态过滤，维修项目候选不套用CHECK/MAINTEN计划类型过滤，也不强加页面不存在的启用条件。业务响应按白名单投影，分页有上限，模型可读摘要包含实际字段和字典值，并在截断时提示继续分页。

### 预览与确认执行

| 模型可见预览 | 模型隐藏执行 | 功能 |
| --- | --- | --- |
| `ktg_preview_dv_repair_create` | `ktg_dv_repair_create` | 新增草稿维修单 |
| `ktg_preview_dv_repair_update` | `ktg_dv_repair_update` | 修改草稿维修单 |
| `ktg_preview_dv_repair_delete` | `ktg_dv_repair_delete` | 删除草稿主单及所属维修内容 |
| `ktg_preview_dv_repair_line_create` | `ktg_dv_repair_line_create` | 新增草稿维修内容 |
| `ktg_preview_dv_repair_line_update` | `ktg_dv_repair_line_update` | 修改草稿维修内容 |
| `ktg_preview_dv_repair_line_delete` | `ktg_dv_repair_line_delete` | 删除单条草稿维修内容 |

预览和对应执行采用相同权限要求，确认时由真实ToolRunner重新检查：

| 动作 | 必需权限 |
| --- | --- |
| create | `mes:dv:repair:add`、`mes:dv:repair:list`、`mes:dv:machinery:query` |
| update | `mes:dv:repair:edit`、`mes:dv:repair:query`、`mes:dv:repair:list`、`mes:dv:machinery:query` |
| delete | `mes:dv:repair:remove`、`mes:dv:repair:query`、`mes:dv:repairline:list` |
| line_create | `mes:dv:repairline:add`、`mes:dv:repairline:list`、`mes:dv:repair:query`、`mes:dv:dvsubject:query` |
| line_update | `mes:dv:repairline:edit`、`mes:dv:repairline:query`、`mes:dv:repair:query`、`mes:dv:dvsubject:query` |
| line_delete | `mes:dv:repairline:remove`、`mes:dv:repairline:query`、`mes:dv:repair:query` |

## 业务参数与确认机制

- 创建要求用户明确提供维修单编号、名称、设备ID和报修日期。自动编号接口会消耗流水，因此不作为只读工具，也不在预览中调用；T07页面自动编号修复保留。
- Agent参数使用白名单，不接受状态、完工/验收日期、人员、审计字段或任意属性。修改只发送实际变动字段，保留未修改值。
- 设备和项目快照来自实际MES详情，不接受模型伪造名称/编码。维修内容新增必须有故障描述；支持故障资源、维修情况及备注等字段，长度匹配既有页面/数据库限制；可选文本支持空串清空。
- 预览存入独立SQLite，绑定用户、动作、变更前后、摘要及有效期。确认入口为 `POST /agent-api/v1/dv-repair/confirm`，请求只接收 `preview_id`，重新读取当前用户身份和权限。
- 隐藏执行要求可信确认通道、匹配的操作上下文和身份，不向模型工具发现暴露。快照变化或非草稿时拒绝；重复确认返回保存的回执。
- 成功响应后仍做MES回读，含设备快照。发送请求后超时、回读不匹配或执行进程中断按UNKNOWN处理，不自动重新提交非幂等写入。

## 后端保障

维修主表及维修内容原先大部分为裸CRUD。本次补齐工具所需的保障，仍未实现完成维修或验收状态机：

- 新增主单只接受/固定为PREPARE，并忽略伪造主键；主单编辑和删除要求草稿。
- 通过事务内父行锁复核最新状态，维修内容写入要求父单存在且为草稿。
- 子行编辑不允许改挂到另一维修单；SQL移除更新 `repair_id` 的入口。
- 主单批删按顺序锁定并验证全部父单，先删子行再删主单；受影响行数异常或后续失败会整体回滚。
- 子行批删先定位归属、按父ID顺序锁定并复核；混入不存在记录或非草稿单据时拒绝。
- 单行删除及按父ID删除辅助入口也经过对应草稿/事务保护。

这些是后端状态、归属和事务保障；Agent确认前的预读只用于识别预览过期，不被当作事务锁的替代品。

## 前端接入

- 新增 `DvRepairCard.vue`，展示查询、详情、字典、设备/项目候选、变更前后和主单删除的关联影响。
- 主单新增预览能显示新编号和名称；维修内容预览比较子表字段，不把父单字段误列为将清空。
- 明细列表以行ID优先取key；状态/结果使用已有中文基线标签，未知值保留原值。
- 确认进行中或已经提交的预览禁用按钮；UNKNOWN或未核实响应不展示为成功，也不自动重试。
- 只有 `outcome=succeeded` 且 `verified=true` 才发送 `ktg:dv-repair-changed` 刷新事件。
- 维修页面处理刷新：若当前打开单据被删除则关闭；异步回读前后核对当前表单ID，避免用户已切换记录后被旧响应覆盖；页面销毁时解除监听。

## 配置与运行状态

示例配置新增：

```dotenv
AGENT_ENABLE_DV_REPAIR_WRITE=false
# AGENT_DV_REPAIR_DB=E:/ktg_agent/ktg_agent_v1/.runtime/agent-dv-repair.sqlite3
```

启用写入时必须提供独立的绝对SQLite路径，并检查不与会话、库存、工单或其他设备工具预览库共用。以上为默认示例；实现验收阶段未修改实际 `.env`。后续根据用户“提交git，顺便打开他然后重启mes”的要求已开启本地写入，具体配置与记录见下节。

部署按Java守卫、Agent和前端的顺序完成；新后端包已替换并启动。服务可访问和开关启用不代表新工具已完成真实业务确认写入联调。

## 实际验证

### Python

在 `ktg-agent` 目录使用已有虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_dv_repair.py -q
.\.venv\Scripts\python.exe -m mypy src tests/unit/test_dv_repair.py
```

- 全套pytest：518项通过，74.40秒。首次有两处旧目录数量断言失败，更新为新增6个只读工具后的真实数量，并保留原有权限与发现断言强度，随后全套通过。
- 全套之后最后补入设备快照回读不匹配及不重复POST用例，并完善相应回读字段，最终维修定向测试8项通过，1.70秒。没有将此后的全套总数另称为已复跑通过。
- 全部生产源码及新增维修测试：mypy通过，48个源文件无错误。
- 按pyproject执行全仓类型检查仍有7个旧测试文件的127条既有诊断。T06完成文档和 `.runtime/t06-mypy-full.txt` 已记录相同基线；本次没有声称全仓mypy通过，也没有扩大修改旧类型问题。
- 覆盖读取定位/歧义、父子归属、字段/权限限制、6类写入、可信确认、快照变化、过期/摘要/持久恢复、UNKNOWN回执及不重放；另通过真实QueryService、发现目录和ToolRunner验证子表权限不能由主表权限替代。
- 本机受限测试进程曾卡在Windows asyncio本地管道初始化；正常本地进程权限下运行恢复。没有为此修改生产异步策略，没有访问真实MES库或外部模型。

pytest和mypy没有单独保存命令日志；上述结果来自Sol实际运行工具输出。本报告如实记录，不补造日志。

### Java

在后端Maven项目根目录执行：

```text
mvn -o -pl ktg-mes -am -Dtest=DvRepairToolWriteIntegrationTest -Dsurefire.failIfNoSpecifiedTests=false test
```

最终8项通过，0失败、0错误、0跳过，Maven reactor `BUILD SUCCESS`。使用隔离H2、真实Mapper XML和Spring事务代理，覆盖状态、归属、级联删除/失败回滚、混合批删保护、父锁等待后状态变化以及辅助删除入口守卫。主任务已读取Surefire结果并归档。

首轮为7项；之后主Sol给辅助服务删除入口增加草稿守卫，并补充直接调用断言后复跑为8项。Maven启动环境的非致命“Access is denied”提示已在单独 `mvn -version` 中复现，不能将其误认为本次测试失败；日志和报告明确成功。

### 前端

Sol在 `ktg-mes-ui` 目录使用Node.js 16.20.2逐个执行以下6个脚本，均通过：

- `node tests/agent-dv-repair.test.js`
- `node tests/agent-dv-checkplan.test.js`
- `node tests/agent-assistant-state.test.js`
- `node tests/agent-assistant-session.test.js`
- `node tests/agent-result-card.test.js`
- `node tests/repair-autocode.test.js`

真实SFC编译和确认状态测试验证重复点击、未知响应、未核实响应、刷新触发和删除当前打开单据等行为。最终生产构建成功，只有2条资源体积告警；构建日志已保存。

### 浏览器与差异审查

主任务通过临时本地静态服务打开从真实 `DvRepairCard.vue` 编译的fixture页面，实际检查查询、新增、主删关联影响、维修内容修改预览、点击后按钮禁用和UNKNOWN提示。最终中文标签版再次查看，浏览器warn/error为空。

该页面没有连接MES；本地确认仅改变fixture提示。临时测试页已关闭，清理核对18721端口监听数为0，原维修单页面保留。

主任务复核最终前后端差异、权限声明、数据字段、测试及构建证据。`git diff --check`通过。所有代码仍由Sol编辑，主任务没有代写产品代码。

## 变更文件

共19个源码、配置及测试文件；不含本地输出、浏览器记录和仓库外文档：

### Agent与配置（8个）

- `.env.example`
- `ktg-agent/src/ktg_agent/ktg/dv_repair.py`
- `ktg-agent/src/ktg_agent/ktg/dv_repair_writes.py`
- `ktg-agent/src/ktg_agent/service.py`
- `ktg-agent/src/ktg_agent/server.py`
- `ktg-agent/tests/unit/test_dv_repair.py`
- `ktg-agent/tests/unit/test_catalog_business_integration.py`
- `ktg-agent/tests/unit/test_dv_machinerytype_writes.py`

### MES后端（6个）

- `ktg-mes-master/ktg-mes-master/ktg-mes/src/main/java/com/ktg/mes/dv/mapper/DvRepairMapper.java`
- `ktg-mes-master/ktg-mes-master/ktg-mes/src/main/java/com/ktg/mes/dv/service/impl/DvRepairServiceImpl.java`
- `ktg-mes-master/ktg-mes-master/ktg-mes/src/main/java/com/ktg/mes/dv/service/impl/DvRepairLineServiceImpl.java`
- `ktg-mes-master/ktg-mes-master/ktg-mes/src/main/resources/mapper/dv/DvRepairMapper.xml`
- `ktg-mes-master/ktg-mes-master/ktg-mes/src/main/resources/mapper/dv/DvRepairLineMapper.xml`
- `ktg-mes-master/ktg-mes-master/ktg-mes/src/test/java/com/ktg/mes/dv/service/impl/DvRepairToolWriteIntegrationTest.java`

### 前端（5个）

- `ktg-mes-ui/src/api/agent.js`
- `ktg-mes-ui/src/components/AgentAssistant/DvRepairCard.vue`
- `ktg-mes-ui/src/components/AgentAssistant/index.vue`
- `ktg-mes-ui/src/views/mes/dv/repair/index.vue`
- `ktg-mes-ui/tests/agent-dv-repair.test.js`

## 证据与边界

证据目录：`E:\ktg_agent\ktg_agent_v1\output\playwright\T08-20261026`。

- `java-guards-final.log`、`java-test-summary.txt`：最终8项Java测试及构建结果。
- `ui-build-final.log`：最终前端构建成功及资源告警。
- `repair-card.html`：真实组件编译的静态fixture，不是产品运行页面。
- `browser-create-preview.txt`、`browser-create-preview.png`：最终新增预览及中文状态。
- `browser-unknown.txt`：结果未知提示；`browser-console.txt`：空warn/error列表。

实现隔离验收阶段未在运行MES执行新工具写入；后续真实对话实测已在专用测试单执行写入并清理，见文末补充报告。原有业务记录未改动，编号流水未重置。未覆盖外部真实模型发现工具到MES确认执行的完整链路、真实MySQL下的并发特性、故障跨进程恢复部署及生产容量。已有隔离保障不被描述成这些场景已验收。

完成维修、验收流转和导出工具不在本批实现范围。业务编号仍显式输入；T07页面自动编号继续保留。本地部署及启用已单独记录如下；后续真实写入联调的测试数据、结果与清理也已另列实测报告。

## 后续本地提交、启用与重启（2026-10-26）

用户要求提交Git、打开维修工具写入并重启MES。共19个文件按层分为3个本地提交，分支为 `codex/t24-context-boundary`，未推送：

| 提交 | 内容 |
| --- | --- |
| `0d7387bed9c95a68ae474ab8b65d40a9132e7240` | MES草稿写入、状态/归属、级联删除及事务守卫，6个文件 |
| `32fdd9c0960630226fbb565f7d7109b4a471880e` | Agent维修查询、预览、可信确认写入及测试，8个文件 |
| `4e8c6e40f710b40ab2ba9ba639ed9ea8cd7ba9fc` | 前端维修卡片、确认与刷新接入及测试，5个文件 |

提交后跟踪文件无未提交差异，`git diff --check`通过。既有未跟踪截图、浏览器记录和output保留，实际`.env`、运行库和日志仍由Git忽略。

实际本地配置仅调整两项，其他既有配置核对保留：

```dotenv
AGENT_ENABLE_DV_REPAIR_WRITE=true
AGENT_DV_REPAIR_DB=E:/ktg_agent/ktg_agent_v1/.runtime/agent-dv-repair.sqlite3
```

独立预览库已由启动服务创建。开启写入仍要求当前用户权限和网页可信确认，隐藏执行工具仍不向模型直接暴露。

旧运行包和本地配置已备份至 `.runtime/t08-activation-20261026/ktg-admin.before-t08.jar`、`.env.before-t08`；配置备份不在文档中展开。使用项目Java 8与既有Maven设置执行离线`clean package -DskipTests`构建成功；此构建没有重复执行测试，上文隔离测试结果为此前验收证据。新可执行包内部业务JAR含维修父行锁Mapper及服务守卫。

最初17:31至17:32完成一次启动并检查成功。21:13继续任务时复查发现三个应用进程已停止，原因尚未确定；数据库与缓存仍监听。21:13:55重新后台启动，MES于21:14:39启动成功，前端于21:15:26编译成功；本次最终端口进程为MES 8080/PID13640、Agent 8001/PID26992、前端81/PID37312。进程号仅为本次核验快照。

最终检查：维修页面HTML返回200且含应用挂载节点，MES后端返回200，Agent `/health` 返回`status=ok`。浏览器原登录已过期，使用浏览器既有记住的账号正常重新登录，未读取口令文件或提取Token；维修页面已重新打开。未执行新工具的真实业务写入。

运行界面补充核验：维修列表正常显示原有1条记录；通过智能助手正常输入“重新检查当前维修单查询和操作预览工具，只列能力不执行写入”。本轮已完成，页面展示search_tools发现记录并列出6个查询和6个操作预览工具，确认新工具已被运行中的助手识别。未调用维修业务写入工具；这不是完整业务写入验收。证据为 `output/playwright/T08-20261026/browser-runtime-tools.txt` 和 `browser-runtime-tools.png`。

运行日志位于 `.runtime/t08-activation-20261026/`：`backend-build.log`、`backend-211355.stdout.log`、`backend-211355.stderr.log`、`agent-211355.stdout.log`、`agent-211355.stderr.log`、`frontend-211355.stdout.log`、`frontend-211355.stderr.log`。本节仅报告本地启动与健康检查，不声称已验证长时间运行稳定性。

## 对应任务

`E:\ktg_agent\docs\总体\任务\任务清单\T08-实现维修单Agent工具.md`。

## 后续真实MES助手对话实测（2026-10-26）

按用户“你来做”的要求，主任务通过内置浏览器执行45轮真实对话，结合实际工具调用、网页确认回执及回读核验44条逻辑测试项。35条功能通过、3条有体验问题、1条规则观察、2条局部覆盖、3条条件场景未完整覆盖，不称全部44条已完整通过。

实际网页确认成功8次，覆盖维修主单及维修内容的新增、修改、删除6类操作；旧快照和自然过期预览均被拒绝。删除在用户明确确认后执行：先删除专用明细ID 1，再刷新预览删除测试主单ID 2及剩余明细ID 2。最终系统恢复为原有1张维修单；原单ID 1的工具可查询字段与测试前完全一致、明细仍为0条，未确认的测试单B没有保存。仅保留对话及预览等测试证据，不存在本轮新增的维修业务记录。

发现工具未加载先调用、日期称呼引发重复确认、自动编号范围表述、超时原因过度断言等改进点。没有修改产品代码，没有将测试观察直接修成代码；非草稿、低权限、真实UNKNOWN故障等未完整覆盖场景在报告中列明。

实测报告：`E:/ktg_agent/docs/总体/任务/完成情况/T08-维修单助手实际对话测试报告-2026-10-26.md`。
证据：`E:/ktg_agent/ktg_agent_v1/output/repair-assistant-e2e-20261026/`，含逐轮审计、页面截图、持久确认回执及最终恢复校验。
