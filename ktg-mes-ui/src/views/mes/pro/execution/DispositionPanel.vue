<template><section aria-label="质量处置"><h3>班组长处置</h3><p>针对最新不合格检验决定返工或报废。已提交记录不可覆盖。</p><el-form label-position="top"><el-form-item label="处置决定"><el-radio-group v-model="decision" :disabled="busy"><el-radio label="REWORK">返工后复检</el-radio><el-radio label="SCRAP">报废</el-radio></el-radio-group></el-form-item><el-form-item label="处置原因" required><el-input v-model="reason" type="textarea" maxlength="500" show-word-limit :disabled="busy" /></el-form-item></el-form><el-button type="warning" :disabled="busy || !reason.trim() || !latest" @click="submit">确认处置</el-button></section></template>
<script>
export default {
  props: { busy: Boolean, history: { type: Array, default: () => [] } }, data: () => ({ decision: 'REWORK', reason: '' }),
  computed: { latest() { return this.history.slice().sort((a, b) => b.roundNo - a.roundNo)[0] } },
  methods: { async submit() { if (!this.latest || !this.reason.trim() || this.busy) return; try { await this.$confirm(this.decision === 'SCRAP' ? '确认报废本件？报废后不能继续生产。' : '确认创建本件返工任务？', '质量处置确认') } catch (_) { return }; this.$emit('submit', { inspectionId: this.latest.inspectionId, decision: this.decision, reason: this.reason.trim() }) } }
}
</script>
