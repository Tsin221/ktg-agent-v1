<template>
  <div class="app-container execution-workbench">
    <div class="workbench-heading"><div><h1>车间工作台</h1><p>查看已授权的生产任务，按实际用料与单件 SN 完成生产和质量处置。</p></div><el-button icon="el-icon-refresh" :disabled="busy" @click="refresh">刷新工作台</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-if="pending" class="pending" role="status" aria-live="polite">
      <el-alert title="操作结果待确认：请先查询原操作回执，不要重复发起新操作。" type="warning" :closable="false" show-icon />
      <p>任务 {{ pending.taskId }} · {{ actionLabel(pending.action) }} · {{ pending.payload.operationId }}</p>
      <el-button type="primary" :loading="busy" @click="recover">查询原操作结果</el-button><el-button v-if="retryable" type="warning" :loading="busy" @click="retry">重试原操作</el-button>
    </div>
    <div v-if="receipt" class="receipt" role="status" aria-live="polite">
      <el-alert :title="'已确认提交：' + (receipt.actionType || '执行操作')" type="success" :closable="false" show-icon />
      <p>操作 ID：{{ receipt.operationId }} · 提交时间：{{ receipt.committedAt || '—' }}</p>
      <p v-if="receiptRef.feedbackId">报工单 ID：{{ receiptRef.feedbackId }} · 耗料单 ID：{{ receiptRef.consumeId }} · 本次 {{ receiptRef.snCount }} 件 · 当前已装配 {{ receiptRef.assembled }} 件</p>
    </div>
    <template v-if="permitted">
      <task-list ref="list" :selected-id="taskId" @select="taskId = $event" />
      <task-detail ref="detail" :task-id="taskId" :busy="busy || !!pending" @assign="openAssignment" @start="confirmStart" @report="openReport" @complete="confirmComplete" @quality="openQuality" @pack="openPacking" />
      <packing-panel ref="packing" :busy="busy || !!pending" @submit="pack" />
      <inspection-panel ref="quality" :busy="busy || !!pending" @inspect="inspect" @dispose="dispose" @ipqc="viewIpqc" />
      <el-dialog title="原过程检验单（只读）" :visible.sync="ipqcVisible" width="min(700px, 94vw)" append-to-body><el-alert v-if="ipqcError" :title="ipqcError" type="error" :closable="false" /><el-descriptions v-if="ipqc" :column="2" border><el-descriptions-item v-for="field in ipqcFields" :key="field[0]" :label="field[1]">{{ ipqc[field[0]] }}</el-descriptions-item></el-descriptions></el-dialog>
      <execution-report-dialog ref="report" :busy="busy || !!pending" :command-error="reportError" @submit="report" />
      <el-dialog title="分派任务" :visible.sync="assignmentVisible" append-to-body width="min(540px, 94vw)" :close-on-click-modal="false">
        <p v-if="assignmentTask">任务 {{ assignmentTask.taskCode }} · 工位 {{ assignmentTask.workstationName }}</p>
        <el-alert v-if="assignmentError" :title="assignmentError" type="error" :closable="false" />
        <el-form v-loading="assignmentLoading" label-width="90px"><el-form-item label="执行人员" required><el-select v-model="assignedUserId" filterable placeholder="选择已授权人员"><el-option v-for="person in assignmentOptions" :key="person.userId" :value="person.userId" :label="(person.nickName || person.userName) + '（' + person.userName + '）'" /></el-select></el-form-item></el-form>
        <div slot="footer"><el-button :disabled="busy" @click="assignmentVisible=false">取消</el-button><el-button type="primary" :loading="busy" :disabled="busy || !!pending || assignmentLoading || !assignedUserId" @click="assign">确认分派</el-button></div>
      </el-dialog>
    </template>
    <el-alert v-else title="当前账号没有车间工作台查询权限。" type="warning" :closable="false" />
  </div>
</template>
<script>
import PackingPanel from './PackingPanel.vue'
import InspectionPanel from './InspectionPanel.vue'
import { getIpqc } from '@/api/mes/qc/ipqc'
import { packExecutionTask, inspectExecutionUnit, disposeExecutionUnit, assignExecutionTask, startExecutionTask, reportExecutionTask, completeExecutionTask, executionOperation, executionAssignmentOptions } from '@/api/mes/pro/execution'
import { ExecutionCommand, pendingKey } from './reportContract'
import TaskList from './TaskList.vue'
import TaskDetail from './TaskDetail.vue'
import ExecutionReportDialog from './ExecutionReportDialog.vue'
export default {
  name: 'ExecutionWorkbench', components: { PackingPanel, InspectionPanel, TaskList, TaskDetail, ExecutionReportDialog },
  data: () => ({ ipqcVisible: false, ipqc: null, ipqcError: '', ipqcFields: [['ipqcId', '单据ID'], ['ipqcCode', '单据编号'], ['ipqcName', '单据名称'], ['ipqcType', '类型'], ['quantityCheck', '检验数量'], ['quantityQualified', '合格数量'], ['checkResult', '结论'], ['inspectDate', '检验日期']], taskId: null, command: null, busy: false, error: '', reportError: '', pending: null, retryable: false, receipt: null, assignmentVisible: false, assignmentTask: null, assignmentOptions: [], assignedUserId: null, assignmentLoading: false, assignmentError: '' }),
  computed: {
    permitted() { const perms = this.$store.getters.permissions || []; return perms.includes('*:*:*') || perms.includes('mes:pro:execution:query') },
    receiptRef() { try { return typeof this.receipt.resultRef === 'string' ? JSON.parse(this.receipt.resultRef) : (this.receipt.resultRef || {}) } catch (_) { return {} } }
  },
  created() {
    if (!this.permitted) return
    try {
      const actor = this.$store.state.user.id
      if (!actor) throw Error('未确认当前登录用户，请重新登录。')
      this.command = new ExecutionCommand(localStorage, pendingKey(actor), { assign: assignExecutionTask, start: startExecutionTask, report: reportExecutionTask, complete: completeExecutionTask, inspect: inspectExecutionUnit, dispose: disposeExecutionUnit, pack: packExecutionTask }, executionOperation)
      this.pending = this.command.pending()
      if (this.pending) this.taskId = ['inspect', 'dispose'].includes(this.pending.action) ? (this.pending.payload.taskId || null) : this.pending.taskId
    } catch (e) { this.error = e.message || '操作恢复存储不可用' }
  },
  activated() { this.refresh() },
  methods: {
    actionLabel(action) { return { assign: '分派', start: '开工', report: '报工', complete: '完成任务', inspect: '单件检验', dispose: '质量处置', pack: '登记包装' }[action] || action },
    refresh() { if (this.$refs.list) this.$refs.list.load(); if (this.$refs.detail) this.$refs.detail.reload() },
    openPacking(task) { this.$refs.packing.open(task) },
    async pack(id, payload) { await this.run(() => this.command.begin('pack', id, payload)) },
    openQuality(task, unit) { this.$refs.quality.open(task, unit) },
    async inspect(id, payload) { await this.run(() => this.command.begin('inspect', id, payload)) },
    async dispose(id, payload) { await this.run(() => this.command.begin('dispose', id, payload)) },
    async viewIpqc(id) { this.ipqc = null; this.ipqcError = ''; this.ipqcVisible = true; try { this.ipqc = (await getIpqc(id)).data } catch (e) { this.ipqcError = e.message || '原检验单读取失败' } },
    openReport(task) { this.reportError = ''; this.$refs.report.open(task) },
    async openAssignment(task) {
      this.assignmentTask = task; this.assignmentVisible = true; this.assignmentOptions = []; this.assignedUserId = task.assignedUserId || null; this.assignmentError = ''; this.assignmentLoading = true
      try { const res = await executionAssignmentOptions(task.taskId); this.assignmentOptions = res.data; if (!this.assignmentOptions.some(p => p.userId === this.assignedUserId)) this.assignedUserId = null }
      catch (e) { this.assignmentError = e.message || '合法执行人员读取失败' }
      finally { this.assignmentLoading = false }
    },
    async assign() { if (this.assignedUserId) await this.run(() => this.command.begin('assign', this.assignmentTask.taskId, { assignedUserId: this.assignedUserId })) },
    async confirmStart(task) { try { await this.$confirm('确认开工？开始时间将由服务器记录。', '开工确认'); await this.run(() => this.command.begin('start', task.taskId, {})) } catch (_) { /* cancelled */ } },
    async confirmComplete(task) { try { await this.$confirm('确认完成当前任务？结束时间将由服务器记录。', '任务完结确认'); await this.run(() => this.command.begin('complete', task.taskId, {})) } catch (_) { /* cancelled */ } },
    async report(taskId, payload) { await this.run(() => this.command.begin('report', taskId, payload)) },
    async recover() { await this.run(() => this.command.recover()) },
    async retry() { await this.run(() => this.command.retry()) },
    async run(action) {
      if (this.busy || !this.command) return
      this.busy = true; this.error = ''; this.reportError = ''; this.receipt = null
      if (this.assignmentVisible) this.assignmentError = ''
      try {
        const result = await action()
        this.pending = result.pending || null; this.retryable = !!result.retryable
        if (result.response) { this.receipt = result.response.data; this.refresh() }
        this.error = result.message || ''
        if (!result.response && !result.pending) {
          if (this.$refs.report && this.$refs.report.visible) this.reportError = this.error
          if (this.$refs.quality && this.$refs.quality.visible) this.$refs.quality.error = this.error
          if (this.assignmentVisible) this.assignmentError = this.error
          if (this.$refs.packing && this.$refs.packing.visible) this.$refs.packing.error = this.error
        }
        // Retain the form on a definite validation error; close only when result is known committed or needs receipt recovery.
        if (result.response || result.pending) { this.assignmentVisible = false; if (this.$refs.packing) this.$refs.packing.visible = false; if (this.$refs.quality) this.$refs.quality.visible = false; if (this.$refs.report) this.$refs.report.visible = false }
        if (!result.response && !result.pending) this.refresh()
      } catch (e) { this.error = e.message || '操作无法处理，未确认结果前请勿重试新操作'; try { this.pending = this.command.pending() } catch (_) { /* storage unavailable */ } }
      finally { this.busy = false }
    }
  }
}
</script>
<style scoped>
.workbench-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.workbench-heading h1 { font-size: 23px; margin: 0 0 8px; }
.workbench-heading p, .pending p, .receipt p { color: #606266; overflow-wrap: anywhere; }
.pending, .receipt { padding: 12px 0; margin-bottom: 12px; }
@media (max-width: 640px) { .workbench-heading { flex-direction: column; } }
</style>
