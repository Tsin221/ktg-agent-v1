<template>
  <section class="task-detail" aria-label="任务详情">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <div v-loading="loading">
      <div v-if="!task" class="empty-detail" role="status">请选择任务查看工序、作业指导书和执行情况。</div>
      <template v-else>
        <div class="detail-heading"><div><h2>{{ task.taskCode }} · {{ task.processName || stepLabel(task.stepKind) }}</h2><span>{{ task.workorderCode }} / {{ task.itemName }} / {{ task.workstationName }}</span></div><el-tag>{{ taskLabel(task.status) }}</el-tag></div>
        <el-row :gutter="16" class="summary-row">
          <el-col :xs="12" :sm="6"><span class="metric-label">计划</span><strong>{{ task.quantity }}</strong></el-col>
          <el-col :xs="12" :sm="6"><span class="metric-label">已处理</span><strong>{{ task.quantityProduced }}</strong></el-col>
          <el-col :xs="12" :sm="6"><span class="metric-label">当前可操作</span><strong>{{ task.availableUnits }}</strong></el-col>
          <el-col :xs="12" :sm="6"><span class="metric-label">实际用时</span><strong>{{ duration }}</strong></el-col>
        </el-row>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="指派人员">{{ task.assignedUserName || task.assignedUserId || '未指派' }}</el-descriptions-item>
          <el-descriptions-item label="工位">{{ task.workstationName || '—' }}</el-descriptions-item>
          <el-descriptions-item label="实际开始">{{ task.actualStartedAt || '未开工' }}</el-descriptions-item>
          <el-descriptions-item label="实际结束">{{ task.actualEndedAt || '—' }}</el-descriptions-item>
        </el-descriptions>
        <div class="actions">
          <el-button v-if="actions.includes('assign')" :disabled="busy" @click="$emit('assign', task)">{{ task.status === 'STARTED' ? '交接人员' : '分派人员' }}</el-button>
          <el-button v-if="actions.includes('start')" type="primary" :disabled="busy" @click="$emit('start', task)">开工</el-button>
          <el-button v-if="actions.includes('report')" type="primary" :disabled="busy" @click="$emit('report', task)">批量报工</el-button>
          <el-button v-if="actions.includes('complete')" type="success" :disabled="busy" @click="$emit('complete', task)">{{ task.taskKind === 'REWORK' ? '完成返工任务' : task.stepKind === 'INSPECTION' ? '完成检验任务' : task.stepKind === 'PACKING' ? '完成包装任务' : '完成装配任务' }}</el-button>
          <el-button v-if="actions.includes('pack')" type="primary" :disabled="busy" @click="$emit('pack', task)">登记包装</el-button>
          <el-button icon="el-icon-refresh" :disabled="busy" @click="reload">刷新详情</el-button>
        </div>
        <el-alert v-if="task.status === 'WAITING'" title="等待前序流转，当前不能开工。" type="info" :closable="false" />
        <rework-panel v-if="task.taskKind === 'REWORK'" />
        <execution-order-progress v-if="orderDetail" :detail="orderDetail" @changed="reload" />
        <h3>作业指导与 SOP</h3>
        <p v-if="!(task.processContents || []).length && !(task.productSops || []).length" class="muted">暂无既有指导内容，请向工艺管理员确认。</p>
        <div v-for="content in task.processContents || []" :key="'c'+content.contentId" class="instruction"><strong>工序 {{ content.orderNum }}</strong><div class="guidance">{{ content.contentText }}</div><p v-if="content.device || content.material">设备：{{ content.device || '—' }}；物料：{{ content.material || '—' }}</p><a v-if="safeDocumentUrl(content.docUrl)" :href="safeDocumentUrl(content.docUrl)" target="_blank" rel="noopener noreferrer">查看指导附件</a></div>
        <div v-for="sop in task.productSops || []" :key="'s'+sop.sopId" class="instruction"><strong>{{ sop.sopTitle || '产品 SOP' }}</strong><div class="guidance">{{ sop.sopDescription }}</div><a v-if="safeDocumentUrl(sop.sopUrl)" :href="safeDocumentUrl(sop.sopUrl)" target="_blank" rel="noopener noreferrer">查看 SOP 附件</a></div>
        <h3>当前任务 SN</h3>
        <el-form :inline="true" size="small" @submit.native.prevent="searchUnits"><el-form-item label="SN"><el-input v-model="unitQuery.snCode" clearable placeholder="SN 编码" @keyup.enter.native="searchUnits" /></el-form-item><el-form-item><el-button @click="searchUnits">查询</el-button></el-form-item></el-form>
        <el-alert v-if="unitError" :title="unitError" type="error" :closable="false" />
        <el-table v-loading="unitLoading" :data="units" :row-key="row => row.snId" empty-text="当前没有符合条件的单件"><el-table-column prop="snCode" label="SN" min-width="140" /><el-table-column label="状态"><template slot-scope="{ row }">{{ taskLabel(row.unitState) }}</template></el-table-column><el-table-column v-if="task.stepKind === 'INSPECTION' || task.taskKind === 'REWORK'" label="质量记录" width="140"><template slot-scope="{ row }"><el-button type="text" :disabled="busy" @click="$emit('quality', task, row)">检验 / 历史 / 处置</el-button></template></el-table-column></el-table>
        <pagination v-show="unitTotal > 0" :total="unitTotal" :page.sync="unitQuery.pageNum" :limit.sync="unitQuery.pageSize" @pagination="loadUnits" />
      </template>
    </div>
  </section>
</template>
<script>
import ExecutionOrderProgress from './ExecutionOrderProgress.vue'
import ReworkPanel from './ReworkPanel.vue'
import { executionOrder, getExecutionTask, listExecutionTaskUnits } from '@/api/mes/pro/execution'
import { taskLabel, stepLabel, elapsedSeconds, formatDuration, availableActions, safeDocumentUrl } from './taskPresentation'
export default {
  name: 'ExecutionTaskDetail', components: { ReworkPanel, ExecutionOrderProgress },
  props: { taskId: Number, busy: Boolean },
  data: () => ({ orderDetail: null, task: null, loading: false, error: '', readAt: 0, now: 0, timer: null, units: [], unitQuery: { pageNum: 1, pageSize: 10, snCode: '' }, unitTotal: 0, unitLoading: false, unitError: '', serial: 0, unitSerial: 0 }),
  computed: {
    actions() { return availableActions(this.task) },
    duration() { return formatDuration(elapsedSeconds(this.task || {}, this.now - this.readAt)) }
  },
  watch: { taskId: { immediate: true, handler() { this.task = null; this.orderDetail = null; this.units = []; this.unitQuery = { pageNum: 1, pageSize: 10, snCode: '' }; this.reload() } } },
  mounted() { this.timer = setInterval(() => { this.now = Date.now() }, 1000) },
  beforeDestroy() { clearInterval(this.timer) },
  methods: {
    taskLabel, stepLabel, safeDocumentUrl,
    async reload() {
      const serial = ++this.serial
      if (!this.taskId) return
      this.loading = true; this.error = ''
      try {
        const response = await getExecutionTask(this.taskId)
        if (serial !== this.serial) return
        this.task = response.data; this.readAt = this.now = Date.now()
        await this.loadUnits()
        const order = await executionOrder(this.task.workorderId)
        if (serial === this.serial) this.orderDetail = order.data
      } catch (error) { if (serial === this.serial) { this.error = error.message || '详情读取失败'; this.task = null; this.orderDetail = null; this.units = []; this.unitTotal = 0 } }
      finally { if (serial === this.serial) this.loading = false }
    },
    searchUnits() { this.unitQuery.pageNum = 1; this.loadUnits() },
    async loadUnits() {
      const serial = ++this.unitSerial, id = this.taskId
      if (!id) return
      this.unitLoading = true; this.unitError = ''
      try { const res = await listExecutionTaskUnits(id, { ...this.unitQuery, includeHistory: this.task && this.task.stepKind === 'INSPECTION' && this.task.taskKind !== 'REWORK' }); if (serial === this.unitSerial && id === this.taskId) { this.units = res.rows; this.unitTotal = res.total } }
      catch (e) { if (serial === this.unitSerial && id === this.taskId) { this.units = []; this.unitTotal = 0; this.unitError = e.message || 'SN 读取失败' } }
      finally { if (serial === this.unitSerial) this.unitLoading = false }
    }
  }
}
</script>
<style scoped>
.task-detail { border-top: 1px solid #ebeef5; margin-top: 18px; padding-top: 16px; }
.empty-detail, .muted { color: #606266; }
.empty-detail { padding: 24px 0; text-align: center; }
.detail-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.detail-heading h2 { font-size: 18px; margin: 0 0 8px; }
.detail-heading span { color: #606266; }
.summary-row { padding: 16px 0; }
.summary-row strong, .metric-label { display: block; margin: 4px 0; }
.summary-row strong { font-size: 20px; }
.metric-label { color: #606266; font-size: 13px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.actions .el-button { margin: 0; }
.instruction { border-left: 3px solid #dcdfe6; padding: 8px 12px; margin: 10px 0; }
.instruction a { color: #409eff; }
.guidance { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 6px; }
</style>
