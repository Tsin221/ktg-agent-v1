<template>
  <section class="repair-card" :aria-label="title">
    <strong>{{ title }}</strong>
    <template v-if="result.rows">
      <p>共 {{ result.total }} 条，第 {{ result.page }} 页，本页 {{ result.rows.length }} 条</p>
      <p v-if="!result.rows.length">没有匹配记录。</p>
      <article v-for="(row, index) in result.rows" :key="row.lineId || row.repairId || row.machineryId || row.subjectId || index">
        <p v-for="field in rowFields(row)" :key="field">{{ label(field) }}：{{ display(row[field], field) }}</p>
      </article>
    </template>
    <template v-else-if="result.kind === 'dv_repair_options'">
      <p>维修结果：{{ options(result.repair_results) }}</p>
      <p>单据状态：{{ options(result.statuses) }}</p>
      <p>本批支持：{{ (result.supported_writes || []).join('、') }}</p>
    </template>
    <template v-else-if="result.kind === 'dv_repair_preview'">
      <p>维修单：{{ display(repair.repairCode) }} {{ display(repair.repairName) }}（ID {{ display(repair.repairId) }}）</p>
      <dl>
        <div v-for="field in previewFields" :key="field" class="repair-card__row">
          <dt>{{ label(field) }}</dt><dd>{{ display(beforeValues[field], field) }} → {{ display(afterValues[field], field) }}</dd>
        </div>
      </dl>
      <p v-if="result.affected_counts && result.affected_counts.line_count !== undefined">将一并删除维修内容 {{ result.affected_counts.line_count }} 条。</p>
      <p v-for="sample in affectedSamples" :key="sample.lineId">维修内容 [{{ sample.lineId }}]：{{ display(sample.subjectName) }}；{{ display(sample.malfunction) }}</p>
      <p v-for="warning in result.warnings || []" :key="warning">{{ warning }}</p>
      <p>确认后使用当前登录账号执行；预览过期或数据变化时需重新预览。</p>
      <button type="button" :disabled="confirming || completed" @click="$emit('confirm', result.preview_id)">{{ confirming ? '正在核验…' : completed ? '已提交确认' : '确认' + action }}</button>
    </template>
    <template v-else-if="result.kind === 'dv_repair_result'">
      <p v-if="result.outcome === 'succeeded' && result.verified === true">{{ result.label || '操作已完成回读核验。' }}</p>
      <p v-else-if="result.outcome === 'succeeded'">结果未核实，请先查询核对MES中的维修单。</p>
      <p v-else-if="result.outcome === 'unknown'">结果未知，请先查询核对；不能直接重试。</p>
      <p v-else>{{ result.label || '操作未核验成功，请查询核对。' }}</p>
    </template>
    <template v-else-if="result.repair">
      <p v-for="field in rowFields(result.repair)" :key="field">{{ label(field) }}：{{ display(result.repair[field], field) }}</p>
    </template>
    <template v-else><p>{{ result.label || '暂无可显示的数据。' }}</p></template>
  </section>
</template>
<script>
const labels = {
  repairId: '维修单ID', repairCode: '维修单编号', repairName: '维修单名称', machineryId: '设备ID',
  machineryCode: '设备编码', machineryName: '设备名称', machineryBrand: '品牌', machinerySpec: '规格',
  machineryTypeId: '设备类型ID', requireDate: '报修日期', finishDate: '维修完成日期', confirmDate: '验收日期',
  repairResult: '维修结果', acceptedBy: '维修人员', confirmBy: '验收人员', status: '状态', remark: '备注',
  lineId: '维修内容ID', subjectId: '项目ID', subjectCode: '项目编码', subjectName: '项目名称',
  subjectType: '项目类型', subjectContent: '项目内容', subjectStandard: '标准',
  malfunction: '故障描述', malfunctionUrl: '故障资源', repairDes: '维修情况'
}
const valueLabels = {
  status: { PREPARE: '草稿', APPROVING: '审批中', APPROVED: '已审批', FINISHED: '已完成', CONFIRMED: '已确认' },
  repairResult: { SUCCESS: '修复成功', SCRAP: '报废' }
}
export default {
  name: 'AgentDvRepairCard',
  props: { result: { type: Object, required: true }, confirming: { type: Boolean, default: false }, completed: { type: Boolean, default: false } },
  computed: {
    title() { return ({ dv_repair_list: '维修单查询结果', dv_repair_detail: '维修单详情', dv_repair_lines: '维修内容',
      dv_repair_options: '维修选项', dv_repair_machinery_candidates: '设备候选', dv_repair_subject_candidates: '项目候选',
      dv_repair_preview: '维修单操作预览', dv_repair_result: '维修单操作结果' })[this.result.kind] || '维修单' },
    before() { return this.result.before || {} },
    after() { return this.result.after || {} },
    beforeValues() { return this.result.action && this.result.action.startsWith('line_') ? this.before.line || {} : this.before.repair || this.before },
    afterValues() { return this.after },
    repair() { return this.before.repair || (this.result.action === 'create' ? this.after : (this.after.repairId ? this.after : this.before)) },
    previewFields() { return [...new Set([...Object.keys(this.beforeValues), ...Object.keys(this.afterValues)])].filter(key => labels[key]) },
    affectedSamples() { return this.before.lines && this.before.lines.samples || [] },
    action() { return ({ create: '新增维修单', update: '修改维修单', delete: '删除维修单',
      line_create: '新增维修内容', line_update: '修改维修内容', line_delete: '删除维修内容' })[this.result.action] || '操作' }
  },
  methods: {
    label(field) { return labels[field] || field },
    display(value, field) {
      if (value === undefined || value === null || value === '') return '—'
      return valueLabels[field] && valueLabels[field][value] || String(value)
    },
    rowFields(row) { return Object.keys(labels).filter(key => row[key] !== undefined) },
    options(rows) { return (rows || []).map(item => `${item.label}（${item.value}）`).join('、') || '—' }
  }
}
</script>
<style scoped>
.repair-card{margin-top:10px;padding:12px;border:1px solid #dfe4ed;border-radius:6px;background:#fff;color:#303133;font-size:12px}.repair-card__row{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid #ebeef5}.repair-card dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.repair-card p{line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}.repair-card article{padding:8px 0;border-bottom:1px solid #ebeef5}.repair-card button{min-height:34px;padding:0 12px;border:0;border-radius:4px;background:var(--current-color,#409eff);color:#fff;cursor:pointer}.repair-card button:disabled{opacity:.55;cursor:not-allowed}
</style>
