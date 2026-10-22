const assert = require('assert')
const fs = require('fs')
const path = require('path')
const Vue = require('vue')
const babel = require('@babel/core')
const compiler = require('vue-template-compiler')

const source = fs.readFileSync(path.resolve(__dirname, '../src/components/AgentAssistant/ResultCard.vue'), 'utf8')
const component = compiler.parseComponent(source)
const script = babel.transformSync(component.script.content, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const componentModule = { exports: {} }
new Function('exports', 'module', 'require', script)(componentModule.exports, componentModule, require)
const options = componentModule.exports.default || componentModule.exports
const compiled = compiler.compileToFunctions(component.template.content)
const result = {
  kind: 'workorder_progress',
  workorder: { workorderCode: 'WO-12', workorderName: 'Demo', quantity: '10' },
  metrics: { unit: '件', workorder_produced_fact: '4', remaining_raw: '6', ratio_percent: '40' },
  completeness: {
    observation_grade: 'MULTI_REQUEST_NON_ATOMIC',
    snapshot_consistent: false,
    business_progress_available: false,
    business_progress_reason: 'BUSINESS_RULE_UNCONFIRMED'
  }
}
const vm = new Vue({ ...options, propsData: { result }, render: compiled.render,
  staticRenderFns: compiled.staticRenderFns })
const vnode = vm._render()
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ')
  if (!node || typeof node !== 'object') return ''
  return [node.text || '', ...(node.children || []).map(text)].join(' ')
}
const rendered = text(vnode)
assert.match(rendered, /MULTI_REQUEST_NON_ATOMIC/)
assert.match(rendered, /BUSINESS_RULE_UNCONFIRMED/)
assert.match(rendered, /非同一快照/)
assert.doesNotMatch(rendered, /来源已核验/)
vm.$destroy()

const feedbackVm = new Vue({ ...options,
  propsData: { result: { kind: 'feedback', rows: [{ recordId: 3, quantityQualified: 0, unitOfMeasure: '件' }] } },
  render: compiled.render, staticRenderFns: compiled.staticRenderFns })
const feedbackText = text(feedbackVm._render())
assert.match(feedbackText, /合格 0 件/)
feedbackVm.$destroy()
console.log('Agent result card renders progress completeness and preserves zero report quantities')
