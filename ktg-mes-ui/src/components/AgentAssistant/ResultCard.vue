<template>
  <section class="result-card" :aria-label="title">
    <div v-if="result.kind === 'product_count'" class="result-card__heading">
      <span>{{ title }}</span>
      <span v-if="result.total !== undefined">共 {{ result.total }} 条</span>
    </div>
    <div v-if="result.kind === 'product_count'" class="result-card__count">当前账号可见的产品记录，不含物料。</div>
    <template v-else-if="result.kind === 'stock_summary'">
      <div class="result-card__heading">{{ title }}</div>
      <p v-if="result.no_records" class="result-card__empty">无匹配库存记录；不代表库存量为零。</p>
      <div v-for="group in result.groups || []" :key="`${group.itemId}-${group.warehouseId}-${group.unitOfMeasure}`" class="result-card__row">
        <strong>{{ group.warehouseName || group.warehouseCode || '未分仓' }}</strong>
        <span>{{ group.itemCode }} · {{ group.quantityOnhand }} {{ group.unitOfMeasure || '' }}</span>
      </div>
      <p v-if="result.queried_at" class="result-card__meta">查询时间 {{ result.queried_at }}</p>
    </template>
    <details v-else class="result-card__history">
      <summary>历史结果（仅供查看，不可执行）</summary>
      <pre>{{ historyText }}</pre>
    </details>
  </section>
</template>

<script>
export default {
  name: 'AgentResultCard',
  props: {
    result: { type: Object, required: true }
  },
  computed: {
    title() {
      return { product_count: '产品总数', stock_summary: '分仓库存汇总' }[this.result.kind] || '历史结果'
    },
    historyText() {
      return JSON.stringify(this.result, null, 2)
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
.result-card__history summary { cursor: pointer; }
.result-card__history pre {
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
</style>

