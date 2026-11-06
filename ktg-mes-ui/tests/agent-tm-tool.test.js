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
