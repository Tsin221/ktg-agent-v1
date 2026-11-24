const assert=require('assert'),fs=require('fs'),vm=require('vm'),compiler=require('vue-template-compiler')
const dir='src/views/mes/pro/execution/'
const s=compiler.parseComponent(fs.readFileSync(dir+'ExecutionOrderProgress.vue','utf8'));assert.deepStrictEqual(compiler.compile(s.template.content).errors,[])
assert(!s.template.content.includes('detail.progress.tasks'),'Task rows must come from the scoped detail list')
assert(s.template.content.includes('整单生产进度'),'Global statistics must be explicitly distinguished from scoped detail rows')
const releaseTemplate=compiler.parseComponent(fs.readFileSync('src/views/mes/pro/workorder/ExecutionReleaseDialog.vue','utf8')).template.content
assert(!releaseTemplate.includes('detail.summary.'),'Release dialog must not repeat the totals from the canonical progress component')
assert(!releaseTemplate.includes(':data="detail.tasks"'),'Release dialog must not repeat the task table')
const ctx={module:{},taskLabel:x=>x,stepLabel:x=>x};vm.runInNewContext(s.script.content.replace(/^import .*$/mg,'').replace('export default','module.exports ='),ctx)
const c=ctx.module.exports;let submitted=0,confirmed=0
const i={...c.data(),detail:{order:{workorderId:9,version:7},progress:{completionAllowed:false}},command:{begin:async(a,id,p)=>{submitted++;assert.strictEqual(id,9);assert.strictEqual(p.version,7);return {response:{data:{operationId:'original'}}}}},$confirm:async()=>{confirmed++},$emit:()=>{}}
Object.assign(i,c.methods)
;(async()=>{await i.complete();assert.strictEqual(submitted,0);i.detail.progress.completionAllowed=true;await i.complete();assert.strictEqual(submitted,1);assert.strictEqual(confirmed,1);assert.strictEqual(i.receipt.operationId,'original');i.pending={};await i.complete();assert.strictEqual(submitted,1);console.log('Completion gate, confirmation, version command and pending lock passed')})().catch(e=>{console.error(e);process.exitCode=1})
