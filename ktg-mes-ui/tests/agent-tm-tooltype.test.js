const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync, transformFileSync } = require('@babel/core')

function loadComponent(relative, dependencies = {}) {
  const filename = path.resolve(__dirname, '..', relative)
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  assert.deepStrictEqual(compiler.compile(parsed.template.content).errors, [], `${relative} template compiles`)
  const code = transformSync(parsed.script.content, { filename: filename + '.js', babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
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
  assert.strictEqual(presentationModule.exports.pageContext({ path: '/tm/tooltype' }).module, 'tm')
  assert.strictEqual(presentationModule.exports.pageContext({ path: '/md/vendor' }).module, undefined)

  const card = loadComponent('src/components/AgentAssistant/TmToolTypeCard.vue').options
  const list = { result: { kind: 'tm_tooltype_list', total: 2, page: 1, rows: [
    { toolTypeId: 2, toolTypeCode: 'TT002', toolTypeName: '按次', codeFlag: 'Y', maintenType: 'USAGE', maintenPeriod: 8 },
    { toolTypeId: 3, toolTypeCode: 'TT003', toolTypeName: '无周期', codeFlag: 'N', maintenType: null, maintenPeriod: null }
  ] } }
  assert.strictEqual(card.computed.title.call(list), '工装夹具类型查询结果')
  assert.strictEqual(card.methods.maintenance.call({ display: card.methods.display }, list.result.rows[0]), '按使用次数维护 · 8次')
  assert.strictEqual(card.methods.maintenance.call({ display: card.methods.display }, list.result.rows[1]), '无编码管理 · 无维护周期')
  const detail = { result: { kind: 'tm_tooltype_detail', tool_type: list.result.rows[0] } }
  assert.strictEqual(card.computed.record.call(detail).toolTypeCode, 'TT002')
  const update = { result: { kind: 'tm_tooltype_preview', action: 'update', preview_id: 'p1',
    before: { toolTypeId: 2, toolTypeName: '旧名', toolTypeCode: 'TT002', codeFlag: 'Y', maintenType: 'USAGE', maintenPeriod: 8, remark: '' },
    after: { toolTypeId: 2, toolTypeName: '新名', toolTypeCode: 'TT002', codeFlag: 'Y', maintenType: 'USAGE', maintenPeriod: 8, remark: '' } } }
  const updateVm = { result: update.result }
  updateVm.record = card.computed.record.call(updateVm)
  updateVm.before = card.computed.before.call(updateVm)
  updateVm.preview = card.computed.preview.call(updateVm)
  assert.strictEqual(updateVm.preview, true)
  assert.deepStrictEqual(card.computed.visibleFields.call(updateVm).map(field => field.key), ['toolTypeName'])
  const typeChange = { result: { kind: 'tm_tooltype_preview', action: 'update', preview_id: 'p2',
    before: { toolTypeId: 2, toolTypeCode: 'TT002', toolTypeName: '按次', codeFlag: 'Y', maintenType: 'USAGE', maintenPeriod: 8 },
    after: { toolTypeId: 2, toolTypeCode: 'TT002', toolTypeName: '按次', codeFlag: 'Y', maintenType: 'REGULAR', maintenPeriod: 30 } } }
  assert.strictEqual(card.methods.display.call({ record: typeChange.result.before }, 8, 'maintenPeriod', typeChange.result.before), '8次')
  assert.strictEqual(card.methods.display.call({ record: typeChange.result.after }, 30, 'maintenPeriod', typeChange.result.after), '30天')
  assert.strictEqual(card.computed.statusText.call({ result: { outcome: 'succeeded', verified: false } }), 'MES返回成功，但尚未完成回读核验。')
  assert.strictEqual(card.computed.isRecordResult.call({ result: { kind: 'tm_tooltype_delete_result' } }), true)

  let calls = 0
  let confirmation = { result: { kind: 'tm_tooltype_create_result', action: 'create', outcome: 'succeeded', verified: true,
    created: { toolTypeId: 41 }, label: 'ok' } }
  const api = { confirmTmToolType: async () => { calls++; return confirmation } }
  const assistant = loadComponent('src/components/AgentAssistant/index.vue', {
    '@/api/agent': api,
    './sessionPresentation': presentationModule.exports,
    './workorderPresentation': { isWorkorderAssistantResult: () => false }
  }).options
  assert.ok(assistant.components.AgentTmToolTypeCard)
  for (const kind of ['tm_tooltype_list', 'tm_tooltype_detail', 'tm_tooltype_options', 'tm_tooltype_preview', 'tm_tooltype_delete_result']) {
    assert.strictEqual(assistant.methods.isTmToolTypeResult({ kind }), true)
  }
  const events = []
  global.CustomEvent = function(type, options) { this.type = type; this.detail = options.detail }
  global.window = { dispatchEvent: event => events.push(event) }
  const view = { ...assistant.data(), epoch: 1, saveLocalState() {}, scrollToBottom() {} }
  await assistant.methods.confirmTmToolTypePreview.call(view, 'preview-a')
  await assistant.methods.confirmTmToolTypePreview.call(view, 'preview-a')
  assert.strictEqual(calls, 1)
  assert.strictEqual(events.length, 1)
  assert.strictEqual(events[0].type, 'ktg:tm-tooltype-changed')
  assert.strictEqual(events[0].detail.tool_type_id, 41)
  confirmation = { result: { kind: 'tm_tooltype_update_result', action: 'update', outcome: 'succeeded', verified: false, updated: { toolTypeId: 42 } } }
  await assistant.methods.confirmTmToolTypePreview.call(view, 'preview-b')
  assert.strictEqual(events.length, 1, 'unverified response does not trigger list refresh')
  confirmation = { result: { kind: 'tm_tooltype_delete_result', action: 'delete', outcome: 'unknown', verified: false, deleted: { toolTypeId: 43 } } }
  await assistant.methods.confirmTmToolTypePreview.call(view, 'preview-c')
  assert.strictEqual(events.length, 1, 'unknown response does not trigger list refresh')
  assert.ok(view.completedTmToolTypePreviewIds.includes('preview-c'))

  let resolveDetail
  const page = loadComponent('src/views/mes/tm/tooltype/index.vue', {
    '@/api/mes/tm/tooltype': { listTooltype() {}, getTooltype() { return new Promise(resolve => { resolveDetail = resolve }) }, delTooltype() {}, addTooltype() {}, updateTooltype() {} },
    '@/api/system/autocode/rule': { genCode() {} }
  }).options
  const callsOnPage = []
  const pageVm = { open: true, form: { toolTypeId: 9 }, getList: () => callsOnPage.push('refresh'), cancel: () => callsOnPage.push('close') }
  page.methods.handleAgentToolTypeChanged.call(pageVm, { detail: { action: 'delete', tool_type_id: 9,
    result: { outcome: 'succeeded', verified: true } } })
  assert.deepStrictEqual(callsOnPage, ['close', 'refresh'])
  callsOnPage.length = 0
  page.methods.handleAgentToolTypeChanged.call(pageVm, { detail: { action: 'delete', tool_type_id: 8,
    result: { outcome: 'succeeded', verified: true } } })
  assert.deepStrictEqual(callsOnPage, ['refresh'], 'deleting a different record does not close the open record')
  callsOnPage.length = 0
  page.methods.handleAgentToolTypeChanged.call(pageVm, { detail: { action: 'update', tool_type_id: 9,
    result: { outcome: 'unknown', verified: false } } })
  assert.deepStrictEqual(callsOnPage, [], 'unknown and unverified results do not refresh')

  const pendingVm = { open: true, optType: 'edit', form: { toolTypeId: 9, toolTypeName: '当前表单' },
    getList() {}, cancel() {} }
  page.methods.handleAgentToolTypeChanged.call(pendingVm, { detail: { action: 'update', tool_type_id: 9,
    result: { outcome: 'succeeded', verified: true } } })
  pendingVm.form = { toolTypeId: 8, toolTypeName: '用户已切换' }
  resolveDetail({ data: { toolTypeId: 9, toolTypeName: 'Agent新值' } })
  await Promise.resolve()
  assert.strictEqual(pendingVm.form.toolTypeId, 8, 'late readback must not overwrite a different selected record')

  const closedVm = { open: true, optType: 'edit', form: { toolTypeId: 9 }, getList() {}, cancel() {} }
  page.methods.handleAgentToolTypeChanged.call(closedVm, { detail: { action: 'update', tool_type_id: 9,
    result: { outcome: 'succeeded', verified: true } } })
  closedVm.open = false
  resolveDetail({ data: { toolTypeId: 9, toolTypeName: '关闭后不写回' } })
  await Promise.resolve()
  assert.strictEqual(closedVm.form.toolTypeName, undefined, 'late readback must not reopen/replace a closed dialog')

  const sameVm = { open: true, optType: 'edit', form: { toolTypeId: 9 }, getList() {}, cancel() {} }
  page.methods.handleAgentToolTypeChanged.call(sameVm, { detail: { action: 'update', tool_type_id: 9,
    result: { outcome: 'succeeded', verified: true } } })
  resolveDetail({ data: { toolTypeId: 9, toolTypeName: '已同步详情' } })
  await Promise.resolve()
  assert.strictEqual(sameVm.form.toolTypeName, '已同步详情')

  let registered
  let removed
  global.window.addEventListener = (name, handler) => { registered = [name, handler] }
  global.window.removeEventListener = (name, handler) => { removed = [name, handler] }
  page.mounted.call(pageVm)
  page.beforeDestroy.call(pageVm)
  assert.strictEqual(registered[0], 'ktg:tm-tooltype-changed')
  assert.deepStrictEqual(removed, registered, 'page removes its listener when destroyed')

  const agentApiSource = fs.readFileSync(path.resolve(__dirname, '../src/api/agent.js'), 'utf8')
  assert.ok(agentApiSource.includes("url: '/agent-api/v1/tm-tooltype/confirm'"))
  assert.ok(agentApiSource.includes('data: { preview_id: previewId }'))
  console.log('tool type agent card, confirmation, route context and refresh tests passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
