const assert = require('assert')
const path = require('path')
const fs = require('fs')
const compiler = require('vue-template-compiler')
const { transformSync, transformFileSync } = require('@babel/core')

const presentationPath = path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js')
const presentationCode = transformFileSync(presentationPath, { plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const presentationModule = { exports: {} }
new Function('exports', 'module', 'require', presentationCode)(presentationModule.exports, presentationModule, require)

let createCount = 0
const sent = []
let failNextRun = false
let handlers
let pointer = null
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
  ], next_cursor: null })
}
const componentPath = path.resolve(__dirname, '../src/components/AgentAssistant/index.vue')
const script = compiler.parseComponent(fs.readFileSync(componentPath, 'utf8')).script.content
const code = transformSync(script, { filename: componentPath + '.js', plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
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
global.window = { crypto: { randomUUID: () => `request-${sent.length + 1}` } }
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
  ] } })
  assert.strictEqual(view.activeRun.status, 'completed')
}

async function main() {
  view.draft = '第一轮'
  await view.send()
  assert.strictEqual(createCount, 1)
  assert.strictEqual(sent[0].sessionId, 'session-1')
  assert.deepStrictEqual(sent[0].pageContext.filters, { itemCode: 'A-01' })
  await settle(1, '第一轮回答')
  view.draft = '第二轮'
  await view.send()
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
  assert.strictEqual(view.messages[1].text, '第一轮回答')
  assert.strictEqual(view.messages[1].results[0].kind, 'stock')
  console.log('agent assistant component state tests passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
