<template>
  <section class="exploration-result" aria-label="业务查询结果">
    <p class="exploration-result__headline">{{ headline }}</p>
    <p v-if="current.coverage !== 'complete'" class="exploration-result__scope">仅已查范围 · 尚未确认全部记录</p>

    <div v-if="records.length">
      <section v-for="group in recordGroups" :key="group.entity" class="exploration-result__record-group">
        <p v-if="group.label && (recordGroups.length > 1 || current.kind === 'exploration_search')" class="exploration-result__section-title">{{ group.label }}</p>
        <div class="exploration-result__table-wrap">
          <table class="exploration-result__table">
            <caption class="exploration-result__sr">{{ group.label || '查询结果' }}，可查看有权限的记录详情</caption>
            <thead><tr><th v-for="column in group.columns" :key="column" scope="col">{{ label(column, group.entity) }}</th><th v-if="group.hasReferences" scope="col">查看</th></tr></thead>
            <tbody><tr v-for="(row, index) in group.rows" :key="row.record_ref || index">
              <td v-for="column in group.columns" :key="column">{{ format(row[column], column) }}</td>
              <td v-if="group.hasReferences"><button v-if="canRead(row)" type="button" :disabled="busy" :aria-pressed="selectedRef === row.record_ref ? 'true' : 'false'" @click="readRecord(row)">{{ selectedRef === row.record_ref ? '已选' : selectionRequired ? '选择' : '查看' }}</button><span v-else>仅列表摘要</span></td>
            </tr></tbody>
          </table>
        </div>
      </section>
    </div>
    <p v-else-if="!flow && !detail && !busy && !analysisTables.length && current.kind !== 'exploration_error'" class="exploration-result__scope">{{ emptyText }}</p>
    <p v-if="current.kind === 'exploration_error'" role="alert" class="exploration-result__error">{{ current.message || '本次查询未完成，请调整线索后重试。' }}</p>
    <button v-if="hasMore" class="exploration-result__more" type="button" :disabled="busy" @click="showMore">继续查看</button>

    <section v-for="(table, tableIndex) in analysisTables" :key="tableIndex" class="exploration-result__analysis">
      <p v-if="table.title" class="exploration-result__section-title">{{ table.title }}</p>
      <p v-if="table.totalRows > 20" class="exploration-result__scope">本表已返回 {{ table.totalRows }} 行，此处展示前 20 行</p>
      <div class="exploration-result__table-wrap"><table class="exploration-result__table"><caption class="exploration-result__sr">分析结果 {{ tableIndex + 1 }}</caption>
        <thead><tr><th v-for="column in table.columns" :key="column" scope="col">{{ label(column) }}</th></tr></thead>
        <tbody><tr v-for="(row, rowIndex) in table.rows" :key="rowIndex"><td v-for="column in table.columns" :key="column">{{ format(row[column], column) }}</td></tr></tbody>
      </table></div>
    </section>

    <section v-if="flow" class="exploration-result__flow" aria-label="完整业务流程">
      <p class="exploration-result__section-title">业务流程 <span>点击有记录的环节展开</span></p>
      <div class="exploration-result__graph-scroll" tabindex="0" aria-label="业务流程图，可滚动查看完整流程">
        <div class="exploration-result__graph" :style="{ width: layout.width + 'px', height: layout.height + 'px' }">
          <svg class="exploration-result__connectors" :viewBox="'0 0 ' + layout.width + ' ' + layout.height" aria-hidden="true">
            <defs><marker :id="arrowId" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="currentColor" /></marker></defs>
            <path v-for="(edge, index) in layout.edges" :key="index" :d="edge.path" fill="none" stroke="currentColor" stroke-width="1.5" :stroke-dasharray="edge.blueprint ? '4 3' : null" :marker-end="'url(#' + arrowId + ')'" />
          </svg>
          <button v-for="node in layout.nodes" :key="node.id" type="button" class="exploration-result__node" :class="nodeClass(node)"
                  :style="{ left: node.x + 'px', top: node.y + 'px' }" :disabled="busy || !canOpenNode(node)"
                  :aria-expanded="activeNode && activeNode.id === node.id ? 'true' : 'false'" @click="openNode(node)">
            <strong>{{ node.label }}</strong><span>{{ stateText(node.state) }}</span>
          </button>
        </div>
      </div>
    </section>

    <section v-if="activeNode || detail" class="exploration-result__detail" aria-label="展开的业务详情">
      <div class="exploration-result__detail-header"><strong>{{ activeNode ? activeNode.label : '所选记录' }}</strong><button type="button" aria-label="收起业务详情" @click="closeDetail">收起</button></div>
      <p v-if="activeNode && activeNode.summary">{{ activeNode.summary }}</p>
      <div v-if="activeRows.length && !detail" class="exploration-result__table-wrap">
        <p v-if="activeNode.records_truncated" class="exploration-result__scope">本环节共 {{ activeNode.record_count }} 条，此处展示前 {{ activeRows.length }} 条；可继续让助手查询明细。</p>
        <table class="exploration-result__table"><caption class="exploration-result__sr">本环节已查询的记录</caption>
          <thead><tr><th v-for="column in activeColumns" :key="column" scope="col">{{ label(column) }}</th><th>详情</th></tr></thead>
          <tbody><tr v-for="(row, index) in activeRows" :key="row.record_ref || index">
            <td v-for="column in activeColumns" :key="column">{{ format(row[column], column) }}</td>
            <td><button v-if="canRead(row)" type="button" :disabled="busy" @click="readRecord(row, true)">查看</button><span v-else>仅列表摘要</span></td>
          </tr></tbody>
        </table>
      </div>
      <div v-if="activeNode && !activeRows.length && activeNode.record_refs && activeNode.record_refs.length > 1" class="exploration-result__choices">
        <button v-for="(reference, index) in activeNode.record_refs" :key="reference" type="button" :disabled="busy" @click="readRecord({ record_ref: reference }, true)">查看第 {{ index + 1 }} 条</button>
      </div>
      <template v-if="detail && detail.kind !== 'exploration_flow'">
        <p v-if="detail.headline" class="exploration-result__detail-conclusion">{{ detail.headline }}</p>
        <p v-if="detail.coverage && detail.coverage !== 'complete'" class="exploration-result__scope">详情仅代表已查范围</p>
        <table v-if="detailFields.length" class="exploration-result__table exploration-result__facts"><caption class="exploration-result__sr">所选记录的已查询字段</caption><tbody>
          <tr v-for="field in detailFields" :key="field.key"><th scope="row">{{ label(field.key) }}</th><td>{{ format(field.value, field.key) }}</td></tr>
        </tbody></table>
        <div class="exploration-result__choices">
          <button v-if="canShowFlow(detailRecord)" type="button" :disabled="busy" @click="readFlow(detailRecord)">查看完整流程</button>
          <button v-if="originalRoute(detailRecord)" type="button" @click="openOriginal(detailRecord)">打开业务页</button>
        </div>
      </template>
    </section>

    <p v-if="busy" role="status" class="exploration-result__scope">正在读取…</p>
    <p v-if="error" role="alert" class="exploration-result__error">{{ error }} <button v-if="lastRequest" type="button" :disabled="busy" @click="retry">重试</button></p>
    <details v-if="warnings.length" class="exploration-result__warnings"><summary>查询说明 · {{ warnings.length }}</summary><ul><li v-for="(warning, index) in warnings" :key="index">{{ warning }}</li></ul></details>
    <p v-if="current.queried_at" class="exploration-result__time">查询时间 {{ timeText(current.queried_at) }}</p>
  </section>
</template>

<script>
import { getAgentExplorationView } from '@/api/agent'

const labels = {
  code: '编号', name: '名称', label: '类型', status: '状态', specification: '规格', machinery_spec: '型号',
  workshop_name: '车间', machinery_name: '设备', product_name: '产品', item_name: '物料', client_name: '客户',
  machinery_code: '设备编号', item_code: '物料编号', workorder_code: '工单编号',
  require_date: '报修日期', finish_date: '完工日期', confirm_date: '验收日期', request_date: '需求日期',
  malfunction: '故障', repair_des: '处理情况', repair_result: '维修结果', unit_of_measure: '单位',
  quantity: '计划数量', quantity_produced: '已生产', quantity_onhand: '现存量', quantity_issued: '领料量',
  quantity_recived: '入库量', quantity_feedback: '报工量', quantity_qualified: '合格量', quantity_unquanlified: '不合格量',
  warehouse_name: '仓库', location_name: '库区', area_name: '库位', batch_code: '批次',
  feedback_time: '报工时间', accepted_by: '维修人员', confirm_by: '验收人员', enable_flag: '启用状态',
  subject_content: '项目内容', subject_standard: '标准', subject_type: '项目类型', bom_item_name: 'BOM物料',
  bom_item_code: '物料编号', bom_item_spec: '物料规格', item_type_name: '物料类别', item_or_product: '物料类型',
  machinery_brand: '品牌', brand: '品牌', spec: '规格', tool_type_name: '工装类型', machinery_type_name: '设备类型'
}
const stateLabels = { completed: '已完成', in_progress: '进行中', not_found: '未查到记录',
  no_permission: '无权限', not_adapted: '本演示未接入', not_applicable: '不适用' }
const valueLabels = { FINISHED: '已完成', PREPARE: '草稿', CONFIRMED: '已确认', APPROVED: '已审核',
  APPROVING: '审核中', WORKING: '生产中', REPAIR: '维修中', STOP: '停机', SUCCESS: '成功', FAILURE: '失败' }
const routes = {
  'md.item': ['/mes/md/mditem', 'itemId'], 'md.workshop': ['/mes/md/workshop', 'workshopId'],
  'dv.machinery_type': ['/mes/dv/machinerytype', 'machineryTypeId'], 'dv.machinery': ['/mes/dv/machinery', 'machineryId'],
  'dv.subject': ['/mes/dv/subject', 'subjectId'], 'dv.check_plan': ['/mes/dv/checkplan', 'planId'],
  'dv.repair': ['/mes/dv/repair', 'repairId'], 'tm.tool_type': ['/mes/tm/tooltype', 'toolTypeId'],
  'tm.tool': ['/mes/tm/tool', 'toolId'], 'pro.workorder': ['/mes/pro/workorder', 'workorderId'],
  'pro.task': ['/mes/pro/schedule', 'taskId'], 'pro.feedback': ['/mes/pro/feedback', 'recordId'],
  'wm.stock': ['/mes/wm/wmstock', 'materialStockId'], 'wm.item_receipt': ['/mes/wm/itemrecpt', 'recptId'],
  'wm.issue': ['/mes/wm/issue', 'issueId']
}
const scalar = value => value == null || ['string', 'number', 'boolean'].includes(typeof value)
const populated = value => scalar(value) && value != null && (typeof value !== 'string' || value.trim() !== '')
const entityLabels = {
  'md.item': '产品物料', 'md.workshop': '车间', 'md.item_bom': '产品物料清单',
  'pro.workorder': '生产工单', 'pro.task': '生产任务', 'pro.feedback': '生产报工', 'pro.workorder_bom': '工单物料需求',
  'dv.machinery_type': '设备类型', 'dv.machinery': '设备台账', 'dv.subject': '点检保养项目', 'dv.repair': '维修单',
  'dv.repair_line': '维修内容', 'dv.check_plan': '点检保养计划', 'dv.check_machinery': '计划设备', 'dv.check_subject': '计划项目',
  'tm.tool_type': '工装类型', 'tm.tool': '工装夹具', 'wm.stock': '现存库存', 'wm.item_receipt': '物料入库',
  'wm.item_receipt_line': '入库明细', 'wm.issue': '生产领料', 'wm.issue_line': '领料明细'
}
const visibleKey = key => !/(?:^|_)(?:ref|id|token|password|owner|scope|permission|permissions|endpoint|tool|session|run|catalog)(?:_|$)/i.test(key) &&
  !['entity', 'source', 'module', 'can_read', 'available_actions', 'match_evidence', 'match_strength', 'requires_confirmation', 'score', 'coverage', 'warnings', 'applied_query', 'path', 'route', 'operators', 'fields'].includes(key)

function columnsFor(rows, entity) {
  const preferred = entity === 'md.item' ? ['code', 'name', 'specification', 'unit_of_measure', 'item_type_name']
    : ['code', 'name', 'item_code', 'item_name', 'bom_item_code', 'bom_item_name', 'machinery_code', 'machinery_name',
      'product_name', 'label', 'workshop_name', 'warehouse_name', 'status', 'malfunction', 'require_date', 'quantity_onhand',
      'quantity_issued', 'quantity_recived', 'quantity', 'specification', 'bom_item_spec']
  const keys = new Set()
  rows.forEach(row => Object.keys(row).filter(key => visibleKey(key) && populated(row[key])).forEach(key => keys.add(key)))
  return [...preferred.filter(key => keys.has(key)), ...[...keys].filter(key => !preferred.includes(key))].slice(0, 5)
}

export default {
  name: 'ExplorationResult',
  props: { result: { type: Object, required: true }, sessionId: { type: String, default: '' }, selectionRequired: { type: Boolean, default: false } },
  data: () => ({ detail: null, flowResult: null, pageResult: null, activeNode: null, selectedRef: '', busy: false,
    error: '', requestSequence: 0, alive: true, visibleLimit: 5, lastRequest: null }),
  computed: {
    current() { return this.pageResult || this.result },
    headline() { return this.flow && this.flow.headline || this.detail && this.detail.headline || this.current.headline || this.current.summary || (this.current.kind === 'exploration_error' ? '本次查询未完成' : '业务查询结果') },
    analysisTables() {
      return (this.current.tables || []).filter(table => table && Array.isArray(table.columns) && Array.isArray(table.rows)).map(table => {
        const columns = table.columns.map(column => typeof column === 'string' ? column : column.name).filter(key => typeof key === 'string' && visibleKey(key))
        const sourceColumns = table.columns.map(column => typeof column === 'string' ? column : column.name)
        return { title: table.title || table.name || '', columns, totalRows: table.rows.length, rows: table.rows.slice(0, 20).map(row => Array.isArray(row)
          ? Object.fromEntries(sourceColumns.map((column, index) => [column, row[index]])) : row) }
      })
    },
    records() {
      const rows = this.current.records || this.current.rows || this.current.items || (this.current.kind === 'exploration_relations' ? this.current.nodes : []) || []
      const columns = this.current.columns || []
      return rows.filter(row => row && typeof row === 'object').map(row => Array.isArray(row)
        ? Object.fromEntries(columns.map((column, index) => [typeof column === 'string' ? column : column.name, row[index]])) : row)
    },
    visibleRecords() { return this.records.slice(0, this.visibleLimit) },
    recordGroups() {
      const groups = new Map()
      for (const row of this.visibleRecords) {
        const entity = row.entity || ''
        if (!groups.has(entity)) groups.set(entity, [])
        groups.get(entity).push(row)
      }
      return [...groups].map(([entity, rows]) => ({ entity, rows, label: entity ? entityLabels[entity] || '其他记录' : '',
        columns: columnsFor(rows, entity), hasReferences: rows.some(row => row.record_ref) }))
    },
    flow() { return this.flowResult || (this.current.kind === 'exploration_flow' ? this.current : null) },
    arrowId() { return 'exploration-arrow-' + this._uid },
    detailRecord() { return this.detail && (this.detail.records || [])[0] || {} },
    detailFields() { return Object.keys(this.detailRecord).filter(key => visibleKey(key) && scalar(this.detailRecord[key]))
      .map(key => ({ key, value: this.detailRecord[key] })) },
    activeRows() { return this.activeNode && this.activeNode.records || [] },
    activeColumns() { return columnsFor(this.activeRows, this.activeRows[0] && this.activeRows[0].entity) },
    hasMore() { return !this.flow && (this.records.length > this.visibleLimit || Number.isInteger(this.current.next_offset)) },
    emptyText() { return this.current.coverage === 'complete' ? '本次查询未返回匹配记录。' : '已查范围暂无匹配，尚未确认全部记录。' },
    warnings() {
      return [...new Set([...(this.current.warnings || []), ...(this.detail && this.detail.warnings || []),
        ...(this.flow && this.flow.warnings || []),
        ...(this.activeNode && this.activeNode.warnings || [])].filter(warning => typeof warning === 'string'))]
    },
    layout() {
      const nodes = this.flow && this.flow.nodes || []
      const edges = this.flow && this.flow.edges || []
      const levels = Object.fromEntries(nodes.map(node => [node.id, 0]))
      for (let i = 0; i < nodes.length; i++) edges.forEach(edge => {
        if (levels[edge.source] != null && levels[edge.target] != null) levels[edge.target] = Math.min(nodes.length - 1, Math.max(levels[edge.target], levels[edge.source] + 1))
      })
      const groups = []
      nodes.forEach(node => { const level = levels[node.id]; if (!groups[level]) groups[level] = []; groups[level].push(node) })
      const width = Math.max(1, ...groups.filter(Boolean).map(group => group.length)) * 112 + 8
      const positioned = []
      groups.forEach((group, level) => group.forEach((node, lane) => positioned.push({ ...node,
        x: (width - group.length * 112) / 2 + lane * 112 + 6, y: level * 90 + 8 })))
      const byId = Object.fromEntries(positioned.map(node => [node.id, node]))
      return { width, height: groups.length * 90, nodes: positioned, edges: edges.filter(edge => byId[edge.source] && byId[edge.target]).map(edge => {
        const start = byId[edge.source]; const end = byId[edge.target]
        return { blueprint: !!edge.kind, path: `M${start.x + 50},${start.y + 64} C${start.x + 50},${start.y + 78} ${end.x + 50},${end.y - 12} ${end.x + 50},${end.y}` }
      }) }
    }
  },
  watch: { sessionId() { this.reset() }, result() { this.reset() } },
  beforeDestroy() { this.alive = false; this.requestSequence++ },
  methods: {
    reset() { this.requestSequence++; this.detail = null; this.flowResult = null; this.pageResult = null; this.activeNode = null; this.selectedRef = ''; this.error = ''; this.busy = false; this.lastRequest = null; this.visibleLimit = 5 },
    label(key, entity) { return key === 'quantity' && entity && !['pro.workorder', 'pro.task'].includes(entity) ? '数量' : labels[key] || key },
    format(value, key) { return value === undefined ? '—' : value === null || value === '' ? '未填写' : key === 'status' || key === 'repair_result' ? valueLabels[value] || value : typeof value === 'boolean' ? value ? '是' : '否' : String(value) },
    timeText(value) { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleString('zh-CN') },
    stateText(value) { return stateLabels[value] || value || '未核实' },
    nodeClass(node) { const state = this.stateText(node.state); return { 'is-complete': state === '已完成', 'is-active': state === '进行中', 'is-unavailable': ['无权限', '本演示未接入', '不适用'].includes(state) } },
    canOpenNode(node) { return ((node.records || []).length > 0 || node.can_read !== false && Array.isArray(node.record_refs) && node.record_refs.length > 0) && !['无权限', '本演示未接入', '不适用'].includes(this.stateText(node.state)) },
    canRead(row) { return row.record_ref && row.can_read !== false && (!Array.isArray(row.available_actions) || row.available_actions.includes('read')) && !['wm.item_receipt_line', 'wm.issue_line'].includes(row.entity) },
    async requestView(tool, args) {
      if (!this.sessionId) { this.error = '请先打开一个有效会话。'; return null }
      const sequence = ++this.requestSequence; const session = this.sessionId
      this.busy = true; this.error = ''; this.lastRequest = { tool, args }
      try {
        const response = await getAgentExplorationView(session, tool, args)
        if (!this.alive || sequence !== this.requestSequence || session !== this.sessionId) return null
        if (!response || !response.result || response.result.kind === 'exploration_error') throw new Error('记录暂不可查看，可能已过期或权限发生变化。')
        return response.result
      } catch (_) {
        if (this.alive && sequence === this.requestSequence && session === this.sessionId) this.error = '读取失败，请重试；记录可能已过期或当前无权查看。'
        return null
      } finally { if (this.alive && sequence === this.requestSequence && session === this.sessionId) this.busy = false }
    },
    async readRecord(row, keepNode = false) {
      if (!row || !this.canRead(row)) return
      if (!keepNode && this.selectedRef === row.record_ref) return
      if (!keepNode) { this.activeNode = null; this.flowResult = null }
      this.detail = null; this.selectedRef = ''
      const result = await this.requestView('ktg_read_record', { record_ref: row.record_ref })
      if (result) {
        this.selectedRef = row.record_ref; this.detail = result
        const record = (result.records || [])[0] || row
        if (!keepNode) this.$emit('selected', { entity: record.entity, name: record.name, code: record.code,
          resume: this.selectionRequired && this.current.kind === 'exploration_search' })
      }
    },
    async readFlow(row) {
      if (!row || !row.record_ref) return
      const result = await this.requestView('ktg_get_business_flow', { record_ref: row.record_ref })
      if (result) { this.flowResult = result; this.detail = null; this.activeNode = null }
    },
    openNode(node) { if (!this.canOpenNode(node)) return; this.activeNode = node; this.detail = null; if (node.record_refs.length === 1) this.readRecord({ record_ref: node.record_refs[0] }, true) },
    closeDetail() { this.requestSequence++; this.busy = false; this.detail = null; this.activeNode = null; this.error = ''; this.lastRequest = null },
    async showMore() {
      if (this.records.length > this.visibleLimit) { this.visibleLimit += 5; return }
      if (!this.current.result_ref || !Number.isInteger(this.current.next_offset)) return
      const result = await this.requestView('ktg_inspect_result', { result_ref: this.current.result_ref, offset: this.current.next_offset, limit: 5 })
      if (result) { this.pageResult = result; this.visibleLimit = 5; this.detail = null; this.activeNode = null; this.flowResult = null }
    },
    async retry() { if (!this.lastRequest) return; const request = this.lastRequest; const result = await this.requestView(request.tool, request.args); if (result) { if (request.tool === 'ktg_inspect_result') this.pageResult = result; else if (result.kind === 'exploration_flow') this.flowResult = result; else this.detail = result } },
    canShowFlow(row) { return row.record_ref && (!Array.isArray(row.available_actions) || row.available_actions.includes('flow')) && ['pro.workorder', 'pro.task', 'pro.feedback', 'pro.workorder_bom', 'wm.issue', 'dv.repair', 'dv.repair_line', 'dv.machinery'].includes(row.entity) },
    safeRoute(path) { return typeof path === 'string' && Object.values(routes).some(route => route[0] === path) ? path : null },
    originalRoute(row) {
      let entry = routes[row.entity]; let id = row.record_id || row.id
      const parents = { 'dv.repair_line': ['dv.repair', 'repair_id'], 'wm.item_receipt_line': ['wm.item_receipt', 'recpt_id'],
        'wm.issue_line': ['wm.issue', 'issue_id'], 'pro.workorder_bom': ['pro.workorder', 'workorder_id'],
        'md.item_bom': ['md.item', 'item_id'], 'dv.check_machinery': ['dv.check_plan', 'plan_id'], 'dv.check_subject': ['dv.check_plan', 'plan_id'] }
      if (!entry && parents[row.entity]) { const parent = parents[row.entity]; entry = routes[parent[0]]; id = row[parent[1]] }
      const path = row.route || entry && entry[0]
      return this.safeRoute(path) && id != null ? { path, query: { [entry ? entry[1] : 'id']: String(id) } } : null
    },
    openOriginal(row) { const route = this.originalRoute(row); if (route && this.$router) this.$router.push(route).catch(() => {}) }
  }
}
</script>

<style scoped>
.exploration-result { color: var(--assistant-text, #24364b); font-size: 13px; line-height: 1.5; min-width: 0; }
.exploration-result__headline { margin: 0 0 8px; font-size: 15px; font-weight: 650; }
.exploration-result__scope, .exploration-result__time { margin: 6px 0; color: var(--assistant-muted, #61758b); font-size: 12px; }
.exploration-result__table-wrap, .exploration-result__graph-scroll { max-width: 100%; overflow-x: auto; }
.exploration-result__table { border-collapse: collapse; width: 100%; font-size: 12px; text-align: left; }
.exploration-result__table th { font-weight: 600; background: var(--assistant-soft, #f4f7fa); }
.exploration-result__table th, .exploration-result__table td { padding: 7px 8px; border-bottom: 1px solid var(--assistant-border, #dce4ec); vertical-align: top; overflow-wrap: anywhere; min-width: 44px; }
.exploration-result button { cursor: pointer; color: var(--assistant-action, #235e99); border: 1px solid var(--assistant-border, #c8d5e2); background: var(--assistant-card, #fff); border-radius: 4px; padding: 4px 8px; font: inherit; }
.exploration-result button:focus-visible, .exploration-result__graph-scroll:focus-visible { outline: 2px solid var(--assistant-action, #235e99); outline-offset: 2px; }
.exploration-result button:disabled { cursor: default; }
.exploration-result__more { margin-top: 8px; }
.exploration-result__section-title { margin: 12px 0 8px; font-weight: 600; }
.exploration-result__section-title span { margin-left: 4px; color: var(--assistant-muted, #61758b); font-size: 11px; font-weight: 400; }
.exploration-result__graph { position: relative; margin: 0 auto; }
.exploration-result__connectors { position: absolute; inset: 0; width: 100%; height: 100%; color: #8d9fb2; pointer-events: none; }
.exploration-result .exploration-result__node { position: absolute; width: 100px; min-height: 64px; padding: 7px 4px; display: flex; flex-direction: column; justify-content: center; gap: 3px; text-align: center; background: var(--assistant-card, #fff); }
.exploration-result__node strong { font-size: 12px; font-weight: 600; }
.exploration-result__node span { font-size: 11px; color: var(--assistant-muted, #61758b); }
.exploration-result .exploration-result__node.is-complete { border-color: #55947c; }
.exploration-result .exploration-result__node.is-active { border-color: var(--assistant-action, #235e99); background: #f0f6fc; }
.exploration-result .exploration-result__node.is-unavailable { background: #f4f6f8; color: #647184; border-style: dashed; }
.exploration-result__detail { margin-top: 12px; border-top: 1px solid var(--assistant-border, #dce4ec); padding-top: 10px; }
.exploration-result__detail p { margin: 8px 0; }
.exploration-result__detail-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.exploration-result__detail-header button { font-size: 12px; }
.exploration-result__detail-conclusion { font-weight: 600; }
.exploration-result__facts th { width: 90px; }
.exploration-result__choices { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.exploration-result__warnings { color: var(--assistant-muted, #61758b); font-size: 12px; margin-top: 10px; }
.exploration-result__warnings ul { padding-left: 20px; margin: 6px 0; }
.exploration-result__error { color: #a33232; }
.exploration-result__sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
