<template>
  <section class="operation-card" aria-label="工单操作方案">
    <header class="operation-card__heading">
      <strong>工单操作方案</strong>
      <span :class="['operation-card__status', 'operation-card__status--' + plan.status]">{{ label(plan.status) }}</span>
    </header>
    <p class="operation-card__summary" role="status">
      <template v-if="plan.status === 'pending'">共 {{ plan.steps.length }} 步，尚未写入 MES。</template>
      <template v-else>{{ successCount }} / {{ plan.steps.length }} 步成功 · {{ writtenCount }} 项 MES 写入已核实<span v-if="unknownCount"> · {{ unknownCount }} 步待核实</span>。</template>
    </p>
    <ol class="operation-card__steps">
      <li v-for="(step, index) in plan.steps" :key="step.step_id" class="operation-card__step">
        <div class="operation-card__step-heading">
          <strong>{{ index + 1 }}. {{ actionLabel(step.action) }}</strong>
          <span :class="['operation-card__status', 'operation-card__status--' + resultAt(index).status]">{{ label(resultAt(index).status) }}</span>
        </div>
        <template v-if="step.target">
          <p class="operation-card__name">{{ step.target.workorder_name }} · {{ step.target.workorder_code }}</p>
          <p class="operation-card__meta">{{ step.target.product_name }} · {{ step.target.product_code }}</p>
        </template>
        <dl v-if="step.action === 'create_draft'" class="operation-card__fields">
          <template v-for="field in draftFields"><div v-if="step.draft[field[0]] !== undefined && !['workorderName','productId'].includes(field[0])" :key="field[0]"><dt>{{ field[1] }}</dt><dd>{{ fieldValue(field[0], step.draft[field[0]]) }}</dd></div></template>
        </dl>
        <p v-if="step.action === 'create_draft' || step.bom_rebuild" class="operation-card__meta">按执行时有效产品 BOM 生成或重算草稿用料，不因缺料取消已确认的草稿创建。</p>
        <dl v-if="step.action === 'edit_draft'" class="operation-card__fields">
          <div v-for="field in changedFields(step)" :key="field[0]"><dt>{{ field[1] }}</dt><dd>{{ fieldValue(field[0], step.before[field[0]]) }} → {{ field[0] === 'productId' && step.after_product ? step.after_product.name + ' · ' + step.after_product.code : fieldValue(field[0], step.draft[field[0]]) }}</dd></div>
        </dl>
        <template v-if="step.action === 'release'">
          <p class="operation-card__meta">计划量 {{ step.target.quantity }} · {{ step.selection.route_name }} · 生成 3 道任务及 {{ step.target.quantity }} 个 SN</p>
          <div class="operation-card__flow">
            <div v-for="routeStep in step.selection.steps" :key="routeStep.route_process_id">
              <strong>{{ routeStep.process_name }}</strong><span>{{ routeStep.workstation_name }}</span>
              <small v-if="routeStep.template_name">{{ routeStep.template_name }} · {{ routeStep.ipqc_type }}</small>
            </div>
          </div>
          <p v-if="step.condition === 'warehouse_reference_sufficient'" class="operation-card__notice">仅在原仓账面备料需求可覆盖时下达；不预留或领料。</p>
        </template>
        <template v-if="step.action === 'check_materials'">
          <p v-for="target in step.targets" :key="target.workorder_code" class="operation-card__meta">{{ target.workorder_name }} · {{ target.workorder_code }} · {{ target.quantity }}</p>
          <p class="operation-card__notice">联合核对账面材料需求，不预留或领料。</p>
        </template>
        <p v-if="step.depends_on.length" class="operation-card__meta">依赖第 {{ dependencyNumbers(step).join('、') }} 步成功。</p>
        <p v-if="resultAt(index).message" class="operation-card__result">{{ resultAt(index).message }}</p>
        <p v-if="resultAt(index).sn_count !== undefined" class="operation-card__result">实际生成 {{ resultAt(index).task_count }} 道任务、{{ resultAt(index).sn_count }} 个 SN。</p>
        <details v-if="resultAt(index).committed_at || resultAt(index).status === 'unknown'" class="operation-card__receipt">
          <summary>操作回执</summary>
          <p class="operation-card__meta">操作编号 {{ resultAt(index).operation_id }}</p>
          <p v-if="resultAt(index).committed_at" class="operation-card__meta">MES 提交时间 {{ resultAt(index).committed_at }}</p>
          <p v-if="resultAt(index).workorder_id" class="operation-card__meta">工单 ID {{ resultAt(index).workorder_id }} · 版本 {{ resultAt(index).version }}</p>
        </details>
        <template v-if="resultAt(index).material_reference">
          <p class="operation-card__notice">{{ assessmentLabel(resultAt(index).material_reference.assessment) }} · 原仓账面参考</p>
          <div class="operation-card__table-wrap" role="region" aria-label="联合备料核对" tabindex="0">
            <table><thead><tr><th>物料</th><th>需求</th><th>账面</th><th>缺口</th></tr></thead><tbody>
              <tr v-for="row in resultAt(index).material_reference.materials" :key="row.itemId">
                <th scope="row">{{ row.itemName }}</th><td>{{ row.planned_required }}</td><td>{{ row.stock_quantity == null ? '待核实' : row.stock_quantity }}</td><td>{{ row.shortage_quantity == null ? '待核实' : row.shortage_quantity }} {{ row.unitOfMeasure }}</td>
              </tr>
            </tbody></table>
          </div>
        </template>
      </li>
    </ol>
    <footer v-if="plan.status === 'pending' || plan.status === 'needs_reconciliation' || processing" class="operation-card__actions">
      <template v-if="plan.status === 'pending'">
        <button type="button" class="operation-card__primary" :disabled="busy || !plan.confirmable" @click="$emit('action', 'confirm')">确认执行</button>
        <button type="button" :disabled="busy" @click="$emit('action', 'cancel')">取消方案</button>
        <small v-if="!plan.confirmable">当前运行尚未正常完成或操作功能已关闭。</small>
      </template>
      <button v-else-if="plan.status === 'needs_reconciliation'" type="button" class="operation-card__primary" :disabled="busy" @click="$emit('action', 'reconcile')">核实原回执</button>
      <span v-else role="status">正在处理，请稍候…</span>
    </footer>
  </section>
</template>

<script>
export default {
  name: 'AgentOperationPlanCard',
  props: { plan: { type: Object, required: true }, busy: { type: Boolean, default: false } },
  data() {
    return { draftFields: [['workorderName', '名称'], ['productId', '产品 ID'], ['quantity', '计划量'], ['requestDate', '需求日期'],
      ['workorderType', '类型'], ['orderSource', '来源'], ['sourceCode', '来源单据'], ['batchCode', '批次'], ['remark', '备注']] }
  },
  computed: {
    successCount() { return this.plan.results.filter(result => result.status === 'succeeded').length },
    writtenCount() { return this.plan.results.filter((result, index) => result.status === 'succeeded' && this.plan.steps[index].action !== 'check_materials').length },
    unknownCount() { return this.plan.results.filter(result => result.status === 'unknown').length },
    processing() { return ['executing', 'reconciling'].includes(this.plan.status) }
  },
  methods: {
    resultAt(index) { return this.plan.results[index] || { status: 'pending' } },
    label(status) { return ({ pending: '待确认', executing: '执行中', reconciling: '核实中', succeeded: '成功', partial: '部分完成',
      failed: '未完成', needs_reconciliation: '待核实', unknown: '待核实', started: '执行中', skipped: '未执行',
      cancelled: '已取消', superseded: '已被新方案替代', expired: '已过期' })[status] || '待核实' },
    actionLabel(action) { return ({ create_draft: '创建草稿', edit_draft: '修改草稿', release: '下达工单', check_materials: '联合备料核对' })[action] || action },
    assessmentLabel(value) { return ({ reference_sufficient: '账面充足', reference_shortage: '账面不足', unavailable: '无法核实' })[value] || '无法核实' },
    fieldValue(field, value) {
      if (value === null || value === undefined || value === '') return '未设置'
      if (field === 'workorderType' && value === 'SELF') return '自制'
      if (field === 'orderSource') return ({ STORE: '库存备货', ORDER: '客户订单' })[value] || value
      return value
    },
    changedFields(step) { return this.draftFields.filter(field => step.before[field[0]] !== step.draft[field[0]]) },
    dependencyNumbers(step) { return step.depends_on.map(id => this.plan.steps.findIndex(candidate => candidate.step_id === id) + 1) }
  }
}
</script>

<style scoped>
.operation-card{border:1px solid #dbe1e9;border-radius:9px;background:#fff;padding:12px;margin:8px 0;color:#253244;font-size:13px;line-height:1.5}
.operation-card__heading,.operation-card__step-heading,.operation-card__actions{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.operation-card__heading{border-bottom:1px solid #e9edf2;padding-bottom:8px}.operation-card__summary,.operation-card__name{margin:8px 0 4px;font-weight:600;overflow-wrap:anywhere}
.operation-card__status{font-size:12px;background:#eef2f7;color:#526173;border-radius:4px;padding:2px 6px;white-space:nowrap}
.operation-card__status--pending,.operation-card__status--needs_reconciliation,.operation-card__status--unknown{background:#fff2d7;color:#885a06}.operation-card__status--succeeded{background:#e7f6ee;color:#17633b}.operation-card__status--failed,.operation-card__status--partial{background:#fdebe9;color:#a83a31}
.operation-card__steps{list-style:none;padding:0;margin:10px 0}.operation-card__step{padding:10px 0;border-top:1px solid #eef1f5}.operation-card__step:first-child{border-top:0;padding-top:0}
.operation-card__meta,.operation-card__notice,.operation-card__result{margin:5px 0;color:#657185;font-size:12px;overflow-wrap:anywhere}.operation-card__notice{color:#795a17;background:#fffaec;padding:6px;border-radius:4px}.operation-card__result{color:#253244}
.operation-card__fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:6px;margin:8px 0}.operation-card__fields div,.operation-card__flow div{background:#f5f7fa;padding:6px 8px;border-radius:4px;overflow-wrap:anywhere}.operation-card__fields dt{color:#657185;font-size:11px}.operation-card__fields dd{margin:2px 0 0;font-weight:500}
.operation-card__flow{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:5px;margin:6px 0}.operation-card__flow strong,.operation-card__flow span,.operation-card__flow small{display:block}.operation-card__flow span,.operation-card__flow small{font-size:11px;color:#657185}
.operation-card__table-wrap{overflow-x:auto}.operation-card table{width:100%;border-collapse:collapse;font-size:12px;text-align:left;white-space:nowrap}.operation-card th,.operation-card td{padding:5px;border-bottom:1px solid #e9edf2}
.operation-card__actions{justify-content:flex-start;border-top:1px solid #e9edf2;padding-top:10px}.operation-card button{font:inherit;padding:6px 11px;border:1px solid #cad2df;background:white;border-radius:5px;cursor:pointer;color:#35445d}.operation-card button.operation-card__primary{background:#2859a8;color:white;border-color:#2859a8}.operation-card button:disabled{opacity:.5;cursor:not-allowed}.operation-card__actions small{color:#657185}
</style>
