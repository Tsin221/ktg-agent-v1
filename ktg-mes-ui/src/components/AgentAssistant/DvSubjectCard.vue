<template>
  <section class="subject-card" :aria-label="title">
    <strong>{{ title }}</strong>
    <template v-if="result.kind === 'dv_subject_list'">
      <p>共 {{ result.total }} 条，第 {{ result.page }} 页</p>
      <p v-if="!result.rows || !result.rows.length">没有匹配的项目。</p>
      <div v-for="row in result.rows" :key="row.subjectId" class="subject-card__row">
        <span>[{{ row.subjectId }}] {{ row.subjectCode }} {{ row.subjectName }}</span>
        <span>{{ display(row.subjectType, 'subjectType') }} · {{ display(row.enableFlag, 'enableFlag') }}</span>
      </div>
    </template>
    <template v-else-if="preview || result.kind === 'dv_subject_detail'">
      <p v-if="record.subjectId">项目 ID：{{ record.subjectId }} · {{ record.subjectCode }} {{ record.subjectName }}</p>
      <dl>
        <div v-for="field in fields" :key="field.key" class="subject-card__row">
          <dt>{{ field.label }}</dt>
          <dd v-if="preview && result.action === 'update'">{{ display(before[field.key], field.key) }} → {{ display(record[field.key], field.key) }}</dd>
          <dd v-else>{{ display(record[field.key], field.key) }}</dd>
        </div>
      </dl>
      <template v-if="preview">
        <p v-for="warning in result.warnings" :key="warning">{{ warning }}</p>
        <p>确认后使用当前登录账号执行。预览过期或项目已变化时，需要重新预览。</p>
        <button type="button" :disabled="confirming || completed" @click="$emit('confirm', result.preview_id)">
          {{ confirming ? '正在核验…' : completed ? '已提交确认' : confirmLabel }}
        </button>
      </template>
    </template>
    <p v-else role="status">{{ result.label }}</p>
    <p v-if="result.outcome === 'unknown'">结果待核对，系统不会自动重新提交。</p>
  </section>
</template>

<script>
const labels = { subjectCode: '项目编码', subjectName: '项目名称', subjectType: '项目类型',
  enableFlag: '是否启用', subjectContent: '项目内容', subjectStandard: '标准', remark: '备注' }
export default {
  name: 'AgentDvSubjectCard',
  dicts: ['mes_dvsubject_type', 'sys_yes_no'],
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    preview() { return this.result.kind === 'dv_subject_preview' },
    before() { return this.result.before || {} },
    record() { return this.result.subject || this.result.after || {} },
    fields() {
      return Object.keys(labels).filter(key => this.record[key] !== undefined &&
        (!this.preview || this.result.action !== 'update' || this.before[key] !== this.record[key]))
        .map(key => ({ key, label: labels[key] }))
    },
    actionText() { return { create: '新增', update: '修改', delete: '删除' }[this.result.action] || '' },
    confirmLabel() { return `确认${this.actionText}` },
    title() {
      if (this.result.kind === 'dv_subject_list') return '点检保养项目查询结果'
      if (this.result.kind === 'dv_subject_detail') return '点检保养项目详情'
      return `点检保养项目${this.actionText}${this.preview ? '预览' : '结果'}`
    }
  },
  methods: {
    display(value, field) {
      if (value === null || value === undefined || value === '') return '—'
      const name = { subjectType: 'mes_dvsubject_type', enableFlag: 'sys_yes_no' }[field]
      const options = name && this.dict && this.dict.type && this.dict.type[name]
      const match = options && options.find(item => item.value === value)
      return match ? match.label : String(value)
    }
  }
}
</script>

<style lang="scss" scoped>
.subject-card { margin-top: 10px; padding: 12px; border: 1px solid #dfe4ed; border-radius: 6px; background: #fff; color: #303133; font-size: 12px; }
.subject-card__row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 6px 0; border-bottom: 1px solid #ebeef5; }
.subject-card dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.subject-card p { line-height: 1.5; }
.subject-card button { min-height: 34px; padding: 0 12px; border: 0; border-radius: 4px; background: var(--current-color, #409eff); color: #fff; cursor: pointer; }
.subject-card button:disabled { opacity: .55; cursor: not-allowed; }
</style>
