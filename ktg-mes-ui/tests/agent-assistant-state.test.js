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
let inputFailure = null
let runFailure = null
let taskVersions = [1]
let statsReads = 0
let operationSnapshot = []
const operationActions = []
const statsSnapshot = { context: { configured: true, window_tokens: 8192,
  threshold_tokens: 6553, estimated_tokens: 100, estimate_kind: 'estimated',
  summary_id: null, spill_count: 0 } }
const controls = []
const client = {
  checkIdentity: () => true,
  getState: () => pointer,
  restoreSession: async () => null,
  setSession: sessionId => { pointer = { sessionId, runId: null } },
  setRun: runId => { pointer.runId = runId },
  createRun: async input => {
    sent.push({ ...input, sessionId: pointer.sessionId })
    if (runFailure) {
      const failure = runFailure
      runFailure = null
      throw failure
    }
    if (failNextRun) {
      failNextRun = false
      throw new Error('network interrupted')
    }
    pointer.runId = `run-${sent.length}`
    return { run_id: pointer.runId, status: 'queued' }
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
  getAgentSessionStats: async () => { statsReads++; return statsSnapshot },
  listAgentTasks: async () => ({ items: [
    { task_id: 'task-a', title: '任务甲' }, { task_id: 'task-b', title: '任务乙' }
  ] }),
  listAgentOperations: async () => ({ items: operationSnapshot }),
  actOnAgentOperation: async (sessionId, planId, action, digest) => {
    operationActions.push({ sessionId, planId, action, digest })
    const updated = { ...operationSnapshot.find(plan => plan.plan_id === planId), status: 'succeeded', confirmable: false }
    operationSnapshot = [updated]
    return updated
  },
  getAgentTask: async (_, taskId) => ({
    task_id: taskId,
    current_version: taskVersions[0],
    versions: taskVersions.map(version => ({ version, steps: [
      { step_id: 'stable', description: '核对结果', status: version === 1 ? 'pending' : 'completed' }
    ] }))
  }),
  listAgentMessages: async () => ({ items: [
    { message_id: 1, run_id: 'run-1', role: 'user', content: [{ type: 'text', text: '第一轮' }] },
    { message_id: 2, run_id: 'run-1', role: 'tool', details: { kind: 'stock', rows: [], total: 0 }, content: [] },
    { message_id: 3, run_id: 'run-1', role: 'assistant', content: [{ type: 'text', text: '第一轮回答' }] }
  ], next_cursor: null }),
  getAgentRun: async runId => ({ run_id: runId, status: 'cancelling' })
}
const componentPath = path.resolve(__dirname, '../src/components/AgentAssistant/index.vue')
const script = compiler.parseComponent(fs.readFileSync(componentPath, 'utf8')).script.content
const code = transformSync(script, { filename: componentPath + '.js', babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const componentModule = { exports: {} }
const mockRequire = name => {
  if (name === '@/api/agent') return api
  if (name === './sessionPresentation') return presentationModule.exports
  if (name.endsWith('.vue')) return {}
  if (name.includes('@babel/runtime/')) return require(name)
  if (name.startsWith('core-js/')) return require(name)
  throw new Error(`unexpected dependency: ${name}`)
}
const browserState = new Map()
let generatedIds = 0
const listeners = new Map()
global.window = { innerWidth: 1200, innerHeight: 900,
  setTimeout, clearTimeout,
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: name => listeners.delete(name),
  localStorage: { getItem: key => browserState.get(key) || null, setItem: (key, value) => browserState.set(key, value) },
  crypto: { randomUUID: () => `request-${++generatedIds}` },
  sessionStorage: {
    getItem: key => browserState.get(key) || null,
    setItem: (key, value) => browserState.set(key, value)
  } }
new Function('exports', 'module', 'require', code)(componentModule.exports, componentModule, mockRequire)
const options = componentModule.exports.default
const templateContent = compiler.parseComponent(fs.readFileSync(componentPath, 'utf8')).template.content
assert.deepStrictEqual(compiler.compile(templateContent).errors, [])
assert.deepStrictEqual(Object.keys(options.components), ['AgentResultCard', 'AgentAnswerContent', 'AgentOperationPlanCard'])
assert.ok(!Object.keys(options.methods).some(name => /^(confirm|refreshInventory|continueExploration|viewWorkorder)/.test(name)))
assert.ok(!Object.keys(options.data()).some(name => /^(confirming|completed.*Ids)/.test(name)))
for (const match of templateContent.matchAll(/<(agent-[a-z0-9-]+)/g)) {
  const tag = match[1]
  const camel = tag.replace(/-([a-z0-9])/g, (_, char) => char.toUpperCase())
  const pascal = camel.charAt(0).toUpperCase() + camel.slice(1)
  assert.ok(options.components[pascal], `unregistered component tag: ${tag} -> ${pascal}`)
}
const view = {
  ...options.data(),
  $route: { name: 'Stock', query: { itemCode: 'A-01' } },
  $store: { state: { user: { token: 'token', id: 7 } } },
  $refs: {},
  $nextTick: callback => callback()
}
for (const [name, method] of Object.entries(options.methods)) view[name] = method.bind(view)
Object.defineProperty(view, 'busy', { get: () => options.computed.busy.call(view) })
for (const name of ['canInput', 'canCancel', 'composerBusy', 'operationPresentation', 'displayedMessages', 'displayedInputs']) {
  Object.defineProperty(view, name, { get: () => options.computed[name].call(view) })
}
Object.defineProperty(view, 'contextStatus', { get: () => options.computed.contextStatus.call(view) })
assert.strictEqual(view.errorText({ response: { status: 500 } }, '提交状态未确认'), '提交状态未确认')
const older = { querySelector: () => ({}), getBoundingClientRect: () => ({ top: 40 }) }
const latest = { querySelector: () => ({}), getBoundingClientRect: () => ({ top: 180 }) }
const tableOnly = { querySelector: () => null }
const scrollContainer = { scrollTop: 20, scrollHeight: 900,
  querySelectorAll: () => [older, latest, tableOnly], getBoundingClientRect: () => ({ top: 100 }) }
view.$refs.messages = scrollContainer
view.scrollToLatestAnswer()
assert.strictEqual(scrollContainer.scrollTop, 92, '打开历史或运行完毕应定位最新答复的开头，而不是表格底部')
delete view.$refs.messages

async function settle(number, answer) {
  const runId = `run-${number}`
  if (view.activeRun.status === 'queued') handlers.onEvent({ type: 'agent_start', run_id: runId, payload: {} })
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
  assert.strictEqual(view.activeRun.status, 'queued')
  assert.strictEqual(view.composerBusy, true)
  handlers.onEvent({ type: 'agent_start', run_id: 'run-1', payload: {} })
  assert.strictEqual(view.activeRun.status, 'running')
  handlers.onEvent({ type: 'message_start', run_id: 'run-1', payload: { message_id: 'run-1:message:1', role: 'assistant' } })
  handlers.onEvent({ type: 'message_update', run_id: 'run-1', payload: { message_id: 'run-1:message:1', delta_type: 'text', text: '首段' } })
  assert.strictEqual(view.progress.messages[0].text, '首段')
  handlers.onEvent({ type: 'message_update', run_id: 'run-1', payload: { message_id: 'run-1:message:1', delta_type: 'text', text: '后段' } })
  assert.strictEqual(view.progress.messages[0].text, '首段后段')
  const originalRunRead = api.getAgentRun
  api.getAgentRun = async runId => ({ run_id: runId, status: 'running', last_seq: 8,
    usage: { known_total_tokens: 7, complete: true } })
  handlers.onEvent({ type: 'model_usage_reported', run_id: 'run-1', payload: { input_tokens: 5, output_tokens: 2 } })
  await new Promise(resolve => setImmediate(resolve))
  assert.strictEqual(view.activeRun.usage.known_total_tokens, 7)
  handlers.onEvent({ type: 'model_usage_reported', run_id: 'run-1', payload: { input_tokens: 5, output_tokens: 2 } })
  await new Promise(resolve => setImmediate(resolve))
  assert.strictEqual(view.activeRun.usage.known_total_tokens, 7)
  api.getAgentRun = originalRunRead
  assert.strictEqual(view.canInput, true)
  assert.strictEqual(view.composerBusy, false)
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
  assert.strictEqual(view.inputAttempt.status, 'rejected')
  assert.strictEqual(view.inputAttempt.reason, 'RUN_NOT_ACTIVE')
  assert.strictEqual(view.displayedInputs.find(input => input.message === '未被接受').request_id,
    view.inputAttempt.requestId)
  assert.match(view.displayedInputs.find(input => input.message === '未被接受').status, /rejected/)
  view.inputAttempt = null
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
  handlers.onSnapshot({ run: { run_id: 'run-1', status: 'cancelled' }, messages: { items: [] }, inputs: [] })
  assert.strictEqual(view.progress.messages[0].status, 'incomplete')
  assert.ok(view.displayedMessages.some(item => item.text === '工单确认成功'))
  assert.strictEqual(view.confirmPreview, undefined)
  assert.strictEqual(view.confirmInventory, undefined)
  view.activeRun.status = 'running'
  handlers.onConnectionError(new Error('offline'))
  assert.strictEqual(view.streamError, true)
  assert.match(view.panelError, /自动重连/)
  handlers.onConnectionRestored()
  assert.strictEqual(view.streamError, false)
  assert.strictEqual(view.panelError, '')
  await new Promise(resolve => setImmediate(resolve))
  assert.strictEqual(view.contextStatus, statsSnapshot.context)
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
  assert.strictEqual(view.contextStatus, statsSnapshot.context)
  assert.ok(statsReads >= 2)
  assert.strictEqual(view.businessMessages[0].text, '工单确认成功')
  assert.strictEqual(view.pastInputs[0].status, 'not_consumed')
  assert.strictEqual(view.messages[1].text, '第一轮回答')
  assert.strictEqual(view.messages[1].results[0].kind, 'stock')
  // A confirmed rejection keeps its original identity until the user chooses a new run.
  for (const code of ['RUN_NOT_RUNNING', 'RUN_NOT_ACTIVE', 'REQUEST_ID_CONFLICT']) {
    view.resetIdentity()
    pointer = { sessionId: 'session-1', runId: 'run-1' }
    view.sessionId = 'session-1'
    view.activeRunId = 'run-1'
    view.activeRun = { run_id: 'run-1', status: 'running' }
    view.draft = `拒绝 ${code}`
    inputFailure = { response: { status: 409, data: { error: { code } } } }
    const before = controls.length
    const runPosts = sent.length
    await view.send()
    const attempt = view.inputAttempt
    assert.strictEqual(attempt.runId, 'run-1')
    assert.strictEqual(attempt.message, `拒绝 ${code}`)
    assert.strictEqual(attempt.status, code === 'REQUEST_ID_CONFLICT' ? 'conflict' : 'rejected')
    assert.strictEqual(view.displayedInputs.find(input => input.request_id === attempt.requestId).status,
      attempt.status)
    assert.strictEqual(controls.length, before)
    assert.strictEqual(sent.length, runPosts)
    await view.send()
    assert.strictEqual(controls.length, before)
    assert.strictEqual(sent.length, runPosts)
    if (code !== 'REQUEST_ID_CONFLICT') {
      view.activeRun.status = 'cancelled'
      view.resendInput(view.displayedInputs.find(input => input.request_id === attempt.requestId))
      await new Promise(resolve => setImmediate(resolve))
      assert.strictEqual(sent[sent.length - 1].message, attempt.message)
      assert.notStrictEqual(sent[sent.length - 1].requestId, attempt.requestId)
      assert.ok(view.pastInputs.some(input => input.request_id === attempt.requestId))
    }
  }
  // Recovered server inputs resolve only their own pending draft, for live and terminal runs.
  for (const status of ['running', 'completed']) {
    view.resetIdentity()
    pointer = { sessionId: 'session-1', runId: 'run-recovered' }
    const attempt = { runId: 'run-recovered', requestId: `recovered-${status}`,
      mode: 'follow_up', message: '已接受的输入' }
    browserState.set('ktg-agent-view:7:session-1', JSON.stringify({ inputAttempt: attempt }))
    api.getAgentRun = async runId => ({ run_id: runId, status })
    let releaseInputs
    client.listInputs = () => new Promise(resolve => { releaseInputs = resolve })
    const boot = view.bootstrap()
    while (!releaseInputs) await new Promise(resolve => setImmediate(resolve))
    view.draft = '稍后写的新草稿'
    releaseInputs([{ request_id: attempt.requestId, mode: attempt.mode,
      message: attempt.message, status: 'consumed' }])
    await boot
    assert.strictEqual(view.inputAttempt, null)
    assert.strictEqual(view.draft, '稍后写的新草稿')
    assert.strictEqual(view.displayedInputs.filter(input => input.request_id === attempt.requestId).length, 1)
    assert.strictEqual(view.runInputs[0].status, 'consumed')
  }
  view.resetIdentity()
  pointer = { sessionId: 'session-1', runId: 'run-unconfirmed' }
  const uncertain = { runId: 'run-unconfirmed', requestId: 'retry-after-refresh',
    mode: 'follow_up', message: '回执丢失' }
  browserState.set('ktg-agent-view:7:session-1', JSON.stringify({ inputAttempt: uncertain }))
  api.getAgentRun = async runId => ({ run_id: runId, status: 'running' })
  client.listInputs = async () => { throw new Error('read failed') }
  await view.bootstrap()
  assert.strictEqual(view.inputAttempt.requestId, uncertain.requestId)
  assert.strictEqual(view.displayedInputs.find(input => input.request_id === uncertain.requestId).status,
    'unconfirmed')
  // Names are durable API results; cancelled/failed/stale saves never invent a title.
  view.resetIdentity()
  view.sessionId = 'session-1'
  view.sessions = [{ session_id: 'session-1', title: 'original' }]
  const beforeCreate = createCount
  view.editTitle('create')
  view.cancelTitle()
  assert.strictEqual(createCount, beforeCreate)
  let renameCalls = 0
  api.renameAgentSession = async (id, title) => { renameCalls++; return { session_id: id, title } }
  view.editTitle('rename')
  view.titleDraft = '   '
  await view.saveTitle()
  assert.ok(view.titleError)
  view.titleDraft = '中'.repeat(200) + '😀'
  await view.saveTitle()
  assert.strictEqual(renameCalls, 0)
  view.titleDraft = '  ' + '中'.repeat(199) + '😀  '
  await view.saveTitle()
  assert.strictEqual(view.sessions[0].title, '中'.repeat(199) + '😀')
  api.renameAgentSession = async () => { throw new Error('offline') }
  view.editTitle('rename')
  view.titleDraft = 'failed'
  const oldTitle = view.sessions[0].title
  await view.saveTitle()
  assert.strictEqual(view.sessions[0].title, oldTitle)
  assert.ok(view.titleError)
  let releaseRename
  api.renameAgentSession = (id, title) => new Promise(resolve => { releaseRename = () => resolve({ session_id: id, title }) })
  for (const changeIdentity of [false, true]) {
    view.sessionId = 'session-1'
    view.sessions = [{ session_id: 'session-1', title: 'original' }]
    view.editTitle('rename')
    view.titleDraft = 'stale'
    const saving = view.saveTitle()
    if (changeIdentity) view.resetIdentity()
    else { view.epoch++; view.cancelTitle(); view.sessionId = 'session-2' }
    releaseRename()
    await saving
    assert.ok(!view.sessions.some(item => item.title === 'stale'))
    assert.strictEqual(view.titleSaving, false)
  }
  api.createAgentSession = async () => { throw new Error('offline') }
  view.editTitle('create')
  view.titleDraft = 'new'
  await view.saveTitle()
  assert.ok(view.titleError)
  assert.strictEqual(view.titleSaving, false)
  api.createAgentSession = async title => ({ session_id: `session-${++createCount}`, title })
  client.listInputs = async () => []
  view.titleDraft = '  named new  '
  await view.saveTitle()
  assert.ok(view.sessions.some(item => item.title === 'named new'))
  assert.strictEqual(view.titleMode, '')

  const { clampPanelSize } = componentModule.exports
  assert.deepStrictEqual(clampPanelSize({ width: 1000, height: 1000 }, { width: 800, height: 600 }), { width: 752, height: 552 })
  view.updateViewport()
  view.panelSize = { width: 390, height: 620 }
  view.startResize({ button: 0, clientX: 800, clientY: 200 })
  view.moveResize({ clientX: 600, clientY: 100 })
  assert.deepStrictEqual(view.panelSize, { width: 590, height: 720 })
  view.closePanel()
  assert.strictEqual(listeners.size, 0)
  assert.deepStrictEqual(JSON.parse(browserState.get('ktg-agent-panel-size')), view.panelSize)
  view.openPanel()
  assert.deepStrictEqual(view.panelSize, { width: 590, height: 720 })
  view.viewport = { width: 750, height: 500 }
  assert.deepStrictEqual(options.computed.panelStyle.call(view), { width: '590px', height: '452px' })
  view.viewport = { width: 375, height: 600 }
  assert.deepStrictEqual(options.computed.panelStyle.call(view), {})
  view.startResize({ button: 0, clientX: 10, clientY: 10 })
  assert.strictEqual(listeners.size, 0)
  view.viewport = { width: 1200, height: 900 }
  view.resizeKey({ key: 'ArrowLeft', preventDefault() {} })
  assert.strictEqual(view.panelSize.width, 610)
  const originalStorage = window.localStorage
  window.localStorage = { setItem() { throw new Error('blocked') } }
  assert.doesNotThrow(() => view.persistSize())
  window.localStorage = originalStorage
  // A fresh mounted component restores preferences; malformed storage stays usable.
  for (const [stored, expected] of [
    [JSON.stringify({ width: 700, height: 750 }), { width: 700, height: 750 }],
    ['{broken', { width: 390, height: 620 }],
    [JSON.stringify({ width: null, height: 750 }), { width: 390, height: 620 }],
    [JSON.stringify({ width: -1, height: 750 }), { width: 390, height: 620 }]
  ]) {
    browserState.set('ktg-agent-panel-size', stored)
    let unsubscribed = false
    const mountedView = { ...options.data(), $store: { subscribe: () => () => { unsubscribed = true } } }
    for (const [name, method] of Object.entries(options.methods)) mountedView[name] = method.bind(mountedView)
    mountedView.bootstrap = () => {}
    options.mounted.call(mountedView)
    assert.deepStrictEqual(mountedView.panelSize, expected)
    mountedView.startResize({ button: 0, clientX: 800, clientY: 200 })
    assert.ok(listeners.has('pointermove'))
    options.beforeDestroy.call(mountedView)
    assert.strictEqual(listeners.size, 0)
    assert.ok(unsubscribed)
  }
  window.localStorage = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  const blockedView = { ...options.data(), $store: { subscribe: () => () => {} } }
  for (const [name, method] of Object.entries(options.methods)) blockedView[name] = method.bind(blockedView)
  blockedView.bootstrap = () => {}
  assert.doesNotThrow(() => options.mounted.call(blockedView))
  options.beforeDestroy.call(blockedView)
  window.localStorage = originalStorage
  // A bare continuation prompts for a choice, while the explicit action preserves
  // the selected task ID across request retries and reads persisted versions.
  view.inputAttempt = null
  view.activeRun = null
  view.loading = false
  view.sessionLoading = false
  view.pendingSubmission = null
  await view.refreshTasks()
  assert.strictEqual(view.taskId, 'task-a')
  assert.strictEqual(view.taskDetail.current_version, 1)
  runFailure = { response: { data: { error: { code: 'TASK_SELECTION_REQUIRED' } } } }
  view.draft = '继续'
  await view.send()
  assert.strictEqual(view.pendingSubmission, null)
  assert.match(view.panelError, /多个未完成任务/)
  await view.selectTask('task-b')
  failNextRun = true
  await view.continueTask()
  assert.strictEqual(view.pendingSubmission.taskId, 'task-b')
  await view.retryPendingRun()
  assert.strictEqual(sent.at(-1).taskId, 'task-b')
  assert.strictEqual(sent.at(-1).requestId, sent.at(-2).requestId)
  assert.strictEqual(sent.at(-1).message, '继续')
  taskVersions = [2, 1]
  handlers.onEvent({ type: 'tool_execution_end', run_id: view.activeRunId,
    payload: { tool_name: 'update_task', status: 'succeeded', result: { task_id: 'task-b', version: 2 } } })
  await new Promise(resolve => setImmediate(resolve))
  assert.strictEqual(view.taskDetail.current_version, 2)
  view.taskVersion = 1
  assert.strictEqual(options.computed.visibleTaskVersion.call(view).steps[0].status, 'pending')
  const pendingPlan = { plan_id: 'test-plan', digest: 'sha256:plan', status: 'pending', confirmable: true, run_id: view.activeRunId,
    steps: [{ action: 'create_draft', step_id: 'create' }], results: [{ status: 'pending' }] }
  operationSnapshot = [pendingPlan]
  await view.refreshOperationPlans()
  await view.handleOperationAction({ ...pendingPlan, digest: 'tampered' }, 'confirm')
  assert.strictEqual(operationActions.length, 0)
  await view.handleOperationAction(pendingPlan, 'confirm')
  assert.deepStrictEqual(operationActions, [{ sessionId: view.sessionId, planId: 'test-plan', action: 'confirm', digest: 'sha256:plan' }])
  await view.handleOperationAction(view.operationPlans[0], 'confirm')
  assert.strictEqual(operationActions.length, 1)
  assert.strictEqual(view.operationPlans[0].status, 'succeeded')
  view.resetOperations()
  assert.deepStrictEqual(view.operationPlans, [])
  const operationCard = compiler.parseComponent(fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/OperationPlanCard.vue'), 'utf8'))
  assert.deepStrictEqual(compiler.compile(operationCard.template.content).errors, [])
  console.log('agent assistant component state tests passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
