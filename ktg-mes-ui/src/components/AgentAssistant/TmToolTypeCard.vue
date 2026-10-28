<template>
  <section class="tooltype-card" :aria-label="title">
    <strong>{{ title }}</strong>
    <template v-if="result.kind === 'tm_tooltype_list'">
      <p>共 {{ result.total }} 条，本页 {{ (result.rows || []).length }} 条（第 {{ result.page }} 页）。</p>
      <div v-for="row in result.rows || []" :key="row.toolTypeId" class="tooltype-card__row">
        <span>[{{ row.toolTypeId }}] {{ row.toolTypeCode }} {{ row.toolTypeName }}</span>
        <span>{{ maintenance(row) }}</span>
      </div>
      <p v-if="!result.rows || !result.rows.length">没有匹配的工装夹具类型。</p>
    </template>
    <template v-else-if="result.kind === 'tm_tooltype_options'">
      <p>是否编码管理：{{ labels(result.options.code_flags) }}</p>
      <p>维护类型：{{ labels(result.options.maintenance_types) }}</p>
      <p>周期范围：{{ result.options.period.minimum }} 至 {{ result.options.period.maximum }} 的正整数；REGULAR 按天，USAGE 按次。</p>
    </template>
    <template v-else-if="isRecordResult">
      <p v-if="result.outcome" role="status">{{ statusText }}</p>
      <p v-if="result.label" role="status">{{ result.label }}</p>
      <p v-if="record.toolTypeId">[{{ record.toolTypeId }}] {{ record.toolTypeCode }} {{ record.toolTypeName }}</p>
      <dl>
        <div v-for="field in visibleFields" :key="field.key" class="tooltype-card__row">
          <dt>{{ field.label }}</dt>
          <dd v-if="preview && result.action === 'update'">{{ display(before[field.key], field.key, before) }} → {{ display(record[field.key], field.key, record) }}</dd>
          <dd v-else>{{ display(record[field.key], field.key, record) }}</dd>
        </div>
      </dl>
      <p v-if="preview && result.action === 'delete'">确认时会再次检查关联工装台账；存在引用时将拒绝删除。</p>
      <p v-if="preview">预览有效期有限。请确认目标和变更内容后继续。</p>
      <button v-if="preview" type="button" :disabled="confirming || completed" @click="$emit('confirm', result.preview_id)">
        {{ confirming ? '正在核验…' : completed ? '已提交确认' : confirmLabel }}
      </button>
    </template>
    <p v-else role="status">{{ result.label || '工装夹具类型操作未完成。' }}</p>
    <p v-if="result.outcome === 'unknown'" role="status">结果未知，系统不会自动重新提交，请先核对MES。</p>
  </section>
</template>

<script>
const fields = [
  { key: 'toolTypeCode', label: '类型编码' }, { key: 'toolTypeName', label: '类型名称' },
  { key: 'codeFlag', label: '是否编码管理' }, { key: 'maintenType', label: '维护类型' },
  { key: 'maintenPeriod', label: '维护周期' }, { key: 'remark', label: '备注' }
]
export default {
  name: 'AgentTmToolTypeCard',
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    preview() { return this.result.kind === 'tm_tooltype_preview' },
    before() { return this.result.before || {} },
    record() { return this.result.tool_type || this.result.after || this.result.created || this.result.updated || this.result.deleted || {} },
    visibleFields() {
      return fields.filter(field => this.record[field.key] !== undefined &&
        (!this.preview || this.result.action !== 'update' || this.before[field.key] !== this.record[field.key]))
    },
    isRecordResult() {
      return this.preview || ['tm_tooltype_detail', 'tm_tooltype_create_result', 'tm_tooltype_update_result',
        'tm_tooltype_delete_result'].includes(this.result.kind)
    },
    actionText() { return { create: '新增', update: '修改', delete: '删除' }[this.result.action] || '' },
    confirmLabel() { return `确认${this.actionText}` },
    statusText() {
      if (this.result.outcome === 'succeeded' && this.result.verified === true) return '操作已完成回读核验。'
      if (this.result.outcome === 'succeeded') return 'MES返回成功，但尚未完成回读核验。'
      if (this.result.outcome === 'unknown') return '操作结果未知，请核对MES；系统不会自动重新提交。'
      return '操作未成功。'
    },
    title() {
      if (this.result.kind === 'tm_tooltype_list') return '工装夹具类型查询结果'
      if (this.result.kind === 'tm_tooltype_detail') return '工装夹具类型详情'
      if (this.result.kind === 'tm_tooltype_options') return '工装夹具类型选项'
      return `工装夹具类型${this.actionText}${this.preview ? '预览' : '结果'}`
    }
  },
  methods: {
    display(value, field, record = this.record) {
      if (value === null || value === undefined || value === '') return '无'
      if (field === 'codeFlag') return value === 'Y' ? '是' : value === 'N' ? '否' : String(value)
      if (field === 'maintenType') return value === 'REGULAR' ? '定期维护' : value === 'USAGE' ? '按使用次数维护' : String(value)
      if (field === 'maintenPeriod') {
        const type = record.maintenType
        return `${value}${type === 'REGULAR' ? '天' : type === 'USAGE' ? '次' : ''}`
      }
      return String(value)
    },
    maintenance(row) {
      if (row.codeFlag === 'N') return '无编码管理 · 无维护周期'
      const period = row.maintenPeriod == null ? '未设置' : `${row.maintenPeriod}${row.maintenType === 'REGULAR' ? '天' : row.maintenType === 'USAGE' ? '次' : ''}`
      return `${this.display(row.maintenType, 'maintenType')} · ${period}`
    },
    labels(items) { return (items || []).map(item => `${item.value}=${item.label}`).join('、') }
  }
}
</script>

<style lang="scss" scoped>
.tooltype-card { margin-top: 10px; padding: 12px; border: 1px solid #dfe4ed; border-radius: 6px; background: #fff; color: #303133; font-size: 12px; }
.tooltype-card__row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 6px 0; border-bottom: 1px solid #ebeef5; }
.tooltype-card dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.tooltype-card p { line-height: 1.5; }
.tooltype-card button { min-height: 34px; padding: 0 12px; border: 0; border-radius: 4px; background: var(--current-color, #409eff); color: #fff; cursor: pointer; }
.tooltype-card button:disabled { opacity: .55; cursor: not-allowed; }
</style>
