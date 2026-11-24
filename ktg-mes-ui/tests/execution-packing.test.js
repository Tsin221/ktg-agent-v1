const assert=require('assert'),fs=require('fs'),vm=require('vm'),compiler=require('vue-template-compiler')
const dir='src/views/mes/pro/execution/'
function component(name, extra={}) {const s=compiler.parseComponent(fs.readFileSync(dir+name+'.vue','utf8'));assert.deepStrictEqual(compiler.compile(s.template.content).errors,[]);const ctx={module:{},taskLabel:x=>x,...extra};vm.runInNewContext(s.script.content.replace(/^import .*$/mg,'').replace('export default','module.exports ='),ctx);return ctx.module.exports}
const c=component('PackingPanel',{listExecutionTaskUnits:async()=>({rows:[{snId:1,unitState:'WAIT_PACKING'},{snId:2,unitState:'WAIT_DISPOSITION'}],total:2})}),events=[]
const instance={...c.data(),busy:false,$emit:(...x)=>events.push(x)};Object.assign(instance,c.methods)
;(async()=>{
  await instance.open({taskId:3,canPack:true});assert.strictEqual(instance.units.length,1)
  const template=compiler.parseComponent(fs.readFileSync(dir+'PackingPanel.vue','utf8')).template.content
  assert(!template.includes('packageLineId'),'No optional carton entry in the demo')
  assert(!('packageLineId' in instance),'No unused carton field')
  instance.selected=instance.units;instance.submit();assert.strictEqual(events.length,1);assert.strictEqual(events[0][2].snIds[0],1);assert(!('packageLineId' in events[0][2]))
  for(const state of ['WAIT_INSPECTION','WAIT_DISPOSITION','REWORK_IN_PROGRESS','WAIT_REINSPECTION','SCRAPPED','PACKED']) {
    instance.selected=[{snId:2,unitState:state}];instance.submit();assert.strictEqual(events.length,1)
  }
  instance.task.canPack=false;instance.selected=instance.units;instance.submit();assert.strictEqual(events.length,1)
  console.log('Packing query filtering, no carton entry/payload, unauthorized and illegal SN checks passed')
})().catch(e=>{console.error(e);process.exitCode=1})
