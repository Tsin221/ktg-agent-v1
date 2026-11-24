const assert = require('assert')
const fs = require('fs')
const path = require('path')
const Vue = require('vue')
const babel = require('@babel/core')
const compiler = require('vue-template-compiler')

const source = fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/ResultCard.vue'), 'utf8')
const component = compiler.parseComponent(source)
const script = babel.transformSync(component.script.content, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const presentation = babel.transformFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js'),
  { babelrc: false, configFile: false, plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const presentationModule = { exports: {} }
new Function('exports', 'module', 'require', presentation)(presentationModule.exports, presentationModule, require)
const componentModule = { exports: {} }
new Function('exports', 'module', 'require', script)(componentModule.exports, componentModule,
  name => name === './sessionPresentation' ? presentationModule.exports : require(name))
const options = componentModule.exports.default || componentModule.exports
const compiled = compiler.compileToFunctions(component.template.content)
function render(result) {
  const vm = new Vue({ ...options, propsData: { result }, render: compiled.render,
    staticRenderFns: compiled.staticRenderFns })
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
