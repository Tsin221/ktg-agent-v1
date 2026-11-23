const assert = require('assert')
const fs = require('fs')
const compiler = require('vue-template-compiler')
for (const file of ['src/views/mes/pro/proroute/routeprocess.vue', 'src/views/mes/pro/workorder/ExecutionReleaseDialog.vue', 'src/views/mes/pro/workorder/index.vue']) {
 const component = compiler.parseComponent(fs.readFileSync(file, 'utf8'))
 assert.deepStrictEqual(compiler.compile(component.template.content).errors, [])
}
console.log('Execution route and release Vue templates compile')
