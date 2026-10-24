# 设备台账 Agent 工具设计方案

日期：2026-10-24
状态：已实现，待人工审阅

## 目标与边界

为 MES「设备台账」页面增加可供 Agent 调用的查询和受控维护能力，沿用设备类型工具的工具目录、权限清单、预览卡片和用户确认执行机制。首期只覆盖设备列表、详情、车间候选和设备新增/修改/单条删除；设备类型候选直接复用现有 `ktg_dv_machinerytype_tree`。不纳入导入（页面已移除）、最近点检/保养时间（页面已移除）、导出和标签打印。维修记录查询可作为下一阶段，不因页面有维修页签就扩大首期写范围。

本方案是静态源码核查结果，不代表对当前运行数据库的约束或权限进行了联调验证。

## 已核实的现有契约

设备台账 API 为 `GET /mes/dv/machinery/list`、`GET /mes/dv/machinery/{machineryId}`、`POST /mes/dv/machinery`、`PUT /mes/dv/machinery`、`DELETE /mes/dv/machinery/{machineryIds}`。对应权限分别为 `mes:dv:machinery:list`、`query`、`add`、`edit`、`remove`。新增、修改、删除已有操作日志；新增还会生成设备条码。

列表后端过滤支持：编码精确、名称模糊、品牌和规格精确、分类 ID（包括该分类的子类）、分类编码精确/名称模糊、车间 ID/编码精确/名称模糊、状态精确。返回分页表格。详情 API 使用设备 ID。

页面必填字段为设备编码（最多 64 字符）、设备名称（最多 255）、分类 ID 和车间 ID；品牌、规格型号各最多 255，备注数据库字段为 500 字符。状态字典在当前页面使用 `STOP`、`WORKING`、`REPAIR`，新建页面默认 `STOP`。设备类型候选使用现有类型树接口；树数据含启用标记和父子关系，选择应限定到启用叶子节点。车间候选使用 `/mes/md/workshop/listAll`，要求 `mes:md:workshop:list`，只返回启用车间。

点检关联记录保存设备 ID 与编码，维修记录按设备编码查询。当前设备删除服务/mapper 中观察到单条或批量直接删除调用，未看到级联清理或关联阻断逻辑；这不能证明运行环境没有其他约束。`mes-dv.sql` 静态 DDL 仅定义设备 ID 主键，没有给设备编码定义唯一索引；运行库是否另有约束尚未验证。设备台账控制器没有发现设备编码唯一性校验方法。自动编码 GET `/system/autocode/get/MACHINERY_CODE` 会在生成后保存编码生成结果，因此它是有状态操作，不能在承诺无副作用的预览读工具中调用。

设备类型工具的安全模式可复用：写预览与执行分开；执行工具不对模型公开，只由前端用户明确确认后调用；服务端重新核验当前用户身份/权限、预览归属、上下文、TTL 和状态；执行失败不自动重试。设备类型写工具由本地预览存储配置开关控制。设备台账写工具应采用独立的显式开关/预览存储配置，不应悄悄共用不同业务数据的存储表。

## 工具清单

首期新增 **6 个模型可见工具**（3 个只读、3 个只生成写入预览），另有 **3 个隐藏的确认执行工具**。已有类型树工具可直接复用，不计入新增数。

| 模型可见工具 | 用途与输入 | 返回与主要校验 |
|---|---|---|
| `ktg_dv_machinery_list` | 可选 `machinery_code`、`machinery_name`、`machinery_brand`、`machinery_spec`、`machinery_type_id`、`machinery_type_code`、`machinery_type_name`、`workshop_id`、`workshop_code`、`workshop_name`、`status`；`page`、`page_size` 有硬上限 | 有界的设备行、总数、筛选条件和截断/分页信息。拒绝未知字段、无效状态和超限页长；模糊筛选只展示候选，不推断唯一对象。权限 `mes:dv:machinery:list`。 |
| `ktg_dv_machinery_detail` | 必须且只能提供 `machinery_id` 或 `machinery_code` 之一；编码必须精确匹配 | 设备详情与规范化 ID/编码。编码查找先做精确筛选；无结果为 NOT_FOUND，多条为 AMBIGUOUS 并返回有限候选让用户改用 ID。权限 `mes:dv:machinery:query`。 |
| `ktg_md_machinery_workshop_options` | 可选车间名称/编码筛选，限制返回数量 | 只调用现有启用车间列表，返回 ID、编码、名称、启用状态。多条同名时要求按编码或 ID 确认，不由模型猜选。权限 `mes:md:workshop:list`。 |
| `ktg_preview_dv_machinery_create` | 设备编码、名称、分类 ID、车间 ID 必填；品牌、规格、状态、备注可选 | 不写入设备；校验字段长度与状态；读取并确认分类为启用叶子、车间为启用且二者 ID/名称匹配；检查编码当前是否已存在。返回完整变更卡片与确认 ID。当前建议要求调用者提供编码，避免预览时调用会消耗流水号的自动编码接口。权限 `mes:dv:machinery:add`、`mes:dv:machinery:list`，以及解析候选所需权限。 |
| `ktg_preview_dv_machinery_update` | `machinery_id` 必填；可修改名称、品牌、规格、分类 ID、车间 ID、状态、备注中的一个或多个字段 | 读取当前设备，返回前后对照；字段未提供表示不变，显式空字符串用于清空可空字段；不允许变更设备编码、ID、审计时间；拒绝无变化请求，验证分类/车间仍可用。权限 `mes:dv:machinery:edit`、`mes:dv:machinery:query`，关联候选权限按需检查。 |
| `ktg_preview_dv_machinery_delete` | 必须提供单个 `machinery_id`；不提供批量接口 | 只展示目标设备和将执行的单条删除；要求用户确认。权限 `mes:dv:machinery:remove`、`mes:dv:machinery:query`。预览需明确说明当前实现未证实有关联保护/级联规则；未经设计并验证安全的关联查询，不宣称“无关联”或“可安全删除”。 |

隐藏执行工具名建议为 `ktg_dv_machinery_create`、`ktg_dv_machinery_update`、`ktg_dv_machinery_delete`。三者 `expose_to_model=false`，仅可信确认路由可调用，策略为 `trusted_confirmation`，效果为 WRITE。创建预览显示用户提供的编码，并提示后端新增会生成条码；编码唯一性需服务端确认后续业务规则，当前源码未看到设备编码唯一性校验或 DDL 唯一索引。不要调用状态有副作用的自动编码接口去伪造“预览候选”。

## 删除及关联记录边界

现有源码显示维修记录通过设备编码关联，点检设备记录也保存设备 ID 和编码。设备 mapper 的删除语句是直接删除，未见与这些表的联动。首期删除只允许单台且在页面展示完整设备编码、名称、ID 和不可撤销提示，依赖同一预览确认机制；执行前重新读取目标，检查记录仍存在且身份/预览匹配。是否需新增基于维修单和点检关系的阻断规则，应由业务方确认，并先核实查询权限、关联完整性和历史数据覆盖，再单独设计；在此之前不能把未覆盖的关系说成“已检查无引用”。

## 典型交互

用户说“找出二车间停机的压缩机”，Agent 先调用类型树和车间候选（若用户给的是名称），将唯一候选 ID 用于设备列表筛选；如候选不唯一，先展示选项并询问，不直接套用模糊名称。

用户说“新增一台设备”，Agent 收集编码、名称、分类和车间；确认分类是启用叶子且车间唯一后生成创建预览。前端卡片展示全部字段、空值默认和条码副作用，用户点击确认后才触发隐藏执行工具。

修改和删除均先按设备 ID 定位。若用户按编码/名称表达且无法唯一确定，先返回候选，不创建写入预览。每次预览均绑定当前身份、会话上下文、目标记录和短时有效期；过期或记录变化时要求重新预览，不自动重放写请求。

## 建议实现落点

- 读取工具：`ktg-agent/src/ktg_agent/ktg/dv_machinery.py`，遵循 `dv_machinerytype.py` 的 schema、KTG 调用、错误分类、结果截断、manifest/factory 模式。
- 写预览与执行：`ktg-agent/src/ktg_agent/ktg/dv_machinery_writes.py`，仿照 `dv_machinerytype_writes.py` 的 SQLite 预览存储和身份/权限/过期校验，新增专属配置开关与存储路径。
- 注册：`ktg-agent/src/ktg_agent/service.py`、`server.py`，按设备类型模式注册读工具并以独立 feature flag 启用写工具。
- 用户确认卡片：`ktg-mes-ui/src/components/AgentAssistant/DvMachineryCard.vue` 和 `AgentAssistant/index.vue`；新增对应确认 API 到 `ktg-mes-ui/src/api/agent.js`，复用现有 trusted confirmation 路由的身份与会话校验模式，并发出台账变更事件刷新页面数据。
- 测试：`ktg-agent/tests/unit/test_dv_machinery.py`、`test_dv_machinery_writes.py`；前端卡片按项目现有测试体系覆盖预览/确认/取消/失败显示。

## 建议验证范围

读取：筛选字段映射、分页上下限、空结果和截断、未知参数、权限缺失、ID/编码精确匹配及歧义处理、启用分类/车间与分类叶子校验。写入：预览不触发 POST/PUT/DELETE、不调用自动编码生成接口；必填与长度/状态校验；关联 ID 名称核验；确认后才写；身份不符、权限撤销、预览过期/重复消费、目标记录被修改、后端超时未知结果和 API 错误均需安全返回且不自动重试。删除测试需覆盖单条确认与风险提示，不用真实数据。

## 实施与验证记录

已按本方案接入三项只读工具、三项模型可见写预览和三项隐藏确认执行工具，并挂接 AgentAssistant 卡片与 `/agent-api/v1/dv-machinery/confirm`。工具目录默认加入只读项；设备台账写入须显式设置 `AGENT_ENABLE_DV_MACHINERY_WRITE=true` 并重启服务，预览数据库可用 `AGENT_DV_MACHINERY_DB` 指定独立绝对路径。确认端使用请求中的登录态重新取得用户身份和权限，核对预览归属，再通过 trusted-confirmation 通道调用隐藏执行定义；模型候选不包含隐藏执行工具。

实现要求创建者提供设备编码；预览和确认前会读取列表检查是否已存在，但源码未发现后端唯一性校验或数据库唯一索引，因此这不能消除并发请求的编码冲突风险。写执行不会自动重试，成功以 MES 回读核验为准；不确定结果提示人工核对。删除仅针对单台设备，并显示关联记录处理方式未验证的风险提示，不声称已检查维修/点检关联。没有访问真实 MES 或修改真实设备。

验证记录：设备列表/详情/候选与新增/修改/删除预览和确认的 mock 测试，服务端确认身份/权限链路、隐藏工具目录可见性、HTTP 确认路由和旧设备类型工具注册回归共 65 项通过。AgentAssistant session 定向脚本通过。Vue staging 构建成功；有既存的 bundle size 警告。新增前端卡片及相关文件定向 ESLint、Agent Python 模块 strict mypy 与 `git diff --check` 的最终结果见交付说明。构建期间仅输出已有的大包体积建议。

本实现未覆盖维修记录、导入、导出和标签打印；导入和最近点检/保养时间已由此前页面改动移除。设备编码并发唯一性、删除关联记录的 MES 后端行为、生产环境实际角色权限仍未作运行环境验证。
