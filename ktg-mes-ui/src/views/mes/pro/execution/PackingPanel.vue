<template>
  <el-dialog title="登记单件包装" :visible.sync="visible" width="min(700px, 94vw)" append-to-body :close-on-click-modal="false">
    <p>仅显示当前合格待包装 SN，可先包装已合格单件。包装不改变库存。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-table ref="table" v-loading="loading" :data="units" row-key="snId" empty-text="当前没有合格待包装 SN" @selection-change="selected = $event">
      <el-table-column type="selection" :selectable="row => row.unitState === 'WAIT_PACKING'" width="50" />
      <el-table-column prop="snCode" label="SN" /><el-table-column label="状态"><template slot-scope="{ row }">{{ taskLabel(row.unitState) }}</template></el-table-column>
    </el-table>
    <pagination v-show="total > 0" :total="total" :page.sync="page" :limit.sync="pageSize" @pagination="load" />
    <div slot="footer"><el-button :disabled="busy" @click="visible=false">取消</el-button><el-button type="primary" :disabled="busy || loading || !selected.length" :loading="busy" @click="submit">确认包装 {{ selected.length }} 件</el-button></div>
  </el-dialog>
</template>
<script>
import { listExecutionTaskUnits } from '@/api/mes/pro/execution'
import { taskLabel } from './taskPresentation'
export default {
  name: 'PackingPanel', props: { busy: Boolean },
  data: () => ({ visible: false, task: null, loading: false, error: '', units: [], selected: [], page: 1, pageSize: 10, total: 0, serial: 0 }),
  methods: {
    taskLabel,
    async open(task) { this.task = task; this.visible = true; this.page = 1; await this.load() },
    async load() {
      const serial = ++this.serial, id = this.task.taskId
      this.loading = true; this.error = ''; this.selected = []
      try { const res = await listExecutionTaskUnits(id, { pageNum: this.page, pageSize: this.pageSize }); if (serial === this.serial) { this.units = res.rows.filter(row => row.unitState === 'WAIT_PACKING'); this.total = res.total } }
      catch (e) { if (serial === this.serial) { this.units = []; this.total = 0; this.error = e.message || '待包装 SN 读取失败' } }
      finally { if (serial === this.serial) this.loading = false }
    },
    submit() {
      if (this.busy || this.loading || !this.task || !this.task.canPack) return
      if (!this.selected.length || this.selected.some(row => row.unitState !== 'WAIT_PACKING')) { this.error = '请选择合格待包装 SN'; return }
      this.$emit('submit', this.task.taskId, { snIds: this.selected.map(row => row.snId) })
    }
  }
}
</script>
