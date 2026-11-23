<template>
  <section aria-label="实际投入分组">
    <h3>2. 选择实际库存与分组</h3>
    <p class="hint">只显示本工单已经领入线边库的可用原料。每组填写实际使用的 SN 和每件用量；同批次库存可有多行，按库存 ID 区分。</p>
    <el-form :inline="true" size="small" @submit.native.prevent="search"><el-form-item label="批次"><el-input v-model="query.batchCode" clearable placeholder="原料批次" @keyup.enter.native="search" /></el-form-item><el-form-item><el-button @click="search">查询库存</el-button></el-form-item></el-form>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table v-loading="loading" :data="stocks" empty-text="没有可用库存，请先按现有生产领料流程备料。" max-height="260">
      <el-table-column prop="itemCode" label="物料编码" min-width="100" />
      <el-table-column prop="itemName" label="物料" min-width="100" />
      <el-table-column prop="sourceBatchCode" label="真实批次" min-width="100" />
      <el-table-column prop="materialStockId" label="库存 ID" width="110" />
      <el-table-column label="余额" min-width="100"><template slot-scope="{ row }">{{ row.quantityOnhand }} {{ row.unitOfMeasure }}</template></el-table-column>
      <el-table-column label="操作" width="100"><template slot-scope="{ row }"><el-button size="mini" :disabled="disabled || !selected.length" @click="add(row)">新增分组</el-button></template></el-table-column>
    </el-table>
    <pagination v-show="total > 0" :total="total" :page.sync="query.pageNum" :limit.sync="query.pageSize" @pagination="load" />
    <div v-for="(group, index) in groups" :key="group.key" class="material-group">
      <div class="group-heading"><strong>投入组 {{ index + 1 }} · {{ group.stock.itemName }} / {{ group.stock.sourceBatchCode }}</strong><el-button type="text" :disabled="disabled" @click="remove(index)">移除组</el-button></div>
      <div class="hint">库存 {{ group.stock.materialStockId }} · 余额 {{ group.stock.quantityOnhand }} {{ group.stock.unitOfMeasure }}</div>
      <el-form label-width="100px" size="small" :disabled="disabled">
        <el-form-item label="对应 SN" required>
          <el-select :value="group.snIds" multiple filterable placeholder="选择本次报工 SN" :popper-class="'execution-material-group-' + group.key" class="group-select" @change="update(index, 'snIds', $event)"><el-option v-for="sn in selected" :key="sn.snId" :value="sn.snId" :label="sn.snCode" /></el-select>
          <el-button size="mini" @click="update(index, 'snIds', selected.map(sn => sn.snId))">全选本次 SN</el-button>
        </el-form-item>
        <el-form-item label="每件用量" required><el-input :value="group.quantityPerUnit" placeholder="每件实际数量" class="quantity" @input="update(index, 'quantityPerUnit', $event)"><template slot="append">{{ group.stock.unitOfMeasure }}</template></el-input></el-form-item>
      </el-form>
    </div>
  </section>
</template>
<script>
import { listExecutionMaterials } from '@/api/mes/pro/execution'
export default {
  name: 'ExecutionMaterialGroups',
  props: { taskId: { type: Number, required: true }, selected: { type: Array, required: true }, groups: { type: Array, required: true }, disabled: Boolean },
  data: () => ({ stocks: [], loading: false, total: 0, query: { pageNum: 1, pageSize: 10, batchCode: '' }, error: '', serial: 0, keySerial: 0 }),
  created() { this.load() },
  methods: {
    search() { this.query.pageNum = 1; this.load() },
    async load() {
      const serial = ++this.serial
      this.loading = true; this.error = ''
      try {
        const res = await listExecutionMaterials(this.taskId, this.query)
        if (serial === this.serial) { this.stocks = res.rows; this.total = res.total }
      } catch (error) { if (serial === this.serial) { this.error = error.message || '可用库存读取失败'; this.stocks = []; this.total = 0 } }
      finally { if (serial === this.serial) this.loading = false }
    },
    add(stock) { this.$emit('change', [...this.groups, { key: ++this.keySerial, stock, snIds: [], quantityPerUnit: '' }]) },
    remove(index) { this.$emit('change', this.groups.filter((g, i) => i !== index)) },
    update(index, field, value) { this.$emit('change', this.groups.map((g, i) => i === index ? { ...g, [field]: value } : g)) }
  }
}
</script>
<style scoped>
.hint { color: #606266; }
.material-group { margin: 14px 0; padding: 12px; border: 1px solid #dcdfe6; border-radius: 4px; }
.group-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.material-group .el-form { margin-top: 12px; }
.group-select { width: 100%; max-width: 520px; margin-right: 8px; margin-bottom: 8px; }
.quantity { max-width: 260px; }
</style>
