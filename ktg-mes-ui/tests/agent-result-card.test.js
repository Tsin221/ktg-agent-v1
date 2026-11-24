const assert = require('assert')
const fs = require('fs')
const path = require('path')
const Vue = require('vue')
const babel = require('@babel/core')
const compiler = require('vue-template-compiler')

const presentation = babel.transformFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js'),
  { babelrc: false, configFile: false, plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const presentationModule = { exports: {} }
new Function('exports', 'module', 'require', presentation)(presentationModule.exports, presentationModule, require)
function loadVue(filename) {
  const source = fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant', filename), 'utf8')
  const component = compiler.parseComponent(source)
  const script = babel.transformSync(component.script.content, { babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const componentModule = { exports: {} }
  new Function('exports', 'module', 'require', script)(componentModule.exports, componentModule,
    name => name === './sessionPresentation' ? presentationModule.exports
      : name.endsWith('.vue') ? loadVue(name) : require(name))
  assert.deepStrictEqual(compiler.compile(component.template.content).errors, [])
  const compiled = compiler.compileToFunctions(component.template.content)
  return { ...(componentModule.exports.default || componentModule.exports), render: compiled.render,
    staticRenderFns: compiled.staticRenderFns }
}
function render(result, filename = 'ResultCard.vue') {
  const vm = new Vue({ ...loadVue(filename), propsData: { result } })
  const vnode = vm._render()
  vm.$destroy()
  return vnode
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ')
  if (!node || typeof node !== 'object') return ''
  return [node.text || '', ...(node.children || []).map(text)].join(' ')
}
function tags(node) {
  if (!node || typeof node !== 'object') return []
  return [node.tag, ...(node.children || []).flatMap(tags)].filter(Boolean)
}
function findComponent(node, name) {
  if (!node || typeof node !== 'object') return undefined
  if (node.componentOptions && node.componentOptions.Ctor.options.name === name) return node
  return (node.children || []).map(child => findComponent(child, name)).find(Boolean)
}

const countText = text(render({ kind: 'product_count', total: 1 }))
assert.match(countText, /产品总数/)
assert.match(countText, /共 1 条/)
assert.match(countText, /不含物料/)

const stockText = text(render({ kind: 'stock_summary', groups: [
  { itemId: 38, itemCode: 'CELL-18650-2200', warehouseId: 1, warehouseName: '原料仓',
    quantityOnhand: '0', unitOfMeasure: 'PCS' }
], queried_at: '2026-11-24T01:00:00Z' }))
assert.match(stockText, /分仓库存汇总/)
assert.match(stockText, /原料仓/)
assert.match(stockText, /CELL-18650-2200.*0 PCS/)
assert.match(stockText, /查询时间/)
assert.match(text(render({ kind: 'stock_summary', no_records: true, groups: [] })), /不代表库存量为零/)

const progress = {
  planned: 10, allocated: 10, assembled: 10, waitingAssembly: 0, waitingInspection: 0,
  pendingDisposition: 1, reworkInProgress: 0, waitingReinspection: 0, qualified: 9,
  packable: 9, packed: 0, scrapped: 0, completionAllowed: false,
  completionReasons: ['仍有未包装或未明确报废的单件'],
  tasks: [{ taskId: '12', stepKind: 'INSPECTION', taskKind: 'NORMAL', status: 'STARTED' }],
  tasks_truncated: true
}
const row = {
  workorderId: '9007199254740997', workorderCode: 'WO-LAMP-RETAIL', workorderName: '阅读灯补单',
  productName: '可充电台灯电池模组', productCode: 'LAMP-BATT-MODULE',
  quantity: '10', unitOfMeasure: 'PCS', requestDate: '2026-11-24',
  status: 'CONFIRMED', progress_state: 'available', progress
}
const result = {
  kind: 'workorder_overview', filters: { product_code: 'LAMP-BATT-MODULE',
    workorder_codes: ['WO-LAMP-RETAIL', 'WO-LAMP-HOME'], statuses: ['APPROVING', 'APPROVED'],
    unfinished_only: false, include_progress: true },
  rows: [row,
    { ...row, workorderId: '2', workorderCode: 'WO-LAMP-HOME', workorderName: '晨光试制',
      status: 'FINISHED', progress: { ...progress, qualified: 10, pendingDisposition: 0, packable: 0, packed: 10,
        completionReasons: ['工单未处于已下达状态'] } },
    { ...row, workorderId: '3', workorderCode: 'WO-LAMP-WINTER', workorderName: '冬季新品',
      quantity: '120', status: 'PREPARE', progress_state: 'draft', progress: null },
    { ...row, workorderId: '4', workorderCode: 'WO-LAMP-OTHER', workorderName: '其他订单',
      progress_state: 'not_requested', progress: null },
    { ...row, workorderId: '5', workorderCode: 'WO-LAMP-UNKNOWN', workorderName: '待核实订单',
      progress_state: 'unavailable', progress: null, progress_reason: '执行进度查询超时，待核实。' }],
  warnings: ['多次查询不是跨工单的一致数据库快照；每个可用执行摘要来自 MES 同一详情响应。'],
  total: null, coverage: 'partial', page: 1, page_size: 10, queried_at: '2026-11-24T02:00:00Z'
}
const overview = render(result)
const overviewText = text(overview)
assert.ok(tags(overview).includes('table'), '真实工单记录应默认以表格呈现')
assert.ok(tags(overview).includes('thead'))
assert.ok(tags(overview).includes('tbody'))
assert.match(overviewText, /工单概览/)
assert.match(overviewText, /总数未核实/)
assert.match(overviewText, /部分数据/)
assert.match(overviewText, /本页 5 张/)
assert.match(overviewText, /工单编号 WO-LAMP-RETAIL、WO-LAMP-HOME/)
assert.match(overviewText, /工单状态 审批中、已审批/)
assert.match(overviewText, /仅未完工 否/)
assert.match(overviewText, /查询执行进度 是/)
assert.match(overviewText, /WO-LAMP-RETAIL/)
assert.match(overviewText, /阅读灯补单/)
assert.match(overviewText, /计划量 10 PCS/)
assert.match(overviewText, /待处置\s+1/)
assert.match(overviewText, /合格\s+9/)
assert.match(overviewText, /已包装\s+0/)
assert.match(overviewText, /仍有未包装或未明确报废的单件/)
assert.match(overviewText, /已完工/)
assert.doesNotMatch(overviewText, /工单未处于已下达状态/)
assert.match(overviewText, /草稿，尚未下达/)
assert.match(overviewText, /未请求执行进度/)
assert.match(overviewText, /执行进度未获取/)
assert.match(overviewText, /执行进度查询超时，待核实/)
assert.match(overviewText, /不是跨工单的一致数据库快照/)
assert.match(overviewText, /仅展示已返回任务，尚有省略/)
assert.match(overviewText, /检验/)
assert.ok(!tags(overview).some(tag => ['button', 'a', 'iframe', 'script', 'form'].includes(tag)))
assert.match(text(render({ ...result, rows: [], coverage: 'complete', total: 0 })), /未找到匹配工单/)
assert.match(text(render({ ...result, rows: [], coverage: 'partial' })), /本页未获工单记录/)
assert.match(text(render({ ...result, rows: [row], coverage: 'complete', total: 21, page: 2 })), /共 21 张/)
const approvedOrders = text(render({ ...result, filters: { unfinished_only: true, include_progress: false },
  rows: [{ ...row, status: 'APPROVING', progress_state: 'not_requested', progress: null },
    { ...row, workorderId: '2', status: 'APPROVED', progress_state: 'not_requested', progress: null }] }))
assert.match(approvedOrders, /审批中/)
assert.match(approvedOrders, /已审批/)
assert.match(approvedOrders, /仅未完工 是/)
assert.match(approvedOrders, /查询执行进度 否/)

const rawCell = { itemId: '38', itemCode: 'CELL-18650-2200', itemName: '电芯', unitOfMeasure: 'PCS',
  planned_required: '120', remaining_required: null, stock_quantity: '160', shortage_quantity: '0',
  stock_state: 'verified', warehouses: [{ warehouseId: '1', warehouseCode: 'WH-LAMP-RM', warehouseName: '原料仓',
    quantity: '160' }], batches: [{ batchCode: 'CELL-01', quantity: '160', warehouseCode: 'WH-LAMP-RM' }] }
const rawBoard = { ...rawCell, itemId: '39', itemCode: 'PCB-BMS-1S', itemName: '保护板',
  stock_quantity: '80', shortage_quantity: '40', warehouses: [], batches: [] }
const draftMaterials = { workorderId: '1', workorderCode: 'WO-LAMP-WINTER', workorderName: '冬季新品',
  status: 'PREPARE', quantity: '120', unitOfMeasure: 'PCS', waitingAssembly: null, material_scope: 'warehouse_reference',
  assessment: 'reference_shortage', materials: [rawCell, rawBoard] }
const lineMaterials = { ...draftMaterials, workorderId: '2', workorderCode: 'WO-LAMP-OFFICE',
  workorderName: '办公订单', status: 'CONFIRMED', quantity: '10', waitingAssembly: 6,
  material_scope: 'line_side', assessment: 'line_side_sufficient',
  materials: [rawCell, rawBoard].map(item => ({ ...item, planned_required: '10', remaining_required: '6',
    stock_quantity: '6', shortage_quantity: '0', warehouses: undefined,
    batches: [{ batchCode: 'LINE-01', quantity: '6', warehouseCode: 'XBK_VIRTUAL' }] })) }
const unavailable = { ...lineMaterials, workorderId: '3', workorderCode: 'WO-LAMP-UNKNOWN',
  assessment: 'unavailable', material_scope: 'unavailable', waitingAssembly: null,
  materials: [{ ...rawBoard, remaining_required: null, stock_quantity: null, shortage_quantity: null,
    stock_state: 'unavailable', warnings: ['物料可用量待核实'] }], warnings: ['当前身份无法核实用料'] }
const finished = { ...lineMaterials, workorderId: '4', workorderCode: 'WO-LAMP-DONE',
  status: 'FINISHED', waitingAssembly: 0, assessment: 'no_remaining',
  materials: [{ ...rawBoard, remaining_required: '0', stock_quantity: null, shortage_quantity: '0',
    stock_state: 'unavailable', warehouses: [], batches: [] }] }
const materialsResult = { kind: 'workorder_materials', filters: {
  workorder_codes: ['WO-LAMP-WINTER', 'WO-LAMP-OFFICE'], include_batches: true },
orders: [draftMaterials, lineMaterials, unavailable, finished], missing_codes: ['WO-LAMP-MISSING'],
coverage: 'partial', queried_at: '2026-11-24T08:00:00Z', warnings: ['库存读取并非同一事务快照'] }
const parentMaterialsCard = render(materialsResult)
assert.strictEqual(parentMaterialsCard.data.attrs['aria-label'], '工单用料')
assert.strictEqual(findComponent(parentMaterialsCard, 'AgentWorkorderMaterialsCard').componentOptions.propsData.result, materialsResult)
const materialsCard = render(materialsResult, 'WorkorderMaterialsCard.vue')
const materialsText = text(materialsCard)
assert.match(materialsText, /本次仅获得部分数据/)
assert.match(materialsText, /WO-LAMP-MISSING/)
assert.match(materialsText, /库存读取并非同一事务快照/)
assert.match(materialsText, /包含批次 是/)
assert.match(materialsText, /冬季新品/)
assert.match(materialsText, /原料仓账面参考/)
assert.match(materialsText, /不代表已齐套或已领料/)
assert.match(materialsText, /保护板.*缺口 40 PCS/)
assert.match(materialsText, /电芯.*120.*160.*0/)
assert.match(materialsText, /办公订单/)
assert.match(materialsText, /待装配 6 PCS/)
assert.match(materialsText, /本工单线边可耗/)
assert.match(materialsText, /剩余需求/)
assert.match(materialsText, /无剩余装配需求/)
assert.match(materialsText, /不代表当前库存齐套/)
assert.match(materialsText, /0（无需求）/)
assert.match(materialsText, /用料结论待核实/)
assert.match(materialsText, /物料可用量待核实/)
assert.match(materialsText, /当前身份无法核实用料/)
assert.match(materialsText, /原料仓.*160/)
assert.match(materialsText, /LINE-01.*XBK_VIRTUAL.*6/)
assert.ok(tags(materialsCard).includes('table'))
assert.ok(tags(materialsCard).includes('details'))
assert.ok(!tags(materialsCard).some(tag => ['button', 'a', 'iframe', 'script', 'form'].includes(tag)))
const unknownOnly = text(render({ ...materialsResult, orders: [unavailable] }, 'WorkorderMaterialsCard.vue'))
assert.match(unknownOnly, /未核实/)
assert.doesNotMatch(unknownOnly, /缺口 0|库存 0/)
const emptyMaterials = text(render({ ...materialsResult, orders: [], missing_codes: [], coverage: 'complete',
  filters: { workorder_codes: ['WO-NONE'], include_batches: false } }, 'WorkorderMaterialsCard.vue'))
assert.match(emptyMaterials, /未返回工单用料记录/)
assert.match(emptyMaterials, /包含批次 否/)

const failedIndex = { indexCode: 'VOLT', indexName: '电压', indexType: 'QUANTITATIVE', value: '2.98', passed: false,
  thresholdMin: '3.2', thresholdMax: '4.2', unitOfMeasure: 'V', defectName: '电压偏低', defectLevel: 'MAJ', reason: '实测值偏低' }
const firstInspection = { inspectionId: '9007199254740301', round_no: 1, result: 'REJECT', inspect_date: '2026-11-24T02:00:00Z',
  failed_indices: [failedIndex] }
const pending = { snId: '9007199254740302', snCode: 'SN-1', unitState: 'WAIT_DISPOSITION', inspection_count: 1,
  first_result: 'REJECT', latest_result: 'REJECT', ever_rejected: true, history_state: 'verified', inspections: [firstInspection] }
const recovered = { ...pending, snId: '9007199254740303', snCode: 'SN-2', unitState: 'PACKED', inspection_count: 2,
  latest_result: 'ACCEPT', inspections: [firstInspection,
    { inspectionId: '9007199254740304', round_no: 2, result: 'ACCEPT', inspect_date: '2026-11-24T03:00:00Z', failed_indices: [] }] }
const uninspected = { ...pending, snId: '4', snCode: 'SN-3', unitState: 'WAIT_INSPECTION', inspection_count: 0, first_result: null,
  latest_result: null, ever_rejected: false, inspections: [] }
const unknownUnit = { ...uninspected, snId: '5', snCode: 'SN-4', inspection_count: null, ever_rejected: null,
  history_state: 'unavailable', warnings: ['无权读取检验历史'] }
const qualityOrder = { workorderId: '1', workorderCode: 'WO-LAMP-RETAIL', workorderName: '阅读灯补单', status: 'CONFIRMED',
  summary: { planned: 10, allocated: 10, waitingAssembly: 4, waitingInspection: 2, pendingDisposition: 1, reworkInProgress: 0,
    waitingReinspection: 1, qualified: 1, scrapped: 1, firstInspectionRejected: 3, reinspectionCount: 2 },
  units: [pending, recovered, uninspected, unknownUnit], history_coverage: 'partial', scanned_units: 4, total_units: 10,
  missing_sn_codes: ['SN-MISSING'], warnings: ['部分历史待核实'] }
const qualityResult = { kind: 'workorder_quality', filters: { workorder_codes: ['WO-LAMP-RETAIL'], sn_codes: ['SN-1', 'SN-2'] },
  orders: [qualityOrder], missing_codes: ['WO-MISSING'], coverage: 'partial', queried_at: '2026-11-24T08:00:00Z', warnings: ['质检记录不能证明根因'] }
const parentQualityCard = render(qualityResult)
assert.strictEqual(parentQualityCard.data.attrs['aria-label'], '工单质量')
assert.strictEqual(findComponent(parentQualityCard, 'AgentWorkorderQualityCard').componentOptions.propsData.result, qualityResult)
const qualityCard = render(qualityResult, 'WorkorderQualityCard.vue')
const qualityText = text(qualityCard).replace(/\s+/g, ' ')
assert.match(qualityText, /本次仅获得部分数据/)
assert.match(qualityText, /WO-MISSING/)
assert.match(qualityText, /SN-MISSING/)
assert.match(qualityText, /SN SN-1、SN-2/)
assert.match(qualityText, /质检记录不能证明根因/)
assert.match(qualityText, /首检不合格件数\s+3/)
assert.match(qualityText, /复检次数\s+2/)
assert.match(qualityText, /待处置\s+1/)
assert.match(qualityText, /SN-1.*待处置/)
assert.match(qualityText, /SN-2.*已包装.*不合格.*合格/)
assert.match(qualityText, /SN-3.*待检.*尚未检验/)
assert.match(qualityText, /SN-4.*待检.*未核实/)
assert.match(qualityText, /第 1 轮.*电压.*2\.98 V.*3\.2.*4\.2.*电压偏低.*实测值偏低/)
assert.match(qualityText, /电压偏低 · 严重缺陷/)
assert.match(qualityText, /全单检验历史统计/)
assert.match(qualityText, /已匹配指定 SN/)
assert.match(qualityText, /第 2 轮.*合格/)
assert.match(qualityText, /已核实.*4.*10/)
assert.ok(tags(qualityCard).includes('table'))
assert.ok(tags(qualityCard).includes('dl'))
assert.ok(tags(qualityCard).includes('details'))
assert.ok(!tags(qualityCard).some(tag => ['button', 'a', 'iframe', 'script', 'form'].includes(tag)))
const onlyUnknown = text(render({ ...qualityResult, filters: { workorder_codes: ['WO-LAMP-RETAIL'] }, orders: [{ ...qualityOrder, summary: null, units: [], scanned_units: 0,
  total_units: null, history_coverage: 'partial' }] }, 'WorkorderQualityCard.vue'))
assert.match(onlyUnknown, /质量汇总未核实/)
assert.match(onlyUnknown, /尚不能判断/)
assert.doesNotMatch(onlyUnknown, /无质量异常/)
const unstarted = text(render({ ...qualityResult, orders: [{ ...qualityOrder, status: 'PREPARE', summary: null, units: [],
  history_coverage: 'not_applicable', scanned_units: 0, total_units: null }] }, 'WorkorderQualityCard.vue'))
assert.match(unstarted, /草稿，尚未下达/)
const noExceptions = text(render({ ...qualityResult, filters: { workorder_codes: ['WO-LAMP-RETAIL'] }, coverage: 'complete',
  orders: [{ ...qualityOrder, units: [], scanned_units: 10, total_units: 10, history_coverage: 'complete', missing_sn_codes: [] }] }, 'WorkorderQualityCard.vue'))
assert.match(noExceptions, /本次查询范围内未发现已记录的质量异常/)
assert.match(noExceptions, /检验任务可见历史成员/)
assert.match(noExceptions, /未检验不代表合格/)
const noInspectedMembers = text(render({ ...qualityResult, filters: { workorder_codes: ['WO-LAMP-STOCK'] }, coverage: 'complete',
  orders: [{ ...qualityOrder, units: [], scanned_units: 0, total_units: 0, history_coverage: 'complete' }] }, 'WorkorderQualityCard.vue'))
assert.match(noInspectedMembers, /当前无可见检验任务成员；未检验不代表合格/)
assert.doesNotMatch(noInspectedMembers, /未发现已记录的质量异常/)
const noMatches = text(render({ ...qualityResult, coverage: 'complete', orders: [{ ...qualityOrder, units: [], scanned_units: 0,
  total_units: 0, history_coverage: 'complete' }] }, 'WorkorderQualityCard.vue'))
assert.match(noMatches, /未返回指定单件/)
assert.doesNotMatch(noMatches, /本次查询范围内未发现已记录的质量异常/)

for (const kind of ['workorder_draft_preview', 'inventory_preview', 'exploration_search', 'workorder_progress']) {
  const historical = render({ kind, url: 'https://example.invalid', label: '<script>test</script>' })
  assert.match(text(historical), /历史结果（仅供查看，不可执行）/)
  assert.ok(tags(historical).includes('pre'))
  assert.ok(!tags(historical).some(tag => ['button', 'a', 'iframe', 'script', 'form'].includes(tag)))
  assert.match(text(historical), /<script>test<\/script>/)
}
const answerSource = fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/AnswerContent.vue'), 'utf8')
const answerComponent = compiler.parseComponent(answerSource)
const answerScript = babel.transformSync(answerComponent.script.content, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const answerModule = { exports: {} }
new Function('exports', 'module', 'require', answerScript)(answerModule.exports, answerModule,
  name => name === './sessionPresentation' ? presentationModule.exports : require(name))
const answerCompiled = compiler.compileToFunctions(answerComponent.template.content)
const answerVm = new Vue({ ...answerModule.exports.default, propsData: {
  text: '**结论：** 尚未完结。\n\n| 工单 | 状态 |\n|---|---|\n| <script>标签</script> | **已下达** |'
}, render: answerCompiled.render, staticRenderFns: answerCompiled.staticRenderFns })
const renderedAnswer = answerVm._render()
answerVm.$destroy()
assert.ok(tags(renderedAnswer).includes('table'))
assert.ok(tags(renderedAnswer).includes('strong'))
assert.match(text(renderedAnswer), /<script>标签<\/script>/)
assert.ok(!tags(renderedAnswer).includes('script'))
assert.doesNotMatch(text(renderedAnswer), /\*\*/)
console.log('Agent result card renders retained read-only cards and inert historical results')
