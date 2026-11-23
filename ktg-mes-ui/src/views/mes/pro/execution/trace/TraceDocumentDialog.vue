<template>
  <el-dialog :title="title" :visible.sync="visible" width="min(760px, 94vw)" append-to-body @close="close">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <p v-if="loading" role="status" aria-live="polite">正在读取原单据……</p>
    <el-descriptions v-if="record" :column="2" border>
      <el-descriptions-item v-for="field in fields" :key="field[0]" :label="field[1]">{{ record[field[0]] == null ? '—' : record[field[0]] }}</el-descriptions-item>
    </el-descriptions>
    <div slot="footer"><el-button @click="visible=false">关闭</el-button></div>
  </el-dialog>
</template>

<script>
import { getIpqc } from '@/api/mes/qc/ipqc'
import { getFeedback } from '@/api/mes/pro/feedback'
import { traceError, hasTracePermission } from './tracePresentation'

const definitions = {
  ipqc: { title: '原过程检验单（只读）', permission: 'mes:qc:ipqc:query', idField: 'ipqcId', load: getIpqc, fields: [['ipqcId', '单据 ID'], ['ipqcCode', '单据编号'], ['ipqcName', '单据名称'], ['ipqcType', '类型'], ['quantityCheck', '检验数量'], ['quantityQualified', '合格数量'], ['checkResult', '检验结论'], ['inspectDate', '检验时间'], ['status', '状态']] },
  feedback: { title: '原生产报工单（只读）', permission: 'mes:pro:feedback:query', idField: 'recordId', load: getFeedback, fields: [['recordId', '单据 ID'], ['workorderCode', '工单编号'], ['taskCode', '任务编号'], ['itemCode', '产品编码'], ['itemName', '产品名称'], ['quantityFeedback', '原报工数量'], ['nickName', '报工人'], ['feedbackTime', '报工时间'], ['status', '状态']] }
}
export default {
  name: 'TraceDocumentDialog',
  data: () => ({ visible: false, title: '', fields: [], record: null, error: '', loading: false, sequence: 0 }),
  methods: {
    async open(kind, id) {
      const definition = definitions[kind]
      if (!definition || !id || !hasTracePermission(this.$store.getters.permissions || [], definition.permission)) return
      const sequence = ++this.sequence
      Object.assign(this, { visible: true, title: definition.title, fields: definition.fields, record: null, error: '', loading: true })
      try {
        const response = await definition.load(id)
        if (!response.data) throw Error('未返回原单据。')
        // Trace provides the exact decimal path ID. Legacy APIs may return an imprecise numeric Long.
        if (sequence === this.sequence) this.record = { ...response.data, [definition.idField]: String(id) }
      } catch (error) { if (sequence === this.sequence) this.error = traceError(error, '原单据读取失败。') }
      finally { if (sequence === this.sequence) this.loading = false }
    },
    close() { ++this.sequence; this.visible = false; this.record = null; this.loading = false; this.error = '' }
  }
}
</script>
