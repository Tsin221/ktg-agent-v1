<template>
  <section class="tm-tool-card" :aria-label="title">
    <strong>{{ title }}</strong>
    <template v-if="result.kind === 'tm_tool_list'">
      <p>共 {{ result.total }} 条，本页 {{ (result.rows || []).length }} 条。</p>
      <div v-for="row in result.rows || []" :key="row.toolId" class="tm-tool-card__row">
        <span>{{ row.toolCode }} {{ row.toolName }} · {{ row.toolTypeName }}</span>
        <span>数量 {{ row.quantity }} / 可用 {{ row.quantityAvail }} · {{ statusLabel(row.status) }}</span>
      </div>
    </template>
    <template v-else-if="result.kind === 'tm_tool_options'">
      <p>工装类型：{{ (result.options.tool_types || []).map(x => `${x.toolTypeId} ${x.toolTypeCode} ${x.toolTypeName} (${x.codeFlag})`).join('、') }}{{ result.options.tool_types_truncated ? `；仅显示${result.options.tool_types.length}/${result.options.tool_types_total}条` : '' }}</p>
      <p>维护方式：{{ (result.options.maintenance_types || []).map(x => `${x.value}=${x.label}`).join('、') }}</p>
      <p>状态：{{ (result.options.statuses || []).map(x => `${x.value}=${x.label}`).join('、') }}</p>
      <p>数量0至99999999整数；维护周期1至2147483647整数，定期维护记录日期，按使用次数记录周期。</p>
    </template>
    <template v-else-if="isRecord">
      <p v-if="result.outcome" role="status">{{ statusText }}</p>
      <p v-if="result.label" role="status">{{ result.label }}</p>
      <p v-if="record.toolCode">{{ record.toolCode }} {{ record.toolName }}</p>
      <dl>
        <div v-for="field in fields" :key="field.key" class="tm-tool-card__row">
          <dt>{{ field.label }}</dt>
          <dd v-if="preview && result.action === 'update'">{{ display(before[field.key]) }} → {{ display(record[field.key]) }}</dd>
          <dd v-else>{{ display(record[field.key]) }}</dd>
        </div>
      </dl>
      <p v-if="preview && result.action === 'delete'">仅允许删除在库且全部可用的单条记录；确认时会重新核对快照。</p>
      <p v-if="preview">请核对编码、目标和变更后再确认。</p>
      <button v-if="preview" type="button" :disabled="confirming || completed" @click="$emit('confirm', result.preview_id)">
        {{ confirming ? '正在核验…' : completed ? '已提交确认' : '确认写入' }}
      </button>
    </template>
    <p v-else role="status">{{ result.label || '工装夹具操作未完成。' }}</p>
    <p v-if="result.outcome === 'unknown'" role="status">结果未知，不会自动重试；请先核对MES。</p>
  </section>
</template>
<script>
const fields = [
  ['toolName', '名称'], ['toolTypeName', '类型'], ['quantity', '数量'], ['quantityAvail', '可用数量'], ['status', '状态'],
  ['brand', '品牌'], ['spec', '型号'], ['maintenType', '维护方式'], ['nextMaintenDate', '下次保养日期'],
  ['nextMaintenPeriod', '下次保养周期'], ['remark', '备注']
].map(([key, label]) => ({ key, label }))
export default {
  name: 'AgentTmToolCard',
  props: { result: { type: Object, required: true }, confirming: { type: Boolean, default: false }, completed: { type: Boolean, default: false } },
  computed: {
    preview() { return this.result.kind === 'tm_tool_preview' },
    before() { return this.result.before || {} },
    record() { return this.result.tool || this.result.after || this.result.verified_record || this.result.created || this.result.updated || this.result.deleted || {} },
    fields() { return fields },
    isRecord() { return this.preview || ['tm_tool_detail','tm_tool_create_result','tm_tool_update_result','tm_tool_delete_result'].includes(this.result.kind) },
    title() { if(this.result.kind==='tm_tool_list') return '工装夹具台账'; if(this.result.kind==='tm_tool_options')return '工装夹具基础选项'; const action={create:'新增',update:'修改',delete:'删除'}[this.result.action]||'详情'; return `工装夹具${action}${this.preview?'预览':'结果'}` },
    statusText() { return this.result.outcome==='succeeded'&&this.result.verified?'操作已回读核实。':this.result.outcome==='unknown'?'操作结果未知，请核对后再继续。':'操作未成功。' }
  },
  methods: {
    display(value) { if(value===null||value===undefined||value==='')return '无'; if(value==='REGULAR')return '定期维护'; if(value==='USAGE')return '按使用次数维护'; return this.statusLabel(value) },
    statusLabel(value) { return ({STORE:'在库',ISSUE:'被领用',SCRAP:'报废',REPARE:'维修中'})[value] || String(value == null || value === '' ? '无' : value) }
  }
}
</script>
<style lang="scss" scoped>
.tm-tool-card { margin-top:10px; padding:12px; border:1px solid #dfe4ed; border-radius:6px; background:#fff; color:#303133; font-size:12px; }
.tm-tool-card__row { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; padding:6px 0; border-bottom:1px solid #ebeef5; }
.tm-tool-card dd { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; }
.tm-tool-card p { line-height:1.5; }
.tm-tool-card button { min-height:34px; padding:0 12px; border:0; border-radius:4px; background:var(--current-color,#409eff); color:#fff; cursor:pointer; }
.tm-tool-card button:disabled { opacity:.55; cursor:not-allowed; }
</style>
