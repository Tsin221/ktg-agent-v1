<template>
  <el-dialog :title="'单件质量记录 · ' + (unit.snCode || '')" :visible.sync="visible" width="min(850px, 96vw)" append-to-body :close-on-click-modal="false">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div v-loading="loading">
      <p>当前状态：{{ taskLabel(unit.unitState) }}。合格结果由服务器依据指标判定；不合格先进入待处置。</p>
      <el-form v-if="editable" label-position="top">
        <div v-for="(index, n) in indices" :key="index.index_id" class="inspection-index">
          <strong>{{ index.index_name }} · {{ index.index_type }}</strong>
          <p>要求：{{ index.check_method || '见工艺要求' }}；标准 {{ index.stander_val }} {{ index.unit_of_measure }}；范围 {{ index.threshold_min }} 至 {{ index.threshold_max }}</p>
          <el-form-item v-if="index.index_type === 'QUANTITATIVE'" label="实测值"><el-input v-model="answers[n].value" :disabled="busy" placeholder="输入实际数值" /></el-form-item>
          <el-form-item v-else label="实际判定"><el-radio-group v-model="answers[n].passed" :disabled="busy"><el-radio :label="true">符合</el-radio><el-radio :label="false">不符合</el-radio></el-radio-group></el-form-item>
          <el-form-item label="不符合时填写缺陷"><el-select v-model="answers[n].defectId" clearable :disabled="busy" placeholder="选择真实缺陷"><el-option v-for="defect in defects.filter(d => !d.indexType || d.indexType === index.index_type)" :key="defect.defectId" :value="defect.defectId" :label="defect.defectName" /></el-select></el-form-item>
          <el-form-item label="不符合原因"><el-input v-model="answers[n].reason" type="textarea" maxlength="500" show-word-limit :disabled="busy" /></el-form-item>
        </div>
      </el-form>
      <h3>检验历史</h3><p v-if="!history.length">暂无已提交检验。</p>
      <div v-for="record in history" :key="record.inspectionId" class="inspection-index">
        <strong>第 {{ record.roundNo }} 轮 · {{ record.checkResult === 'ACCEPT' ? '合格' : '不合格' }}</strong>
        <p>原过程检验单 {{ record.ipqcId }} · {{ record.inspectDate }} · 产出行 {{ record.produceLineId || '无' }}</p>
        <el-button type="text" @click="$emit('ipqc', record.ipqcId)">查看原检验单</el-button>
        <p v-for="(line, n) in record.lines || []" :key="n">{{ line.indexName || line.index_name }}：{{ historicalValue(line) }}；要求 {{ line.checkMethod || '—' }}；范围 {{ line.thresholdMin }} 至 {{ line.thresholdMax }} {{ line.unitOfMeasure }}</p>
        <p v-for="(defect, n) in record.defects || []" :key="'d'+n">缺陷：{{ defect.defectName || defect.defect_name }} {{ defect.remark || defect.reason }}</p>
      </div>
      <disposition-panel v-if="unit.unitState === 'WAIT_DISPOSITION' && canDispose" :busy="busy" :history="history" @submit="dispose" />
    </div>
    <div slot="footer"><el-button :disabled="busy" @click="visible=false">关闭</el-button><el-button v-if="editable" type="primary" :disabled="busy || loading || !indices.length" @click="submit">提交本件检验</el-button></div>
  </el-dialog>
</template>
<script>
import { inspectionRequirements, inspectionHistory } from '@/api/mes/pro/execution'
import { taskLabel } from './taskPresentation'
import DispositionPanel from './DispositionPanel.vue'
export default {
  components: { DispositionPanel }, props: { busy: Boolean },
  data: () => ({ visible: false, loading: false, error: '', unit: {}, task: {}, indices: [], answers: [], defects: [], history: [], serial: 0 }),
  computed: {
    permissions() { return this.$store.getters.permissions || [] },
    canDispose() { return this.permissions.includes('*:*:*') || this.permissions.includes('mes:pro:execution:disposition') },
    editable() { return this.task.status === 'STARTED' && this.task.canInspect && ['WAIT_INSPECTION', 'WAIT_REINSPECTION'].includes(this.unit.unitState) }
  },
  methods: {
    taskLabel,
    historicalValue(line) {
      try { const fact = JSON.parse(line.remark || '{}'); return fact.value != null ? fact.value : typeof fact.passed === 'boolean' ? (fact.passed ? '符合' : '不符合') : '未记录' } catch (_) { return line.remark || '未记录' }
    },
    async open(task, unit) {
      const serial = ++this.serial
      Object.assign(this, { visible: true, loading: true, error: '', task, unit, indices: [], answers: [], defects: [], history: [] })
      try {
        const [requirements, history] = await Promise.all([task.taskKind === 'REWORK' ? Promise.resolve({ data: { template: { indices: [] }, defects: [] } }) : inspectionRequirements(task.taskId), inspectionHistory(unit.snId)])
        if (serial !== this.serial) return
        this.indices = requirements.data.template.indices; this.defects = requirements.data.defects; this.history = history.data
        this.answers = this.indices.map(index => ({ indexId: index.index_id, value: '', passed: null, defectId: null, reason: '' }))
      } catch (e) { if (serial === this.serial) this.error = e.message || '质量记录读取失败' }
      finally { if (serial === this.serial) this.loading = false }
    },
    submit() {
      if (!this.editable || this.busy || this.loading) return
      if (this.answers.some((answer, n) => this.indices[n].index_type === 'QUANTITATIVE' ? answer.value.trim() === '' || !Number.isFinite(Number(answer.value)) : typeof answer.passed !== 'boolean')) { this.error = '请填写每个指标的实际检测结果。'; return }
      this.$emit('inspect', this.unit.snId, { taskId: this.task.taskId, indices: this.answers.map((answer, n) => ({ ...answer, value: this.indices[n].index_type === 'QUANTITATIVE' ? answer.value : null })) })
    },
    dispose(payload) { this.$emit('dispose', this.unit.snId, payload) }
  }
}
</script>
<style scoped>.inspection-index { border-top: 1px solid #ebeef5; padding: 16px 0; }.inspection-index p { color: #606266; overflow-wrap: anywhere; }</style>
