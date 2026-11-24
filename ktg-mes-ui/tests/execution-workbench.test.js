const assert = require('assert')
const fs = require('fs')
const vm = require('vm')
const compiler = require('vue-template-compiler')
const directory='src/views/mes/pro/execution/'
const source=fs.readFileSync(directory+'taskPresentation.js','utf8').replace(/export (function|const)/g,'$1')
const ctx={module:{}}
vm.runInNewContext(source+'\nmodule.exports={taskLabel,stepLabel,elapsedSeconds,formatDuration,availableActions};',ctx)
const {taskLabel,stepLabel,elapsedSeconds,formatDuration,availableActions}=ctx.module.exports
assert.strictEqual(taskLabel('WAITING'),'等待前序')
assert.strictEqual(stepLabel('ASSEMBLY'),'装配')
const ready={stepKind:'ASSEMBLY',status:'READY',availableUnits:10,canAssign:true,canStart:true,canReport:false,canComplete:false}
const actionNames=task=>Array.from(availableActions(task))
assert.deepStrictEqual(actionNames(ready),['assign','start'])
assert.deepStrictEqual(actionNames({...ready,status:'STARTED',canAssign:false,canStart:false,canReport:true}),['report'])
assert.deepStrictEqual(actionNames({...ready,status:'STARTED',canAssign:true,canStart:false,canReport:false}),['assign'],'coordinator can hand off an already started task')
assert.deepStrictEqual(actionNames({...ready,status:'STARTED',availableUnits:0,canAssign:false,canStart:false,canReport:false,canComplete:true}),['complete'])
assert.deepStrictEqual(actionNames({...ready,status:'WAITING',canStart:false}),['assign'])
assert.deepStrictEqual(actionNames({...ready,stepKind:'INSPECTION'}),['assign','start'],'inspection start uses server permission')
assert.strictEqual(elapsedSeconds({actualStartedAt:'2026-11-23 10:00:00',serverTime:'2026-11-23 10:02:00'},3000),123)
assert.strictEqual(elapsedSeconds({actualStartedAt:'2026-11-23 10:00:00',actualEndedAt:'2026-11-23 10:02:00',serverTime:'2026-11-23 12:02:00'},3000),120)
assert.strictEqual(formatDuration(3661),'01:01:01')
assert.strictEqual(elapsedSeconds({},5000),null)
for(const file of ['index.vue','TaskList.vue','TaskDetail.vue','ExecutionReportDialog.vue','MaterialGroups.vue','InspectionPanel.vue','DispositionPanel.vue','ReworkPanel.vue']) {
 const component=compiler.parseComponent(fs.readFileSync(directory+file,'utf8'))
 const result=compiler.compile(component.template.content)
 assert.deepStrictEqual(result.errors,[],file)
}
assert.match(fs.readFileSync(directory+'TaskDetail.vue','utf8'),/交接人员/)
assert.match(fs.readFileSync(directory+'index.vue','utf8'),/保留原开工时间和开工人/)
console.log('Execution workbench task gating, server time, finished duration, and component compilation passed')
