const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')

// Compare every actionable detail button with the permission enforced by its API.
const source = fs.readFileSync(path.join(__dirname, '../src/views/mes/dv/repair/line.vue'), 'utf8')
const controller = fs.readFileSync(path.join(__dirname, '../../ktg-mes-master/ktg-mes-master/ktg-mes/src/main/java/com/ktg/mes/dv/controller/DvRepairLineController.java'), 'utf8')
const template = compiler.parseComponent(source).template.content
const ast = compiler.compile(template).ast
const checked = []
function visit(node) {
  const attrs = node.attrsMap || {}
  const action = { handleAdd: 'add', handleDelete: 'remove', 'handleUpdate(scope.row)': 'edit', 'handleDelete(scope.row)': 'remove' }[attrs['@click']]
  if (action) {
    const permission = `mes:dv:repairline:${action}`
    assert(controller.includes(`@ss.hasPermi('${permission}')`))
    assert.strictEqual(attrs['v-hasPermi'], `['${permission}']`, `${attrs['@click']} must match the backend permission`)
    checked.push(action)
  }
  ;(node.children || []).forEach(visit)
  Object.values(node.scopedSlots || {}).forEach(visit)
}
visit(ast)
assert.strictEqual(checked.length, 4, 'Check toolbar and row actions')
console.log('Repair detail buttons match backend permissions')
