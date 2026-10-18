const assert = require('assert')
const path = require('path')
const fs = require('fs')
const compiler = require('vue-template-compiler')
const { transformSync, transformFileSync } = require('@babel/core')

const presentationPath = path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js')
const presentationCode = transformFileSync(presentationPath, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const presentationModule = { exports: {} }
new Function('exports', 'module', 'require', presentationCode)(presentationModule.exports, presentationModule, require)

let createCount = 0
const sent = []
let failNextRun = false
let handlers
let pointer = null
let resolveWorkorder
let resolveInventory
let inputFailure = null
const controls = []
const client = {
  checkIdentity: () => true,
  getState: () => pointer,
  restoreSession: async () => null,
  setSession: sessionId => { pointer = { sessionId, runId: null } },
  setRun: runId => { pointer.runId = runId },
  createRun: async input => {
    sent.push({ ...input, sessionId: pointer.sessionId })
    if (failNextRun) {
      failNextRun = false
      throw new Error('network interrupted')
    }
    pointer.runId = `run-${sent.length}`
    return { run_id: pointer.runId, status: 'running' }
  },
  subscribeRun: async options => { handlers = options; return new Promise(() => {}) },
  listInputs: async () => controls.filter(input => input.requestId).map(input => ({
    request_id: input.requestId, mode: input.mode, message: input.message, status: 'queued'
  })),
  addInput: async input => {
    if (inputFailure) {
      const error = inputFailure
      inputFailure = null
      throw error
    }
    controls.push(input)
    return { input_status: 'queued' }
  },
  cancelRun: async requestId => { controls.push({ cancel: requestId }); return { status: 'cancelling' } },
  close: () => {}
}
const api = {
  agentSession: client,
  createAgentSession: async () => ({ session_id: `session-${++createCount}`, title: null }),
  listAgentSessions: async () => ({ items: [{ session_id: 'session-1', title: '第一轮' }] }),
  getAgentSession: async () => ({ session_id: 'session-1', active_run_id: null }),
  listAgentMessages: async () => ({ items: [
    { message_id: 1, run_id: 'run-1', role: 'user', content: [{ type: 'text', text: '第一轮' }] },
    { message_id: 2, run_id: 'run-1', role: 'tool', details: { kind: 'stock', rows: [], total: 0 }, content: [] },
    { message_id: 3, run_id: 'run-1', role: 'assistant', content: [{ type: 'text', text: '第一轮回答' }] }
  ], next_cursor: null }),
  getAgentRun: async runId => ({ run_id: runId, status: 'cancelling' }),
  confirmWorkorderDraft: async () => new Promise(resolve => { resolveWorkorder = resolve }),
  confirmInventoryOperation: async () => new Promise(resolve => { resolveInventory = resolve })
}
const componentPath = path.resolve(__dirname, '../src/components/AgentAssistant/index.vue')
const script = compiler.parseComponent(fs.readFileSync(componentPath, 'utf8')).script.content
const code = transformSync(script, { filename: componentPath + '.js', babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const componentModule = { exports: {} }
const mockRequire = name => {
  if (name === '@/api/agent') return api
  if (name === './sessionPresentation') return presentationModule.exports
  if (name === './workorderPresentation') return { isWorkorderAssistantResult: () => false }
  if (name.endsWith('.vue')) return {}
  if (name.includes('@babel/runtime/')) return require(name)
  if (name.startsWith('core-js/')) return require(name)
  throw new Error(`unexpected dependency: ${name}`)
}
const browserState = new Map()
let generatedIds = 0
global.window = { crypto: { randomUUID: () => `request-${++generatedIds}` },
  sessionStorage: {
    getItem: key => browserState.get(key) || null,
    setItem: (key, value) => browserState.set(key, value)
  } }
new Function('exports', 'module', 'require', code)(componentModule.exports, componentModule, mockRequire)
const options = componentModule.exports.default
const view = {
  ...options.data(),
  $route: { name: 'Stock', query: { itemCode: 'A-01' } },
  $store: { state: { user: { token: 'token', id: 7 } } },
  $refs: {},
  $nextTick: callback => callback()
}
for (const [name, method] of Object.entries(options.methods)) view[name] = method.bind(view)
Object.defineProperty(view, 'busy', { get: () => options.computed.busy.call(view) })
for (const name of ['canInput', 'canCancel', 'composerBusy', 'displayedMessages', 'displayedInputs']) {
  Object.defineProperty(view, name, { get: () => options.computed[name].call(view) })
}
assert.strictEqual(view.errorText({ response: { status: 500 } }, '提交状态未确认'), '提交状态未确认')

async function settle(number, answer) {
  const runId = `run-${number}`
  handlers.onEvent({ type: 'message_end', run_id: runId, payload: { message: { content: [{ type: 'text', text: answer }] } } })
  assert.strictEqual(view.activeRun.status, 'running')
  handlers.onEvent({ type: 'tool_execution_end', run_id: runId, payload: { tool_call_id: 'tool', status: 'succeeded' } })
  assert.strictEqual(view.activeRun.status, 'running')
  handlers.onEvent({ type: 'run_settled', run_id: runId, payload: { status: 'completed' } })
  assert.strictEqual(view.activeRun.status, 'running')
  handlers.onSnapshot({ run: { run_id: runId, status: 'completed' }, messages: { items: [
    { message_id: number * 2 - 1, run_id: runId, role: 'user', content: [{ type: 'text', text: sent[number - 1].message }] },
    { message_id: number * 2, run_id: runId, role: 'assistant', content: [{ type: 'text', text: answer }] }
  ] }, inputs: [] })
  assert.strictEqual(view.activeRun.status, 'completed')
}

async function main() {
  view.draft = '第一轮'
  await view.send()
  assert.strictEqual(createCount, 1)
  assert.strictEqual(sent[0].sessionId, 'session-1')
  assert.deepStrictEqual(sent[0].pageContext.filters, { itemCode: 'A-01' })
  view.draft = '追加查库存'
  await view.send()
  assert.strictEqual(controls[0].mode, 'follow_up')
  assert.strictEqual(view.runInputs[0].status, 'queued')
  view.draft = '优先查工单'
  view.inputMode = 'steer'
  await view.send()
  assert.strictEqual(controls[1].mode, 'steer')
  assert.strictEqual(sent.length, 1)
  inputFailure = { response: { status: 409, data: { error: { code: 'RUN_NOT_ACTIVE' } } } }
  view.draft = '未被接受'
  await view.send()
  assert.strictEqual(view.draft, '未被接受')
  assert.strictEqual(view.inputAttempt, null)
  view.activeRun.status = 'running'
  inputFailure = new Error('network interrupted')
  view.draft = '回执不明'
  await view.send()
  const uncertainId = view.inputAttempt.requestId
  view.activeRun.status = 'running'
  view.draft = '另一问题'
  await view.send()
  assert.strictEqual(view.inputAttempt.requestId, uncertainId)
  view.draft = '回执不明'
  await view.retryInput()
  assert.strictEqual(controls[2].requestId, uncertainId)
  assert.strictEqual(view.inputAttempt, null)
  await view.stopRun()
  assert.strictEqual(view.activeRun.status, 'cancelling')
  assert.ok(controls[3].cancel)
  view.activeRun.status = 'running'
  view.businessMessages.push({ id: 'business', role: 'assistant', text: '工单确认成功' })
  view.saveLocalState()
  handlers.onSnapshot({ run: { run_id: 'run-1', status: 'running' }, messages: { items: [] }, inputs: [] })
  assert.strictEqual(view.displayedMessages[0].text, '工单确认成功')
  const workorderConfirmation = view.confirmPreview('preview-1')
  const inventoryConfirmation = view.confirmInventory('inventory-1')
  handlers.onSnapshot({ run: { run_id: 'run-1', status: 'cancelled' }, messages: { items: [] }, inputs: [] })
  assert.strictEqual(view.confirmingPreviewId, 'preview-1')
  assert.strictEqual(view.confirmingInventoryId, 'inventory-1')
  resolveWorkorder({ result: { kind: 'workorder_draft_result', outcome: 'failed', label: '工单失败' } })
  resolveInventory({ result: { kind: 'inventory_result', operation_id: 'inventory-1', outcome: 'failed', label: '库存失败' } })
  await Promise.all([workorderConfirmation, inventoryConfirmation])
  assert.ok(view.displayedMessages.some(item => item.text === '工单失败'))
  assert.ok(view.displayedMessages.some(item => item.results && item.results[0].label === '库存失败'))
  view.activeRun.status = 'running'
  handlers.onConnectionError(new Error('offline'))
  assert.strictEqual(view.streamError, true)
  assert.match(view.panelError, /自动重连/)
  handlers.onConnectionRestored()
  assert.strictEqual(view.streamError, false)
  assert.strictEqual(view.panelError, '')
  await settle(1, '第一轮回答')
  view.runInputs = [{ request_id: 'not-consumed-1', mode: 'follow_up', message: '保留问题', status: 'not_consumed' }]
  view.draft = '第二轮'
  await view.send()
  assert.strictEqual(view.pastInputs[0].message, '保留问题')
  assert.strictEqual(createCount, 1)
  assert.strictEqual(sent[1].sessionId, 'session-1')
  await settle(2, '第二轮回答')
  failNextRun = true
  view.draft = '第三轮'
  await view.send()
  assert.strictEqual(view.draft, '第三轮')
  await view.send()
  assert.strictEqual(sent[2].requestId, sent[3].requestId)
  view.resetIdentity()
  assert.deepStrictEqual(view.messages, [])
  assert.strictEqual(view.sessionId, '')
  assert.strictEqual(view.activeRun, null)
  pointer = null
  await view.bootstrap()
  assert.strictEqual(view.sessionId, 'session-1')
  assert.strictEqual(view.businessMessages[0].text, '工单确认成功')
  assert.strictEqual(view.pastInputs[0].status, 'not_consumed')
  assert.strictEqual(view.messages[1].text, '第一轮回答')
  assert.strictEqual(view.messages[1].results[0].kind, 'stock')
  console.log('agent assistant component state tests passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
