const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync, transformFileSync } = require('@babel/core')

function loadComponent(relative, dependencies = {}) {
  const filename = path.resolve(__dirname, '..', relative)
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  assert.deepStrictEqual(compiler.compile(parsed.template.content).errors, [], `${relative} template compiles`)
  const code = transformSync(parsed.script.content, { filename: `${filename}.js`, babelrc: false,
    configFile: false, plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const loaded = { exports: {} }
  new Function('exports', 'module', 'require', code)(loaded.exports, loaded,
    name => dependencies[name] === undefined ? {} : dependencies[name])
  return { options: loaded.exports.default, parsed }
}

const presentationPath = path.resolve(__dirname, '../src/components/AgentAssistant/sessionPresentation.js')
const presentationCode = transformFileSync(presentationPath, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const presentationModule = { exports: {} }
new Function('exports', 'module', 'require', presentationCode)(presentationModule.exports, presentationModule, require)

const { optionCardPresentation, presentMessages } = presentationModule.exports
const items = [
  { message_id: 101, run_id: 'one', role: 'user', content: [{ type: 'text', text: '有哪些类型' }] },
  { message_id: 102, run_id: 'one', role: 'tool', details: { kind: 'tm_tool_options', options: { tool_types: [{ toolTypeId: 1 }], statuses: [] }, queried_at: 'first' } },
  { message_id: 103, run_id: 'two', role: 'user', content: [{ type: 'text', text: '再说一次' }] },
  { message_id: 104, run_id: 'two', role: 'tool', details: { kind: 'tm_tool_options', options: { statuses: [], tool_types: [{ toolTypeId: 1 }] }, queried_at: 'later' } },
  { message_id: 105, run_id: 'three', role: 'tool', details: { kind: 'tm_tooltype_options', options: { tool_types: [{ toolTypeId: 1 }], statuses: [] } } },
  { message_id: 106, run_id: 'four', role: 'tool', details: { kind: 'tm_tool_options', options: { tool_types: [{ toolTypeId: 2 }], statuses: [] } } },
  { message_id: 107, run_id: 'five', role: 'user', content: [{ type: 'text', text: '请重新查询最新的工装夹具基础选项，并展开给我看。' }] },
  { message_id: 108, run_id: 'five', role: 'tool', details: { kind: 'tm_tool_options', options: { tool_types: [{ toolTypeId: 2 }], statuses: [] } } },
  { message_id: 109, run_id: 'six', role: 'tool', details: { kind: 'tm_tool_preview', preview_id: 'write-1', action: 'update' } }
]
const history = presentMessages(items)
const views = optionCardPresentation(history)
const cards = views.flatMap(item => item.resultPresentation || [])
assert.strictEqual(cards[0].open, true, 'first necessary options remain visible')
assert.strictEqual(cards[1].open, false, 'timestamps and object key order do not create business changes')
assert.match(cards[1].summary, /相同/)
assert.strictEqual(cards[2].open, true, 'different result kinds are independent')
assert.strictEqual(cards[3].open, true, 'changed business data remains visible')
assert.match(cards[3].summary, /更新/)
assert.strictEqual(cards[4].open, true, 'explicit fresh query remains open even when equal')
assert.strictEqual(cards[5], null, 'write preview never participates')
const expanded = optionCardPresentation(presentMessages(items), { [cards[1].id]: true })
assert.strictEqual(expanded[3].resultPresentation[0].open, true, 'expansion survives history reconstruction')
assert.deepStrictEqual(optionCardPresentation(presentMessages(items)), views)
assert.strictEqual(views.flatMap(item => item.results || []).length, 6, 'no results removed')
const filtered = JSON.parse(JSON.stringify(items))
filtered[3].details.filters = { scope: 'different' }
assert.strictEqual(optionCardPresentation(presentMessages(filtered))[3].resultPresentation[0].open, true,
  'different filters never count as repeats')

async function main() {
  assert.strictEqual(presentationModule.exports.pageContext({ path: '/tm/tool' }).module, 'tm')

  const card = loadComponent('src/components/AgentAssistant/TmToolCard.vue').options
  assert.strictEqual(card.computed.title.call({ preview: true, result: { kind: 'tm_tool_preview', action: 'update' } }), '工装夹具修改预览')
  assert.strictEqual(card.methods.statusLabel('STORE'), '在库')
  assert.strictEqual(card.methods.display(0), '0')
  assert.strictEqual(card.computed.statusText.call({ result: { outcome: 'unknown', verified: false } }), '操作结果未知，请核对后再继续。')

  const assistantApi = { confirmTmTool: async () => ({ result: {
    kind: 'tm_tool_create_result', action: 'create', outcome: 'succeeded', verified: true,
    verified_record: { toolId: 23 }, label: 'done'
  } }) }
  const assistant = loadComponent('src/components/AgentAssistant/index.vue', {
    '@/api/agent': assistantApi,
    './sessionPresentation': presentationModule.exports,
    './workorderPresentation': { isWorkorderAssistantResult: () => false }
  }).options
  assert.ok(assistant.components.AgentTmToolCard)
  const events = []
  global.CustomEvent = function (type, options) { this.type = type; this.detail = options.detail }
  global.window = { dispatchEvent: event => events.push(event) }
  const saved = new Map()
  window.sessionStorage = { setItem: (key, value) => saved.set(key, value), getItem: key => saved.get(key) }
  const foldView = { ...assistant.data(), sessionId: 'options-session', $store: { state: { user: { id: 7 } } },
    $set(target, key, value) { target[key] = value } }
  foldView.localKey = assistant.methods.localKey.bind(foldView)
  foldView.saveLocalState = assistant.methods.saveLocalState.bind(foldView)
  assistant.methods.toggleOptions.call(foldView, cards[1])
  assert.strictEqual(foldView.expandedOptions[cards[1].id], true)
  foldView.expandedOptions = {}
  assistant.methods.restoreLocalState.call(foldView)
  assert.strictEqual(foldView.expandedOptions[cards[1].id], true, 'user expansion survives a history reload')
  assistant.methods.toggleOptions.call(foldView, { ...cards[1], open: true })
  assert.strictEqual(foldView.expandedOptions[cards[1].id], false, 'user can collapse an expanded result')
  foldView.sessionId = 'another-session'
  foldView.expandedOptions = {}
  assistant.methods.restoreLocalState.call(foldView)
  assert.deepStrictEqual(foldView.expandedOptions, {}, 'preferences do not cross sessions')
  const view = { ...assistant.data(), epoch: 1, saveLocalState() {}, scrollToBottom() {} }
  await assistant.methods.confirmTmToolPreview.call(view, 'preview-1')
  await assistant.methods.confirmTmToolPreview.call(view, 'preview-1')
  assert.strictEqual(events.length, 1, 'a preview can be confirmed only once')
  assert.strictEqual(events[0].type, 'ktg:tm-tool-changed')
  assert.strictEqual(events[0].detail.tool_id, 23)

  assistantApi.confirmTmTool = async () => ({ result: {
    kind: 'tm_tool_update_result', action: 'update', outcome: 'unknown', verified: false,
    verified_record: { toolId: 23 }, label: 'uncertain'
  } })
  await assistant.methods.confirmTmToolPreview.call(view, 'preview-2')
  assert.strictEqual(events.length, 1, 'unknown confirmation does not refresh the ledger')

  let resolveDetail
  const page = loadComponent('src/views/mes/tm/tool/index.vue', {
    '@/api/mes/tm/tool': {
      listTool() {}, getTool() { return new Promise(resolve => { resolveDetail = resolve }) },
      delTool() {}, addTool() {}, updateTool() {}
    },
    '@/api/mes/tm/tooltype': { listAllTooltype() {} }
  }).options
  const calls = []
  const pageVm = {
    open: true, optType: 'edit', form: { toolId: 23 },
    getList: () => calls.push('refresh'), cancel: () => calls.push('close')
  }
  page.methods.handleAgentToolChanged.call(pageVm, { detail: {
    action: 'update', tool_id: 23, result: { outcome: 'succeeded', verified: true }
  } })
  assert.deepStrictEqual(calls, ['refresh'])
  pageVm.form = { toolId: 24 }
  resolveDetail({ data: { toolId: 23, toolName: 'late update' } })
  await Promise.resolve()
  assert.strictEqual(pageVm.form.toolId, 24, 'late detail read cannot replace a different selected row')

  calls.length = 0
  page.methods.handleAgentToolChanged.call(pageVm, { detail: {
    action: 'delete', tool_id: 23, result: { outcome: 'unknown', verified: false }
  } })
  assert.deepStrictEqual(calls, [], 'unknown result does not refresh or close a row')

  calls.length = 0
  pageVm.form = { toolId: 23 }
  page.methods.handleAgentToolChanged.call(pageVm, { detail: {
    action: 'delete', tool_id: 23, result: { outcome: 'succeeded', verified: true }
  } })
  assert.deepStrictEqual(calls, ['close', 'refresh'])
  console.log('tool ledger card, trusted confirmation, unknown outcome and refresh behavior passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
