<template>
  <section class="result-card" :aria-label="title">
    <div class="result-card__heading">
      <span>{{ title }}</span>
      <span v-if="result.total !== undefined">共 {{ result.total }} 条</span>
    </div>
    <div v-if="result.kind === 'product_count'" class="result-card__count">当前账号可见的产品记录，不含物料。</div>
    <dl v-else-if="result.kind === 'workorder_progress'" class="result-card__facts">
      <div><dt>工单</dt><dd>{{ result.workorder.workorderCode }} · {{ result.workorder.workorderName }}</dd></div>
      <div><dt>计划量</dt><dd>{{ result.workorder.quantity }} {{ result.metrics.unit || '' }}</dd></div>
      <div><dt>已报产量</dt><dd>{{ result.metrics.workorder_produced_fact }} {{ result.metrics.unit || '' }}</dd></div>
      <div><dt>算术差</dt><dd>{{ result.metrics.remaining_raw === null ? '不可用' : result.metrics.remaining_raw }}</dd></div>
      <div><dt>算术比</dt><dd>{{ result.metrics.ratio_percent === null ? '不可用' : `${result.metrics.ratio_percent}%` }}</dd></div>
      <div><dt>数据观察</dt><dd>{{ result.completeness.observation_grade }} · {{ result.completeness.snapshot_consistent ? '同一快照' : '非同一快照' }}</dd></div>
      <p class="result-card__meta">{{ result.completeness.business_progress_reason || '业务进度规则未确认' }}；该视图由多次独立读取组成。</p>
    </dl>
    <dl v-else-if="result.data" class="result-card__facts">
      <div v-for="(value, key) in result.data" :key="key"><dt>{{ fieldLabel(key) }}</dt><dd>{{ value === null ? '—' : value }}</dd></div>
    </dl>
    <div v-else-if="result.rows && result.rows.length" class="result-card__rows">
      <div v-for="(row, index) in result.rows" :key="rowKey(row, index)" class="result-card__row">
        <template v-if="result.kind === 'items'">
          <strong>{{ row.itemName || '未命名产品' }}</strong>
          <span>{{ row.itemCode || '-' }} · {{ row.specification || '无规格' }}</span>
        </template>
        <template v-else-if="result.kind === 'stock'">
          <strong>{{ row.itemName || row.itemCode || '库存记录' }}</strong>
          <span>{{ row.warehouseName || '未分仓' }} · {{ row.quantityOnhand }} {{ row.unitOfMeasure || '' }}</span>
        </template>
        <template v-else-if="result.kind === 'protask'">
          <strong>{{ row.taskCode || `任务 ${row.taskId}` }}</strong>
          <span>{{ row.taskName || '-' }} · 工单 {{ row.workorderCode || row.workorderId }} · {{ row.status || '未知状态' }}</span>
        </template>
        <template v-else-if="result.kind === 'feedback'">
          <strong>{{ row.taskCode || row.processName || `报工 ${row.recordId}` }}</strong>
          <span>{{ row.feedbackTime || '-' }} · 合格 {{ row.quantityQualified == null ? '—' : row.quantityQualified }} {{ row.unitOfMeasure || '' }}</span>
        </template>
        <template v-else>
          <strong>{{ row.workorderCode || '未编号工单' }}</strong>
          <span>{{ row.workorderName || row.productName || '-' }} · {{ row.status || '未知状态' }}</span>
        </template>
      </div>
    </div>
    <p v-else class="result-card__empty">没有符合条件的数据</p>
    <p v-if="result.kind !== 'product_count' && result.rows" class="result-card__meta">第 {{ result.page }} 页，每页 {{ result.page_size }} 条</p>
  </section>
</template>

<script>
export default {
  name: 'AgentResultCard',
  props: {
    result: {
      type: Object,
      required: true
    }
  },
  computed: {
    title() {
      return {
        product_count: '产品总数',
        items: '产品查询',
        stock: '库存查询',
        workorders: '工单查询',
        workorder: '工单详情', protask: '生产任务', feedback: '报工记录', workorder_progress: '工单进度试验视图'
      }[this.result.kind] || '查询结果'
    }
  },
  methods: {
    fieldLabel(key) {
      return ({ workorderId: '工单 ID', workorderCode: '工单编号', workorderName: '工单名称',
        taskId: '任务 ID', taskCode: '任务编号', taskName: '任务名称', feedbackType: '报工类型',
        recordId: '报工 ID', processName: '工序', productName: '产品', productCode: '产品编码',
        quantity: '数量', quantityProduced: '已生产', quantityFeedback: '报工数量',
        quantityQualified: '合格数', quantityUnquanlified: '不合格数', unitOfMeasure: '单位',
        status: '状态', feedbackTime: '报工时间', requestDate: '需求日期', version: '版本'
      })[key] || key
    },
    rowKey(row, index) {
      return row.itemId || row.materialStockId || row.workorderId || index
    }
  }
}
</script>

<style lang="scss" scoped>
.result-card {
  margin-top: 10px;
  padding: 12px;
  border: 1px solid #dfe4ed;
  border-radius: 6px;
  background: #fff;
}

.result-card__heading,
.result-card__row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.result-card__heading {
  margin-bottom: 8px;
  color: #303133;
  font-size: 13px;
  font-weight: 600;
}

.result-card__row {
  flex-direction: column;
  gap: 3px;
  padding: 8px 0;
  border-top: 1px solid #ebeef5;
  color: #606266;
  font-size: 12px;
}

.result-card__row strong {
  color: #303133;
  font-size: 13px;
}

.result-card__empty,
.result-card__meta {
  margin: 6px 0 0;
  color: #909399;
  font-size: 12px;
}
</style>

