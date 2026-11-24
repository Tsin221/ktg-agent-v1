<template>
  <div class="quality-card">
    <header class="quality-card__heading"><strong>工单质量</strong><span>已返回 {{ orders.length }} 张</span></header>
    <p v-if="filters" class="quality-card__meta">{{ filters }}</p>
    <p v-if="result.coverage !== 'complete'" class="quality-card__notice">本次仅获得部分数据，不能据此判断完整的质量情况。</p>
    <p v-if="result.missing_codes && result.missing_codes.length" class="quality-card__notice">未返回工单：{{ result.missing_codes.join('、') }}（未找到或未能核实）</p>
    <ul v-if="result.warnings && result.warnings.length" class="quality-card__notes">
      <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
    </ul>
    <p v-if="!orders.length" class="quality-card__meta">未返回工单质量记录。</p>

    <article v-for="order in orders" :key="order.workorderId" class="quality-card__order">
      <div class="quality-card__order-heading">
        <strong>{{ order.workorderCode }}</strong><span>{{ statusLabel(order.status) }}</span>
      </div>
      <div class="quality-card__order-name">{{ order.workorderName }}</div>
      <template v-if="order.summary">
        <p class="quality-card__meta">全单当前进度：计划 {{ display(order.summary.planned) }} 件 · 已分配 {{ display(order.summary.allocated) }} 件</p>
        <dl class="quality-card__metrics">
          <div v-for="metric in progressMetrics" :key="metric[0]" :class="{ 'quality-card__attention': metric[2] && order.summary[metric[0]] > 0 }">
            <dt>{{ metric[1] }}</dt><dd>{{ display(order.summary[metric[0]]) }}</dd>
          </div>
        </dl>
        <p class="quality-card__meta">全单检验历史统计（首检件数与复检次数分别计数）</p>
        <dl class="quality-card__metrics quality-card__metrics--history">
          <div v-for="metric in historyMetrics" :key="metric[0]">
            <dt>{{ metric[1] }}</dt><dd>{{ display(order.summary[metric[0]]) }}</dd>
          </div>
        </dl>
      </template>
      <p v-else-if="order.history_coverage === 'not_applicable' && order.status === 'PREPARE'" class="quality-card__meta">草稿，尚未下达，无执行检验历史。</p>
      <p v-else class="quality-card__notice">质量汇总未核实。</p>

      <p v-if="order.history_coverage !== 'not_applicable'" class="quality-card__meta">
        检验历史覆盖：{{ coverageLabel(order.history_coverage) }} · 已核实 {{ display(order.scanned_units) }} / {{ display(order.total_units) }} 件（{{ requestedSNs ? '已匹配指定 SN' : '检验任务可见历史成员' }}）
      </p>
      <p v-if="order.missing_sn_codes && order.missing_sn_codes.length" class="quality-card__notice">未返回 SN：{{ order.missing_sn_codes.join('、') }}（未发现或未核实）</p>
      <div v-if="order.units && order.units.length" class="quality-card__table-wrap" role="region" :aria-label="order.workorderCode + ' 单件质量数据'" tabindex="0">
        <table class="quality-card__table">
          <caption>{{ requestedSNs ? '所查单件与检验结果' : '当前及历史异常单件' }}</caption>
          <thead><tr><th scope="col">单件 SN / 当前状态</th><th scope="col">首检 / 最近检验</th><th scope="col">不合格指标与记录</th></tr></thead>
          <tbody>
            <tr v-for="unit in order.units" :key="unit.snId || unit.snCode">
              <th scope="row">{{ unit.snCode || 'SN 未核实' }}<small :class="{ 'quality-card__bad': isAbnormal(unit.unitState) }">{{ unitLabel(unit.unitState) }}</small></th>
              <td>
                <template v-if="unit.history_state === 'verified' && unit.inspection_count === 0">尚未检验</template>
                <template v-else-if="unit.history_state === 'verified'">
                  <span :class="{ 'quality-card__bad': unit.first_result === 'REJECT' }">首检 {{ inspectionLabel(unit.first_result) }}</span>
                  <small :class="{ 'quality-card__bad': unit.latest_result === 'REJECT' }">最近 {{ inspectionLabel(unit.latest_result) }}</small>
                  <small>共 {{ display(unit.inspection_count) }} 次检验</small>
                </template>
                <template v-else>检验历史未核实</template>
              </td>
              <td>
                <template v-if="unit.history_state === 'verified' && rejectedRounds(unit).length">
                  <div v-for="record in rejectedRounds(unit)" :key="record.inspectionId || record.round_no" class="quality-card__round">
                    <strong class="quality-card__bad">第 {{ display(record.round_no) }} 轮不合格</strong>
                    <div v-for="(item, index) in record.failed_indices || []" :key="index" class="quality-card__failure">
                      {{ item.indexName || item.indexCode || '指标未核实' }}：{{ indexValue(item) }}
                      <small v-if="item.thresholdMin != null || item.thresholdMax != null">要求 {{ display(item.thresholdMin) }} 至 {{ display(item.thresholdMax) }} {{ item.unitOfMeasure || '' }}</small>
                      <small v-if="item.defectName">缺陷：{{ item.defectName }}<template v-if="item.defectLevel"> · {{ defectLevelLabel(item.defectLevel) }}</template></small>
                      <small v-if="item.reason">记录说明：{{ item.reason }}</small>
                    </div>
                    <small v-if="!record.failed_indices || !record.failed_indices.length">不合格指标未核实。</small>
                  </div>
                </template>
                <template v-else-if="unit.history_state === 'verified' && unit.inspection_count === 0">暂无检验记录</template>
                <template v-else-if="unit.history_state === 'verified' && unit.ever_rejected === false">所获记录未发现不合格</template>
                <template v-else>不合格指标未核实</template>
                <details v-if="unit.history_state === 'verified' && unit.inspections && unit.inspections.length" class="quality-card__history">
                  <summary>查看检验历史（{{ unit.inspections.length }} 条）</summary>
                  <div v-for="record in unit.inspections" :key="record.inspectionId || record.round_no" class="quality-card__history-line">
                    第 {{ display(record.round_no) }} 轮 · {{ inspectionLabel(record.result) }}
                    <small>{{ record.inspect_date || '检验时间未记录' }}</small>
                  </div>
                </details>
                <ul v-if="unit.warnings && unit.warnings.length" class="quality-card__notes"><li v-for="(warning, index) in unit.warnings" :key="index">{{ warning }}</li></ul>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <template v-else-if="order.history_coverage !== 'not_applicable'">
        <p v-if="requestedSNs" class="quality-card__notice">未返回指定单件，不能据此判断其质量。</p>
        <p v-else-if="order.history_coverage === 'complete' && order.total_units > 0" class="quality-card__meta">本次查询范围内未发现已记录的质量异常；未检验不代表合格。</p>
        <p v-else-if="order.history_coverage === 'complete' && order.total_units === 0" class="quality-card__meta">当前无可见检验任务成员；未检验不代表合格。</p>
        <p v-else class="quality-card__notice">未返回异常单件，尚不能判断是否存在质量异常。</p>
      </template>
      <ul v-if="order.warnings && order.warnings.length" class="quality-card__notes">
        <li v-for="(warning, index) in order.warnings" :key="index">{{ warning }}</li>
      </ul>
    </article>
    <p v-if="result.queried_at" class="quality-card__meta">查询时间 {{ result.queried_at }}</p>
  </div>
</template>

<script>
import { queryScope, workorderStatusLabel } from './sessionPresentation'

export default {
  name: 'AgentWorkorderQualityCard',
  props: { result: { type: Object, required: true }},
  computed: {
    orders() { return Array.isArray(this.result.orders) ? this.result.orders : [] },
    filters() { return queryScope(this.result.filters || {}, false) },
    requestedSNs() { return Boolean(this.result.filters && this.result.filters.sn_codes && this.result.filters.sn_codes.length) },
    progressMetrics() { return [['waitingAssembly', '待装配'], ['waitingInspection', '待检'], ['pendingDisposition', '待处置', true],
      ['reworkInProgress', '返工中', true], ['waitingReinspection', '待复检', true], ['qualified', '当前合格'], ['scrapped', '已报废', true]] },
    historyMetrics() { return [['firstInspectionRejected', '首检不合格件数'], ['reinspectionCount', '复检次数']] }
  },
  methods: {
    statusLabel: workorderStatusLabel,
    display(value) { return value == null ? '未核实' : value },
    unitLabel(value) { return { WAIT_ASSEMBLY: '待装配', WAIT_INSPECTION: '待检', WAIT_DISPOSITION: '待处置', REWORK_IN_PROGRESS: '返工中',
      WAIT_REINSPECTION: '待复检', WAIT_PACKING: '待包装（当前合格）', PACKED: '已包装（当前合格）', SCRAPPED: '已报废' }[value] || '状态未核实' },
    isAbnormal(value) { return ['WAIT_DISPOSITION', 'REWORK_IN_PROGRESS', 'WAIT_REINSPECTION', 'SCRAPPED'].includes(value) },
    coverageLabel(value) { return { complete: '完整', partial: '部分' }[value] || '未核实' },
    inspectionLabel(value) { return { ACCEPT: '合格', REJECT: '不合格' }[value] || '未核实' },
    defectLevelLabel(value) { return { CR: '致命缺陷', MAJ: '严重缺陷', MIN: '轻微缺陷' }[value] || value },
    rejectedRounds(unit) { return (unit.inspections || []).filter(record => record.result === 'REJECT') },
    indexValue(item) {
      if (item.value != null) return `${item.value}${item.unitOfMeasure ? ' ' + item.unitOfMeasure : ''}`
      return item.passed === false ? '不符合' : item.passed === true ? '符合' : '实测结果未核实'
    }
  }
}
</script>

<style lang="scss" scoped>
.quality-card__heading, .quality-card__order-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; overflow-wrap: anywhere; }
.quality-card__heading { font-size: 13px; font-weight: 600; color: #303133; }
.quality-card__order { margin-top: 12px; padding-top: 12px; border-top: 1px solid #dfe4ed; }
.quality-card__order-heading { font-size: 12px; color: #606266; }
.quality-card__order-heading strong { font-size: 13px; color: #303133; }
.quality-card__order-heading span { flex-shrink: 0; }
.quality-card__order-name { font-size: 14px; color: #303133; margin-top: 4px; overflow-wrap: anywhere; }
.quality-card__meta, .quality-card__notice, .quality-card__notes { margin: 6px 0 0; font-size: 12px; color: #606266; overflow-wrap: anywhere; }
.quality-card__notice { color: #805300; }
.quality-card__notes { padding-left: 18px; }
.quality-card__metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(94px, 1fr)); gap: 4px; margin: 8px 0; }
.quality-card__metrics div { background: #f2f6fc; border-radius: 4px; padding: 6px 8px; }
.quality-card__metrics .quality-card__attention { background: #fff1f2; color: #922e38; }
.quality-card__metrics dt { font-size: 12px; }
.quality-card__metrics dd { margin: 2px 0 0; font-size: 14px; font-weight: 600; }
.quality-card__metrics--history { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.quality-card__table-wrap { margin: 10px 0 0; max-width: 100%; overflow-x: auto; border: 1px solid #dfe4ed; border-radius: 6px; }
.quality-card__table-wrap:focus-visible { outline: 2px solid #409eff; outline-offset: 2px; }
.quality-card__table { width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; color: #303133; }
.quality-card__table caption { text-align: left; font-weight: 600; padding: 8px; }
.quality-card__table th, .quality-card__table td { padding: 8px; border-bottom: 1px solid #dfe4ed; vertical-align: top; overflow-wrap: anywhere; }
.quality-card__table thead th { background: #f2f6fc; color: #303133; white-space: nowrap; }
.quality-card__table tbody th { min-width: 105px; font-weight: 500; }
.quality-card__table tbody td { min-width: 90px; }
.quality-card__table tbody td:last-child { min-width: 165px; }
.quality-card__table tbody tr:last-child > * { border-bottom: 0; }
.quality-card__table small { display: block; font-size: 12px; color: #606266; margin: 2px 0; }
.quality-card__table .quality-card__bad { color: #922e38; font-weight: 600; }
.quality-card__round + .quality-card__round { margin-top: 8px; }
.quality-card__failure { margin-top: 4px; }
.quality-card__history { margin-top: 6px; color: #606266; }
.quality-card__history summary { cursor: pointer; }
.quality-card__history-line { padding: 4px 0 0 8px; }
</style>
