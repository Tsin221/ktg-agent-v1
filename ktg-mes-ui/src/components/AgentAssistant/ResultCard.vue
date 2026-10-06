<template>
  <section class="result-card" :aria-label="title">
    <div class="result-card__heading">
      <span>{{ title }}</span>
      <span>{{ result.total }} 条</span>
    </div>
    <div v-if="result.rows && result.rows.length" class="result-card__rows">
      <div v-for="(row, index) in result.rows" :key="rowKey(row, index)" class="result-card__row">
        <template v-if="result.kind === 'items'">
          <strong>{{ row.itemName || '未命名产品' }}</strong>
          <span>{{ row.itemCode || '-' }} · {{ row.specification || '无规格' }}</span>
        </template>
        <template v-else-if="result.kind === 'stock'">
          <strong>{{ row.itemName || row.itemCode || '库存记录' }}</strong>
          <span>{{ row.warehouseName || '未分仓' }} · {{ row.quantityOnhand }} {{ row.unitOfMeasure || '' }}</span>
        </template>
        <template v-else>
          <strong>{{ row.workorderCode || '未编号工单' }}</strong>
          <span>{{ row.workorderName || row.productName || '-' }} · {{ row.status || '未知状态' }}</span>
        </template>
      </div>
    </div>
    <p v-else class="result-card__empty">没有符合条件的数据</p>
    <p class="result-card__meta">第 {{ result.page }} 页，每页 {{ result.page_size }} 条</p>
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
        items: '产品查询',
        stock: '库存查询',
        workorders: '工单查询'
      }[this.result.kind] || '查询结果'
    }
  },
  methods: {
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

