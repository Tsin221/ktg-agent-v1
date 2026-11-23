const assert=require('assert'),fs=require('fs'),vm=require('vm'),compiler=require('vue-template-compiler')
const sfc=compiler.parseComponent(fs.readFileSync('src/views/mes/pro/execution/DispositionPanel.vue','utf8'))
assert.deepStrictEqual(compiler.compile(sfc.template.content).errors,[])
const ctx={module:{}};vm.runInNewContext(sfc.script.content.replace('export default','module.exports='),ctx)
const c=ctx.module.exports, emitted=[]
const state={...c.data(),history:[{inspectionId:11,roundNo:1},{inspectionId:12,roundNo:2}],busy:false,$confirm:async()=>{},$emit:(...args)=>emitted.push(args)}
Object.defineProperty(state,'latest',{get:()=>c.computed.latest.call(state)})
;(async()=>{
 await c.methods.submit.call(state);assert.strictEqual(emitted.length,0,'reason is required')
 state.reason=' 修复接线 ';await c.methods.submit.call(state)
 assert.strictEqual(emitted[0][1].inspectionId,12,'disposition references latest round')
 assert.strictEqual(emitted[0][1].reason,'修复接线');assert.strictEqual(emitted[0][1].decision,'REWORK')
 state.$confirm=async()=>{throw Error('cancel')};state.decision='SCRAP';await c.methods.submit.call(state);assert.strictEqual(emitted.length,1,'cancelled scrap never submits')
 console.log('Disposition requires reason, uses latest failure and respects cancellation passed')
})().catch(e=>{console.error(e);process.exitCode=1})
