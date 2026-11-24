<template>
  <div class="materials-card">
    <header class="materials-card__heading">
      <strong>工单用料</strong><span>已返回 {{ orders.length }} 张</span>
    </header>
    <p v-if="filters" class="materials-card__meta">{{ filters }}</p>
    <p v-if="result.coverage !== 'complete'" class="materials-card__notice">本次仅获得部分数据，不代表所有指定工单均已核实。</p>
    <p v-if="result.missing_codes && result.missing_codes.length" class="materials-card__notice">
      未返回工单：{{ result.missing_codes.join('、') }}（未找到或未能核实）
    </p>
    <ul v-if="result.warnings && result.warnings.length" class="materials-card__notes">
      <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
    </ul>
    <p v-if="!orders.length" class="materials-card__meta">未返回工单用料记录。</p>

    <article v-for="order in orders" :key="order.workorderId" class="materials-card__order">
      <div class="materials-card__order-heading">
        <strong>{{ order.workorderCode }}</strong>
        <span>{{ statusLabel(order.status) }}</span>
      </div>
      <div class="materials-card__order-name">{{ order.workorderName }}</div>
      <p class="materials-card__meta">
        计划量 {{ quantity(order.quantity) }} {{ order.unitOfMeasure }}
        <span v-if="order.waitingAssembly != null"> · 待装配 {{ order.waitingAssembly }} {{ order.unitOfMeasure }}</span>
        <span v-else-if="order.material_scope !== 'warehouse_reference'"> · 待装配未核实</span>
      </p>
      <p :class="['materials-card__assessment', assessmentClass(order.assessment)]">{{ assessmentLabel(order.assessment) }}</p>
      <p v-if="order.material_scope === 'warehouse_reference'" class="materials-card__meta">
        原料仓账面参考，仅与计划需求比较，不代表已齐套或已领料。
      </p>
      <p v-else-if="order.material_scope === 'line_side'" class="materials-card__meta">
        本工单线边可耗料口径，仅与剩余装配需求比较，不计其他工单或原料仓库存。
      </p>
      <p v-else class="materials-card__meta">当前用料口径未核实。</p>
      <p v-if="order.assessment === 'no_remaining'" class="materials-card__meta">不代表当前库存齐套，无需根据零剩余需求判定备料充足。</p>
      <ul v-if="shortages(order).length" class="materials-card__shortages">
        <li v-for="item in shortages(order)" :key="item.itemId">{{ item.itemName || item.itemCode }} · 缺口 {{ item.shortage_quantity }} {{ item.unitOfMeasure }}</li>
      </ul>
      <div v-if="order.materials && order.materials.length" class="materials-card__table-wrap" role="region" :aria-label="order.workorderCode + ' 用料数据'" tabindex="0">
        <table class="materials-card__table">
          <thead>
            <tr>
              <th scope="col">物料 / 单位</th><th scope="col">计划需求</th>
              <th v-if="order.material_scope !== 'warehouse_reference'" scope="col">剩余需求</th>
              <th scope="col">{{ stockLabel(order.material_scope) }}</th><th scope="col">缺口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in order.materials" :key="item.itemId + ':' + item.unitOfMeasure">
              <th scope="row">{{ item.itemName || item.itemCode }}<small>{{ item.itemCode }} · {{ item.unitOfMeasure }}</small></th>
              <td>{{ quantity(item.planned_required) }}</td>
              <td v-if="order.material_scope !== 'warehouse_reference'">{{ quantity(item.remaining_required) }}</td>
              <td>{{ stockValue(item, 'stock_quantity') }}</td>
              <td :class="{ 'materials-card__shortage-value': hasShortage(item) }">{{ shortageValue(order, item) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="order.assessment !== 'no_remaining'" class="materials-card__meta">未返回物料明细，不代表没有物料需求。</p>
      <ul v-if="order.warnings && order.warnings.length" class="materials-card__notes">
        <li v-for="(warning, index) in order.warnings" :key="index">{{ warning }}</li>
      </ul>
      <template v-for="item in order.materials || []">
        <div v-if="item.warnings && item.warnings.length" :key="'warnings:' + item.itemId + ':' + item.unitOfMeasure" class="materials-card__meta">
          <strong>{{ item.itemName || item.itemCode }}：</strong>{{ item.warnings.join('；') }}
        </div>
        <details v-if="hasDetails(item)" :key="'detail:' + item.itemId + ':' + item.unitOfMeasure" class="materials-card__details">
          <summary>{{ item.itemName || item.itemCode }} · 分仓/批次明细</summary>
          <div v-for="(warehouse, index) in item.warehouses || []" :key="'w:' + index" class="materials-card__detail-row">
            <span>{{ warehouse.warehouseName || warehouse.warehouseCode || warehouse.warehouseId }}</span><strong>{{ quantity(warehouse.quantity) }} {{ item.unitOfMeasure }}</strong>
          </div>
          <div v-for="(batch, index) in item.batches || []" :key="'b:' + index" class="materials-card__detail-row">
            <span>{{ batch.batchCode }}<small v-if="batch.warehouseCode">{{ batch.warehouseCode }}</small></span><strong>{{ quantity(batch.quantity) }} {{ item.unitOfMeasure }}</strong>
          </div>
        </details>
      </template>
    </article>
    <p v-if="result.queried_at" class="materials-card__meta">查询时间 {{ result.queried_at }}</p>
  </div>
</template>

<script>
import { queryScope, workorderStatusLabel } from './sessionPresentation'

export default {
  name: 'AgentWorkorderMaterialsCard',
  props: { result: { type: Object, required: true }},
  computed: {
    orders() { return Array.isArray(this.result.orders) ? this.result.orders : [] },
    filters() { return queryScope({ include_batches: false, ...(this.result.filters || {}) }, false) }
  },
  methods: {
    statusLabel: workorderStatusLabel,
    assessmentLabel(value) {
      return {
        reference_sufficient: '参考库存满足计划需求',
        reference_shortage: '参考库存存在缺口',
        line_side_sufficient: '本工单线边可耗料满足剩余需求',
        line_side_shortage: '本工单线边可耗料不足',
        no_remaining: '无剩余装配需求',
        unavailable: '用料结论待核实'
      }[value] || '用料结论待核实'
    },
    assessmentClass(value) {
      return ['reference_shortage', 'line_side_shortage'].includes(value) ? 'materials-card__assessment--shortage'
        : value === 'unavailable' ? 'materials-card__assessment--unknown' : ''
    },
    quantity(value) { return value == null ? '未核实' : value },
    stockValue(item, key) { return item.stock_state === 'verified' ? this.quantity(item[key]) : '未核实' },
    shortageValue(order, item) {
      if (order.assessment === 'no_remaining' && item.remaining_required === '0' && item.shortage_quantity === '0') return '0（无需求）'
      return this.stockValue(item, 'shortage_quantity')
    },
    stockLabel(scope) {
      return scope === 'warehouse_reference' ? '账面现存量' : scope === 'line_side' ? '线边可耗量' : '库存量（待核实）'
    },
    hasShortage(item) {
      const value = item.shortage_quantity
      return item.stock_state === 'verified' && typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) && /[1-9]/.test(value)
    },
    shortages(order) { return (order.materials || []).filter(this.hasShortage) },
    hasDetails(item) {
      return item.stock_state === 'verified' && ((item.warehouses || []).length || (item.batches || []).length)
    }
  }
}
</script>

<style lang="scss" scoped>
.materials-card__heading, .materials-card__order-heading, .materials-card__detail-row {
  display: flex; justify-content: space-between; align-items: baseline; gap: 8px; overflow-wrap: anywhere;
}
.materials-card__heading { color: #303133; font-size: 13px; font-weight: 600; }
.materials-card__meta { margin: 6px 0 0; color: #606266; font-size: 12px; overflow-wrap: anywhere; }
.materials-card__notice { margin: 8px 0 0; color: #805300; font-size: 12px; overflow-wrap: anywhere; }
.materials-card__order { margin-top: 12px; padding-top: 12px; border-top: 1px solid #dfe4ed; }
.materials-card__order-heading { font-size: 12px; color: #606266; }
.materials-card__order-heading strong { font-size: 13px; color: #303133; }
.materials-card__order-heading span { flex-shrink: 0; }
.materials-card__order-name { margin-top: 4px; font-size: 14px; color: #303133; overflow-wrap: anywhere; }
.materials-card__assessment { margin: 8px 0 4px; padding: 7px 8px; background: #f2f6fc; color: #303133; font-size: 13px; font-weight: 600; border-radius: 4px; }
.materials-card__assessment--shortage { background: #fff1f2; color: #922e38; }
.materials-card__assessment--unknown { background: #fff6e6; color: #805300; }
.materials-card__shortages { margin: 6px 0 0; padding-left: 18px; color: #922e38; font-size: 12px; font-weight: 600; }
.materials-card__table-wrap { margin: 10px 0 0; max-width: 100%; overflow-x: auto; border: 1px solid #dfe4ed; border-radius: 6px; }
.materials-card__table-wrap:focus-visible { outline: 2px solid #409eff; outline-offset: 2px; }
.materials-card__table { width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; color: #303133; }
.materials-card__table th, .materials-card__table td { padding: 8px; border-bottom: 1px solid #dfe4ed; vertical-align: top; }
.materials-card__table thead th { background: #f2f6fc; color: #303133; white-space: nowrap; }
.materials-card__table tbody th { font-weight: 500; min-width: 105px; overflow-wrap: anywhere; }
.materials-card__table tbody td { min-width: 55px; white-space: nowrap; }
.materials-card__table tbody tr:last-child th, .materials-card__table tbody tr:last-child td { border-bottom: 0; }
.materials-card__table small, .materials-card__detail-row small { display: block; color: #606266; }
.materials-card__shortage-value { color: #922e38; font-weight: 700; background: #fff1f2; }
.materials-card__notes { margin: 6px 0; padding-left: 18px; color: #606266; font-size: 12px; overflow-wrap: anywhere; }
.materials-card__details { margin-top: 8px; color: #606266; font-size: 12px; }
.materials-card__details summary { cursor: pointer; }
.materials-card__detail-row { padding: 5px 0 0 10px; }
</style>
