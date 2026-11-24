const assert = require('assert')
const path = require('path')
const { transformFileSync } = require('@babel/core')

const filename = path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js')
const transformed = transformFileSync(filename, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] })
const moduleRef = { exports: {} }
new Function('exports', 'module', 'require', transformed.code)(moduleRef.exports, moduleRef, require)
const { pageContext, presentMessages, progressFromEvent, isTerminalRun, runStatusText,
  visibleInputs, inputStatusText, usageText, contextStatusText, contextPressureText,
  queryCardPresentation, compactResultPresentation, answerBlocks, toolProgressText } = moduleRef.exports

const context = pageContext({ name: 'Workorder', query: {
  workorderId: '42', itemCode: 'A-01', warehouseId: 3, token: 'secret', arbitrary: 'ignore'
} })
assert.deepStrictEqual(context.selected_entity, { type: 'workorder', id: '42' })
assert.deepStrictEqual(context.filters, { itemCode: 'A-01', warehouseId: 3 })
assert.strictEqual(context.route_name, 'Workorder')
assert.strictEqual(JSON.stringify(context).includes('secret'), false)

const history = presentMessages([
  { message_id: 1, run_id: 'r1', role: 'user', content: [{ type: 'text', text: '查库存' }] },
  { message_id: 2, run_id: 'r1', role: 'assistant', content: [{ type: 'tool_call', name: 'stock' }] },
  { message_id: 3, run_id: 'r1', role: 'tool', details: { kind: 'stock', rows: [], total: 0 }, content: [] },
  { message_id: 4, run_id: 'r1', role: 'assistant', content: [{ type: 'text', text: '当前无库存' }] },
  { message_id: 5, run_id: 'r2', role: 'user', content: [{ type: 'text', text: '再查工单' }] },
  { message_id: 6, run_id: 'r2', role: 'tool', details: { kind: 'workorders', rows: [] }, content: [] }
])
assert.strictEqual(history.length, 4)
assert.strictEqual(history[1].text, '当前无库存')
assert.strictEqual(history[1].sourceText, '查库存')
assert.strictEqual(history[1].results[0].kind, 'stock')
assert.strictEqual(history[3].results[0].kind, 'workorders')
const followUpHistory = presentMessages([
  { message_id: 1, run_id: 'follow', role: 'user', content: [{ type: 'text', text: '第一问' }] },
  { message_id: 2, run_id: 'follow', role: 'assistant', content: [{ type: 'text', text: '第一答' }] },
  { message_id: 3, run_id: 'follow', role: 'user', content: [{ type: 'text', text: '追问' }] },
  { message_id: 4, run_id: 'follow', role: 'assistant', content: [{ type: 'text', text: '第二答' }] }
])
assert.deepStrictEqual(followUpHistory.map(item => item.text), ['第一问', '第一答', '追问', '第二答'])
assert.strictEqual(followUpHistory[3].sourceText, '追问')

let messageId = 10
const message = (runId, role, content, fields = {}) => ({
  message_id: ++messageId, run_id: runId, role, content, ...fields
})
const chat = (runId, role, text) => message(runId, role, [{ type: 'text', text }])
const query = (runId, name, args, details, error = '') => {
  const id = 'call-' + messageId
  return [message(runId, 'assistant', [{ type: 'tool_call', id, name, arguments: args }]),
    message(runId, 'tool', error ? [{ type: 'text', text: error }] : [], {
      tool_call_id: id, details, is_error: !!error
    })]
}
const count = { kind: 'product_count', total: 1, rows: [], queried_at: '2026-11-24T01:00:00Z' }
const stock = { kind: 'stock_summary', filters: { item_code: 'CELL-18650-2200' }, groups: [], no_records: true,
  queried_at: '2026-11-24T02:00:00Z' }
const multi = [chat('long', 'user', '查询产品数和电芯库存'),
  ...query('long', 'ktg_count_products', {}, count),
  ...query('long', 'ktg_query_stock_summary', { item_code: 'CELL-18650-2200' }, stock),
  ...query('long', 'ktg_query_stock_summary', { item_code: 'CELL-18650-2200' }, { ...stock, queried_at: '2026-11-24T03:00:00Z' }),
  ...query('long', 'ktg_query_stock_summary', { item_code: 'PCB-BMS-1S' }, null, '读取超时'),
  chat('long', 'assistant', '已汇总产品数和电芯库存。保护板库存待核实。')]
const detailEntries = queryCardPresentation(presentMessages(multi)).filter(entry => entry.queryDetails)
assert.strictEqual(detailEntries.length, 1)
const multiResult = detailEntries[0]
assert.match(multiResult.text, /保护板库存待核实/)
const groups = Object.fromEntries(multiResult.queryDetails.groups.map(group => [group.title, group.queries]))
assert.strictEqual(multiResult.queryDetails.count, 4)
assert.strictEqual(multiResult.queryDetails.failed, 1)
assert.strictEqual(groups['通用查询'][0].attempts[0].result.kind, 'product_count')
assert.deepStrictEqual(groups['物料 CELL-18650-2200'][0].attempts.map(item => item.time),
  [stock.queried_at, '2026-11-24T03:00:00Z'])
assert.strictEqual(groups['物料 PCB-BMS-1S'][0].attempts[0].error, '读取超时')

const single = [chat('single', 'user', '产品总数'),
  ...query('single', 'ktg_count_products', {}, count), chat('single', 'assistant', '当前 1 条产品记录')]
assert.strictEqual(queryCardPresentation(presentMessages(single))[1].results[0].kind, 'product_count')
const overview = { kind: 'workorder_overview', filters: { product_code: 'LAMP-BATT-MODULE' },
  rows: [], total: 0, coverage: 'complete', page: 1, page_size: 10, queried_at: '2026-11-24T04:00:00Z' }
const overviewQueries = [chat('orders', 'user', '查询工单'),
  ...query('orders', 'ktg_query_workorder_overview', { product_code: 'LAMP-BATT-MODULE', page: 1,
    workorder_codes: ['WO-LAMP-RETAIL', 'WO-LAMP-HOME'], statuses: ['APPROVING', 'APPROVED'],
    unfinished_only: false, include_progress: true }, overview),
  ...query('orders', 'ktg_query_workorder_overview', { product_code: 'LAMP-BATT-MODULE', page: 2,
    unfinished_only: true, include_progress: false }, null, '工单查询失败'),
  chat('orders', 'assistant', '工单查询待核实')]
const orderDetails = queryCardPresentation(presentMessages(overviewQueries)).at(-1).queryDetails
assert.strictEqual(orderDetails.count, 2)
assert.strictEqual(orderDetails.failed, 1)
assert.strictEqual(orderDetails.groups[0].title, '工单概览')
assert.strictEqual(orderDetails.groups[0].id, 'workorders')
assert.match(orderDetails.groups[0].queries[0].attempts[0].scope, /产品编码 LAMP-BATT-MODULE/)
assert.match(orderDetails.groups[0].queries[0].attempts[0].scope, /工单编号 WO-LAMP-RETAIL、WO-LAMP-HOME/)
assert.match(orderDetails.groups[0].queries[0].attempts[0].scope, /工单状态 审批中、已审批/)
assert.match(orderDetails.groups[0].queries[0].attempts[0].scope, /仅未完工 否/)
assert.match(orderDetails.groups[0].queries[0].attempts[0].scope, /查询执行进度 是/)
assert.match(orderDetails.groups[0].queries[1].attempts[0].scope, /页码 2/)
assert.match(orderDetails.groups[0].queries[1].attempts[0].scope, /仅未完工 是/)
assert.match(orderDetails.groups[0].queries[1].attempts[0].scope, /查询执行进度 否/)
assert.strictEqual(orderDetails.groups[0].queries[1].attempts[0].error, '工单查询失败')
assert.strictEqual(toolProgressText({ workorder: { name: 'ktg_query_workorder_overview', status: 'running' } }), '正在查询工单概览')
const materials = { kind: 'workorder_materials', filters: { workorder_codes: ['WO-LAMP-WINTER'], include_batches: true },
  orders: [], missing_codes: [], coverage: 'complete', queried_at: '2026-11-24T07:05:00Z' }
const materialsQueries = [chat('materials', 'user', '查询工单用料'),
  ...query('materials', 'ktg_query_workorder_materials', { workorder_codes: ['WO-LAMP-WINTER'], include_batches: true }, materials),
  ...query('materials', 'ktg_query_workorder_materials', { workorder_codes: ['WO-LAMP-OFFICE'], include_batches: false }, null, '用料查询失败'),
  chat('materials', 'assistant', '工单用料已核实部分。')]
const materialDetails = queryCardPresentation(presentMessages(materialsQueries)).at(-1).queryDetails
assert.strictEqual(materialDetails.count, 2)
assert.strictEqual(materialDetails.failed, 1)
assert.strictEqual(materialDetails.groups[0].title, '工单用料')
assert.match(materialDetails.groups[0].queries[0].attempts[0].scope, /包含批次 是/)
assert.match(materialDetails.groups[0].queries[1].attempts[0].scope, /包含批次 否/)
assert.match(materialDetails.groups[0].queries[1].attempts[0].scope, /工单编号 WO-LAMP-OFFICE/)
assert.strictEqual(toolProgressText({ material: { name: 'ktg_query_workorder_materials', status: 'running' } }), '正在查询工单用料')
const singleMaterialPreface = [
  chat('material-preface', 'user', '看冬季用料'),
  message('material-preface', 'assistant', [{ type: 'text', text: '我先查询准确用料。' },
    { type: 'tool_call', id: 'single-material', name: 'ktg_query_workorder_materials', arguments: materials.filters }]),
  message('material-preface', 'tool', [], { tool_call_id: 'single-material', details: materials }),
  chat('material-preface', 'assistant', '结论：保护板有缺口。')]
const singleMaterialPresentation = compactResultPresentation(queryCardPresentation(presentMessages(singleMaterialPreface)))
assert.strictEqual(singleMaterialPresentation.length, 2, '单次材料查完，前言不应把结果卡与结论分开')
assert.strictEqual(singleMaterialPresentation[1].textPreview, '结论：保护板有缺口。')
assert.deepStrictEqual(singleMaterialPresentation[1].results.map(item => item.kind), ['workorder_materials'])
assert.strictEqual(singleMaterialPresentation[1].queryDetails, undefined)
const resumed = [...multi, chat('resume', 'user', '继续此任务'),
  ...query('resume', 'update_task', {}, { task_id: 'task-a', version: 2 }),
  ...query('resume', 'ktg_query_stock_summary', { item_code: 'CELL-18650-2200' }, stock),
  chat('resume', 'assistant', '续查完成')]
const restored = queryCardPresentation(presentMessages(JSON.parse(JSON.stringify(resumed))))
assert.deepStrictEqual(restored.filter(entry => entry.queryDetails).map(entry => [entry.runId, entry.queryDetails.count]), [['long', 4], ['resume', 1]])
const legacy = [message('old', 'tool', [], { details: { kind: 'workorder_draft_preview', preview_id: 'past' }}),
  message('old', 'tool', [], { details: { kind: 'exploration_search', records: [] }})]
const historical = compactResultPresentation(queryCardPresentation(presentMessages(legacy)))[0]
assert.deepStrictEqual(historical.results.map(item => item.kind), ['workorder_draft_preview', 'exploration_search'])
assert.strictEqual(historical.queryDetails, undefined)
assert.strictEqual(historical.resultSelection, undefined)
const longAnswer = compactResultPresentation([{ role: 'assistant', text: '完整答复'.repeat(100) }])[0]
assert.strictEqual(longAnswer.hasFullText, false, '无可靠段落不能把任意前120字当作概要')
const conclusion = '床头阅读灯工单尚不能完结：装配已完成，1件待处置，9件合格品尚未包装。'
const scope = '**查询范围与时间**：按名称查询，共1张，覆盖完整；查询时间15:05。'
const evidence = '**生产进度**\n- 已装配10件\n- 合格9件\n- 待处置1件'
const legacyAnswer = [scope, evidence, '也就是说，' + conclusion, '待处置的具体原因尚未核实。'].join('\n\n')
const legacyPreview = compactResultPresentation([{ role: 'assistant', text: legacyAnswer }])[0]
assert.strictEqual(legacyPreview.textPreview, '也就是说，' + conclusion)
assert.ok(legacyPreview.hasFullText)
assert.ok(legacyPreview.textDetails.includes(scope))
assert.ok(legacyPreview.textDetails.includes(evidence))
assert.ok(legacyPreview.textDetails.includes('待处置的具体原因尚未核实。'))
assert.ok(!legacyPreview.textDetails.includes(conclusion), '展开区域不重复结论')
const firstAnswer = ['**结论：** ' + conclusion, scope, evidence].join('\n\n')
const firstPreview = compactResultPresentation([{ role: 'assistant', text: firstAnswer }])[0]
assert.strictEqual(firstPreview.textPreview, '结论： ' + conclusion)
assert.strictEqual(firstPreview.textDetails, [scope, evidence].join('\n\n'))
assert.ok(firstPreview.hasFullText)
const noConclusion = compactResultPresentation([{ role: 'assistant', text: [scope, evidence].join('\n\n') }])[0]
assert.strictEqual(noConclusion.hasFullText, false, '只有范围和数据时直接展示，不冒充结论')
const longConclusion = '结论：' + '当前数据仍不完整，应核实剩余工单。'.repeat(12)
const longConclusionPreview = compactResultPresentation([{ role: 'assistant', text: [longConclusion, scope].join('\n\n') }])[0]
assert.strictEqual(longConclusionPreview.textPreview, longConclusion, '不能截断结论或限制条件')
const shortAnswer = compactResultPresentation([{ role: 'assistant', text: '共有3个产品。' }])[0]
assert.strictEqual(shortAnswer.textPreview, '共有3个产品。')
assert.strictEqual(shortAnswer.hasFullText, false)
const followUp = '需要我继续查询其他未完工工单吗？'
const withQuestion = compactResultPresentation([{ role: 'assistant', text: [scope, evidence, '简单说：' + conclusion, followUp].join('\n\n') }])[0]
assert.strictEqual(withQuestion.textPreview, '简单说：' + conclusion)
assert.strictEqual(withQuestion.followUp, followUp)
assert.ok(!withQuestion.textDetails.includes(followUp), '追问必须一直可见且不重复')
const answerTable = '| 工单 | 状态 | 数量 |\n|---|:---:|---:|\n| 阅读灯 | 已下达 | **10** |\n| 办公灯 | 待装配 | 4 |'
const comparisonPreview = compactResultPresentation([{ role: 'assistant', text: ['结论：办公灯进度更慢。', answerTable, scope].join('\n\n'), results: [] }])[0]
assert.strictEqual(comparisonPreview.textTables, answerTable, '没有直接数据卡时，对比表默认可见')
assert.strictEqual(comparisonPreview.textDetails, scope)
const withResultCard = compactResultPresentation([{ role: 'assistant', text: ['结论：办公灯进度更慢。', answerTable, scope].join('\n\n'), results: [{ kind: 'workorder_overview' }] }])[0]
assert.strictEqual(withResultCard.textTables, '', '已有权威数据卡时，避免默认重复展示模型表格')
assert.ok(withResultCard.textDetails.includes(answerTable))
const materialTable = '| 物料 | 计划需求 | 账面量 | 缺口 |\n|---|---:|---:|---:|\n| 保护板 | 120 | 80 | 40 |'
const officeMaterials = { ...materials, filters: { workorder_codes: ['WO-LAMP-OFFICE'], include_batches: true },
  queried_at: '2026-11-24T07:07:00Z' }
const materialMulti = [chat('material-multi', 'user', '对照冬季和办公工单用料'),
  ...query('material-multi', 'ktg_query_workorder_overview', { workorder_name: '冬季' }, overview),
  ...query('material-multi', 'ktg_query_workorder_materials', materials.filters, materials),
  ...query('material-multi', 'ktg_query_workorder_materials', materials.filters,
    { ...materials, queried_at: '2026-11-24T07:06:00Z' }),
  ...query('material-multi', 'ktg_query_workorder_materials', officeMaterials.filters, officeMaterials),
  ...query('material-multi', 'ktg_query_workorder_materials', { workorder_codes: ['WO-MISSING'] }, null, '未核实'),
  chat('material-multi', 'assistant', ['结论：冬季保护板有缺口，办公工单另有独立线边用料。', materialTable, scope].join('\n\n'))]
const materialMultiPresentation = compactResultPresentation(queryCardPresentation(presentMessages(materialMulti))).at(-1)
assert.strictEqual(materialMultiPresentation.textPreview, '结论：冬季保护板有缺口，办公工单另有独立线边用料。')
assert.strictEqual(materialMultiPresentation.results.length, 2, '不同材料查询分别展示；相同查询保留最新结果')
assert.deepStrictEqual(materialMultiPresentation.results.map(item => item.kind), ['workorder_materials', 'workorder_materials'])
assert.strictEqual(materialMultiPresentation.results[0].queried_at, '2026-11-24T07:06:00Z')
assert.strictEqual(materialMultiPresentation.textTables, '', '权威材料卡直接展示后不重复默认展示模型表格')
assert.strictEqual(materialMultiPresentation.queryDetails.count, 5)
assert.strictEqual(materialMultiPresentation.queryDetails.failed, 1)
const materialAudits = materialMultiPresentation.queryDetails.groups.find(group => group.title === '工单用料').queries.flatMap(q => q.attempts)
assert.strictEqual(materialAudits.filter(item => item.resultShown && !item.result).length, 3)
assert.strictEqual(materialAudits.filter(item => item.error).length, 1)
assert.ok(materialMultiPresentation.queryDetails.groups.find(group => group.title === '工单概览').queries[0].attempts[0].result)
const quality = { kind: 'workorder_quality', filters: { workorder_codes: ['WO-LAMP-RETAIL'], sn_codes: ['SN-1'] },
  orders: [], missing_codes: [], coverage: 'complete', queried_at: '2026-11-24T08:00:00Z' }
const qualityPreface = [chat('quality-preface', 'user', '哪个质量不合格？'),
  message('quality-preface', 'assistant', [{ type: 'text', text: '先查质量。' },
    { type: 'tool_call', id: 'single-quality', name: 'ktg_query_workorder_quality', arguments: quality.filters }]),
  message('quality-preface', 'tool', [], { tool_call_id: 'single-quality', details: quality }),
  chat('quality-preface', 'assistant', '结论：1件待处置。')]
const singleQualityPresentation = compactResultPresentation(queryCardPresentation(presentMessages(qualityPreface)))
assert.strictEqual(singleQualityPresentation.length, 2)
assert.strictEqual(singleQualityPresentation[1].textPreview, '结论：1件待处置。')
assert.deepStrictEqual(singleQualityPresentation[1].results, [quality])
assert.strictEqual(singleQualityPresentation[1].queryDetails, undefined)
const qualityTable = '| SN | 状态 |\n|---|---|\n| SN-1 | 待处置 |'
const qualityMulti = [chat('quality-multi', 'user', '看看工单质量与材料'),
  ...query('quality-multi', 'ktg_query_workorder_overview', { workorder_name: '床头阅读灯' }, overview),
  ...query('quality-multi', 'ktg_query_workorder_quality', quality.filters, quality),
  ...query('quality-multi', 'ktg_query_workorder_quality', quality.filters, { ...quality, queried_at: '2026-11-24T08:01:00Z' }),
  ...query('quality-multi', 'ktg_query_workorder_quality', { workorder_codes: ['WO-MISSING'] }, null, '质量查询失败'),
  ...query('quality-multi', 'ktg_query_workorder_materials', materials.filters, materials),
  chat('quality-multi', 'assistant', ['结论：有一件待处置。', qualityTable, scope].join('\n\n'))]
const qualityPresentation = compactResultPresentation(queryCardPresentation(presentMessages(qualityMulti))).at(-1)
assert.deepStrictEqual(qualityPresentation.results.map(item => item.kind), ['workorder_quality', 'workorder_materials'])
assert.strictEqual(qualityPresentation.results[0].queried_at, '2026-11-24T08:01:00Z')
assert.strictEqual(qualityPresentation.textPreview, '结论：有一件待处置。')
assert.strictEqual(qualityPresentation.textTables, '')
assert.ok(qualityPresentation.textDetails.includes(qualityTable))
assert.strictEqual(qualityPresentation.queryDetails.count, 5)
assert.strictEqual(qualityPresentation.queryDetails.failed, 1)
const qualityAudits = qualityPresentation.queryDetails.groups.find(group => group.title === '工单质量').queries.flatMap(q => q.attempts)
assert.strictEqual(qualityAudits.filter(item => item.resultShown && !item.result).length, 2)
assert.strictEqual(qualityAudits.filter(item => item.error).length, 1)
assert.match(qualityAudits[0].scope, /工单编号 WO-LAMP-RETAIL/)
assert.match(qualityAudits[0].scope, /SN SN-1/)
assert.strictEqual(toolProgressText({ quality: { name: 'ktg_query_workorder_quality', status: 'running' } }), '正在查询工单质量')
const unitTrace = { kind: 'production_trace', direction: 'unit_to_material', filters: { direction: 'unit_to_material', sn_codes: ['SN-1'] },
  units: [], batches: [], coverage: 'complete', queried_at: '2026-11-24T08:00:00Z' }
const batchTrace = { kind: 'production_trace', direction: 'material_to_unit', filters: { direction: 'material_to_unit',
  batches: [{ item_code: 'CELL-18650', batch_code: 'CELL-BATCH-A' }], page: 1, page_size: 20 },
  units: [], batches: [], coverage: 'complete', queried_at: '2026-11-24T08:01:00Z' }
const singleTrace = [chat('trace-single', 'user', '这个 SN 用什么原料？'),
  message('trace-single', 'assistant', [{ type: 'text', text: '先追溯。' },
    { type: 'tool_call', id: 'single-trace', name: 'ktg_query_production_trace', arguments: unitTrace.filters }]),
  message('trace-single', 'tool', [], { tool_call_id: 'single-trace', details: unitTrace }),
  chat('trace-single', 'assistant', '结论：实际使用两种原料。\n\n请确认是否继续追溯？')]
const tracePresentation = compactResultPresentation(queryCardPresentation(presentMessages(singleTrace)))
assert.strictEqual(tracePresentation.length, 2)
assert.strictEqual(tracePresentation[1].textPreview, '结论：实际使用两种原料。')
assert.strictEqual(tracePresentation[1].followUp, '请确认是否继续追溯？')
assert.deepStrictEqual(tracePresentation[1].results, [unitTrace])
const traceMulti = [chat('trace-multi', 'user', '看同批原料质量情况'),
  ...query('trace-multi', 'ktg_query_production_trace', unitTrace.filters, unitTrace),
  ...query('trace-multi', 'ktg_query_production_trace', batchTrace.filters, batchTrace),
  ...query('trace-multi', 'ktg_query_production_trace', batchTrace.filters, { ...batchTrace, queried_at: '2026-11-24T08:02:00Z' }),
  ...query('trace-multi', 'ktg_query_production_trace', { direction: 'material_to_unit', batches: [{ item_code: 'X', batch_code: 'B' }] }, null, '追溯失败'),
  ...query('trace-multi', 'ktg_query_workorder_quality', quality.filters, quality),
  chat('trace-multi', 'assistant', ['结论：同批有多个 SN。', qualityTable, '请提供另一个批次？'].join('\n\n'))]
const tracedMulti = compactResultPresentation(queryCardPresentation(presentMessages(traceMulti))).at(-1)
assert.deepStrictEqual(tracedMulti.results.map(result => result.direction || result.kind), ['unit_to_material', 'material_to_unit', 'workorder_quality'])
assert.strictEqual(tracedMulti.results[1].queried_at, '2026-11-24T08:02:00Z')
assert.strictEqual(tracedMulti.textTables, '')
assert.ok(tracedMulti.textDetails.includes(qualityTable))
assert.strictEqual(tracedMulti.followUp, '请提供另一个批次？')
assert.strictEqual(tracedMulti.queryDetails.count, 5)
assert.strictEqual(tracedMulti.queryDetails.failed, 1)
const traceAudits = tracedMulti.queryDetails.groups.find(group => group.title === '生产追溯').queries.flatMap(q => q.attempts)
assert.strictEqual(traceAudits.filter(item => item.resultShown && !item.result).length, 3)
assert.match(traceAudits[0].scope, /SN SN-1/)
assert.match(traceAudits[1].scope, /CELL-18650.*CELL-BATCH-A/)
assert.doesNotMatch(traceAudits[1].scope, /\[object Object\]/)
assert.strictEqual(toolProgressText({ trace: { name: 'ktg_query_production_trace', status: 'running' } }), '正在查询生产追溯')
assert.deepStrictEqual(answerBlocks(['查询结果', answerTable, '还有1件待处置。'].join('\n\n')), [
  { type: 'text', text: '查询结果' },
  { type: 'table', headers: ['工单', '状态', '数量'], rows: [['阅读灯', '已下达', '**10**'], ['办公灯', '待装配', '4']] },
  { type: 'text', text: '还有1件待处置。' }
])
assert.deepStrictEqual(answerBlocks('不要把 A | B 当成表格'), [{ type: 'text', text: '不要把 A | B 当成表格' }])
assert.deepStrictEqual(answerBlocks('| A | B |\n|---|---|\n| <script>标签</script> | 2 |'), [
  { type: 'table', headers: ['A', 'B'], rows: [['<script>标签</script>', '2']] }
])

let progress = { messages: [], tools: {} }
progress = progressFromEvent(progress, { type: 'message_start', payload: { message_id: 'm1', role: 'assistant' } })
progress = progressFromEvent(progress, { type: 'message_update', payload: { message_id: 'm1', delta_type: 'text', text: '正在' } })
progress = progressFromEvent(progress, { type: 'message_update', payload: { message_id: 'm1', delta_type: 'text', text: '查' } })
progress = progressFromEvent(progress, { type: 'message_end', payload: { message_id: 'm1', stop_reason: 'completed',
  message: { content: [{ type: 'text', text: '正在查' }] } } })
progress = progressFromEvent(progress, { type: 'message_start', payload: { message_id: 'm2', role: 'assistant' } })
progress = progressFromEvent(progress, { type: 'message_update', payload: { message_id: 'm2', delta_type: 'text', text: '再查' } })
progress = progressFromEvent(progress, { type: 'message_end', payload: { message_id: 'm2', stop_reason: 'cancelled' } })
progress = progressFromEvent(progress, { type: 'tool_execution_start', payload: { tool_call_id: 't1', tool_name: 'query_stock' } })
progress = progressFromEvent(progress, { type: 'tool_execution_end', payload: { tool_call_id: 't1', status: 'succeeded' } })
assert.deepStrictEqual(progress.messages.map(message => message.text), ['正在查', '再查'])
assert.strictEqual(progress.messages[1].status, 'incomplete')
assert.strictEqual(progress.tools.t1.status, 'succeeded')
assert.strictEqual(toolProgressText({ a: { name: 'ktg_count_products', status: 'running' },
  b: { name: 'ktg_query_stock_summary', status: 'running' } }), '正在查询产品总数 · 另有 1 项正在执行')
assert.strictEqual(toolProgressText({ a: progress.tools.t1 }), '已执行 1 项操作')
assert.strictEqual(isTerminalRun('running'), false)
assert.strictEqual(isTerminalRun('completed'), true)
assert.strictEqual(runStatusText({ status: 'needs_reconciliation' }), '业务结果待核验；请查询对应操作回执。')
assert.match(runStatusText({ status: 'needs_reconciliation', stop_reason: 'cancelled' }), /业务结果待核验.*用户取消/)
assert.strictEqual(runStatusText({ status: 'failed' }), '本轮处理失败，请重试。')
assert.match(runStatusText({ status: 'cancelling' }), /核验结果/)
assert.match(runStatusText({ status: 'failed', result_summary: { limit_reason: 'max_total_tokens' } }), /总 token/)
assert.match(runStatusText({ status: 'failed', result_summary: { error_code: 'MODEL_USAGE_UNAVAILABLE' } }), /用量暂缺/)
assert.strictEqual(usageText({ source: 'legacy_unknown', complete: false }), '用量：无记录')
assert.strictEqual(usageText(null), '用量：暂缺')
assert.strictEqual(usageText({ known_total_tokens: 0, complete: true }), '已知用量 0 token · 统计完整')
assert.match(usageText({ known_total_tokens: 8, complete: false, unknown_request_count: 1 }), /未完整统计/)
assert.strictEqual(contextStatusText(null), '')
assert.strictEqual(contextPressureText({ configured: false }), '')
const contextSnapshot = { configured: true, window_tokens: 8192, threshold_tokens: 6553,
  estimated_tokens: 7000, estimate_kind: 'estimated', summary_id: '12345678-abcd',
  covers_start_message_id: 1, covers_end_message_id: 9, spill_count: 2 }
assert.match(contextPressureText(contextSnapshot), /估算值，非已知用量/)
assert.match(contextStatusText(contextSnapshot), /12345678，覆盖 1-9/)
assert.match(contextStatusText(contextSnapshot), /spill 2/)
for (const [reason, phrase] of Object.entries({
  length: '单次模型输出', max_run_seconds: '运行时间', max_model_turns: '模型调用次数',
  max_tool_calls: '工具启动次数', cancelled: '用户取消', USAGE_PERSISTENCE_FAILED: '用量记录失败'
})) assert.match(runStatusText({ status: 'failed', stop_reason: reason }), new RegExp(phrase))
assert.strictEqual(inputStatusText({ status: 'not_consumed' }), '本轮未消费，可主动作为新问题发送')
assert.strictEqual(inputStatusText({ status: 'not_consumed', reason: 'limit' }), '本轮执行额度已用完，可作为新问题发送')
assert.strictEqual(inputStatusText({ status: 'queued', mode: 'follow_up' }), '已排队，等待当前工作收尾')
assert.deepStrictEqual(visibleInputs([
  { request_id: 'i1', status: 'consumed', message: '查库存' },
  { request_id: 'i2', status: 'not_consumed', message: '继续' }
], history, 'r1').map(input => input.request_id), ['i1', 'i2'])
assert.deepStrictEqual(visibleInputs([{ request_id: 'i1', status: 'consumed', message: '继续' }],
  [{ runId: 'r1', role: 'user', text: '查库存' }, { runId: 'r1', role: 'user', text: '继续' }], 'r1'), [])

console.log('agent assistant session presentation tests passed')
