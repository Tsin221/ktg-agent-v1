<template>
  <section :class="['machinery-card', `machinery-card--${tone}`]" :aria-label="title">
    <header class="machinery-card__header">
      <strong>{{ title }}</strong>
      <span v-if="result.outcome" class="machinery-card__status">{{ statusText }}</span>
    </header>

    <template v-if="isList">
      <p class="machinery-card__meta">共 {{ result.total }} 台；第 {{ result.page }} 页</p>
      <div v-if="result.rows && result.rows.length" class="machinery-card__rows">
        <div v-for="row in result.rows" :key="row.machineryId" class="machinery-card__row">
          <strong>[{{ row.machineryId }}] {{ row.machineryCode }} {{ row.machineryName }}</strong>
          <span>{{ row.machineryTypeName || '未分类' }} · {{ row.workshopName || '未指定车间' }} · {{ row.status }}</span>
        </div>
      </div>
      <p v-else class="machinery-card__empty">没有匹配的设备。</p>
    </template>

    <template v-else-if="isWorkshops">
      <div v-if="result.rows && result.rows.length" class="machinery-card__rows">
        <div v-for="row in result.rows" :key="row.workshopId" class="machinery-card__row">
          <strong>[{{ row.workshopId }}] {{ row.workshopName }}</strong><span>{{ row.workshopCode }}</span>
        </div>
      </div>
      <p v-else class="machinery-card__empty">没有匹配的启用车间。</p>
    </template>

    <dl v-else-if="isDetail" class="machinery-card__facts">
      <div><dt>编码</dt><dd>{{ device.machineryCode }}</dd></div>
      <div><dt>设备</dt><dd>{{ device.machineryName }}</dd></div>
      <div><dt>分类</dt><dd>{{ device.machineryTypeName || '—' }}（ID {{ device.machineryTypeId || '—' }}）</dd></div>
      <div><dt>车间</dt><dd>{{ device.workshopName || '—' }}（ID {{ device.workshopId || '—' }}）</dd></div>
      <div><dt>状态</dt><dd>{{ device.status }}</dd></div>
      <div v-if="device.machineryBrand"><dt>品牌 / 规格</dt><dd>{{ device.machineryBrand }} / {{ device.machinerySpec }}</dd></div>
      <div v-if="device.remark"><dt>备注</dt><dd>{{ device.remark }}</dd></div>
    </dl>

    <template v-else-if="preview">
      <dl class="machinery-card__facts">
        <div v-for="field in fields" :key="field.key">
          <dt>{{ field.label }}</dt>
          <dd v-if="result.action === 'update'">{{ display(before[field.key]) }} → {{ display(after[field.key]) }}</dd>
          <dd v-else>{{ display(after[field.key]) }}</dd>
        </div>
      </dl>
      <p v-if="result.action === 'create'" class="machinery-card__warning">设备编码由你提供；确认后 MES 会生成设备条码。</p>
      <p v-if="result.action === 'delete'" class="machinery-card__warning">删除只针对这台设备。维修/点检历史关联处理尚未核实，请确认已人工检查影响。</p>
      <p class="machinery-card__warning">确认后将使用当前登录账号写入。预览不可修改，过期或数据变化时需重新预览。</p>
      <ul v-if="result.warnings && result.warnings.length" class="machinery-card__warnings">
        <li v-for="warning in result.warnings" :key="warning">{{ warning }}</li>
      </ul>
      <div class="machinery-card__actions">
        <button type="button" :disabled="confirming || completed" @click="$emit('confirm', result.preview_id)">
          {{ confirming ? '正在核验…' : completed ? '已提交确认' : confirmLabel }}
        </button>
      </div>
    </template>

    <p v-else-if="result.label" class="machinery-card__meta">{{ result.label }}</p>
    <p v-if="result.outcome === 'unknown'" class="machinery-card__warning">系统不会自动重试，请先核对设备台账再决定后续操作。</p>
  </section>
</template>

<script>
const fieldLabels = {
  machineryCode: '设备编码', machineryName: '设备名称', machineryBrand: '品牌', machinerySpec: '规格型号',
  machineryTypeName: '设备分类', workshopName: '所属车间', status: '设备状态', remark: '备注'
}

export default {
  name: 'AgentDvMachineryCard',
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    preview() { return this.result.kind === 'dv_machinery_preview' },
    isList() { return this.result.kind === 'dv_machinery_list' },
    isWorkshops() { return this.result.kind === 'md_machinery_workshop_options' },
    isDetail() { return this.result.kind === 'dv_machinery_detail' },
    device() { return this.result.machinery || {} },
    before() { return this.result.before || {} },
    after() { return this.result.after || {} },
    fields() {
      const keys = ['machineryCode', 'machineryName', 'machineryBrand', 'machinerySpec', 'machineryTypeName', 'workshopName', 'status', 'remark']
      return keys.filter(key => this.after[key] !== undefined && (this.result.action !== 'update' || this.before[key] !== this.after[key]))
        .map(key => ({ key, label: fieldLabels[key] }))
    },
    confirmLabel() { return { create: '确认新增', update: '确认修改', delete: '确认删除' }[this.result.action] || '确认' },
    title() {
      if (this.isList) return '设备台账查询结果'
      if (this.isDetail) return '设备详情'
      if (this.isWorkshops) return '启用车间候选'
      if (this.preview) return `设备${{ create: '新增', update: '修改', delete: '删除' }[this.result.action] || ''}预览`
      return `设备${{ create: '新增', update: '修改', delete: '删除' }[this.result.action] || ''}结果`
    },
    tone() { return this.preview ? 'preview' : this.result.outcome || 'neutral' },
    statusText() {
      return { succeeded: '已完成', failed: '未完成', unknown: '待核对' }[this.result.outcome] || ''
    }
  },
  methods: {
    display(value) { return value === null || value === undefined || value === '' ? '—' : String(value) }
  }
}
</script>

<style lang="scss" scoped>
.machinery-card { margin-top: 10px; padding: 12px; border: 1px solid #dfe4ed; border-left-width: 4px; border-radius: 6px; background: #fff; color: #303133; }
.machinery-card--preview { border-left-color: #e6a23c; }
.machinery-card--succeeded { border-left-color: #67c23a; }
.machinery-card--failed, .machinery-card--unknown { border-left-color: #f56c6c; }
.machinery-card__header, .machinery-card__facts div, .machinery-card__actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.machinery-card__status, .machinery-card__meta, .machinery-card__empty { color: #909399; font-size: 12px; }
.machinery-card__rows, .machinery-card__facts { margin-top: 8px; }
.machinery-card__row { display: flex; flex-direction: column; gap: 3px; padding: 8px 0; border-top: 1px solid #ebeef5; color: #606266; font-size: 12px; }
.machinery-card__row strong { color: #303133; font-size: 13px; }
.machinery-card__facts { font-size: 12px; }
.machinery-card__facts div { padding: 4px 0; }
.machinery-card__facts dt { color: #909399; }
.machinery-card__facts dd { margin: 0; text-align: right; overflow-wrap: anywhere; }
.machinery-card__warning { margin: 9px 0 0; color: #606266; font-size: 12px; line-height: 1.5; }
.machinery-card__warnings { padding-left: 20px; color: #606266; font-size: 12px; }
.machinery-card__actions { justify-content: flex-start; margin-top: 10px; }
.machinery-card__actions button { min-height: 34px; padding: 0 12px; border: 0; border-radius: 4px; background: var(--current-color, #409eff); color: #fff; cursor: pointer; }
.machinery-card__actions button:disabled { opacity: .55; cursor: not-allowed; }
</style>
