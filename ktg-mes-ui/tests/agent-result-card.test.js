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
function render(result) {
  const vm = new Vue({ ...options, propsData: { result }, render: compiled.render,
    staticRenderFns: compiled.staticRenderFns })
  const vnode = vm._render()
  vm.$destroy()
  return vnode
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ')
  if (!node || typeof node !== 'object') return ''
  return [node.text || '', ...(node.children || []).map(text)].join(' ')
}
function tags(node) {
  if (!node || typeof node !== 'object') return []
  return [node.tag, ...(node.children || []).flatMap(tags)].filter(Boolean)
}

const countText = text(render({ kind: 'product_count', total: 1 }))
assert.match(countText, /产品总数/)
assert.match(countText, /共 1 条/)
assert.match(countText, /不含物料/)

const stockText = text(render({ kind: 'stock_summary', groups: [
  { itemId: 38, itemCode: 'CELL-18650-2200', warehouseId: 1, warehouseName: '原料仓',
    quantityOnhand: '0', unitOfMeasure: 'PCS' }
], queried_at: '2026-11-24T01:00:00Z' }))
assert.match(stockText, /分仓库存汇总/)
assert.match(stockText, /原料仓/)
assert.match(stockText, /CELL-18650-2200.*0 PCS/)
assert.match(stockText, /查询时间/)
assert.match(text(render({ kind: 'stock_summary', no_records: true, groups: [] })), /不代表库存量为零/)

for (const kind of ['workorder_draft_preview', 'inventory_preview', 'exploration_search', 'workorder_progress']) {
  const historical = render({ kind, url: 'https://example.invalid', label: '<script>test</script>' })
  assert.match(text(historical), /历史结果（仅供查看，不可执行）/)
  assert.ok(tags(historical).includes('pre'))
  assert.ok(!tags(historical).some(tag => ['button', 'a', 'iframe', 'script', 'form'].includes(tag)))
  assert.match(text(historical), /<script>test<\/script>/)
}
console.log('Agent result card renders retained read-only cards and inert historical results')
