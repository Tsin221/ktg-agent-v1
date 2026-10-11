# 生产工单字段与状态契约（S2-01）

本契约以 `pro_workorder`、`ProWorkorder`、`ProWorkorderMapper.xml`、工单页面和 Agent 工具的实际实现为准。数据库迁移为 `ktg-mes-master/ktg-mes-master/sql/20261009_s2_01_workorder_contract.sql` 和 `20261009_s2_04_workorder_draft_version.sql`。

## 业务结论

- `workorderType` 必须保留。`SELF`（自制）、`OUTSOURCE`（外协）和 `PURCHASE`（外购）被排产、BOM 操作、外协领料及外协收货使用，不是纯展示字段。
- 新建工单只能创建 `PREPARE` 草稿。服务端忽略调用方传入的其他状态；缺少 `workorderType` 时按 `SELF` 保存。
- 通用编辑接口只允许修改数据库中仍为 `PREPARE` 的工单，且 Mapper 不更新 `status`。确认、完成和取消必须使用后续独立动作，不能借通用编辑接口改状态。
- 草稿编辑使用 `version` 乐观锁。详情和列表返回当前版本；编辑必须原样回传。数据库以 `workorder_id + status=PREPARE + version` 条件更新并原子递增版本，旧客户端缺少版本会收到 409，不能继续无条件覆盖。
- `workorderCode` 是业务唯一键。服务层保留友好提示，数据库迁移增加 `uk_pro_workorder_code` 唯一索引，消除并发写入竞态。
- 产品的真实数据源是 `md_item`。写入时服务端按 `productId` 回读产品，并生成 `productCode/productName/productSpc/unitOfMeasure` 快照；客户端提交的同名快照字段会被忽略。
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
| `productCode` | `product_code` | 已映射 | 只读快照、筛选 | 查询输入/输出；草稿写入忽略 | 服务端取自 `md_item.item_code` |
| `productName` | `product_name` | 已映射 | 只读快照、筛选 | 查询输出；草稿写入忽略 | 服务端取自 `md_item.item_name` |
| `productSpc` | `product_spc` | 已映射 | 只读快照 | 草稿写入忽略 | 服务端取自 `md_item.specification` |
| `unitOfMeasure` | `unit_of_measure` | 已映射 | 保存并显示单位编码 | 查询输出；草稿写入忽略 | 服务端取自 `md_item.unit_of_measure`；页面已修正误用 `unitName` |
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
| `version` | `version` | 已映射 | 编辑时随详情回传 | 查询输出；草稿写入必填 | 新建为 `0`；每次成功编辑加 `1` |
| `remark` | `remark` | `BaseEntity` + Mapper | 草稿备注 | 后续草稿写入可选 | 最长 500（页面当前限制 250） |
| `attr1..attr4` | 同名列 | 已映射 | 不展示 | 无 | 兼容保留，不纳入 Agent 输入 |
| `createBy/createTime/updateBy/updateTime` | 同名蛇形列 | 基类 + Mapper | 审计 | 无 | 服务端维护，只读 |

当前 `ktg_list_workorders` 的输入为 `workorder_code`、`product_code`、`workorder_type`、`status`、`page`、`page_size`；输出除分页信息外包含工单标识、编号、名称、类型、产品、单位、数量、已生产数量、需求日期、状态和 `version`。

## 状态与动作

| 动作 | 目标状态 | 当前后端 | 当前前端处理 | 后续归属 |
|---|---|---|---|---|
| 创建草稿 | `PREPARE` | `POST /mes/pro/workorder` 可用，工单与 BOM 在同一事务创建 | 可用 | S2-02 可据此封装 Agent 草稿工具 |
| 编辑草稿 | `PREPARE` | `PUT /mes/pro/workorder` 可用；请求必须携带 `version`，仅产品或数量变化时在同一事务重建 BOM | 可用 | S2-02 |
| 查询 | 不变 | `GET /list`、`GET /{id}` 可用 | 可用 | S2-02 复用 |
| 确认 | `CONFIRMED` | **不可用**；无独立接口 | 本任务移除通用编辑伪确认按钮 | S2-06 实现独立动作及并发校验 |
| 完成 | `FINISHED` | **不可用**；`/finish/{id}` 无控制器映射 | API 占位声明已移除，页面不展示失效按钮 | 后续独立状态动作任务 |
| 取消 | `CANCELED` | **不可用**；`/cancel/{id}` 无控制器映射 | API 占位声明已移除，页面不展示失效按钮 | 后续独立状态动作任务 |
| 携任务查询 | 不变 | **不可用**；`/listWithTaskJson` 无控制器映射 | API 占位声明和遗留调用已移除；旧进度页只展示真实列表并明确提示任务进度暂不可用 | 后续任务进度接口任务 |

## S2-06 前端交接

- 创建和编辑统一通过草稿字段白名单组装请求；产品快照、状态、执行数量、审计字段和未落库的供应商字段不会发送。
- 创建成功后用响应中的工单 ID 回读详情；编辑成功后同样回读，页面以服务端的 `status`、`version`、产品快照、单位和日期为准。
- 编辑遇到 409 乐观锁冲突时保留用户输入，展示服务端返回的当前状态和版本，并提供带覆盖确认的“重新读取最新工单”入口。
- 可复用草稿输入字段：`workorderCode`、`workorderName`、`workorderType`、`orderSource`、`sourceCode`、`productId`、`batchCode`、`quantity`、`clientId/clientCode/clientName`、`requestDate`、`parentId`、`remark`；编辑额外要求 `workorderId` 和 `version`。

## 样例（交付 S2-02 / S2-06 / S2-07）

创建草稿请求：

```json
{
  "workorderCode": "WO-20261009-001",
  "workorderName": "A01 自制工单",
  "workorderType": "SELF",
  "orderSource": "MANUAL",
  "productId": 101,
  "quantity": 100,
  "requestDate": "2026-10-31",
  "parentId": 0,
  "remark": "首批"
}
```

成功响应的 `data` 是新工单 ID；随后查询必须返回 `status: "PREPARE"` 和请求中的 `workorderType`。调用方即使在创建请求中传入其他 `status`，服务端仍保存为 `PREPARE`。

编辑草稿请求必须使用最近一次详情或列表响应中的版本：

```json
{
  "workorderId": 101,
  "version": 0,
  "workorderCode": "WO-20261009-001",
  "workorderName": "A01 自制工单（修订）",
  "workorderType": "SELF",
  "orderSource": "MANUAL",
  "productId": 101,
  "quantity": 120,
  "requestDate": "2026-10-31",
  "parentId": 0
}
```

成功后数据库版本变为 `1`；页面会同步递增本地版本。若版本过期或状态已不再是 `PREPARE`，接口返回 `code: 409`，消息包含数据库当前状态和版本，例如“当前状态：PREPARE，当前版本：1”。调用方必须重新读取并让用户合并，不能自动覆盖或盲目重试。缺少 `version` 同样返回 409，并要求重新加载工单。

### S2-07 回读交接

- 创建成功：`POST /mes/pro/workorder` 返回 `{"code":200,"data":<workorderId>}`，`data` 是数据库生成且已提交的确定工单 ID；响应成功时对应 BOM 已在同一事务提交。
- 回读：使用 `GET /mes/pro/workorder/{workorderId}`。调用方无需按工单编号二次查找，也不应在创建成功后自行补写 BOM。
- 冲突：并发提交相同 `workorderCode` 时数据库唯一索引只允许一条成功，失败方得到 `code: 409` 和“生产工单编号已存在！”；不能把它当成可重试的未知错误。
- 编辑冲突：两个客户端读取同一 `version` 后只能有一个更新成功；失败方得到 `code: 409` 和当前状态/版本，工单与 BOM 均保持获胜事务的完整结果。
- 回滚：创建或编辑过程中任一 BOM 行写入失败时接口失败，数据库保持调用前状态。

迁移前应确认没有重复 `workorder_code`；若存在重复值，唯一索引会拒绝创建，需先由业务负责人确定保留或改号方案。迁移执行后应确认 `workorder_type` 非空、既有行已回填 `SELF`、`uk_pro_workorder_code` 存在，再做一次草稿新增与查询往返。
