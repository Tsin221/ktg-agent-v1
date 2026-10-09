# 生产工单字段与状态契约（S2-01）

本契约以 `pro_workorder`、`ProWorkorder`、`ProWorkorderMapper.xml`、工单页面和 Agent 工具的实际实现为准。数据库迁移为 `ktg-mes-master/ktg-mes-master/sql/20261009_s2_01_workorder_contract.sql`。

## 业务结论

- `workorderType` 必须保留。`SELF`（自制）、`OUTSOURCE`（外协）和 `PURCHASE`（外购）被排产、BOM 操作、外协领料及外协收货使用，不是纯展示字段。
- 新建工单只能创建 `PREPARE` 草稿。服务端忽略调用方传入的其他状态；缺少 `workorderType` 时按 `SELF` 保存。
- 通用编辑接口只允许修改数据库中仍为 `PREPARE` 的工单，且 Mapper 不更新 `status`。确认、完成和取消必须使用后续独立动作，不能借通用编辑接口改状态。
- `workorderCode` 是业务唯一键。服务层保留友好提示，数据库迁移增加 `uk_pro_workorder_code` 唯一索引，消除并发写入竞态。
- 产品的真实数据源是 `md_item`。页面通过产品选择器取得 `itemId/itemCode/itemName/specification/unitOfMeasure`，保存为工单快照字段 `productId/productCode/productName/productSpc/unitOfMeasure`。当前后端尚未按 `productId` 重新校验快照，Agent 写工具在 S2-02 中必须先查产品并传入一致快照。
- `quantity` 是计划生产数量，必须大于 0；`quantityProduced`、`quantityChanged`、`quantityScheduled` 是执行侧累计量，不属于草稿编辑字段。`requestDate` 是需求完成日期，不是创建或开工时间。

## 字段对照

| JSON / 页面字段 | 数据库列 | Java / Mapper | 页面用途 | Agent 工具 | 处理结论 |
|---|---|---|---|---|---|
| `workorderId` | `workorder_id` | 已映射 | 详情、树主键 | 查询输出 | 数据库生成，只读 |
| `workorderCode` | `workorder_code` | 已映射 | 新建、编辑、筛选 | 查询输入/输出；后续草稿写入必填 | 最长 64，数据库唯一 |
| `workorderName` | `workorder_name` | 已映射 | 新建、编辑、筛选 | 查询输出；后续草稿写入必填 | 最长 255（页面当前限制 100，可继续从严） |
| `workorderType` | `workorder_type` | 本任务补齐 | 输入、筛选、条件操作 | 查询输入/输出；后续草稿写入可选，默认 `SELF` | 枚举 `SELF/OUTSOURCE/PURCHASE`，既有数据回填 `SELF` |
| `orderSource` | `order_source` | 已映射 | 输入、展示 | 后续草稿写入必填 | 来源类型快照 |
| `sourceCode` | `source_code` | 已映射 | 订单来源时输入、筛选 | 后续草稿写入可选 | 外部来源单号 |
| `productId` | `product_id` | 已映射 | 产品选择器 | 后续草稿写入必填 | 引用 `md_item.item_id` |
| `productCode` | `product_code` | 已映射 | 只读快照、筛选 | 查询输入/输出；后续草稿写入必填 | 来源 `md_item.item_code` |
| `productName` | `product_name` | 已映射 | 只读快照、筛选 | 查询输出；后续草稿写入必填 | 来源 `md_item.item_name` |
| `productSpc` | `product_spc` | 已映射 | 只读快照 | 后续草稿写入可选 | 来源 `md_item.specification` |
| `unitOfMeasure` | `unit_of_measure` | 已映射 | 保存并显示单位编码 | 查询输出；后续草稿写入必填 | 来源 `md_item.unit_of_measure`；本任务已修正页面误用 `unitName` |
| `unitName` | 无 | 无 | 产品选择器返回的临时字段 | 无 | 不持久化，不能作为写入字段 |
| `batchCode` | `batch_code` | 已映射 | 当前主页面未编辑 | 无 | 保留后端字段，不纳入 Agent 草稿输入 |
| `quantity` | `quantity` | 已映射 | 草稿输入 | 查询输出；后续草稿写入必填 | 计划生产数量，正数 |
| `quantityProduced` | `quantity_produced` | 已映射；查询已补齐 | 只读展示 | 查询输出 | 执行累计量，草稿编辑不可写 |
| `quantityChanged` | `quantity_changed` | 已映射；查询已补齐 | 页面模型预留 | 无 | 调整累计量，草稿编辑不可写 |
| `quantityScheduled` | `quantity_scheduled` | 已映射；查询已补齐 | 页面模型预留 | 无 | 排产累计量，草稿编辑不可写 |
| `clientId/clientCode/clientName` | 同名蛇形列 | 已映射 | 订单来源时选择客户 | 后续草稿写入可选 | 客户快照，三者应一致 |
| `vendorId/vendorCode/vendorName` | 无 | 无 | 外协/外购时的遗留输入 | 无 | 当前不会保存，不能进入 Agent 工具；供应商归属待后续业务任务确定后另行迁移 |
| `requestDate` | `request_date` | 已映射 | 草稿必填 | 查询输出；后续草稿写入必填 | 需求完成日期，`yyyy-MM-dd` |
| `parentId/ancestors` | 同名蛇形列 | `TreeEntity` + Mapper | 父子工单树 | 无 | `parentId` 可由页面选择；`ancestors` 服务端生成 |
| `status` | `status` | 已映射 | 展示及可编辑性判断 | 查询输入/输出 | 创建固定 `PREPARE`；通用编辑不可变更 |
| `remark` | `remark` | `BaseEntity` + Mapper | 草稿备注 | 后续草稿写入可选 | 最长 500（页面当前限制 250） |
| `attr1..attr4` | 同名列 | 已映射 | 不展示 | 无 | 兼容保留，不纳入 Agent 输入 |
| `createBy/createTime/updateBy/updateTime` | 同名蛇形列 | 基类 + Mapper | 审计 | 无 | 服务端维护，只读 |

当前 `ktg_list_workorders` 的输入为 `workorder_code`、`product_code`、`workorder_type`、`status`、`page`、`page_size`；输出除分页信息外包含工单标识、编号、名称、类型、产品、单位、数量、已生产数量、需求日期和状态。

## 状态与动作

| 动作 | 目标状态 | 当前后端 | 当前前端处理 | 后续归属 |
|---|---|---|---|---|
| 创建草稿 | `PREPARE` | `POST /mes/pro/workorder` 可用，服务端强制默认值 | 可用 | S2-02 可据此封装 Agent 草稿工具 |
| 编辑草稿 | `PREPARE` | `PUT /mes/pro/workorder` 可用，仅限既有草稿且不能改状态 | 可用 | S2-02 |
| 查询 | 不变 | `GET /list`、`GET /{id}` 可用 | 可用 | S2-02 复用 |
| 确认 | `CONFIRMED` | **不可用**；无独立接口 | 本任务移除通用编辑伪确认按钮 | S2-06 实现独立动作及并发校验 |
| 完成 | `FINISHED` | **不可用**；`/finish/{id}` 无控制器映射 | API 声明保留为契约占位，页面不再展示失效按钮 | S2-07 |
| 取消 | `CANCELED` | **不可用**；`/cancel/{id}` 无控制器映射 | API 声明保留为契约占位，页面不再展示失效按钮 | S2-07 |
| 携任务查询 | 不变 | **不可用**；`/listWithTaskJson` 无控制器映射 | 排产页面仍有遗留调用，不应视为已交付能力 | S2-06 核定响应结构后实现或替换 |

## 样例（交付 S2-02 / S2-06 / S2-07）

创建草稿请求：

```json
{
  "workorderCode": "WO-20261009-001",
  "workorderName": "A01 自制工单",
  "workorderType": "SELF",
  "orderSource": "MANUAL",
  "productId": 101,
  "productCode": "A01",
  "productName": "产品 A01",
  "productSpc": "标准型",
  "unitOfMeasure": "PCS",
  "quantity": 100,
  "requestDate": "2026-10-31",
  "parentId": 0,
  "remark": "首批"
}
```

成功响应的 `data` 是新工单 ID；随后查询必须返回 `status: "PREPARE"` 和请求中的 `workorderType`。调用方即使在创建请求中传入其他 `status`，服务端仍保存为 `PREPARE`。

迁移前应确认没有重复 `workorder_code`；若存在重复值，唯一索引会拒绝创建，需先由业务负责人确定保留或改号方案。迁移执行后应确认 `workorder_type` 非空、既有行已回填 `SELF`、`uk_pro_workorder_code` 存在，再做一次草稿新增与查询往返。
