const assert = require('assert')
const fs = require('fs')
const path = require('path')
const Vue = require('vue')
const babel = require('@babel/core')
const compiler = require('vue-template-compiler')
const source = fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/ExplorationResult.vue'), 'utf8')
const parsed = compiler.parseComponent(source)
const script = babel.transformSync(parsed.script.content, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const calls = []
let response = () => Promise.resolve({ result: { kind: 'exploration_record', headline: '已完工，尚未验收', coverage: 'complete', records: [] } })
const moduleObject = { exports: {} }
new Function('exports', 'module', 'require', script)(moduleObject.exports, moduleObject,
  name => name === '@/api/agent' ? { getAgentExplorationView: (...args) => { calls.push(args); return response() } } : require(name))
const options = moduleObject.exports.default
const compiled = compiler.compileToFunctions(parsed.template.content)
function vmFor(result, sessionId = 'session-a', selectionRequired = false) {
  return new Vue({ ...options, propsData: { result, sessionId, selectionRequired }, render: compiled.render, staticRenderFns: compiled.staticRenderFns })
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ')
  return !node || typeof node !== 'object' ? '' : [node.text || '', ...(node.children || []).map(text)].join(' ')
}
async function main() {
  const vm = vmFor({ kind: 'exploration_search', headline: '找到两台设备，请选择', coverage: 'partial',
    records: [{ id: 1, name: '装配机', code: 'R-001', workshop_name: '装配车间', record_ref: 'rec_private' }] })
  const rendered = text(vm._render())
  assert.match(rendered, /找到两台设备/)
  assert.match(rendered, /仅已查范围/)
  assert.match(rendered, /装配车间/)
  assert.doesNotMatch(rendered, /rec_private/)
  await vm.readRecord(vm.records[0])
  assert.deepStrictEqual(calls[0], ['session-a', 'ktg_read_record', { record_ref: 'rec_private' }])
  assert.strictEqual(vm.detail.headline, '已完工，尚未验收')
  assert.ok(vm.safeRoute('/mes/dv/repair'))
  for (const route of ['https://evil.test', '//evil.test', '/\\evil.test', 'javascript:alert(1)']) assert.strictEqual(vm.safeRoute(route), null)
  vm.$destroy()

  // A material lookup may also find stock and BOM records. Their respective
  // fields must not be merged into a table full of fake missing values.
  const mixed = vmFor({ kind: 'exploration_search', coverage: 'complete', records: [
    { id: 1, entity: 'md.item', code: 'BRG-6204', name: '深沟球轴承', specification: '6204 / 20×47×14 mm', record_ref: 'rec_item' },
    { id: 2, entity: 'md.item_bom', bom_item_code: 'BRG-6204', bom_item_name: '深沟球轴承', bom_item_spec: '6204 / 20×47×14 mm', quantity: 2, record_ref: 'rec_bom', can_read: false },
    { id: 3, entity: 'wm.stock', item_code: 'BRG-6204', item_name: '深沟球轴承', warehouse_name: '原料仓', quantity_onhand: 200, record_ref: 'rec_stock' },
    { id: 4, entity: 'wm.stock', item_code: 'BRG-6204', item_name: '深沟球轴承', warehouse_name: '线边仓', quantity_onhand: 0, record_ref: 'rec_zero' }
  ] })
  const mixedText = text(mixed._render())
  assert.match(mixedText, /产品物料/)
  assert.match(mixedText, /产品物料清单/)
  assert.match(mixedText, /现存库存/)
  assert.match(mixedText, /原料仓/)
  assert.match(mixedText, /线边仓/)
  assert.match(mixedText, /\b0\b/)
  assert.doesNotMatch(mixedText, /未填写|rec_item|rec_bom|rec_stock/)
  assert.strictEqual(mixed.format(undefined, 'status'), '—')
  assert.strictEqual(mixed.format(null, 'status'), '未填写')
  let selection
  mixed.$on('selected', event => { selection = event })
  await mixed.readRecord(mixed.records[0])
  assert.strictEqual(selection.resume, false, 'viewing evidence must not repeat an answered question')
  const choosing = vmFor(mixed.result, 'session-a', true)
  choosing.$on('selected', event => { selection = event })
  assert.match(text(choosing._render()), /选择/)
  await choosing.readRecord(choosing.records[0])
  assert.strictEqual(selection.resume, true, 'an explicit selection continues the pending question')
  choosing.$destroy()
  mixed.$destroy()

  const listOnly = vmFor({ kind: 'exploration_query', coverage: 'complete', records: [
    { id: 5, entity: 'md.item_bom', bom_item_name: '轴承', record_ref: 'rec_list_only', can_read: false, available_actions: [] }] })
  assert.strictEqual(!!listOnly.canRead(listOnly.records[0]), false)
  const before = calls.length
  await listOnly.readRecord(listOnly.records[0])
  assert.strictEqual(calls.length, before)
  assert.strictEqual(!!listOnly.canShowFlow({ record_ref: 'rec_hidden', entity: 'pro.task', available_actions: [] }), false)
  assert.deepStrictEqual(listOnly.originalRoute({ entity: 'dv.repair_line', id: 7, repair_id: 2 }), { path: '/mes/dv/repair', query: { repairId: '2' } })
  listOnly.$destroy()

  const sql = vmFor({ kind: 'exploration_sql', coverage: 'partial', columns: ['车间', '总数'], rows: [['装配', 0]] })
  assert.match(text(sql._render()), /装配/)
  assert.match(text(sql._render()), /0/)
  sql.$destroy()

  const analysis = vmFor({ kind: 'exploration_analysis', summary: '装配车间维修最多', coverage: 'partial', tables: [
    { title: '按车间统计', columns: [{ name: '车间' }, { name: '维修次数' }], rows: [['装配', 3]] }] })
  assert.match(text(analysis._render()), /维修最多/)
  assert.match(text(analysis._render()), /装配/)
  analysis.$destroy()

  const sourceAssistant = fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/index.vue'), 'utf8')
  assert.match(sourceAssistant, /<exploration-result/)
  assert.match(sourceAssistant, /:session-id="sessionId"/)
  assert.doesNotMatch(sourceAssistant, /输入不会撤销已执行的操作/)

  const flow = vmFor({ kind: 'exploration_flow', headline: '正在生产', coverage: 'partial', nodes: [
    { id: 'workorder', label: '工单', state: 'completed', record_refs: [] },
    { id: 'quality', label: '质检', state: 'not_found', record_refs: [] },
    { id: 'receipt', label: '入库', state: 'no_permission', record_refs: [] }] })
  const flowText = text(flow._render())
  assert.match(flowText, /工单/)
  assert.match(flowText, /未查到记录/)
  assert.match(flowText, /无权限/)
  assert.doesNotMatch(flowText, /未开始/)
  flow.$destroy()

  response = () => Promise.reject(new Error('hidden transport failure rec_secret'))
  const failed = vmFor({ kind: 'exploration_search', records: [] })
  await failed.readRecord({ record_ref: 'rec_failed' })
  assert.ok(failed.error)
  assert.doesNotMatch(failed.error, /rec_secret/)
  assert.strictEqual(failed.busy, false)
  failed.$destroy()

  let finish
  response = () => new Promise(resolve => { finish = resolve })
  const pending = vmFor({ kind: 'exploration_search', records: [], coverage: 'complete' })
  const loading = pending.readRecord({ record_ref: 'rec_old' })
  pending.sessionId = 'session-b'
  await Vue.nextTick()
  finish({ result: { kind: 'exploration_record', headline: '旧会话私密结果' } })
  await loading
  assert.strictEqual(pending.detail, null)
  pending.$destroy()
  console.log('Exploration results: conclusion, state evidence, private ref interaction and stale-session isolation verified')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
