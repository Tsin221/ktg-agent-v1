const assert = require('assert'), fs = require('fs'), vm = require('vm'), compiler = require('vue-template-compiler')
const dir = 'src/views/mes/pro/execution/'
function component(name) {
 const sfc = compiler.parseComponent(fs.readFileSync(dir + name + '.vue', 'utf8'))
 assert.deepStrictEqual(compiler.compile(sfc.template.content).errors, [])
 const ctx = { module: {}, taskLabel: x => x, inspectionRequirements: async () => ({data: {template: {indices: [{index_id: 1,index_type: 'QUANTITATIVE'}]}, defects: []}}), inspectionHistory: async () => ({data: []}) }
 vm.runInNewContext(sfc.script.content.replace(/^import .*$/mg, '').replace('export default', 'module.exports =').replace('components: { DispositionPanel },',''), ctx)
 return ctx.module.exports
}
const c = component('InspectionPanel'), emitted = []
const instance = {...c.data(), editable: true, busy: false, $emit: (...args) => emitted.push(args)}
Object.assign(instance,c.methods)
assert.strictEqual(instance.historicalValue({remark:'{"value":12,"passed":null}'}),12,'history reads immutable measured value')
assert.strictEqual(instance.historicalValue({remark:'{"value":null,"passed":false}'}),'不符合','history reads qualitative failure')
;(async () => {
 await instance.open({taskId: 9}, {snId: 7, unitState: 'WAIT_REINSPECTION'})
 instance.submit(); assert.strictEqual(emitted.length,0,'blank measurement cannot submit')
 instance.answers[0].value = '3.15';instance.submit()
 assert.strictEqual(emitted[0][0],'inspect'); assert.strictEqual(emitted[0][1],7)
 assert.strictEqual(emitted[0][2].taskId,9);assert(!('checkResult' in emitted[0][2]),'UI cannot submit a final result')
 instance.editable=false;instance.submit();assert.strictEqual(emitted.length,1,'unauthorized read-only panel cannot submit')
 console.log('Inspection measurement validation, server result authority, task/SN identity, and readonly access passed')
})().catch(e=>{console.error(e);process.exitCode=1})
