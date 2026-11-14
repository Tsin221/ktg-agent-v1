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
  queryCardPresentation, toolProgressText } = moduleRef.exports

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
const orderA = { workorderId: 12, workorderCode: 'WO-A', productCode: 'A-01' }
const orderB = { workorderId: 31, workorderCode: 'WO-B', productCode: 'A-01' }
const orderC = { workorderId: 38, workorderCode: 'WO-C', productCode: 'B-01' }
const orders = { kind: 'workorders', page: 1, page_size: 5, rows: [orderA, orderB, orderC] }
const orderDetail = { kind: 'workorder', id: 12, data: orderA, queried_at: '2026-11-14T01:00:00Z' }
const multi = [chat('long', 'user', '核对三张工单'),
  ...query('long', 'ktg_list_workorders', { page: 1, page_size: 5 }, orders),
  ...query('long', 'ktg_list_workorders', { page: 1, page_size: 20 }, { ...orders, page_size: 20 }),
  ...query('long', 'ktg_list_workorders', { workorder_code: 'WO', page: 1, page_size: 5 }, orders),
  ...query('long', 'ktg_pro_workorder_get', { workorder_id: 12 }, orderDetail),
  ...query('long', 'ktg_pro_workorder_get', { workorder_id: 12 }, { ...orderDetail, queried_at: '2026-11-14T02:00:00Z' }),
  ...query('long', 'ktg_pro_workorder_get', { workorder_id: 31 }, { kind: 'workorder', id: 31, data: orderB }),
  ...query('long', 'ktg_pro_workorder_get', { workorder_id: 38 }, { kind: 'workorder', id: 38, data: orderC }),
  ...query('long', 'ktg_pro_task_list', { workorder_id: 12 }, { kind: 'protask', rows: [], total: 0 }),
  ...query('long', 'ktg_pro_feedback_list', { workorder_id: 12 }, null, '读取超时'),
  ...query('long', 'ktg_query_stock_summary', { item_code: 'A-01' }, { kind: 'stock_summary', groups: [] }),
  ...query('long', 'ktg_query_stock', { item_code: 'B-01' }, { kind: 'stock', rows: [{ itemCode: 'B-01', quantityOnhand: 0 }] }),
  ...query('long', 'preview', {}, { kind: 'workorder_draft_preview' }),
  chat('long', 'assistant', '已汇总三张工单。WO-A 的报工待核实。')]
const detailEntries = queryCardPresentation(presentMessages(multi)).filter(entry => entry.queryDetails)
assert.strictEqual(detailEntries.length, 1)
const multiResult = detailEntries[0]
assert.match(multiResult.text, /报工待核实/)
assert.deepStrictEqual(multiResult.results.map(result => result.kind), ['workorder_draft_preview'])
const groups = Object.fromEntries(multiResult.queryDetails.groups.map(group => [group.title, group.queries]))
assert.strictEqual(multiResult.queryDetails.count, 11)
assert.strictEqual(multiResult.queryDetails.failed, 1)
assert.strictEqual(groups['通用查询'].length, 3)
assert.deepStrictEqual(groups['WO-A'][0].attempts.map(item => item.time), [orderDetail.queried_at, '2026-11-14T02:00:00Z'])
assert.strictEqual(groups['WO-A'][2].attempts[0].error, '读取超时')
assert.strictEqual(groups['产品 A-01'][0].attempts[0].result.kind, 'stock_summary')
assert.strictEqual(groups['WO-C'][1].attempts[0].result.kind, 'stock')

const single = [chat('single', 'user', '查 WO-A'),
  ...query('single', 'ktg_pro_workorder_get', { workorder_id: 12 }, orderDetail), chat('single', 'assistant', '工单 WO-A')]
assert.strictEqual(queryCardPresentation(presentMessages(single))[1].results[0].kind, 'workorder')
const resumed = [...multi, chat('resume', 'user', '继续此任务'),
  ...query('resume', 'update_task', {}, { task_id: 'task-a', version: 2 }),
  ...query('resume', 'ktg_pro_workorder_get', { workorder_id: 12 }, orderDetail), chat('resume', 'assistant', '续办完成')]
const restored = queryCardPresentation(presentMessages(JSON.parse(JSON.stringify(resumed))))
assert.deepStrictEqual(restored.filter(entry => entry.queryDetails).map(entry => [entry.runId, entry.queryDetails.count]), [['long', 11], ['resume', 1]])
const legacy = [message('old', 'tool', [], { details: orders }), message('old', 'tool', [], { details: orders })]
assert.strictEqual(queryCardPresentation(presentMessages(legacy))[0].queryDetails.groups[0].queries.length, 2)
const changed = [...query('changed', 'ktg_pro_workorder_get', { workorder_id: 12 }, orderDetail),
  ...query('changed', 'ktg_pro_workorder_get', { workorder_id: 12 }, { ...orderDetail, data: { ...orderA, quantity: 5 } })]
assert.strictEqual(queryCardPresentation(presentMessages(changed))[0].queryDetails.groups[0].queries.length, 2)
const resumeHistory = [
  ...query('task-start', 'create_task', {}, { task_id: 'tracked', version: 1 }),
  ...query('task-start', 'ktg_pro_workorder_get', { workorder_id: 12 }, orderDetail),
  ...query('different-task', 'create_task', {}, { task_id: 'other', version: 1 }),
  ...query('different-task', 'ktg_pro_workorder_get', { workorder_id: 31 }, { kind: 'workorder', data: orderB }),
  ...query('task-resume', 'ktg_query_stock_summary', { item_code: 'A-01' }, { kind: 'stock_summary', groups: [] }),
  ...query('task-resume', 'update_task', {}, { task_id: 'tracked', version: 2 }),
  chat('task-resume', 'assistant', '已补查 WO-A 的库存')
]
const resumedDetails = queryCardPresentation(presentMessages(resumeHistory)).at(-1).queryDetails
assert.deepStrictEqual(resumedDetails.groups.map(group => group.title), ['WO-A'])
assert.strictEqual(resumedDetails.count, 1)

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
assert.strictEqual(toolProgressText({ a: { name: 'ktg_pro_workorder_get', status: 'running' },
  b: { name: 'ktg_query_stock', status: 'running' } }), '正在查询工单详情 · 另有 1 项正在执行')
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
