<template>
  <section class="checkplan-card" :aria-label="title">
    <strong>{{ title }}</strong>
    <template v-if="result.rows">
      <p>共 {{ result.total }} 条，第 {{ result.page }} 页</p>
      <p v-if="!result.rows.length">没有匹配记录。</p>
      <article v-for="(row, index) in result.rows" :key="row.recordId || row.machineryId || row.subjectId || row.planId || index">
        <b>{{ row.planCode || row.machineryCode || row.subjectCode || ('ID ' + (row.planId || row.recordId || row.machineryId || row.subjectId)) }}</b>　{{ row.planName || row.machineryName || row.subjectName }}
        <p v-for="field in rowFields(row)" :key="field">{{ fieldLabel(field) }}：{{ display(row[field], field) }}</p>
      </article>
    </template>
    <template v-else-if="result.kind === 'dv_checkplan_options'">
      <p>计划类型：{{ optionsText(result.plan_types) }}</p>
      <p>周期类型：{{ optionsText(result.cycle_types) }}</p>
      <p>状态：{{ optionsText(result.statuses) }}</p>
      <p v-if="result.source_baseline">源码基线：{{ optionsText(result.source_baseline.plan_types) }}；{{ optionsText(result.source_baseline.cycle_types) }}</p>
    </template>
    <template v-else-if="preview">
      <p v-if="plan.planId || plan.planCode">计划：{{ value(plan.planCode) }} {{ value(plan.planName) }}（ID {{ value(plan.planId || result.plan_id) }}）</p>
      <dl>
        <div v-for="field in previewFields" :key="field" class="checkplan-card__row">
          <dt>{{ fieldLabel(field) }}</dt>
          <dd>{{ display(beforeValues[field], field) }} → {{ display(afterValues[field], field) }}</dd>
        </div>
      </dl>
      <p v-if="affectedCounts">级联影响：设备 {{ value(affectedCounts.machinery_count !== undefined ? affectedCounts.machinery_count : affectedCounts.equipment_count) }} 条，项目 {{ value(affectedCounts.subject_count) }} 条</p>
      <p v-for="(sample, index) in affectedSamples" :key="index">关联快照：{{ describeLink(sample) }}</p>
      <p v-if="link">关联对象：{{ describeLink(link) }}</p>
      <p v-for="warning in result.warnings || []" :key="warning">{{ warning }}</p>
      <p>确认后使用当前登录账号执行；预览过期或计划已变化时需要重新预览。</p>
      <button type="button" :disabled="confirming || completed" @click="$emit('confirm', result.preview_id)">{{ confirming ? '正在核验…' : completed ? '已提交确认' : '确认' + operationText }}</button>
    </template>
    <template v-else-if="result.kind === 'dv_checkplan_result'">
      <p v-if="result.outcome === 'succeeded' && result.verified === true">{{ result.label || '操作已完成回读核验。' }}</p>
      <p v-else-if="result.outcome === 'succeeded'">结果未核实，请先核对MES中的计划。</p>
      <p v-else-if="result.outcome === 'unknown'">结果尚未确认，请先核对；不能直接重试。</p>
      <p v-else>{{ result.label || '操作失败，需要重新预览。' }}</p>
    </template>
    <template v-else-if="plan && Object.keys(plan).length">
      <p>计划：{{ value(plan.planCode) }} {{ value(plan.planName) }}（ID {{ value(plan.planId) }}）</p>
      <p v-for="field in rowFields(plan)" :key="field">{{ fieldLabel(field) }}：{{ display(plan[field], field) }}</p>
    </template>
    <template v-else><p>{{ result.label || '暂无可显示的数据。' }}</p></template>
  </section>
</template>
<script>
const labels = {
  planId: '计划ID', planCode: '计划编号', planName: '计划名称', planType: '计划类型',
  cycleType: '周期类型', cycleCount: '周期次数', startDate: '开始日期', endDate: '结束日期',
  status: '状态', remark: '备注', recordId: '关联记录ID', machineryId: '设备ID',
  machineryCode: '设备编码', machineryName: '设备名称', machineryBrand: '品牌快照',
  machinerySpec: '规格快照', subjectId: '项目ID', subjectCode: '项目编码',
  subjectName: '项目名称', subjectType: '项目类型快照', subjectContent: '项目内容快照',
  subjectStandard: '项目标准快照', enableFlag: '启用状态'
}
const valueLabels = {
  planType: { CHECK: '点检', MAINTEN: '保养' },
  status: { PREPARE: '草稿', FINISHED: '已启用' },
  cycleType: { HOUR: '小时', DAY: '天', WEEK: '周', MONTH: '月', QUARTER: '季度', YEAR: '年' }
}
export default {
  name: 'AgentDvCheckPlanCard',
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    preview() { return this.result.kind === 'dv_checkplan_preview' },
    before() { return this.result.before || {} },
    after() { return this.result.after || {} },
    beforeValues() { return this.before.plan || this.before },
    afterValues() { return this.after.plan || this.after },
    plan() { return this.result.plan || this.after.plan || this.before.plan || this.after },
    link() {
      if (this.result.link || this.after.link || this.before.link || this.before.record) {
        return this.result.link || this.after.link || this.before.link || this.before.record
      }
      if (this.after.machineryId || this.after.subjectId) return this.after
      const source = this.before.machinery || this.before.subject
      if (source && (source.machineryId || source.subjectId)) return source
      return null
    },
    affectedCounts() { return this.result.affected_counts || this.before.affected_counts || null },
    affectedSamples() {
      const before = this.before
      const machinery = before.machinery && (before.machinery.rows || before.machinery.samples) || []
      const subject = before.subject && (before.subject.rows || before.subject.samples) || []
      return [...machinery, ...subject]
    },
    transition() { return this.result.transition || this.after.transition || '' },
    operationText() {
      if (this.transition === 'activate') return '启用'
      if (this.transition === 'deactivate') return '停用'
      return this.actionText(this.result.action)
    },
    previewFields() {
      return [...new Set([...Object.keys(this.beforeValues), ...Object.keys(this.afterValues)])]
        .filter(key => labels[key] && !['plan', 'links', 'machinery', 'subject', 'record', 'affected_counts'].includes(key))
    },
    title() {
      const titles = {
        dv_checkplan_list: '点检保养计划查询结果', dv_checkplan_detail: '点检保养计划详情',
        dv_checkplan_options: '点检保养计划可用选项', dv_checkplan_links: '计划关联明细',
        dv_checkplan_machinery_candidates: '设备候选', dv_checkplan_subject_candidates: '项目候选',
        dv_checkplan_preview: '点检保养计划操作预览', dv_checkplan_result: '点检保养计划操作结果'
      }
      return titles[this.result.kind] || '点检保养计划'
    }
  },
  methods: {
    value(value) { return value === undefined || value === null || value === '' ? '—' : String(value) },
    display(value, field) {
      if (value === undefined || value === null || value === '') return '—'
      return valueLabels[field] && valueLabels[field][value] || String(value)
    },
    optionsText(rows) { return (rows || []).map(item => item.label + '（' + item.value + '）').join('、') || '—' },
    rowFields(row) {
      return Object.keys(labels).filter(key => row[key] !== undefined && !['planId', 'planCode', 'planName'].includes(key))
    },
    fieldLabel(field) { return labels[field] || field },
    describeLink(link) {
      return [link.recordId, link.machineryId || link.subjectId, link.machineryCode || link.subjectCode,
        link.machineryName || link.subjectName, link.machineryBrand, link.machinerySpec,
        link.subjectType, link.subjectContent, link.subjectStandard]
        .filter(value => value !== undefined && value !== null && value !== '').join('　') || '—'
    },
    actionText(action) {
      return { create: '新增', update: '修改', delete: '删除', machinery_add: '关联设备',
        machinery_remove: '移除设备', subject_add: '关联项目', subject_remove: '移除项目' }[action] || '操作'
    }
  }
}
</script>
<style scoped>
.checkplan-card{margin-top:10px;padding:12px;border:1px solid #dfe4ed;border-radius:6px;background:#fff;color:#303133;font-size:12px}.checkplan-card__row{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid #ebeef5}.checkplan-card dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.checkplan-card p{line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}.checkplan-card article{padding:8px 0;border-bottom:1px solid #ebeef5}.checkplan-card button{min-height:34px;padding:0 12px;border:0;border-radius:4px;background:var(--current-color,#409eff);color:#fff;cursor:pointer}.checkplan-card button:disabled{opacity:.55;cursor:not-allowed}
</style>
