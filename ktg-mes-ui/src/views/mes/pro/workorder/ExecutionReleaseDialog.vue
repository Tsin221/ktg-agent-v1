<template>
  <el-dialog title="生产执行" :visible.sync="visible" width="900px" append-to-body :close-on-click-modal="false">
    <div v-loading="loading">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <template v-if="order && order.status === 'PREPARE' && !detail && !unknown">
        <p>工单 {{ order.workorderCode }} · 计划 {{ order.quantity }} 件。下达生成待装配 SN，实际产量和库存不变。</p>
        <el-form label-width="110px" :disabled="submitting || unknown">
          <el-form-item label="工艺路线" required><el-select v-model="routeId" placeholder="请选择已配置工艺" @change="selectRoute"><el-option v-for="route in routes" :key="route.routeId" :value="route.routeId" :label="route.routeCode + ' · ' + route.routeName" /></el-select></el-form-item>
          <el-alert v-if="!routes.length && !loading" title="没有可下达的工艺，请检查产品关联、启用状态及执行类型配置。" type="info" :closable="false" />
          <div v-for="(step, index) in steps" :key="step.routeProcessId">
            <el-form-item :label="(index + 1) + ' · ' + kindLabel(step.executionKind)" required>
              <span>{{ step.processName }}</span>
              <el-select v-model="step.workstationId" placeholder="请选择对应工作站"><el-option v-for="station in step.workstations" :key="station.workstationId" :value="station.workstationId" :label="station.workstationCode + ' · ' + station.workstationName" /></el-select>
            </el-form-item>
            <template v-if="step.executionKind === 'INSPECTION'">
              <el-form-item label="检测模板" required><el-select v-model="step.templateId" placeholder="请选择检测模板" @change="step.ipqcType = null"><el-option v-for="template in step.templates" :key="template.templateId" :value="template.templateId" :label="template.templateName" /></el-select></el-form-item>
              <el-form-item label="检验类型" required><el-select v-model="step.ipqcType" placeholder="请选择检验类型"><el-option v-for="type in templateTypes(step)" :key="type" :value="type" :label="type" /></el-select></el-form-item>
            </template>
          </div>
        </el-form>
      </template>
      <template v-if="detail">
        <p>固定工艺：{{ detail.order.routeName }} · 计划 {{ detail.order.plannedUnits }} 件</p>
        <p>待装配 SN 总数 {{ detail.summary.allocated }} · 已装配 {{ detail.summary.assembled }} · 合格 {{ detail.summary.qualified }} · 已包装 {{ detail.summary.packed }} · 报废 {{ detail.summary.scrapped }}</p>
        <el-table :data="detail.tasks"><el-table-column label="任务" prop="taskCode" /><el-table-column label="工序" prop="processName" /><el-table-column label="工作站" prop="workstationName" /><el-table-column label="计划量" prop="quantity" /><el-table-column label="执行状态"><template slot-scope="scope">{{ stateLabel(scope.row.status) }}</template></el-table-column></el-table>
        <el-table :data="detail.units" max-height="260"><el-table-column label="SN" prop="snCode" /><el-table-column label="单件状态"><template slot-scope="scope">{{ stateLabel(scope.row.unitState) }}</template></el-table-column></el-table>
      </template>
      <el-alert v-if="unknown" title="下达结果待确认，请查询原操作回执。关闭后重新打开仍可恢复查询。" type="warning" :closable="false" />
    </div>
    <div slot="footer">
      <el-button @click="visible = false" :disabled="submitting">关闭</el-button>
      <el-button v-if="unknown && retryable && pendingPayload" type="primary" :loading="submitting" @click="retryOriginal">重试原下达</el-button>
      <el-button v-if="unknown" type="primary" :loading="submitting" @click="recover">查询下达结果</el-button>
      <el-button v-else-if="!detail && order && order.status === 'PREPARE'" v-hasPermi="['mes:pro:execution:release']" type="primary" :loading="submitting" :disabled="loading || !ready" @click="submit">确认下达</el-button>
      <el-button v-else-if="detail" @click="readDetail">刷新执行数据</el-button>
    </div>
  </el-dialog>
</template>
<script>
import { releaseOptions, releaseOrder, executionOrder, executionOperation } from '@/api/mes/pro/execution'
export default {
  name: 'ExecutionReleaseDialog',
  data: () => ({ visible: false, loading: false, submitting: false, order: null, routes: [], routeId: null, steps: [], error: '', unknown: false, operationId: null, pendingPayload: null, retryable: false, detail: null }),
  computed: { ready() { return this.steps.length === 3 && this.steps.every(s => s.workstationId && (s.executionKind !== 'INSPECTION' || (s.templateId && s.ipqcType))) } },
  methods: {
    newOperationId() {
      if (crypto.randomUUID) return crypto.randomUUID()
      const bytes = crypto.getRandomValues(new Uint8Array(16))
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128
      const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
      return [hex.slice(0,8), hex.slice(8,12), hex.slice(12,16), hex.slice(16,20), hex.slice(20)].join('-')
    },
    kindLabel(kind) { return { ASSEMBLY: '生产', INSPECTION: '质检', PACKING: '包装' }[kind] || '未配置' },
    stateLabel(state) { return { WAITING: '等待前序', READY: '可开工', STARTED: '已开工', FINISHED: '已完成', WAIT_ASSEMBLY: '待装配' }[state] || state },
    templateTypes(step) { const t = (step.templates || []).find(t => t.templateId === step.templateId); return t ? (Array.isArray(t.qcTypes) ? t.qcTypes : String(t.qcTypes || '').split(',')).map(v => v.trim()).filter(v => ['FIRST', 'FINAL', 'PATROL'].includes(v)) : [] },
    storageKey() { return 'mes-release-' + this.order.workorderId },
    async open(order) {
      Object.assign(this.$data, { visible: true, order, loading: true, routes: [], routeId: null, steps: [], error: '', unknown: false, operationId: null, pendingPayload: null, retryable: false, detail: null })
      try {
        const saved = sessionStorage.getItem(this.storageKey())
        if (saved) {
          try { this.pendingPayload = JSON.parse(saved); this.operationId = this.pendingPayload.operationId } catch (_) { this.operationId = saved }
        }
        if (this.operationId) { this.unknown = true; return }
        if (order.status !== 'PREPARE') { await this.readDetail(); return }
        this.routes = (await releaseOptions(order.workorderId)).data.routes
      } catch (e) { this.error = e.message || '读取下达配置失败' } finally { this.loading = false }
    },
    selectRoute() { const r = this.routes.find(r => r.routeId === this.routeId); this.steps = r ? r.steps.map(s => ({ ...s, workstationId: null, templateId: null, ipqcType: null })) : [] },
    async readDetail() { try { this.detail = (await executionOrder(this.order.workorderId)).data } catch (e) { this.error = e.message || '读取执行数据失败' } },
    async committed() { sessionStorage.removeItem(this.storageKey()); this.unknown = false; await this.readDetail(); this.$emit('released') },
    async submit() {
      if (!this.ready || this.submitting || this.unknown) return
      this.operationId = this.newOperationId()
      this.pendingPayload = { operationId: this.operationId, expectedVersion: this.order.version, routeId: this.routeId, steps: this.steps.map(s => ({ routeProcessId: s.routeProcessId, workstationId: s.workstationId, ...(s.executionKind === 'INSPECTION' ? { templateId: s.templateId, ipqcType: s.ipqcType } : {}) })) }
      sessionStorage.setItem(this.storageKey(), JSON.stringify(this.pendingPayload))
      await this.performRelease()
    },
    async retryOriginal() {
      if (this.submitting || !this.retryable || !this.pendingPayload) return
      await this.performRelease()
    },
    async performRelease() {
      this.submitting = true; this.error = ''; this.retryable = false
      try {
        const response = await releaseOrder(this.order.workorderId, this.pendingPayload)
        if (response.code === 202) { this.unknown = true; return }
        await this.committed()
      } catch (e) {
        const code = e.errorCode || (e.data && e.data.errorCode) || (e.response && e.response.data && e.response.data.errorCode)
        this.unknown = !code || code === 'RESULT_UNKNOWN'
        if (!this.unknown) sessionStorage.removeItem(this.storageKey())
        this.error = e.message || '下达请求未完成'
      } finally { this.submitting = false }
    },
    async recover() {
      this.submitting = true; this.error = ''
      try { const response = await executionOperation(this.operationId); if (response.code !== 202) await this.committed() } catch (e) {
        const data = e.data || (e.response && e.response.data) || {}
        this.retryable = data.errorCode === 'NOT_FOUND' && !!this.pendingPayload
        this.error = this.retryable ? '尚无已提交回执，可按原操作重试下达。' : (e.message || '回执暂不可用，请稍后查询原操作')
      } finally { this.submitting = false }
    }
  }
}
</script>
