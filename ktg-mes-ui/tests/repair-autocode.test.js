const assert = require('assert')
const fs = require('fs')
const path = require('path')
const babel = require('@babel/core')
const commonjsPlugin = require.resolve('@babel/plugin-transform-modules-commonjs')
const compiler = require('vue-template-compiler')

const generatedCodes = []
const api = {
  listRepair: () => Promise.resolve({ rows: [], total: 0 }),
  getRepair: () => Promise.resolve({ data: {} }),
  delRepair: () => Promise.resolve(),
  addRepair: () => Promise.resolve(),
  updateRepair: () => Promise.resolve(),
  genCode: ruleCode => {
    generatedCodes.push(ruleCode)
    return Promise.resolve('REP2026001')
  }
}

function loadRepairComponent() {
  const filename = path.resolve(__dirname, '../src/views/mes/dv/repair/index.vue')
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  const code = babel.transformSync(parsed.script.content, {
    babelrc: false,
    configFile: false,
    plugins: [commonjsPlugin]
  }).code
  const componentModule = { exports: {} }
  const mockRequire = name => {
    if (name === '@/api/mes/dv/repair' || name === '@/api/system/autocode/rule') return api
    if (name === '@/components/machinerySelect/single.vue') return { name: 'MachinerySelectSingle' }
    if (name === './line.vue') return { name: 'Repairline' }
    throw new Error(`unexpected dependency: ${name}`)
  }
  new Function('exports', 'module', 'require', code)(componentModule.exports, componentModule, mockRequire)
  return componentModule.exports.default || componentModule.exports
}

async function main() {
  const component = loadRepairComponent()
  const context = { form: { repairCode: null } }

  await component.methods.handleAutoGenChange.call(context, true)
  assert.strictEqual(generatedCodes[0], 'REPAIR_CODE', '维修单应调用已配置的维修编号规则')
  assert.strictEqual(context.form.repairCode, 'REP2026001', '生成结果应回填维修单编号')

  await component.methods.handleAutoGenChange.call(context, false)
  assert.strictEqual(context.form.repairCode, null, '关闭自动生成应清空当前编号')

  console.log('Repair auto-code uses the configured rule, fills the code, and clears it when disabled')
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
