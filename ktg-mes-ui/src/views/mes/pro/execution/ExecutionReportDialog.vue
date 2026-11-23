<template>
  <el-dialog title="批量装配报工" :visible.sync="visible" width="min(960px, 94vw)" top="6vh" custom-class="execution-report-modal" append-to-body :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @closed="clear">
    <template v-if="task">
      <p>任务 {{ task.taskCode }} · 工单 {{ task.workorderCode }}。提交后一次性扣减真实线边库存，并将本次 SN 转为待检；本步骤不计合格成品。</p>
      <h3>1. 选择待装配 SN</h3>
      <el-form :inline="true" size="small" @submit.native.prevent="search"><el-form-item label="SN"><el-input v-model="query.snCode" clearable placeholder="SN 编码" @keyup.enter.native="search" /></el-form-item><el-form-item><el-button @click="search">查询</el-button></el-form-item><el-form-item><el-button :disabled="busy || !units.length" @click="selectPage">选择本页</el-button></el-form-item></el-form>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <el-table v-loading="loading" :data="units" row-key="snId" max-height="260" empty-text="没有尚待装配的 SN">
        <el-table-column label="选择" width="70"><template slot-scope="{ row }"><el-checkbox :value="isSelected(row)" :disabled="busy" :aria-label="'选择 SN ' + row.snCode" @change="choose(row, $event)" /></template></el-table-column>
        <el-table-column prop="snCode" label="SN" min-width="150" />
        <el-table-column label="状态"><template slot-scope="{ row }">{{ taskLabel(row.unitState) }}</template></el-table-column>
      </el-table>
      <pagination v-show="total > 0" :total="total" :page.sync="query.pageNum" :limit.sync="query.pageSize" @pagination="load" />
      <div class="selection"><strong>已选择 {{ selected.length }} 件</strong><el-tag v-for="sn in selected" :key="sn.snId" size="small" :closable="!busy" @close="choose(sn,false)">{{ sn.snCode }}</el-tag></div>
      <material-groups :key="task.taskId" :task-id="task.taskId" :selected="selected" :groups="groups" :disabled="busy" @change="groups = $event" />
      <h3>3. 核对提交</h3>
      <el-alert v-if="commandError" :title="commandError" type="error" :closable="false" show-icon />
      <el-alert v-if="validation.error" :title="validation.error" type="info" :closable="false" />
      <el-table v-else :data="validation.totals"><el-table-column prop="stock.itemName" label="物料" /><el-table-column prop="stock.sourceBatchCode" label="批次" /><el-table-column prop="stock.materialStockId" label="库存 ID" /><el-table-column label="实际扣减"><template slot-scope="{ row }">{{ row.quantity }} {{ row.stock.unitOfMeasure }}</template></el-table-column></el-table>
      <p class="hint">服务端以当前真实库存、SN 和固定用料要求复核；余额不足、重复或状态冲突时整批不提交。</p>
    </template>
    <div slot="footer"><el-button :disabled="busy" @click="visible=false">取消</el-button><el-button type="primary" :loading="busy" :disabled="busy || !!validation.error" @click="submit">确认报工 {{ selected.length }} 件</el-button></div>
  </el-dialog>
</template>
<script>
import { listExecutionTaskUnits } from '@/api/mes/pro/execution'
import { validateReport } from './reportContract'
import { taskLabel } from './taskPresentation'
import MaterialGroups from './MaterialGroups.vue'
export default {
  name: 'ExecutionReportDialog', components: { MaterialGroups },
  props: { busy: Boolean, commandError: String },
  data: () => ({ visible: false, task: null, units: [], selected: [], groups: [], query: { pageNum: 1, pageSize: 10, snCode: '', state: 'WAIT_ASSEMBLY' }, total: 0, loading: false, error: '', serial: 0 }),
  computed: { validation() { return validateReport(this.selected, this.groups) } },
  methods: {
    taskLabel,
    open(task) { this.clear(); this.task = task; this.visible = true; this.load() },
    clear() { ++this.serial; this.task = null; this.units = []; this.selected = []; this.groups = []; this.query = { pageNum: 1, pageSize: 10, snCode: '', state: 'WAIT_ASSEMBLY' }; this.total = 0; this.loading = false; this.error = '' },
    search() { this.query.pageNum = 1; this.load() },
    async load() {
      if (!this.task) return
      const serial = ++this.serial
      this.loading = true; this.error = ''
      try {
        const res = await listExecutionTaskUnits(this.task.taskId, this.query)
        if (serial === this.serial) { this.units = res.rows.filter(row => row.unitState === 'WAIT_ASSEMBLY'); this.total = res.total }
      } catch (error) { if (serial === this.serial) { this.error = error.message || '待装配 SN 读取失败'; this.units = []; this.total = 0 } }
      finally { if (serial === this.serial) this.loading = false }
    },
    isSelected(row) { return this.selected.some(sn => sn.snId === row.snId) },
    choose(row, checked) {
      if (checked && !this.isSelected(row)) this.selected = [...this.selected, row]
      if (!checked) { this.selected = this.selected.filter(sn => sn.snId !== row.snId); this.groups = this.groups.map(g => ({ ...g, snIds: g.snIds.filter(id => id !== row.snId) })) }
    },
    selectPage() { this.units.forEach(row => this.choose(row, true)) },
    submit() { if (!this.busy && !this.validation.error) this.$emit('submit', this.task.taskId, this.validation.payload) }
  }
}
</script>
<style scoped>
.selection { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0; }
.hint { color: #606266; }
</style>
<style>
.execution-report-modal .el-dialog__body { max-height: 68vh; overflow-y: auto; }
</style>
