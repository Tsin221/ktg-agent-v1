const assert = require('assert')
const path = require('path')
const fs = require('fs')
const Vue = require('vue')
const compiler = require('vue-template-compiler')

const componentPath = path.resolve(__dirname, '../src/views/mes/tm/tooltype/index.vue')
const source = fs.readFileSync(componentPath, 'utf8')
const descriptor = compiler.parseComponent(source)
const compiledTemplate = compiler.compile(descriptor.template.content)
const compiledFunctions = compiler.compileToFunctions(descriptor.template.content)
assert.deepStrictEqual(compiledTemplate.errors, [], 'tool type page template compiles')

const nodeScript = descriptor.script.content
  .replace(/^import\s+({[^}]+})\s+from\s+(['"][^'"]+['"]);?/gm, 'const $1 = require($2);')
  .replace(/^export default/m, 'module.exports.default =')
const loadedModule = { exports: {} }
const calls = { deletedIds: null, confirmText: '' }
const api = {
  listTooltype: () => Promise.resolve({ rows: [], total: 0 }),
  getTooltype: () => Promise.resolve({ data: {} }),
  delTooltype: ids => {
    calls.deletedIds = ids
    return Promise.resolve()
  },
  addTooltype: () => Promise.resolve(),
  updateTooltype: () => Promise.resolve()
}
const dependencies = {
  '@/api/mes/tm/tooltype': api,
  '@/api/system/autocode/rule': { genCode: () => Promise.resolve('T09-AUTO') }
}
new Function('module', 'require', nodeScript)(
  loadedModule,
  name => {
    if (!dependencies[name]) throw new Error(`Unexpected import: ${name}`)
    return dependencies[name]
  }
)
const component = loadedModule.exports.default

function formContext(codeFlag) {
  const context = Object.assign({}, component.methods)
  Object.assign(context, component.data.call(context))
  context.form.codeFlag = codeFlag
  return context
}

function ruleAccepts(context, property, value) {
  const rule = context.rules[property] && context.rules[property][0]
  if (!rule) return false
  if (rule.validator) {
    let error = null
    rule.validator({}, value, result => { error = result || null })
    return error === null
  }
  if (rule.pattern instanceof RegExp) return rule.pattern.test(value == null ? '' : String(value))
  return false
}

const validPeriods = ['1', '10', '2147483647', 1, 2147483647]
const invalidPeriods = ['', ' ', null, undefined, '0', '000', '-1', '1.5', 'abc', '2147483648', 0, -1, 1.5, 2147483648]
for (const value of validPeriods) {
  assert.strictEqual(ruleAccepts(formContext('Y'), 'maintenPeriod', value), true, `accept period ${value}`)
}
for (const value of invalidPeriods) {
  assert.strictEqual(ruleAccepts(formContext('Y'), 'maintenPeriod', value), false, `reject period ${value}`)
}
assert.strictEqual(ruleAccepts(formContext('N'), 'maintenPeriod', ''), true, 'N does not require a hidden period')
assert.strictEqual(ruleAccepts(formContext('N'), 'maintenType', null), true, 'N does not require a hidden type')
for (const value of ['REGULAR', 'USAGE']) {
  assert.strictEqual(ruleAccepts(formContext('Y'), 'maintenType', value), true, `accept maintenance type ${value}`)
}
for (const value of ['', ' ', 'OTHER', 'regular', null]) {
  assert.strictEqual(ruleAccepts(formContext('Y'), 'maintenType', value), false, `reject maintenance type ${value}`)
}

const switched = formContext('Y')
switched.form.maintenType = 'REGULAR'
switched.form.maintenPeriod = '7'
let clearedFields = null
switched.$refs = { form: { clearValidate(fields) { clearedFields = fields } } }
switched.$nextTick = callback => callback()
component.methods.handleCodeFlagChange.call(switched)
assert.strictEqual(switched.form.maintenType, null, 'switching code flag clears maintenance type')
assert.strictEqual(switched.form.maintenPeriod, null, 'switching code flag clears maintenance period')
assert.deepStrictEqual(clearedFields, ['maintenType', 'maintenPeriod'], 'switch clears hidden-field validation state')

const formatPeriod = component.methods.formatMaintenancePeriod
const formatType = component.methods.formatMaintenanceType
assert.strictEqual(typeof formatPeriod, 'function', 'page formats period values explicitly')
assert.strictEqual(typeof formatType, 'function', 'page formats maintenance types explicitly')
assert.strictEqual(formatPeriod.call(component.methods, 'N', 'USAGE', 10), '无')
assert.strictEqual(formatPeriod.call(component.methods, 'Y', 'REGULAR', null), '未设置')
assert.strictEqual(formatPeriod.call(component.methods, 'Y', 'REGULAR', 0), '未设置')
assert.strictEqual(formatPeriod.call(component.methods, 'Y', 'USAGE', 7), '7次')
assert.strictEqual(formatType.call({}, 'Y', null), '未设置')

function renderFormInputsDisabled(optType) {
  const page = new Vue({
    data() {
      return {
        queryParams: { toolTypeCode: '', toolTypeName: '', maintenType: null },
        tooltypeList: [], loading: false, total: 0,
        single: true, multiple: true, showSearch: true,
        title: '测试', open: true, optType,
        autoGenFlag: false,
        form: { toolTypeCode: 'T09-READONLY', toolTypeName: '测试', codeFlag: 'Y',
          maintenType: 'REGULAR', maintenPeriod: '3', remark: '备注' },
        rules: component.data.call(component.methods).rules,
        dict: { type: { mes_mainten_type: [], sys_yes_no: [] } }
      }
    },
    render: compiledFunctions.render,
    staticRenderFns: compiledFunctions.staticRenderFns,
    methods: component.methods
  })
  const root = page._render()
  const controls = []
  function visit(node) {
    if (!node) return
    if (Array.isArray(node)) return node.forEach(visit)
    if (node.tag === 'el-input' || node.tag === 'el-select' || node.tag === 'el-radio-group') controls.push(node)
    visit(node.children)
    if (node.componentOptions) visit(node.componentOptions.children)
  }
  function findDialog(node) {
    if (!node) return null
    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findDialog(child)
        if (found) return found
      }
      return null
    }
    if (node.tag === 'el-dialog') return node
    return findDialog(node.children)
  }
  visit(findDialog(root))
  return controls.map(node => {
    const props = node.componentOptions ? node.componentOptions.propsData : null
    const attrs = node.data && node.data.attrs
    return !!((props && props.disabled) || (attrs && attrs.disabled))
  })
}
const viewDisabled = renderFormInputsDisabled('view')
const editDisabled = renderFormInputsDisabled('edit')
assert(viewDisabled.length >= 6, 'view mode renders all editable fields')
assert(viewDisabled.every(Boolean), 'every form control is disabled in view mode')
assert(editDisabled.every(value => !value), 'edit mode keeps all form controls available')

async function verifyDeletePrompt(rows, expectedCodes, ids) {
  calls.confirmText = ''
  calls.deletedIds = null
  const context = Object.assign({}, component.methods, {
    ids: [],
    selectedRows: [],
    getList() {},
    $modal: {
      confirm(text) {
        calls.confirmText = text
        return Promise.resolve()
      },
      msgSuccess() {}
    }
  })
  if (rows.length > 1) component.methods.handleSelectionChange.call(context, rows)
  component.methods.handleDelete.call(context, rows.length === 1 ? rows[0] : { type: 'click' })
  await new Promise(resolve => setImmediate(resolve))
  for (const code of expectedCodes) assert(calls.confirmText.includes(code), `confirmation includes ${code}`)
  assert(!calls.confirmText.includes('99'), 'confirmation does not expose the internal ID')
  assert.deepStrictEqual(calls.deletedIds, ids, 'delete request still uses internal ID(s)')
}

verifyDeletePrompt(
  [{ toolTypeId: 99, toolTypeCode: 'T09-SINGLE', toolTypeName: '单删测试' }],
  ['T09-SINGLE'],
  99
).then(() => verifyDeletePrompt(
  [
    { toolTypeId: 99, toolTypeCode: 'T09-ALPHA', toolTypeName: '批删甲' },
    { toolTypeId: 100, toolTypeCode: 'T09-BETA', toolTypeName: '批删乙' }
  ],
  ['T09-ALPHA', 'T09-BETA'],
  [99, 100]
)).then(async () => {
  calls.deletedIds = null
  const context = Object.assign({}, component.methods, {
    selectedRows: [],
    $modal: { confirm: () => Promise.reject(new Error('cancel')), msgSuccess() {} },
    getList() {}
  })
  await component.methods.handleDelete.call(context, {
    toolTypeId: 101, toolTypeCode: 'T09-CANCEL', toolTypeName: '取消删除'
  })
  assert.strictEqual(calls.deletedIds, null, 'cancelling confirmation does not send the delete request')
  console.log('tool type validation tests passed')
}).catch(error => {
  console.error(error)
  process.exitCode = 1
})
