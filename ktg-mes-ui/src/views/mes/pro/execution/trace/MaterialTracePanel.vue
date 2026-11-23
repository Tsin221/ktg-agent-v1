<template>
  <section aria-label="原料批次追溯查询" :aria-busy="loading">
    <el-form :inline="true" size="small" @submit.native.prevent="search()">
      <el-form-item label="原料物料 ID" required>
        <el-input v-model="itemId" maxlength="19" aria-label="原料物料 ID" placeholder="输入唯一物料 ID" clearable @input="selectedItem = null">
          <el-button v-if="canSelectItem" slot="append" icon="el-icon-search" aria-label="选择物料" @click="chooseItem">选择</el-button>
        </el-input>
      </el-form-item>
      <el-form-item label="原料批次" required><el-input v-model="batchCode" maxlength="255" aria-label="原料批次" placeholder="输入完整原料批次号" clearable /></el-form-item>
      <el-form-item><el-button native-type="submit" type="primary" icon="el-icon-search" :loading="loading">查询</el-button><el-button @click="reset">重置</el-button></el-form-item>
    </el-form>
    <item-select v-if="selectorMounted && canSelectItem" ref="itemSelect" @onSelected="onItemSelected" />
    <p v-if="selectedItem" class="hint">已选择物料：{{ selectedItem.itemCode }} · {{ selectedItem.itemName }}</p>
    <p class="hint">同时指定物料 ID 和实际原料批次。仅显示有真实投入记录且当前账号可见的成品 SN，每个 SN 只列一次。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon role="alert" />
    <p v-if="criteria" class="query-context" role="status" aria-live="polite">已提交条件：物料 ID {{ criteria.itemId }} · 批次 {{ criteria.batchCode }}<template v-if="loading"> · 正在查询……</template><template v-else-if="!error"> · 共 {{ total }} 件可见成品</template></p>
    <p v-if="criteria && !error && !loading && !rows.length && total === 0" class="hint" role="status">无可见实际投入记录；不能据此推断没有发生历史业务。</p>
    <p v-if="criteria && !error && !loading && !rows.length && total > 0" class="hint" role="status">当前页没有可见记录（结果或权限范围可能已变化）。<el-button type="text" @click="firstPage">返回第一页</el-button></p>
    <el-table v-if="criteria && !error && rows.length" :data="rows" row-key="snId">
      <el-table-column label="受影响成品 SN" min-width="170"><template slot-scope="{ row }"><el-button type="text" @click="$emit('trace-unit', row.snCode)">{{ row.snCode }}</el-button></template></el-table-column>
      <el-table-column label="产品" min-width="150" show-overflow-tooltip><template slot-scope="{ row }">{{ row.itemCode || '—' }} · {{ row.itemName || '—' }}</template></el-table-column>
      <el-table-column prop="workorderCode" label="所属工单" min-width="135" show-overflow-tooltip />
      <el-table-column label="当前单件状态" min-width="110"><template slot-scope="{ row }">{{ taskLabel(row.unitState) }}</template></el-table-column>
      <el-table-column prop="inputCount" label="投入记录数" width="110" align="right" />
      <el-table-column prop="firstUsedAt" label="首次投入时间" min-width="175" />
      <el-table-column prop="lastUsedAt" label="最近投入时间" min-width="175" />
    </el-table>
    <pagination v-if="!error && total > 0" :total="total" :page.sync="pageNum" :limit.sync="pageSize" :page-sizes="[10, 20, 50, 100]" @pagination="load" />
  </section>
</template>

<script>
import ItemSelect from '@/components/itemSelect/single.vue'
import { traceExecutionMaterials } from '@/api/mes/pro/execution'
import { taskLabel } from '../taskPresentation'
import { traceError, hasTracePermission } from './tracePresentation'

export default {
  name: 'MaterialTracePanel',
  components: { ItemSelect },
  data: () => ({ itemId: '', batchCode: '', selectedItem: null, selectorMounted: false, pageNum: 1, pageSize: 10, rows: [], total: 0, criteria: null, loading: false, error: '', sequence: 0 }),
  computed: {
    canSelectItem() { return hasTracePermission(this.$store.getters.permissions || [], 'mes:md:mditem:list') }
  },
  methods: {
    taskLabel,
    chooseItem() {
      if (!this.canSelectItem) return
      this.selectorMounted = true
      this.$nextTick(() => { if (this.$refs.itemSelect) this.$refs.itemSelect.handleOpen(null) })
    },
    onItemSelected(item) {
      if (!item || item.itemId == null) return
      if (typeof item.itemId === 'number' && !Number.isSafeInteger(item.itemId)) {
        this.itemId = ''; this.selectedItem = null; this.error = '物料选择器未返回精确 ID，请手工输入完整物料 ID。'; return
      }
      this.itemId = String(item.itemId); this.selectedItem = item
    },
    open(itemId, batchCode) {
      this.itemId = String(itemId)
      this.batchCode = batchCode
      this.selectedItem = null
      return this.search()
    },
    reset() {
      ++this.sequence
      Object.assign(this, { itemId: '', batchCode: '', selectedItem: null, pageNum: 1, pageSize: 10, criteria: null, rows: [], total: 0, loading: false, error: '' })
    },
    firstPage() { this.pageNum = 1; return this.load() },
    async search() {
      ++this.sequence
      Object.assign(this, { rows: [], total: 0, criteria: null, error: '', loading: false, pageNum: 1 })
      const itemId = String(this.itemId).trim()
      // Batch identity follows exact persisted bytes. Trim only for the blank check.
      const batchCode = String(this.batchCode || '')
      if (!/^[1-9]\d{0,18}$/.test(itemId) || (itemId.length === 19 && itemId > '9223372036854775807') || !batchCode.trim() || batchCode.length > 255) {
        this.error = '请输入有效的正整数物料 ID 及完整批次（1–255 个字符）。'
        return
      }
      this.criteria = { itemId, batchCode }
      return this.load()
    },
    async load() {
      if (!this.criteria) return
      const sequence = ++this.sequence
      this.rows = []; this.loading = true; this.error = ''
      try {
        const response = await traceExecutionMaterials({ ...this.criteria, pageNum: this.pageNum, pageSize: this.pageSize })
        if (!Array.isArray(response.rows) || typeof response.total !== 'number') throw Error('追溯接口未返回有效分页结果。')
        if (sequence === this.sequence) { this.rows = response.rows; this.total = response.total }
      } catch (error) {
        if (sequence === this.sequence) { this.total = 0; this.error = traceError(error, '原料批次追溯读取失败。') }
      } finally { if (sequence === this.sequence) this.loading = false }
    }
  }
}
</script>

<style scoped>
.hint, .query-context { line-height: 1.6; overflow-wrap: anywhere; color: #606266; }
.query-context { margin: 16px 0; white-space: pre-wrap; }
</style>
