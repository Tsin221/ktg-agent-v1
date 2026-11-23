<template>
  <section aria-label="成品 SN 追溯查询" :aria-busy="loading">
    <el-form :inline="true" size="small" @submit.native.prevent="search()">
      <el-form-item label="成品 SN" required>
        <el-input v-model="snCode" maxlength="64" aria-label="成品 SN" placeholder="输入唯一成品 SN" clearable />
      </el-form-item>
      <el-form-item>
        <el-button native-type="submit" type="primary" icon="el-icon-search" :loading="loading">查询</el-button><el-button @click="reset">重置</el-button>
      </el-form-item>
    </el-form>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon role="alert" />
    <p v-else-if="!result && !loading" class="hint" role="status">输入完整 SN，查询当前可见的生产、用料、检验和处置记录。</p>
    <p v-if="loading" class="hint" role="status" aria-live="polite">正在读取追溯记录……</p>
    <div v-if="result" class="result">
      <el-descriptions title="成品身份" :column="2" border>
        <el-descriptions-item label="成品 SN"><span class="raw-code">{{ result.snCode }}</span></el-descriptions-item>
        <el-descriptions-item label="SN ID">{{ result.snId }}</el-descriptions-item>
        <el-descriptions-item label="产品编码">{{ result.itemCode || '—' }}</el-descriptions-item>
        <el-descriptions-item label="产品名称">{{ result.itemName || '—' }}</el-descriptions-item>
        <el-descriptions-item label="成品批次">{{ result.batchCode || '—' }}</el-descriptions-item>
        <el-descriptions-item v-if="result.traceStatus === 'TRACKED'" label="当前单件状态">{{ taskLabel(result.unitState) }}</el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="result.traceStatus === 'LEGACY_NO_RECORD'" class="legacy" title="该历史 SN 尚无新生产执行闭环采集记录。不能据此判断历史业务是否发生。" type="warning" :closable="false" show-icon />
      <unit-facts v-else-if="result.traceStatus === 'TRACKED'" :result="result" @trace-material="(itemId, batchCode) => $emit('trace-material', itemId, batchCode)" @document="openDocument" />
      <el-alert v-else title="未识别的追溯记录状态，请联系管理员核对接口。" type="warning" :closable="false" show-icon />
    </div>
    <trace-document-dialog ref="document" />
  </section>
</template>

<script>
import { traceExecutionUnit } from '@/api/mes/pro/execution'
import { taskLabel } from '../taskPresentation'
import { traceError } from './tracePresentation'
import UnitFacts from './UnitFacts.vue'
import TraceDocumentDialog from './TraceDocumentDialog.vue'

export default {
  name: 'UnitTracePanel',
  components: { UnitFacts, TraceDocumentDialog },
  data: () => ({ snCode: '', result: null, loading: false, error: '', sequence: 0 }),
  methods: {
    taskLabel,
    reset() {
      ++this.sequence
      Object.assign(this, { snCode: '', result: null, loading: false, error: '' })
      if (this.$refs && this.$refs.document) this.$refs.document.close()
    },
    async search(value = this.snCode) {
      const sequence = ++this.sequence
      this.result = null
      this.error = ''
      if (this.$refs && this.$refs.document) this.$refs.document.close()
      // Preserve identity bytes; trimming is only for validation, not lookup.
      this.snCode = String(value == null ? '' : value)
      this.loading = false
      if (!this.snCode.trim() || this.snCode.length > 64) { this.error = '请输入完整的成品 SN（1–64 个字符）。'; return }
      this.loading = true
      try {
        const response = await traceExecutionUnit({ snCode: this.snCode })
        if (!response.data || !['TRACKED', 'LEGACY_NO_RECORD'].includes(response.data.traceStatus)) throw Error('追溯接口未返回有效记录。')
        if (sequence === this.sequence) this.result = response.data
      } catch (error) {
        if (sequence === this.sequence) this.error = traceError(error, '成品 SN 追溯读取失败。')
      } finally { if (sequence === this.sequence) this.loading = false }
    },
    openDocument(kind, id) { this.$refs.document.open(kind, id) }
  }
}
</script>

<style scoped>
.hint { color: #606266; line-height: 1.6; }
.result, .legacy { margin-top: 16px; }
.raw-code { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
