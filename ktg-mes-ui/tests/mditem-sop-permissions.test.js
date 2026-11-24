const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync } = require('@babel/core')

const root = path.join(__dirname, '../src/views/mes/md/mditem')
let grants = []
const checkPermi = permissions => grants.includes('*:*:*') || permissions.some(p => grants.includes(p))
const calls = []
const deps = {
  '@/utils/permission': { checkPermi },
  '@/api/mes/md/sop': {
    listSop: async () => { calls.push('sop'); return { rows: [] } },
    getSop: async () => { calls.push('detail'); return { data: { sopId: 1 } } }
  },
  '@/api/mes/pro/process': {
    listAllProcess: async () => { calls.push('process'); return { data: [] } }
  }
}

function load(file) {
  const parsed = compiler.parseComponent(fs.readFileSync(path.join(root, file), 'utf8'))
  const compiled = compiler.compile(parsed.template.content)
  assert.deepStrictEqual(compiled.errors, [])
  const code = transformSync(parsed.script.content, {
    babelrc: false, configFile: false, plugins: ['@babel/plugin-transform-modules-commonjs']
  }).code
  const loaded = { exports: {} }
  new Function('exports', 'module', 'require', code)(loaded.exports, loaded, name => deps[name] || {})
  return { options: loaded.exports.default, ast: compiled.ast }
}

function find(node, predicate) {
  if (predicate(node)) return node
  for (const child of node.children || []) {
    const match = find(child, predicate)
    if (match) return match
  }
}

async function main() {
  for (const file of ['index.vue', 'form.vue']) {
    const { options, ast } = load(file)
    const sop = find(ast, node => node.attrsMap && node.attrsMap.label === 'SOP')
    assert.strictEqual(sop.if, 'canReadSop', `${file}: unauthorized SOP must not mount`)
    grants = ['mes:md:mditem:query', 'mes:md:bom:list']
    assert.strictEqual(options.computed.canReadSop(), false)
    grants = ['mes:md:sop:list']
    assert.strictEqual(options.computed.canReadSop(), true)
  }

  const { options, ast } = load('components/sop.vue')
  function instance(optType) {
    const vm = { itemId: 7, optType, resetForm() {} }
    Object.assign(vm, options.data.call(vm))
    Object.entries(options.methods).forEach(([key, fn]) => { vm[key] = fn.bind(vm) })
    Object.entries(options.computed || {}).forEach(([key, fn]) => {
      Object.defineProperty(vm, key, { get: () => fn.call(vm) })
    })
    return vm
  }
  grants = ['mes:md:mditem:query', 'mes:md:bom:list']
  calls.length = 0
  options.created.call(instance('view'))
  assert.deepStrictEqual(calls, [], 'unauthorized component must not request protected resources')
  grants = ['mes:md:sop:list', 'mes:pro:process:list', 'mes:md:sop:add', 'mes:md:sop:edit', 'mes:md:sop:query', 'mes:md:sop:remove']
  calls.length = 0
  const viewer = instance('view')
  options.created.call(viewer)
  assert.deepStrictEqual(calls, ['sop'], 'view mode only requests SOP list')
  assert.strictEqual(viewer.canAddSop, false)
  assert.strictEqual(viewer.canEditSop, false)
  assert.strictEqual(viewer.canDeleteSop, false)
  const editor = instance('edit')
  calls.length = 0
  options.created.call(editor)
  assert.deepStrictEqual(calls, ['sop'], 'editing mode also waits until form opens to load processes')
  editor.handleAdd()
  assert.deepStrictEqual(calls, ['sop', 'process'])
  assert.ok(editor.open)
  calls.length = 0
  editor.handleUpdate({ sopId: 1 })
  await Promise.resolve()
  assert.deepStrictEqual(calls, ['detail', 'process'])
  assert.ok(editor.open)
  grants = ['mes:md:sop:list', 'mes:md:sop:add', 'mes:md:sop:edit', 'mes:md:sop:query']
  calls.length = 0
  const limited = instance('edit')
  limited.handleAdd()
  limited.handleUpdate({ sopId: 1 })
  assert.deepStrictEqual(calls, [], 'no permission for process selector, no editing requests')
  assert.strictEqual(limited.open, false)
  for (const [action, guard] of [['handleAdd', 'canAddSop'], ['handleUpdate(sopList[index])', 'canEditSop'], ['handleDelete(sopList[index])', 'canDeleteSop']]) {
    const button = find(ast, node => node.attrsMap && node.attrsMap['@click'] === action)
    assert.ok(button && (button.if === guard || (action === 'handleAdd' && button.attrsMap['v-if'] === guard)), `${action} must be permission gated`)
  }
  console.log('Material SOP: view, edit, and permission guards passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
