const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync } = require('@babel/core')
function component(relative, dependencies = {}) {
  const filename = path.resolve(__dirname, '..', relative)
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  assert.deepStrictEqual(compiler.compile(parsed.template.content).errors, [])
  const code = transformSync(parsed.script.content, { filename: filename + '.js', babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const loaded = { exports: {} }
  new Function('exports', 'module', 'require', code)(loaded.exports, loaded, name => dependencies[name] || {})
  return { options: loaded.exports.default, parsed }
}
async function main() {
  const received = []
  const api = { confirmDvCheckPlan: async id => {
    received.push(id)
    await new Promise(resolve => setTimeout(resolve, 5))
    if (id === 'p1') return { result: { kind:'dv_checkplan_result', outcome:'succeeded', verified:true, action:'update', plan_id:42, label:'核验完成' } }
    if (id === 'pfailed') return { result: { kind:'dv_checkplan_result', outcome:'failed', verified:false, action:'delete', plan_id:42, label:'明确失败' } }
    if (id === 'punknown') return { result: { kind:'dv_checkplan_result', outcome:'unknown', verified:false, action:'delete', plan_id:42, label:'结果尚未确认' } }
    if (id === 'punverified') return { result: { kind:'dv_checkplan_result', outcome:'succeeded', verified:false, action:'update', plan_id:42, label:'尚未核实' } }
    if (id === 'pthrow') throw new Error('lost response')
    throw new Error('unexpected preview id')
  } }
  const { options, parsed } = component('src/components/AgentAssistant/index.vue', { '@/api/agent': api })
  assert.ok(options.components.AgentDvCheckPlanCard)
  assert.ok(parsed.template.content.includes('@confirm="confirmDvCheckPlanPreview"'))
  for (const kind of ['dv_checkplan_list','dv_checkplan_detail','dv_checkplan_options','dv_checkplan_links','dv_checkplan_machinery_candidates','dv_checkplan_subject_candidates','dv_checkplan_preview','dv_checkplan_result']) assert.strictEqual(options.methods.isDvCheckPlanResult({ kind }), true)
  assert.strictEqual(options.methods.isDvCheckPlanResult({ kind:'dv_subject_list' }), false)
  const events = []
  global.CustomEvent = function(type, options) { this.type = type; this.detail = options.detail }
  global.window = { dispatchEvent: event => events.push(event) }
  const vm = { ...options.data(), epoch: 1, saveLocalState(){}, scrollToBottom(){} }
  vm.confirm = options.methods.confirmDvCheckPlanPreview.bind(vm)
  const first = vm.confirm('p1'); const second = vm.confirm('p1')
  await Promise.all([first, second])
  assert.deepStrictEqual(received, ['p1'])
  assert.strictEqual(events.length, 1)
  assert.strictEqual(events[0].type, 'ktg:dv-checkplan-changed')
  assert.strictEqual(events[0].detail.plan_id, 42)
  await vm.confirm('pfailed')
  assert.strictEqual(vm.businessMessages.find(item=>item.id==='confirm-checkplan-pfailed').results[0].outcome,'failed')
  assert.strictEqual(events.length,1)
  await vm.confirm('punknown')
  assert.strictEqual(vm.businessMessages.find(item=>item.id==='confirm-checkplan-punknown').results[0].outcome,'unknown')
  assert.ok(vm.businessMessages.find(item=>item.id==='confirm-checkplan-punknown').text.includes('结果尚未确认'))
  assert.strictEqual(events.length,1)
  await vm.confirm('punverified')
  assert.strictEqual(vm.businessMessages.find(item=>item.id==='confirm-checkplan-punverified').results[0].verified,false)
  assert.strictEqual(vm.businessMessages.find(item=>item.id==='confirm-checkplan-punverified').text,'结果未核实，请先核对MES中的计划。')
  assert.strictEqual(events.length,1)
  await vm.confirm('pthrow')
  assert.ok(vm.businessMessages.find(item=>item.id==='confirm-checkplan-pthrow').text.includes('结果尚未确认'))
  assert.strictEqual(events.length,1)
  const countBeforeRepeat=received.length
  await vm.confirm('punknown')
  assert.strictEqual(received.length,countBeforeRepeat)
  assert.deepStrictEqual(received,['p1','pfailed','punknown','punverified','pthrow'])
  assert.ok(options.methods.saveLocalState.toString().includes('completedDvCheckPlanPreviewIds'))
  const card = component('src/components/AgentAssistant/DvCheckPlanCard.vue').options
  const previewVm = { result:{ kind:'dv_checkplan_preview', action:'delete', before:{planCode:'P1'}, after:{}, affected_counts:{equipment_count:2,subject_count:3} }, before:{planCode:'P1'}, after:{}, beforeValues:{planCode:'P1'}, afterValues:{} }
  assert.strictEqual(card.computed.preview.call(previewVm), true)
  assert.strictEqual(card.computed.previewFields.call(previewVm).includes('planCode'), true)
  const transitionPreview={result:{kind:'dv_checkplan_preview',action:'update',transition:'activate',before:{plan:{status:'PREPARE'}},after:{status:'FINISHED',transition:'activate'},beforeValues:{status:'PREPARE'},afterValues:{status:'FINISHED',transition:'activate'}},before:{plan:{status:'PREPARE'}},after:{status:'FINISHED',transition:'activate'},beforeValues:{status:'PREPARE'},afterValues:{status:'FINISHED',transition:'activate'}}
  assert.strictEqual(card.computed.previewFields.call(transitionPreview).includes('status'),true)
  assert.strictEqual(card.computed.beforeValues.call(transitionPreview).status,'PREPARE')
  assert.strictEqual(card.computed.afterValues.call(transitionPreview).status,'FINISHED')
  assert.strictEqual(card.methods.actionText('machinery_remove'), '移除设备')
  assert.strictEqual(card.methods.value(null), '—')
  assert.strictEqual(card.methods.fieldLabel('machineryBrand'), '品牌快照')
  assert.strictEqual(card.methods.actionText('delete'), '删除')
  assert.strictEqual(card.methods.display('FINISHED', 'status'), '已启用')
  assert.strictEqual(card.methods.display('PREPARE', 'status'), '草稿')
  assert.strictEqual(card.methods.display('CHECK', 'planType'), '点检')
  assert.strictEqual(card.methods.display('HOUR', 'cycleType'), '小时')
  assert.strictEqual(card.methods.display('FUTURE', 'cycleType'), 'FUTURE')
  const deletePreview={result:{kind:'dv_checkplan_preview',action:'delete',before:{machinery:{count:0,rows:[]}}},before:{machinery:{count:0,rows:[]}},after:{}}
  assert.strictEqual(card.computed.link.call(deletePreview),null)
  const removedLink = { recordId:303, machineryId:710, machineryCode:'EQ-0710', machineryBrand:'KTG' }
  const removedPreview = { result:{ kind:'dv_checkplan_preview', action:'machinery_remove', before:{record:removedLink}, after:{planId:42,recordId:303} }, before:{record:removedLink}, after:{planId:42,recordId:303} }
  assert.strictEqual(card.computed.link.call(removedPreview), removedLink)
  const addPreview = { result:{ kind:'dv_checkplan_preview', action:'machinery_add', before:{plan:{}}, after:{machineryId:711,machineryCode:'EQ-0711'} }, before:{plan:{}}, after:{machineryId:711,machineryCode:'EQ-0711'} }
  assert.strictEqual(card.computed.link.call(addPreview), addPreview.after)
  const activating = { result:{ transition:'activate' }, after:{}, transition:'activate', actionText:card.methods.actionText }
  assert.strictEqual(card.computed.operationText.call(activating), '启用')
  assert.ok(component('src/components/AgentAssistant/DvCheckPlanCard.vue').parsed.template.content.includes('结果未核实，请先核对MES中的计划。'))
  const page = component('src/views/mes/dv/checkplan/index.vue', { '@/api/mes/dv/checkplan': {}, '@/api/mes/dv/checkmachinery': {}, '@/api/mes/dv/checksubject': {}, '@/api/system/autocode/rule': {} })
  assert.ok(page.options.mounted && page.options.beforeDestroy)
  assert.ok(page.options.beforeDestroy.toString().includes('removeEventListener'))
  const refreshes=[]; const view={open:true,form:{planId:42},$refs:{machinerylist:{getList:()=>refreshes.push('machine')},subjectlist:{getList:()=>refreshes.push('subject')}},getList:()=>refreshes.push('plans')}
  page.options.methods.handleAgentCheckPlanChanged.call(view,{detail:{plan_id:42,action:'update'}})
  assert.deepStrictEqual(refreshes,['plans','machine','subject'])
  const deleted={...view,open:true,$modal:{msgWarning:message=>refreshes.push(message)}}
  page.options.methods.handleAgentCheckPlanChanged.call(deleted,{detail:{plan_id:42,action:'delete'}})
  assert.strictEqual(deleted.open,false)
  assert.ok(refreshes.includes('当前打开的计划已删除。'))
  const apiSource = fs.readFileSync(path.resolve(__dirname, '..', 'src/api/agent.js'), 'utf8')
  assert.ok(apiSource.includes("url: '/agent-api/v1/dv-checkplan/confirm'"))
  assert.ok(apiSource.includes('data: { preview_id: previewId }'))
  console.log('checkplan assistant/card confirmation tests passed')
}
main().catch(error=>{console.error(error);process.exitCode=1})
