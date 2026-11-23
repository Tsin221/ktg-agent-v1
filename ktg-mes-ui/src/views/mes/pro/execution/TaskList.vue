<template>
  <section aria-label="生产任务列表">
    <el-form :inline="true" size="small" @submit.native.prevent="search">
      <el-form-item label="工单 ID"><el-input v-model="filters.workorderId" clearable placeholder="工单 ID" @keyup.enter.native="search" /></el-form-item>
      <el-form-item label="状态"><el-select v-model="filters.status" clearable placeholder="所有状态"><el-option v-for="state in states" :key="state" :value="state" :label="taskLabel(state)" /></el-select></el-form-item>
      <el-form-item><el-button type="primary" icon="el-icon-search" @click="search">查询</el-button><el-button @click="reset">重置</el-button></el-form-item>
    </el-form>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-table v-loading="loading" :data="rows" row-key="taskId" highlight-current-row :current-row-key="selectedId" empty-text="暂无可见任务，请确认任务已下达及分派。">
      <el-table-column prop="taskCode" label="任务编号" min-width="145" show-overflow-tooltip />
      <el-table-column prop="workorderCode" label="工单" min-width="130" show-overflow-tooltip />
      <el-table-column label="工序 / 工位" min-width="145"><template slot-scope="{ row }">{{ row.processName || stepLabel(row.stepKind) }}<span class="subline">{{ row.workstationName || '—' }}</span></template></el-table-column>
      <el-table-column label="执行状态" width="110"><template slot-scope="{ row }"><el-tag size="small" :type="row.status === 'WAITING' ? 'info' : row.status === 'FINISHED' ? 'success' : ''">{{ taskLabel(row.status) }}</el-tag></template></el-table-column>
      <el-table-column label="可执行" prop="availableUnits" width="76" align="center" />
      <el-table-column label="操作" width="76" align="center"><template slot-scope="{ row }"><el-button type="text" @click="$emit('select', row.taskId)">查看</el-button></template></el-table-column>
    </el-table>
    <pagination v-show="total > 0" :total="total" :page.sync="filters.pageNum" :limit.sync="filters.pageSize" @pagination="load" />
  </section>
</template>
<script>
import { listExecutionTasks } from '@/api/mes/pro/execution'
import { taskLabel, stepLabel } from './taskPresentation'
export default {
  name: 'ExecutionTaskList',
  props: { selectedId: Number },
  data: () => ({ filters: { pageNum: 1, pageSize: 10, workorderId: '', status: '' }, rows: [], total: 0, loading: false, error: '', states: ['WAITING', 'READY', 'STARTED', 'FINISHED'], sequence: 0 }),
  created() { this.load() },
  methods: {
    taskLabel, stepLabel,
    reset() { this.filters = { pageNum: 1, pageSize: 10, workorderId: '', status: '' }; this.load() },
    search() { this.filters.pageNum = 1; this.load() },
    async load() {
      const sequence = ++this.sequence
      this.loading = true; this.error = ''
      try {
        if (this.filters.workorderId && !/^\d+$/.test(this.filters.workorderId)) throw Error('工单 ID 应为正整数。')
        const response = await listExecutionTasks(this.filters)
        if (sequence === this.sequence) { this.rows = response.rows; this.total = response.total }
      } catch (error) { if (sequence === this.sequence) { this.error = error.message || '任务列表读取失败'; this.rows = []; this.total = 0 } }
      finally { if (sequence === this.sequence) this.loading = false }
    }
  }
}
</script>
<style scoped>
.subline { display: block; color: #606266; font-size: 12px; }
</style>
