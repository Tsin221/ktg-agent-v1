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
      <div v-if="result.groups && result.groups.length" class="result-card__table-wrap" role="region" aria-label="分仓库存数据" tabindex="0">
        <table class="result-card__table">
          <thead><tr><th scope="col">仓库</th><th scope="col">物料</th><th scope="col">现存量</th></tr></thead>
          <tbody><tr v-for="group in result.groups" :key="`${group.itemId}-${group.warehouseId}-${group.unitOfMeasure}`">
            <td>{{ group.warehouseName || group.warehouseCode || '未分仓' }}</td><td>{{ group.itemCode }}</td><td>{{ group.quantityOnhand }} {{ group.unitOfMeasure || '' }}</td>
          </tr></tbody>
        </table>
      </div>
      <p v-if="result.queried_at" class="result-card__meta">查询时间 {{ result.queried_at }}</p>
    </template>
    <template v-else-if="result.kind === 'workorder_overview'">
      <div class="result-card__heading">
        <span>{{ title }}</span>
        <span>{{ result.total == null ? '总数未核实' : '共 ' + result.total + ' 张' }}</span>
      </div>
      <p class="result-card__meta">第 {{ result.page }} 页 · 本页 {{ (result.rows || []).length }} 张 · 每页 {{ result.page_size }} 张</p>
      <p v-if="workorderFilters" class="result-card__meta">{{ workorderFilters }}</p>
      <p v-if="result.coverage !== 'complete'" class="result-card__notice">本次仅获得部分数据，不能据此判断全部工单及其执行进度。</p>
      <ul v-if="Array.isArray(result.warnings) && result.warnings.length" class="result-card__reasons">
        <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
      </ul>
      <p v-if="!result.rows || !result.rows.length" class="result-card__empty">
        {{ result.coverage === 'complete' && result.total === 0 ? '未找到匹配工单' : '本页未获工单记录' }}
      </p>
      <div v-if="result.rows && result.rows.length" class="result-card__table-wrap" role="region" aria-label="工单及生产进度数据" tabindex="0">
        <table v-if="result.rows.length === 1" class="result-card__table result-card__table--single">
          <thead><tr><th scope="col">工单指标</th><th scope="col">当前结果</th></tr></thead>
          <tbody>
            <tr><th scope="row">工单</th><td>{{ result.rows[0].workorderName || result.rows[0].workorderCode }}</td></tr>
            <tr><th scope="row">状态</th><td>{{ statusLabel(result.rows[0].status) }}<small v-if="result.rows[0].progress_state === 'unavailable'">进度待核实</small></td></tr>
            <tr><th scope="row">计划量</th><td>{{ result.rows[0].quantity }} {{ result.rows[0].unitOfMeasure || '' }}</td></tr>
            <tr v-for="metric in overviewMetrics" :key="metric[0]"><th scope="row">{{ metric[1] }}</th><td>{{ result.rows[0].progress_state === 'available' && result.rows[0].progress ? metricValue(result.rows[0].progress, metric[0]) : '—' }}</td></tr>
          </tbody>
        </table>
        <table v-else class="result-card__table">
          <thead><tr><th scope="col">工单</th><th scope="col">状态</th><th scope="col">计划量</th>
            <th v-for="metric in overviewMetrics" :key="metric[0]" scope="col">{{ metric[1] }}</th>
          </tr></thead>
          <tbody><tr v-for="row in result.rows" :key="row.workorderId">
            <th scope="row">{{ row.workorderName || row.workorderCode }}</th>
            <td>{{ statusLabel(row.status) }}<small v-if="row.progress_state === 'draft'">未下达</small><small v-else-if="row.progress_state === 'unavailable'">进度待核实</small></td>
            <td>{{ row.quantity }} {{ row.unitOfMeasure || '' }}</td>
            <td v-for="metric in overviewMetrics" :key="metric[0]">{{ row.progress_state === 'available' && row.progress ? metricValue(row.progress, metric[0]) : '—' }}</td>
          </tr></tbody>
        </table>
      </div>
      <details v-for="row in result.rows || []" :key="row.workorderId" class="result-card__order">
        <summary>工单详情 · {{ row.workorderCode }}</summary>
        <div class="result-card__order-heading">
          <strong>{{ row.workorderCode }}</strong>
          <span class="result-card__status">{{ statusLabel(row.status) }}</span>
        </div>
        <div class="result-card__order-name">{{ row.workorderName }}</div>
        <p class="result-card__meta">{{ row.productName }} · {{ row.productCode }}</p>
        <p class="result-card__meta">计划量 {{ row.quantity }} {{ row.unitOfMeasure || '' }} · 需求日期 {{ row.requestDate || '未提供' }}</p>
        <template v-if="row.progress_state === 'available' && row.progress">
          <dl class="result-card__metrics">
            <div v-for="metric in mainMetrics" :key="metric[0]">
              <dt>{{ metric[1] }}</dt><dd>{{ metricValue(row.progress, metric[0]) }}</dd>
            </div>
          </dl>
          <dl class="result-card__secondary-metrics">
            <div v-for="metric in detailMetrics" :key="metric[0]">
              <dt>{{ metric[1] }}</dt><dd>{{ metricValue(row.progress, metric[0]) }}</dd>
            </div>
          </dl>
          <p v-if="row.status === 'FINISHED'" class="result-card__meta">工单已完工。</p>
          <p v-else-if="row.progress.completionAllowed === true" class="result-card__meta">当前已具备完结条件。</p>
          <template v-else-if="row.progress.completionAllowed === false">
            <p class="result-card__notice">当前暂不具备完结条件。</p>
            <ul v-if="Array.isArray(row.progress.completionReasons) && row.progress.completionReasons.length" class="result-card__reasons">
              <li v-for="(reason, index) in row.progress.completionReasons" :key="index">{{ reason }}</li>
            </ul>
          </template>
          <p v-else class="result-card__meta">完结条件未核实。</p>
          <details v-if="taskRows(row).length" class="result-card__tasks">
            <summary>查看返回任务（{{ taskRows(row).length }} 项）</summary>
            <div v-for="task in taskRows(row)" :key="task.taskId" class="result-card__task">
              <span>{{ taskLabel(task) }}</span><span>{{ taskStatusLabel(task.status) }}</span>
            </div>
          </details>
          <p v-if="row.progress.tasks_truncated === true" class="result-card__notice">仅展示已返回任务，尚有省略。</p>
        </template>
        <p v-else-if="row.progress_state === 'draft'" class="result-card__notice">草稿，尚未下达，无执行进度。</p>
        <p v-else-if="row.progress_state === 'not_requested'" class="result-card__meta">本次未请求执行进度。</p>
        <template v-else>
          <p class="result-card__notice">执行进度未获取，无法判断实际进展。</p>
          <p v-if="row.progress_reason" class="result-card__meta">{{ row.progress_reason }}</p>
        </template>
      </details>
      <p v-if="result.queried_at" class="result-card__meta">查询时间 {{ result.queried_at }}</p>
    </template>
    <agent-workorder-materials-card v-else-if="result.kind === 'workorder_materials'" :result="result" />
    <agent-workorder-quality-card v-else-if="result.kind === 'workorder_quality'" :result="result" />
    <details v-else class="result-card__history">
      <summary>历史结果（仅供查看，不可执行）</summary>
      <pre>{{ historyText }}</pre>
    </details>
  </section>
</template>

<script>
import { queryScope, workorderStatusLabel } from './sessionPresentation'
import AgentWorkorderMaterialsCard from './WorkorderMaterialsCard.vue'
import AgentWorkorderQualityCard from './WorkorderQualityCard.vue'

export default {
  name: 'AgentResultCard',
  components: { AgentWorkorderMaterialsCard, AgentWorkorderQualityCard },
  props: {
    result: { type: Object, required: true }
  },
  computed: {
    title() {
      return { product_count: '产品总数', stock_summary: '分仓库存汇总', workorder_overview: '工单概览',
        workorder_materials: '工单用料', workorder_quality: '工单质量' }[this.result.kind] || '历史结果'
    },
    historyText() {
      return JSON.stringify(this.result, null, 2)
    },
    workorderFilters() {
      return queryScope({ unfinished_only: false, include_progress: false, ...(this.result.filters || {}) }, false)
    },
    overviewMetrics() {
      const requested = this.result.filters && this.result.filters.include_progress === true
      return requested || (this.result.rows || []).some(row => row.progress_state === 'available')
        ? [['assembled', '已装配'], ['qualified', '合格'], ['pendingDisposition', '待处置'], ['packed', '已包装']] : []
    },
    mainMetrics() {
      return [['allocated', '已分配'], ['assembled', '已装配'], ['qualified', '合格'], ['packed', '已包装']]
    },
    detailMetrics() {
      return [['planned', '执行计划'], ['waitingAssembly', '待装配'], ['waitingInspection', '待检'],
        ['pendingDisposition', '待处置'], ['reworkInProgress', '返工'], ['waitingReinspection', '待复检'],
        ['packable', '可包装'], ['scrapped', '已报废']]
    }
  },
  methods: {
    statusLabel: workorderStatusLabel,
    metricValue(progress, key) {
      return progress[key] == null ? '—' : progress[key]
    },
    taskRows(row) {
      const tasks = row.progress && row.progress.tasks || row.tasks
      return Array.isArray(tasks) ? tasks : []
    },
    taskLabel(task) {
      const kind = task.taskKind === 'REWORK' ? 'REWORK' : task.stepKind
      return { ASSEMBLY: '装配', INSPECTION: '检验', PACKING: '包装', REWORK: '返工' }[kind] || task.taskName || kind || '任务'
    },
    taskStatusLabel(status) {
      return { WAITING: '待前置', READY: '待开工', STARTED: '已开工', FINISHED: '已完成' }[status] || status || '状态未获取'
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
.result-card__table-wrap { margin: 10px 0; max-width: 100%; overflow-x: auto; border: 1px solid #dfe4ed; border-radius: 6px; }
.result-card__table { width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; }
.result-card__table th, .result-card__table td { padding: 8px; border-bottom: 1px solid #dfe4ed; min-width: 64px; vertical-align: top; overflow-wrap: anywhere; }
.result-card__table thead th { background: #f2f6fc; color: #303133; white-space: nowrap; }
.result-card__table tbody th { font-weight: 500; min-width: 112px; }
.result-card__table--single tbody th { min-width: 0; width: 32%; white-space: nowrap; }
.result-card__table--single th, .result-card__table--single td { padding: 6px 8px; }
.result-card__table tbody tr:last-child th, .result-card__table tbody tr:last-child td { border-bottom: 0; }
.result-card__table small { display: block; color: #606266; }
.result-card__order {
  margin-top: 12px;
  padding: 12px 0 0;
  border-top: 1px solid #dfe4ed;
}
.result-card__order > summary { cursor: pointer; font-size: 12px; color: #303133; margin-bottom: 8px; overflow-wrap: anywhere; }
.result-card__order-heading, .result-card__task {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  overflow-wrap: anywhere;
}
.result-card__order-heading strong { color: #303133; font-size: 13px; }
.result-card__status { flex-shrink: 0; color: #606266; font-size: 12px; font-weight: 600; }
.result-card__order-name { margin-top: 4px; color: #303133; font-size: 14px; overflow-wrap: anywhere; }
.result-card__metrics, .result-card__secondary-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin: 10px 0 0;
}
.result-card__metrics div { padding: 6px; background: #f5f7fa; border-radius: 4px; }
.result-card__metrics dt, .result-card__secondary-metrics dt { color: #606266; font-size: 12px; }
.result-card__metrics dd, .result-card__secondary-metrics dd { margin: 2px 0 0; color: #303133; font-weight: 600; overflow-wrap: anywhere; }
.result-card__metrics dd { font-size: 17px; }
.result-card__secondary-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); font-size: 13px; }
.result-card__notice { margin: 8px 0 0; color: #805300; font-size: 12px; overflow-wrap: anywhere; }
.result-card__reasons { margin: 4px 0; padding-left: 18px; color: #606266; font-size: 12px; }
.result-card__tasks { margin-top: 8px; color: #606266; font-size: 12px; }
.result-card__tasks summary { cursor: pointer; }
.result-card__task { padding: 4px 0; }
@media (max-width: 400px) {
  .result-card__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.result-card__history pre {
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
</style>

