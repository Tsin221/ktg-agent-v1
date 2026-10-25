const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync } = require('@babel/core')
const filename = path.resolve(__dirname, '../src/components/dvsubjectSelect/multi.vue')
const script = compiler.parseComponent(fs.readFileSync(filename, 'utf8')).script.content
const code = transformSync(script, { filename: filename + '.js', babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const sent = []
const moduleScope = { exports: {} }
new Function('exports', 'module', 'require', code)(moduleScope.exports, moduleScope, () => ({
  listDvsubject: async params => { sent.push(params); return { rows: [], total: 0 } }
}))
const component = moduleScope.exports.default
async function run() {
  for (const type of ['CHECK', 'MAINTEN']) {
    const vm = { subjectType: type }
    Object.assign(vm, component.data.call(vm))
    for (const [name, method] of Object.entries(component.methods)) vm[name] = method.bind(vm)
    assert.strictEqual(vm.queryParams.enableFlag, 'Y')
    vm.resetQueryState()
    assert.strictEqual(vm.queryParams.enableFlag, 'Y')
    // A stale query must not override the component's current type/enabled restriction.
    vm.queryParams.subjectType = 'OTHER'
    vm.queryParams.enableFlag = null
    await vm.getList()
    assert.strictEqual(sent[sent.length - 1].subjectType, type)
    assert.strictEqual(sent[sent.length - 1].enableFlag, 'Y')
  }
  console.log('dvsubject selector restrictions passed')
}
run().catch(error => { console.error(error); process.exitCode = 1 })
