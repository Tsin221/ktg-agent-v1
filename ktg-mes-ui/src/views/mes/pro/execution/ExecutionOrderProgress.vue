<template>
  <section aria-label="工单真实生产进度">
    <h3>整单生产进度</h3>
    <el-descriptions v-if="detail.progress" :column="3" border size="small"><el-descriptions-item v-for="metric in metrics" :key="metric[0]" :label="metric[1]">{{ detail.progress[metric[0]] }}</el-descriptions-item></el-descriptions>
    <h3>可见任务明细</h3>
    <el-table :data="detail.tasks || []" size="small" empty-text="暂无可见任务">
      <el-table-column prop="taskCode" label="任务" min-width="120" /><el-table-column label="工序"><template slot-scope="{ row }">{{ row.processName || stepLabel(row.stepKind) }}</template></el-table-column>
      <el-table-column prop="workstationName" label="工作站" /><el-table-column prop="quantity" label="任务计划" /><el-table-column prop="quantityProduced" label="任务已处理" />
      <el-table-column label="任务状态"><template slot-scope="{ row }">{{ taskLabel(row.status) }}</template></el-table-column>
      <el-table-column prop="actualStartedAt" label="实际开始" min-width="150" /><el-table-column prop="actualEndedAt" label="实际结束" min-width="150" />
    </el-table>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="detail.progress">
      <p v-for="reason in detail.progress.completionReasons || []" :key="reason">{{ reason }}</p>
      <el-button v-hasPermi="['mes:pro:execution:complete']" type="success" :disabled="busy || !!pending || !detail.progress.completionAllowed" @click="complete">完结工单</el-button>
    </template>
    <div v-if="pending" role="status"><p>完结结果待确认，请查询原操作回执。</p><el-button :loading="busy" @click="recover">查询原回执</el-button><el-button v-if="retryable" :loading="busy" @click="retry">重试原操作</el-button></div>
    <p v-if="receipt" role="status">工单完结已提交 · {{ receipt.operationId }} · {{ receipt.committedAt }}</p>
  </section>
</template>
<script>
import { completeExecutionOrder, executionOperation } from '@/api/mes/pro/execution'
import { taskLabel, stepLabel } from './taskPresentation'
import { ExecutionCommand, pendingKey } from './reportContract'
export default {
  name: 'ExecutionOrderProgress', props: { detail: { type: Object, required: true } },
  data: () => ({ busy: false, pending: null, retryable: false, receipt: null, error: '', command: null, metrics: [['planned','计划'],['allocated','已分配'],['assembled','初始装配'],['waitingAssembly','待装配'],['waitingInspection','待检'],['pendingDisposition','待处置'],['reworkInProgress','返工中'],['waitingReinspection','待复检'],['qualified','当前合格'],['packable','待包装'],['packed','已包装'],['scrapped','报废'],['firstInspectionRejected','历史首检不合格'],['reinspectionCount','复检次数']] }),
  created() {
    try { const actor = this.$store.state.user.id; if (!actor) throw Error('请重新登录'); this.command = new ExecutionCommand(localStorage, pendingKey(actor) + '-order', { orderComplete: completeExecutionOrder }, executionOperation); this.pending = this.command.pending() }
    catch (e) { this.error = e.message || '操作恢复存储不可用' }
  },
  methods: {
    taskLabel, stepLabel,
    async complete() {
      if (this.busy || this.pending || !this.detail.progress.completionAllowed) return
      try { await this.$confirm('确认完结工单？系统将核实所有单件终态和任务结束条件。', '工单完结'); await this.run(() => this.command.begin('orderComplete', this.detail.order.workorderId, { version: this.detail.order.version })) } catch (_) { /* cancelled */ }
    },
    async recover() { await this.run(() => this.command.recover()) },
    async retry() { await this.run(() => this.command.retry()) },
    async run(action) {
      if (this.busy || !this.command) return
      this.busy = true; this.error = ''
      try { const res = await action(); this.pending = res.pending || null; this.retryable = !!res.retryable; this.error = res.message || ''; if (res.response) { this.receipt = res.response.data; this.$emit('changed') } }
      catch (e) { this.error = e.message || '结果待确认'; this.pending = this.command.pending() }
      finally { this.busy = false }
    }
  }
}
</script>
