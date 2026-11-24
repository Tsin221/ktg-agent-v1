const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync } = require('@babel/core')

const root = path.join(__dirname, '../src')
function load(relative, deps = {}) {
  const source = fs.readFileSync(path.join(root, relative), 'utf8')
  const parsed = relative.endsWith('.vue') ? compiler.parseComponent(source) : null
  const compiled = parsed && compiler.compile(parsed.template.content)
  if (compiled) assert.deepStrictEqual(compiled.errors, [], relative)
  const code = transformSync(parsed ? parsed.script.content : source, { babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const mod = { exports: {} }
  new Function('exports', 'module', 'require', code)(mod.exports, mod, name => deps[name] || {})
  return { options: mod.exports.default || mod.exports, ast: compiled && compiled.ast }
}
function find(node, predicate) {
  if (predicate(node)) return node
  for (const child of node.children || []) { const result = find(child, predicate); if (result) return result }
}
function bind(options, props = {}) {
  const vm = Object.assign({}, props)
  Object.assign(vm, options.data ? options.data.call(vm) : {}, props)
  Object.entries(options.methods || {}).forEach(([key, fn]) => { vm[key] = fn.bind(vm) })
  Object.entries(options.computed || {}).forEach(([key, fn]) => Object.defineProperty(vm, key, { get: () => fn.call(vm) }))
  return vm
}

async function vendor() {
  let grants = []
  const checkPermi = permissions => permissions.some(p => grants.includes(p))
  let requests = 0
  const deps = { '@/utils/permission': { checkPermi }, '@/api/mes/md/vendor': {
    listVendor: async () => { requests++; return { rows: [], total: 0 } }
  } }
  for (const file of ['index.vue', 'form.vue']) {
    const { options, ast } = load(`views/mes/wm/itemrecpt/${file}`, deps)
    const selector = find(ast, node => node.tag === 'VendorSelect')
    const button = find(ast, node => node.attrsMap && node.attrsMap['@click'] === 'handleSelectVendor')
    assert.strictEqual(selector.if, 'canSelectVendor')
    assert.strictEqual(button.if, 'canSelectVendor')
    grants = []
    assert.strictEqual(options.computed.canSelectVendor.call({ optType: 'edit' }), false)
    grants = ['mes:md:vendor:list']
    assert.strictEqual(options.computed.canSelectVendor.call({ optType: 'view' }), false)
    assert.strictEqual(options.computed.canSelectVendor.call({ optType: 'edit' }), true)
  }
  const { options } = load('components/vendorSelect/single.vue', deps)
  const vm = bind(options)
  if (options.created) options.created.call(vm)
  assert.strictEqual(requests, 0, 'hidden selector must not request the list')
  grants = []
  vm.handleOpen(3)
  assert.strictEqual(vm.showFlag, false)
  assert.strictEqual(requests, 0)
  grants = ['mes:md:vendor:list']
  vm.handleOpen(3)
  assert.strictEqual(vm.showFlag, true)
  options.watch.showFlag.call(vm, true)
  await Promise.resolve()
  assert.strictEqual(requests, 1)
  assert.strictEqual(vm.selectedVendorId, 3)
  console.log('Vendor selector: permission checks and deferred load passed')
}

async function equipment() {
  const { options: { handleTree } } = load('utils/ruoyi.js')
  const rows = [1, 4, 5, 6].map((id, i) => ({ machineryTypeId: id, parentTypeId: i === 3 ? 4 : 0, machineryTypeName: 'type-' + id }))
  const deps = { '@/api/mes/dv/machinerytype': { listMachinerytype: async () => ({ data: rows.map(row => ({ ...row })) }) } }
  for (const file of ['views/mes/dv/machinery/index.vue', 'views/mes/dv/machinerytype/index.vue', 'components/machinerySelect/single.vue', 'components/machinerySelect/index.vue']) {
    const { options } = load(file, deps)
    const target = { handleTree, machineryTypeOptions: [], machinerytypeOptions: [] }
    options.methods.getTreeselect.call(target)
    await Promise.resolve()
    const tree = file.includes('machinerytype/index') ? target.machinerytypeOptions : target.machineryTypeOptions
    assert.deepStrictEqual(tree.map(node => node.machineryTypeId), [1, 4, 5], file)
    assert.deepStrictEqual(tree[1].children.map(node => node.machineryTypeId), [6], file)
  }
  console.log('Equipment classifications: all roots and child nodes retained')
}

async function barcode() {
  const pending = []
  const { options, ast } = load('components/barcodeImg/index.vue', { '@/api/mes/wm/barcode': {
    getBarcodeUrl: params => new Promise((resolve, reject) => pending.push({ params, resolve, reject }))
  } })
  assert.strictEqual(ast.if, 'barcodeUrl', 'no image container for missing barcode')
  const image = find(ast, node => node.tag === 'el-image')
  assert.ok(image.attrsMap['@error'])
  const vm = bind(options, { bussinessId: 1, bussinessCode: 'A', barcodeType: 'ITEM', barcodeFormart: 'QR_CODE' })
  assert.strictEqual(vm.barcodeUrl, '')
  const first = vm.getBarcode()
  vm.bussinessId = 2; vm.bussinessCode = 'B'
  const second = vm.getBarcode()
  pending[1].resolve({ data: null }); await second
  pending[0].resolve({ data: { barcodeUrl: '/wrong.png' } }); await first
  assert.strictEqual(vm.barcodeUrl, '', 'old responses must not show a different record barcode')
  const third = vm.getBarcode()
  pending[2].resolve({ data: { barcodeUrl: '/valid.png' } }); await third
  assert.strictEqual(vm.barcodeUrl, '/valid.png')
  vm.hideBarcode()
  assert.strictEqual(vm.barcodeUrl, '')
  const fourth = vm.getBarcode()
  pending[3].reject(new Error('missing file')); await fourth
  assert.strictEqual(vm.barcodeUrl, '')
  vm.bussinessCode = null
  await vm.getBarcode()
  assert.strictEqual(pending.length, 4)
  console.log('Barcode: absent, stale, broken and valid image handling passed')
}

const checks = { vendor, equipment, barcode }
async function main() {
  for (const check of process.argv[2] ? [checks[process.argv[2]]] : Object.values(checks)) await check()
}
main().catch(error => { console.error(error); process.exitCode = 1 })
