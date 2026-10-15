<template>
  <section class="inventory-card" :aria-label="title">
    <header><strong>{{ title }}</strong><span>{{ result.documentCode || result.document_code || '-' }}</span></header>
    <template v-if="result.kind === 'inventory_preview'">
      <p>单据状态：{{ result.documentStatus }} · 版本：{{ result.documentVersion }}</p>
      <div v-for="(line, index) in lines" :key="line.sourceDocLineId || index" class="inventory-card__line">
        <strong>{{ line.itemName || line.itemCode }}（{{ line.itemCode }}）</strong>
        <span>数量：{{ line.quantity }} {{ line.unitOfMeasure || '' }} · 批次：{{ line.batchCode || '-' }}</span>
        <span v-if="isReceipt">目标：{{ location(line.warehouseName, line.locationName, line.areaName) }}</span>
        <template v-else>
          <span>来源：{{ location(line.sourceWarehouseName, line.sourceLocationName, line.sourceAreaName) }}</span>
          <span>目标：{{ location(line.target && line.target.warehouseName, line.target && line.target.locationName, line.target && line.target.areaName) }}</span>
        </template>
      </div>
      <p>确认后使用当前登录账号执行。单据或位置变化时需重新预览。</p>
      <button type="button" :disabled="confirming || completed" @click="$emit('confirm', result.operation_id)">
        {{ confirming ? '执行并核验中…' : completed ? '已提交' : '确认执行' }}
      </button>
    </template>
    <template v-else>
      <p role="status">{{ result.label }}</p>
      <p v-if="result.error_code">原因：{{ result.error_code }}</p>
      <p>操作 ID：{{ result.operation_id }}</p>
      <button v-if="result.outcome === 'unknown'" type="button" :disabled="confirming" @click="$emit('refresh', result.operation_id)">查询回执</button>
      <button v-if="result.document_id" type="button" @click="$emit('view', result)">查看单据</button>
    </template>
  </section>
</template>

<script>
export default {
  name: 'AgentInventoryCard',
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    isReceipt() { return this.result.action_type === 'ITEM_RECEIPT_EXECUTE' },
    title() {
      const action = this.isReceipt ? '物料入库' : '生产领料'
      if (this.result.kind === 'inventory_preview') return action + '执行预览'
      return action + ' · ' + ({ succeeded: '成功', failed: '未完成', unknown: '待核实', prepared: '待确认' }[this.result.outcome] || '结果')
    },
    lines() { return this.isReceipt ? this.result.additions || [] : this.result.movements || [] }
  },
  methods: {
    location(warehouse, location, area) { return [warehouse, location, area].filter(Boolean).join(' / ') || '未知位置' }
  }
}
</script>

<style lang="scss" scoped>
.inventory-card { margin-top: 10px; padding: 12px; border: 1px solid #dfe4ed; border-radius: 6px; background: #fff; color: #303133; font-size: 12px; overflow-wrap: anywhere; }
.inventory-card header { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.inventory-card header span { text-align: right; }
.inventory-card__line { display: flex; flex-direction: column; gap: 3px; padding: 8px 0; border-top: 1px solid #ebeef5; }
.inventory-card button { margin: 6px 8px 0 0; padding: 7px 10px; border: 1px solid #409eff; border-radius: 4px; background: #fff; color: #337ecc; cursor: pointer; }
.inventory-card button:disabled { opacity: .5; cursor: not-allowed; }
</style>
