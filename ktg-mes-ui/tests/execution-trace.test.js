const assert = require('assert')
const fs = require('fs')
const vm = require('vm')
const compiler = require('vue-template-compiler')

const dir = 'src/views/mes/pro/execution/trace/'
function component(name, globals = {}) {
  const s = compiler.parseComponent(fs.readFileSync(dir + name + '.vue', 'utf8'))
  assert.deepStrictEqual(compiler.compile(s.template.content).errors, [])
  const ctx = { module: {}, taskLabel: value => value, stepLabel: value => value, hasTracePermission: () => true, traceError: (e, fallback) => e.message || fallback, ...globals }
  vm.runInNewContext(s.script.content.replace(/^import .*$/mg, '').replace('export default', 'module.exports ='), ctx)
  return ctx.module.exports
}
function instance(component) {
  return Object.assign({ ...component.data(), $emit() {} }, component.methods)
}
function deferred() {
  let resolve, reject
  const promise = new Promise((r, j) => { resolve = r; reject = j })
  return { promise, resolve, reject }
}

async function verify() {
  let calls = []
  const unit = instance(component('UnitTracePanel', { traceExecutionUnit: async params => { calls.push(params); return { data: { snCode: params.snCode, traceStatus: 'LEGACY_NO_RECORD', tasks: [], inputs: [], inspections: [], dispositions: [] } } }, UnitFacts: {}, TraceDocumentDialog: {} }))
  unit.result = { snCode: 'PREVIOUS' }
  let closed = 0
  unit.$refs = { document: { close() { closed++ } } }
  unit.snCode = '  '
  await unit.search()
  assert.strictEqual(unit.result, null, 'invalid input must clear earlier result')
  assert(unit.error)
  assert.strictEqual(calls.length, 0)
  await unit.search(' LEGACY-1 ')
  assert.strictEqual(closed, 2, 'both invalid and valid new searches close previous document details')
  assert.strictEqual(calls[0].snCode, ' LEGACY-1 ', 'SN identity is passed raw')
  assert.strictEqual(unit.result.traceStatus, 'LEGACY_NO_RECORD', 'legacy identity is not replaced with guessed facts')

  const delayed = deferred()
  const newer = deferred()
  const asyncUnit = instance(component('UnitTracePanel', { traceExecutionUnit: params => params.snCode === 'FIRST' ? delayed.promise : newer.promise, UnitFacts: {}, TraceDocumentDialog: {} }))
  const first = asyncUnit.search('FIRST')
  const second = asyncUnit.search('SECOND')
  newer.resolve({ data: { snCode: 'SECOND', traceStatus: 'TRACKED' } })
  await second
  delayed.resolve({ data: { snCode: 'FIRST', traceStatus: 'TRACKED' } })
  await first
  assert.strictEqual(asyncUnit.result.snCode, 'SECOND', 'later query takes precedence')
  const failure = deferred()
  const failingUnit = instance(component('UnitTracePanel', { traceExecutionUnit: () => failure.promise, UnitFacts: {}, TraceDocumentDialog: {} }))
  failingUnit.result = { snCode: 'PREVIOUS' }
  const pending = failingUnit.search('UNKNOWN')
  assert.strictEqual(failingUnit.result, null)
  failure.reject(new Error('查询失败'))
  await pending
  assert.strictEqual(failingUnit.result, null)
  assert.strictEqual(failingUnit.error, '查询失败')

  calls = []
  const material = instance(component('MaterialTracePanel', { traceExecutionMaterials: async params => { calls.push(params); return { rows: [{ snCode: 'L03' }, { snCode: 'L04' }, { snCode: 'L05' }], total: 3 } }, ItemSelect: {} }))
  material.itemId = '12.5'; material.batchCode = 'B07'
  await material.search()
  assert(material.error)
  assert.strictEqual(calls.length, 0)
  material.itemId = '9007199254740993'; material.batchCode = ' éb07 '
  await material.search()
  assert.strictEqual(calls[0].itemId, '9007199254740993', 'long IDs should not lose precision')
  assert.strictEqual(calls[0].batchCode, ' éb07 ', 'batch bytes, case, accent and padding must be preserved')
  assert.deepStrictEqual(material.rows.map(row => row.snCode), ['L03', 'L04', 'L05'])
  material.itemId = '20'; material.batchCode = 'B08'; material.pageNum = 2
  await material.load()
  assert.strictEqual(calls[1].itemId, '9007199254740993', 'pagination uses the submitted criteria')
  assert.strictEqual(calls[1].batchCode, ' éb07 ')
  assert.strictEqual(calls[1].pageNum, 2)
  material.onItemSelected({ itemId: 9007199254740992 })
  assert.strictEqual(material.itemId, '', 'legacy selector unsafe number is not accepted')
  assert(material.error)
  material.onItemSelected({ itemId: '9007199254740993' })
  assert.strictEqual(material.itemId, '9007199254740993')
  material.reset()
  assert.strictEqual(material.total, 0)
  assert.strictEqual(material.rows.length, 0)
  assert.strictEqual(material.criteria, null)

  const matFailure = instance(component('MaterialTracePanel', { traceExecutionMaterials: async () => { throw Error('连接失败') }, ItemSelect: {} }))
  matFailure.itemId = '12'; matFailure.batchCode = 'B07'; matFailure.rows = [{ snCode: 'OLD' }]; matFailure.total = 1
  await matFailure.search()
  assert.strictEqual(matFailure.rows.length, 0)
  assert.strictEqual(matFailure.total, 0)
  assert.strictEqual(matFailure.error, '连接失败')

  const emptyPage = instance(component('MaterialTracePanel', { traceExecutionMaterials: async params => ({ rows: params.pageNum === 1 ? [{ snCode: 'VISIBLE' }] : [], total: 1 }), ItemSelect: {} }))
  emptyPage.itemId = '12'; emptyPage.batchCode = 'B07'
  await emptyPage.search()
  emptyPage.pageNum = 2
  await emptyPage.load()
  assert.strictEqual(emptyPage.rows.length, 0)
  assert.strictEqual(emptyPage.total, 1, 'empty page is not a zero-result query')
  await emptyPage.firstPage()
  assert.strictEqual(emptyPage.pageNum, 1)
  assert.strictEqual(emptyPage.rows[0].snCode, 'VISIBLE')

  const pendingDocument = deferred()
  let loaded = 0
  const document = instance(component('TraceDocumentDialog', {
    getIpqc(id) { loaded++; assert.strictEqual(id, '9007199254740993'); return pendingDocument.promise },
    getFeedback: () => { throw Error('no permission') },
    hasTracePermission: (permissions, permission) => permissions.includes(permission)
  }))
  document.$store = { getters: { permissions: [] } }
  await document.open('ipqc', '9007199254740993')
  assert.strictEqual(loaded, 0, 'no IPQC permission means no detail request')
  document.$store.getters.permissions = ['mes:qc:ipqc:query']
  const documentRequest = document.open('ipqc', '9007199254740993')
  document.close()
  pendingDocument.resolve({ data: { ipqcId: '9007199254740993' } })
  await documentRequest
  assert.strictEqual(document.record, null, 'closed/stale document responses are ignored')
  const preciseDocument = instance(component('TraceDocumentDialog', { getIpqc: async () => ({ data: { ipqcId: 9007199254740992 } }), getFeedback: async () => ({ data: { recordId: 9007199254740992 } }), hasTracePermission: () => true }))
  preciseDocument.$store = { getters: { permissions: [] } }
  await preciseDocument.open('ipqc', '9007199254740993')
  assert.strictEqual(preciseDocument.record.ipqcId, '9007199254740993')
  await preciseDocument.open('feedback', '9007199254740995')
  assert.strictEqual(preciseDocument.record.recordId, '9007199254740995')

  for (const name of ['index', 'UnitFacts', 'TraceDocumentDialog']) {
    const s = compiler.parseComponent(fs.readFileSync(dir + name + '.vue', 'utf8'))
    assert.deepStrictEqual(compiler.compile(s.template.content).errors, [])
  }
  const src = fs.readFileSync('src/api/mes/pro/execution.js', 'utf8')
  assert(src.includes('`${base}/units/trace`') && src.includes('`${base}/materials/trace`'))
  console.log('Execution trace: validation, legacy, failure clearing, stale query protection, exact material ID, pagination, and Vue templates passed.')
}
verify().catch(e => { console.error(e); process.exitCode = 1 })
