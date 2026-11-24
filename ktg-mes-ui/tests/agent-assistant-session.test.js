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
  queryCardPresentation, compactResultPresentation, toolProgressText } = moduleRef.exports

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
assert.ok(longAnswer.hasFullText)
assert.ok(longAnswer.textPreview.length < longAnswer.text.length)

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
