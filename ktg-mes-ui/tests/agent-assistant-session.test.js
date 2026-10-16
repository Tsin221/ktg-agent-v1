const assert = require('assert')
const path = require('path')
const { transformFileSync } = require('@babel/core')

const filename = path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js')
const transformed = transformFileSync(filename, { plugins: ['@babel/plugin-transform-modules-commonjs'] })
const moduleRef = { exports: {} }
new Function('exports', 'module', 'require', transformed.code)(moduleRef.exports, moduleRef, require)
const { pageContext, presentMessages, progressFromEvent, isTerminalRun, runStatusText } = moduleRef.exports

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

let progress = { text: '', tools: {} }
progress = progressFromEvent(progress, { type: 'message_update', payload: { delta_type: 'text', text: '正在查' } })
progress = progressFromEvent(progress, { type: 'tool_execution_start', payload: { tool_call_id: 't1', tool_name: 'query_stock' } })
progress = progressFromEvent(progress, { type: 'tool_execution_end', payload: { tool_call_id: 't1', status: 'succeeded' } })
assert.strictEqual(progress.text, '正在查')
assert.strictEqual(progress.tools.t1.status, 'succeeded')
assert.strictEqual(isTerminalRun('running'), false)
assert.strictEqual(isTerminalRun('completed'), true)
assert.strictEqual(runStatusText({ status: 'needs_reconciliation' }), '运行中断，请核对业务操作结果')

console.log('agent assistant session presentation tests passed')
