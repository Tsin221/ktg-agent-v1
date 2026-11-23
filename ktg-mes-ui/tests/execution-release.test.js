const assert = require('assert')
const fs = require('fs')
const vm = require('vm')
const compiler = require('vue-template-compiler')
const script = compiler.parseComponent(fs.readFileSync('src/views/mes/pro/workorder/ExecutionReleaseDialog.vue', 'utf8')).script.content.replace(/^import .*$/mg, '').replace('export default', 'module.exports =')
let writes = 0, reads = 0, mode = 'unknown', missing = false, lastPayload = null
const sessionStorage = { values: {}, setItem(k,v) { this.values[k]=v }, getItem(k) { return this.values[k] }, removeItem(k) { delete this.values[k] } }
const context = { module: {}, sessionStorage, crypto: {randomUUID: () => '93dc7d38-4c93-4f9c-b971-48140891cb24'}, releaseOrder: async (id, payload) => { writes++; lastPayload = JSON.stringify(payload); if(mode === 'unknown') throw Error('Network Error'); if(mode === 'invalid') throw Object.assign(Error('版本冲突'), {data:{errorCode:'VERSION_CONFLICT'}}); return {code:200} }, executionOperation: async () => {reads++; if(missing) throw Object.assign(Error('not found'),{data:{errorCode:'NOT_FOUND'}}); return {code:200}}, executionOrder: async () => ({data:{order:{},tasks:[],units:[],summary:{allocated:10}}}) }
vm.runInNewContext(script, context)
const component = context.module.exports
const state = Object.assign(component.data(), {$emit(){}})
for(const [k,v] of Object.entries(component.methods)) state[k] = v.bind(state)
Object.defineProperty(state, 'ready', {get: () => component.computed.ready.call(state)})
state.order = {workorderId:1,version:0,status:'PREPARE'}
state.steps = [{routeProcessId:1,workstationId:1},{routeProcessId:2,workstationId:2,executionKind:'INSPECTION',templateId:3,ipqcType:'FINAL'},{routeProcessId:3,workstationId:3}]
;(async () => {
 assert(state.ready)
 assert.deepStrictEqual(Array.from(state.templateTypes({templateId:1,templates:[{templateId:1,qcTypes:'FIRST,FINAL,IQC'}]})),['FIRST','FINAL'])
 assert.deepStrictEqual(Array.from(state.templateTypes({templateId:1,templates:[{templateId:1,qcTypes:['FINAL']}]})),['FINAL'])
 await state.submit(); assert(state.unknown); assert.strictEqual(writes,1)
 await state.submit(); assert.strictEqual(writes,1)
 const originalPayload=lastPayload; missing=true; await state.recover(); assert(state.retryable); assert.strictEqual(JSON.parse(sessionStorage.getItem(state.storageKey())).operationId,state.operationId); mode='success'; await state.retryOriginal(); assert.strictEqual(lastPayload,originalPayload); missing=false; await state.recover(); assert.strictEqual(reads,2); assert(!state.unknown); assert(state.detail); assert(!sessionStorage.getItem(state.storageKey()))
 mode='invalid'; await state.submit(); assert(!state.unknown); assert.strictEqual(state.error,'版本冲突'); assert(!sessionStorage.getItem(state.storageKey()))
 state.steps[1].templateId=null; assert(!state.ready)
 console.log('Release validation, duplicate prevention, ambiguous result recovery and conflict tests passed')
})().catch(e => {console.error(e);process.exit(1)})
