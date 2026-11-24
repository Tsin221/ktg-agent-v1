<template>
  <div class="trace-card">
    <header class="trace-card__heading"><strong>生产追溯</strong><span>{{ directionLabel }}</span></header>
    <p v-if="filters" class="trace-card__meta">{{ filters }}</p>
    <p v-if="result.coverage !== 'complete'" class="trace-card__notice">本次追溯范围未完整核实，仅以下返回的事实可用。</p>
    <ul v-if="result.warnings && result.warnings.length" class="trace-card__notes"><li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li></ul>
    <template v-if="result.direction === 'unit_to_material'">
      <p class="trace-card__meta">展示单件实际投入，不等于计划用料或领料。</p>
      <p v-if="!units.length" class="trace-card__notice">未返回指定单件，无法核实实际投入。</p>
      <article v-for="(unit, index) in units" :key="unit.snId || unit.snCode || index" class="trace-card__group">
        <div class="trace-card__group-heading"><strong>{{ display(unit.snCode) }}</strong><span>{{ traceLabel(unit.trace_state) }}</span></div>
        <p class="trace-card__meta">成品：{{ display(unit.itemName) }} · {{ display(unit.itemCode) }}</p>
        <p class="trace-card__meta">工单：{{ display(unit.workorderCode) }}<template v-if="unit.workorderName"> · {{ unit.workorderName }}</template> · {{ statusLabel(unit.orderStatus) }} · {{ unitLabel(unit.unitState) }}</p>
        <div v-if="unit.trace_state === 'tracked' && unit.input_state === 'recorded' && unit.inputs && unit.inputs.length" class="trace-card__table-wrap" role="region" :aria-label="unit.snCode + ' 实际投入明细'" tabindex="0">
          <table class="trace-card__table">
            <caption>单件实际投入明细</caption>
            <thead><tr><th scope="col">原料</th><th scope="col">实际批次</th><th scope="col">本件投入量</th><th scope="col">投入时间</th></tr></thead>
            <tbody><tr v-for="(input, inputIndex) in unit.inputs" :key="input.inputId || inputIndex">
              <th scope="row">{{ display(input.itemName) }}<small>{{ display(input.itemCode) }}</small></th>
              <td>{{ display(input.sourceBatchCode) }}</td><td>{{ display(input.quantity) }} {{ input.unitOfMeasure || '' }}</td><td>{{ display(input.usedAt) }}</td>
            </tr></tbody>
          </table>
        </div>
        <p v-else class="trace-card__notice">{{ inputNotice(unit) }}</p>
        <ul v-if="unit.warnings && unit.warnings.length" class="trace-card__notes"><li v-for="(warning, i) in unit.warnings" :key="i">{{ warning }}</li></ul>
      </article>
    </template>
    <template v-else-if="result.direction === 'material_to_unit'">
      <p class="trace-card__meta">只列当前账号可见、有实际投入记录的单件。影响范围不等于不合格范围，也不能据此认定原料为根因。</p>
      <p v-if="!batches.length" class="trace-card__notice">未返回指定批次，影响范围未核实。</p>
      <article v-for="(batch, index) in batches" :key="index" class="trace-card__group">
        <div class="trace-card__group-heading"><strong>{{ display(batch.itemName) }} · {{ display(batch.itemCode) }}</strong><span>{{ coverageLabel(batch.coverage) }}</span></div>
        <p class="trace-card__batch">实际原料批次：{{ display(batch.batchCode) }}</p>
        <p class="trace-card__meta">{{ batch.total_units == null ? '可见去重 SN 总数未核实' : '当前账号可见去重 SN 总数 ' + batch.total_units + ' 件' }}</p>
        <p v-if="batch.coverage !== 'unavailable'" class="trace-card__meta">第 {{ display(batch.page) }} 页 · 本页 {{ display(batch.returned_units) }} 件 · 每页 {{ display(batch.page_size) }} 件</p>
        <p v-if="batch.coverage !== 'complete'" class="trace-card__notice">本批次结果未完整核实，不可据此判断全部受影响单件的状态。</p>
        <template v-if="batch.state_counts && batch.coverage !== 'unavailable'">
          <p class="trace-card__meta">本页已返回单件状态统计（当前合格 = 待包装 + 已包装）</p>
          <dl class="trace-card__metrics"><div v-for="metric in metrics" :key="metric[0]">
            <dt>{{ metric[1] }}</dt><dd>{{ display(batch.state_counts[metric[0]]) }}</dd>
          </div></dl>
        </template>
        <p v-else class="trace-card__notice">本页状态统计未核实。</p>
        <div v-if="batch.coverage !== 'unavailable' && batch.rows && batch.rows.length" class="trace-card__table-wrap" role="region" :aria-label="batch.batchCode + ' 受影响成品'" tabindex="0">
          <table class="trace-card__table">
            <caption>本页实际使用该批次的成品（去重 SN）</caption>
            <thead><tr><th scope="col">成品 SN</th><th scope="col">所属工单</th><th scope="col">当前单件状态</th><th scope="col">投入记录</th></tr></thead>
            <tbody><tr v-for="(row, rowIndex) in batch.rows" :key="row.snId || rowIndex">
              <th scope="row">{{ display(row.snCode) }}<small>{{ display(row.itemName) }} · {{ display(row.itemCode) }}</small></th>
              <td>{{ display(row.workorderCode) }}<small>{{ display(row.workorderName) }} · {{ statusLabel(row.orderStatus) }}</small></td>
              <td>{{ unitLabel(row.unitState) }}</td>
              <td>{{ display(row.inputCount) }} 条<small>首次 {{ display(row.firstUsedAt) }}</small><small>最近 {{ display(row.lastUsedAt) }}</small></td>
            </tr></tbody>
          </table>
        </div>
        <p v-else-if="batch.coverage === 'complete' && batch.total_units === 0" class="trace-card__meta">无可见实际投入记录；不代表未发生历史业务。</p>
        <p v-else class="trace-card__notice">本次未返回可核实单件，不代表影响范围为零。</p>
        <ul v-if="batch.warnings && batch.warnings.length" class="trace-card__notes"><li v-for="(warning, i) in batch.warnings" :key="i">{{ warning }}</li></ul>
      </article>
    </template>
    <p v-else class="trace-card__notice">追溯方向未核实。</p>
    <p v-if="result.queried_at" class="trace-card__meta">查询时间 {{ result.queried_at }}</p>
  </div>
</template>

<script>
import { queryScope, workorderStatusLabel } from './sessionPresentation'

export default {
  name: 'AgentProductionTraceCard',
  props: { result: { type: Object, required: true }},
  computed: {
    directionLabel() { return { unit_to_material: '成品 → 原料', material_to_unit: '原料 → 成品' }[this.result.direction] || '方向未核实' },
    filters() { return queryScope(this.result.filters || {}, false) },
    units() { return Array.isArray(this.result.units) ? this.result.units : [] },
    batches() { return Array.isArray(this.result.batches) ? this.result.batches : [] },
    metrics() { return [['waitingInspection', '待检'], ['pendingDisposition', '待处置'], ['reworkInProgress', '返工中'],
      ['waitingReinspection', '待复检'], ['qualified', '当前合格'], ['waitingPacking', '待包装'], ['packed', '已包装'], ['scrapped', '已报废']] }
  },
  methods: {
    display(value) { return value == null || value === '' ? '未核实' : value },
    statusLabel: workorderStatusLabel,
    coverageLabel(value) { return { complete: '完整', partial: '部分', unavailable: '未核实' }[value] || '未核实' },
    traceLabel(value) { return { tracked: '已建立追溯', legacy: '历史 SN', unavailable: '追溯未核实' }[value] || '追溯未核实' },
    unitLabel(value) { return { WAIT_ASSEMBLY: '待装配', WAIT_INSPECTION: '待检', WAIT_DISPOSITION: '待处置', REWORK_IN_PROGRESS: '返工中',
      WAIT_REINSPECTION: '待复检', WAIT_PACKING: '待包装（当前合格）', PACKED: '已包装（当前合格）', SCRAPPED: '已报废' }[value] || '状态未核实' },
    inputNotice(unit) {
      if (unit.trace_state === 'legacy' && unit.input_state === 'legacy') return '历史 SN 未建立追溯，实际投入无法核实。'
      if (unit.trace_state === 'tracked' && unit.input_state === 'waiting_assembly') return '待装配，尚无实际投入记录。'
      return '实际投入未核实，不代表使用量为零。'
    }
  }
}
</script>

<style lang="scss" scoped>
.trace-card__heading, .trace-card__group-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; overflow-wrap: anywhere; }
.trace-card__heading, .trace-card__group-heading strong { font-size: 13px; color: #303133; }
.trace-card__group { margin-top: 12px; padding-top: 12px; border-top: 1px solid #dfe4ed; }
.trace-card__group-heading { font-size: 12px; color: #606266; }
.trace-card__group-heading span { flex-shrink: 0; }
.trace-card__batch { font-size: 14px; color: #303133; margin: 6px 0 0; overflow-wrap: anywhere; }
.trace-card__meta, .trace-card__notice, .trace-card__notes { margin: 6px 0 0; font-size: 12px; color: #606266; overflow-wrap: anywhere; }
.trace-card__notice { color: #805300; }
.trace-card__notes { padding-left: 18px; }
.trace-card__metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(94px, 1fr)); gap: 4px; margin: 8px 0; }
.trace-card__metrics div { background: #f2f6fc; border-radius: 4px; padding: 6px 8px; }
.trace-card__metrics dt { font-size: 12px; }
.trace-card__metrics dd { margin: 2px 0 0; font-size: 14px; font-weight: 600; }
.trace-card__table-wrap { margin: 10px 0 0; max-width: 100%; overflow-x: auto; border: 1px solid #dfe4ed; border-radius: 6px; }
.trace-card__table-wrap:focus-visible { outline: 2px solid #409eff; outline-offset: 2px; }
.trace-card__table { width: 100%; border-collapse: collapse; font-size: 12px; color: #303133; text-align: left; }
.trace-card__table caption { padding: 8px; text-align: left; font-weight: 600; }
.trace-card__table th, .trace-card__table td { min-width: 90px; padding: 8px; border-bottom: 1px solid #dfe4ed; vertical-align: top; overflow-wrap: anywhere; }
.trace-card__table thead th { background: #f2f6fc; white-space: nowrap; }
.trace-card__table tbody th { min-width: 112px; font-weight: 500; }
.trace-card__table tbody tr:last-child > * { border-bottom: 0; }
.trace-card__table small { display: block; font-size: 12px; color: #606266; margin: 2px 0; }
</style>
