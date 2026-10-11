const assert = require('assert')
const path = require('path')
const { transformFileSync } = require('@babel/core')

function loadEsModule(relativePath) {
  const filename = path.resolve(__dirname, '..', relativePath)
  const transformed = transformFileSync(filename, {
    plugins: ['@babel/plugin-transform-modules-commonjs']
  })
  const module = { exports: {} }
  new Function('exports', 'module', 'require', transformed.code)(module.exports, module, require)
  return module.exports
}

const {
  isWorkorderAssistantResult,
  workorderFieldLabel,
  formatWorkorderValue
} = loadEsModule('src/components/AgentAssistant/workorderPresentation.js')

assert.strictEqual(isWorkorderAssistantResult({ kind: 'workorder_draft_preview' }), true)
assert.strictEqual(isWorkorderAssistantResult({ kind: 'workorder_draft_result' }), true)
assert.strictEqual(isWorkorderAssistantResult({ kind: 'workorder_product_candidates' }), true)
assert.strictEqual(isWorkorderAssistantResult({ kind: 'workorders' }), false)
assert.strictEqual(workorderFieldLabel('quantity'), '数量')
assert.strictEqual(workorderFieldLabel('status'), '状态')
assert.strictEqual(formatWorkorderValue(null), '未设置')
assert.strictEqual(formatWorkorderValue(''), '空')
assert.strictEqual(formatWorkorderValue(120), '120')

console.log('agent workorder presentation tests passed')
