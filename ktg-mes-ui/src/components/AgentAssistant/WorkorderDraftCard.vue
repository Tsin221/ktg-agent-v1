<template>
  <section class="draft-card" :class="`draft-card--${tone}`" :aria-label="title">
    <header class="draft-card__header">
      <strong>{{ title }}</strong>
      <span v-if="result.status" class="draft-card__status">{{ result.status }}</span>
    </header>

    <template v-if="result.kind === 'workorder_product_candidates'">
      <p class="draft-card__hint">{{ candidateHint }}</p>
      <button
        v-for="candidate in result.candidates || []"
        :key="candidate.itemId"
        class="candidate"
        type="button"
        @click="$emit('select-product', candidate)"
      >
        <strong>{{ candidate.itemName || '未命名产品' }}</strong>
        <span>{{ candidate.itemCode || '-' }} · {{ candidate.unitOfMeasure || '无单位' }}</span>
      </button>
    </template>

    <template v-else>
      <dl class="draft-card__facts">
        <div v-if="productName"><dt>产品</dt><dd>{{ productName }}</dd></div>
        <div v-if="result.product && result.product.specification"><dt>规格型号</dt><dd>{{ result.product.specification }}</dd></div>
        <div v-if="result.workorderName"><dt>工单名称</dt><dd>{{ result.workorderName }}</dd></div>
        <div v-if="result.workorderCode"><dt>工单编号</dt><dd>{{ result.workorderCode }}</dd></div>
        <div v-if="result.code_mode"><dt>编号方式</dt><dd>{{ result.code_mode === 'auto' ? '自动生成' : '手工填写' }}</dd></div>
        <div v-if="result.orderSource"><dt>来源类型</dt><dd>{{ orderSourceLabel(result.orderSource) }}</dd></div>
        <div v-if="result.sourceCode"><dt>来源单号</dt><dd>{{ result.sourceCode }}</dd></div>
        <div v-if="result.workorderType"><dt>工单类型</dt><dd>{{ formatType(result.workorderType) }}</dd></div>
        <div v-if="result.quantity !== undefined"><dt>数量</dt><dd>{{ result.quantity }} {{ unit }}</dd></div>
        <div v-if="result.requestDate"><dt>需求日期</dt><dd>{{ shortDate(result.requestDate) }}</dd></div>
        <div v-if="result.remark"><dt>备注</dt><dd>{{ result.remark }}</dd></div>
        <div v-if="result.workorderId"><dt>工单 ID</dt><dd>{{ result.workorderId }}</dd></div>
        <div v-if="result.version !== undefined"><dt>版本</dt><dd>{{ result.version }}</dd></div>
        <div v-if="result.kind === 'workorder_draft_result' && result.bom_verified !== undefined">
          <dt>BOM</dt>
          <dd>{{ result.bom_verified ? `已核验（${result.bom_count} 行）` : '未核验' }}</dd>
        </div>
      </dl>

      <div v-if="isPreview && result.changes && result.changes.length" class="draft-card__changes">
        <h3>{{ result.action === 'edit' ? '将改变的字段' : '将写入的字段' }}</h3>
        <div v-for="change in result.changes" :key="change.field" class="draft-card__change">
          <span>{{ fieldLabel(change.field) }}</span>
          <span>{{ valueText(change.before) }} → {{ valueText(change.after) }}</span>
        </div>
      </div>

      <p v-if="isPreview" class="draft-card__warning">确认后将使用当前登录账号写入，预览参数不可更改。</p>
      <p v-else-if="result.outcome === 'unknown'" class="draft-card__warning">
        系统不会自动重试，请从工单列表核对后再操作。
      </p>

      <div class="draft-card__actions">
        <button
          v-if="isPreview"
          type="button"
          class="draft-card__primary"
          :disabled="confirming || completed"
          @click="$emit('confirm', result.preview_id)"
        >{{ confirming ? '正在核验…' : completed ? '已提交确认' : confirmLabel }}</button>
        <button
          v-if="result.workorderId"
          type="button"
          class="draft-card__secondary"
          @click="$emit('view-workorder', result)"
        >查看工单</button>
      </div>
    </template>
  </section>
</template>

<script>
import { formatProductCandidatesHint, formatWorkorderType, formatWorkorderValue, workorderFieldLabel } from './workorderPresentation'

export default {
  name: 'AgentWorkorderDraftCard',
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    isPreview() { return this.result.kind === 'workorder_draft_preview' },
    candidateHint() { return formatProductCandidatesHint(this.result) },
    title() {
      if (this.result.kind === 'workorder_product_candidates') return '请选择产品'
      if (this.isPreview) return this.result.action === 'edit' ? '工单草稿修改预览' : '添加生产工单预览'
      return this.result.label || '工单操作结果'
    },
    tone() {
      if (this.isPreview) return 'preview'
      return this.result.outcome || 'neutral'
    },
    productName() {
      const product = this.result.product || {}
      const code = product.itemCode ? `（${product.itemCode}）` : ''
      return this.result.productName || (product.itemName ? `${product.itemName}${code}` : '')
    },
    unit() {
      return this.result.unitOfMeasure || (this.result.product && this.result.product.unitOfMeasure) || ''
    },
    confirmLabel() { return this.result.action === 'edit' ? '确认修改' : '确认创建' }
  },
  methods: {
    fieldLabel: workorderFieldLabel,
    valueText: formatWorkorderValue,
    formatType: formatWorkorderType,
    orderSourceLabel(value) {
      if (value === 'ORDER') return '客户订单'
      if (value === 'STORE') return '库存备货'
      return value
    },
    shortDate(value) { return String(value).slice(0, 10) }
  }
}
</script>

<style lang="scss" scoped>
.draft-card { margin-top: 10px; padding: 12px; border: 1px solid #dfe4ed; border-left-width: 4px; border-radius: 6px; background: #fff; color: #303133; }
.draft-card--preview { border-left-color: #e6a23c; }
.draft-card--succeeded { border-left-color: #67c23a; }
.draft-card--failed, .draft-card--unknown { border-left-color: #f56c6c; }
.draft-card__header, .draft-card__facts div, .draft-card__change, .draft-card__actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.draft-card__header { font-size: 14px; }
.draft-card__status { color: #606266; font-size: 12px; }
.draft-card__facts { margin: 10px 0 0; font-size: 12px; }
.draft-card__facts div { padding: 4px 0; }
.draft-card__facts dt { color: #909399; }
.draft-card__facts dd { margin: 0; text-align: right; overflow-wrap: anywhere; }
.draft-card__changes { margin-top: 10px; padding-top: 8px; border-top: 1px solid #ebeef5; }
.draft-card__changes h3 { margin: 0 0 5px; font-size: 12px; }
.draft-card__change { padding: 3px 0; color: #606266; font-size: 12px; }
.draft-card__change span:last-child { text-align: right; overflow-wrap: anywhere; }
.draft-card__hint, .draft-card__warning { margin: 9px 0 0; color: #606266; font-size: 12px; line-height: 1.5; }
.candidate { display: flex; flex-direction: column; width: 100%; margin-top: 8px; padding: 8px; border: 1px solid #dcdfe6; border-radius: 4px; background: #fff; text-align: left; cursor: pointer; }
.candidate span { margin-top: 3px; color: #606266; font-size: 12px; }
.draft-card__actions { justify-content: flex-start; margin-top: 10px; }
.draft-card__actions button { min-height: 34px; padding: 0 12px; border-radius: 4px; cursor: pointer; }
.draft-card__primary { border: 0; background: var(--current-color, #409eff); color: #fff; }
.draft-card__secondary { border: 1px solid #dcdfe6; background: #fff; color: #409eff; }
.draft-card__actions button:disabled { opacity: 0.55; cursor: not-allowed; }
.candidate:focus, .draft-card__actions button:focus { outline: 2px solid #303133; outline-offset: 2px; }
</style>
