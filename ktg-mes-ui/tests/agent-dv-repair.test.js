const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync } = require('@babel/core')

function component(relative, dependencies = {}) {
  const filename = path.resolve(__dirname, '..', relative)
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  const compiled = compiler.compile(parsed.template.content)
  assert.deepStrictEqual(compiled.errors, [])
  const code = transformSync(parsed.script.content, { filename: filename + '.js', babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const loaded = { exports: {} }
  new Function('exports', 'module', 'require', code)(loaded.exports, loaded, name => dependencies[name] || {})
  return { options: loaded.exports.default, parsed, compiled, script: code }
}

async function main() {
  const received = []
  const api = { confirmDvRepair: async id => {
    received.push(id)
    await new Promise(resolve => setTimeout(resolve, 5))
    if (id === 'ok') return { result: { kind: 'dv_repair_result', outcome: 'succeeded', verified: true, action: 'update', repair_id: 8, label: '核验完成' } }
    if (id === 'unknown') return { result: { kind: 'dv_repair_result', outcome: 'unknown', verified: false, action: 'delete', repair_id: 8, label: '未知' } }
    if (id === 'unverified') return { result: { kind: 'dv_repair_result', outcome: 'succeeded', verified: false, action: 'update', repair_id: 8, label: '误导性成功' } }
    throw new Error('lost response')
  } }
  const assistant = component('src/components/AgentAssistant/index.vue', { '@/api/agent': api }).options
  assert.ok(assistant.components.AgentDvRepairCard)
  for (const kind of ['dv_repair_list', 'dv_repair_detail', 'dv_repair_lines', 'dv_repair_options',
    'dv_repair_machinery_candidates', 'dv_repair_subject_candidates', 'dv_repair_preview', 'dv_repair_result']) {
    assert.strictEqual(assistant.methods.isDvRepairResult({ kind }), true)
  }
  const events = []
  global.CustomEvent = function (type, options) { this.type = type; this.detail = options.detail }
  global.window = { dispatchEvent: event => events.push(event) }
  const vm = { ...assistant.data(), epoch: 1, saveLocalState () {}, scrollToBottom () {} }
  vm.confirm = assistant.methods.confirmDvRepairPreview.bind(vm)
  await Promise.all([vm.confirm('ok'), vm.confirm('ok')])
  assert.deepStrictEqual(received, ['ok'])
  assert.strictEqual(events.length, 1)
  assert.strictEqual(events[0].type, 'ktg:dv-repair-changed')
  await vm.confirm('unknown')
  assert.ok(vm.businessMessages.find(item => item.id === 'confirm-repair-unknown').text.includes('结果未知'))
  await vm.confirm('unverified')
  assert.strictEqual(vm.businessMessages.find(item => item.id === 'confirm-repair-unverified').text, '结果未核实，请先核对MES中的维修单。')
  await vm.confirm('throw')
  assert.ok(vm.businessMessages.find(item => item.id === 'confirm-repair-throw').text.includes('结果未知'))
  assert.strictEqual(events.length, 1)
  await vm.confirm('unknown')
  assert.deepStrictEqual(received, ['ok', 'unknown', 'unverified', 'throw'])
  assert.ok(assistant.methods.saveLocalState.toString().includes('completedDvRepairPreviewIds'))

  const card = component('src/components/AgentAssistant/DvRepairCard.vue')
  assert.strictEqual(card.options.computed.beforeValues.call({ result: { action: 'line_create' }, before: { repair: { repairId: 8 } } }).repairId, undefined)
  assert.strictEqual(card.options.computed.repair.call({ result: { action: 'create' }, before: {}, after: { repairCode: 'R-1' } }).repairCode, 'R-1')
  assert.ok(card.parsed.template.content.includes('row.lineId || row.repairId'))
  assert.ok(card.parsed.template.content.includes('结果未核实'))
  assert.ok(card.parsed.template.content.includes('确认后使用当前登录账号执行'))
  assert.ok(card.parsed.template.content.includes('将一并删除维修内容'))
  assert.strictEqual(card.options.methods.display('PREPARE', 'status'), '草稿')
  assert.strictEqual(card.options.methods.display('FINISHED', 'status'), '已完成')
  assert.strictEqual(card.options.methods.display('APPROVED', 'status'), '已审批')
  assert.strictEqual(card.options.methods.display('SUCCESS', 'repairResult'), '修复成功')
  assert.strictEqual(card.options.methods.display('FUTURE', 'status'), 'FUTURE')
  const page = component('src/views/mes/dv/repair/index.vue', {
    '@/api/mes/dv/repair': {}, '@/api/system/autocode/rule': {}
  }).options
  assert.ok(page.beforeDestroy && page.beforeDestroy.toString().includes('removeEventListener'))
  const refreshes = []
  const view = { open: true, form: { repairId: 8 }, getList: () => refreshes.push('list'),
    $refs: { line: { getList: () => refreshes.push('lines') } }, cancel: () => refreshes.push('close') }
  page.methods.handleAgentRepairChanged.call(view, { detail: { repair_id: 8, action: 'delete' } })
  assert.deepStrictEqual(refreshes, ['list', 'close'])
  const apiSource = fs.readFileSync(path.resolve(__dirname, '..', 'src/api/agent.js'), 'utf8')
  assert.ok(apiSource.includes("url: '/agent-api/v1/dv-repair/confirm'"))
  console.log('repair assistant/card confirmation tests passed')

  if (process.argv.includes('--emit-harness')) emitHarness(card)
}

function emitHarness(card) {
  const root = path.resolve(__dirname, '../..', 'output/playwright/T08-20261026')
  fs.mkdirSync(root, { recursive: true })
  const vueSource = fs.readFileSync(path.resolve(__dirname, '..', 'node_modules/vue/dist/vue.min.js'), 'utf8')
  const componentScript = card.script.replace(/require\([^)]*\)/g, '{}')
  const render = JSON.stringify(card.compiled.render)
  const staticFns = JSON.stringify(card.compiled.staticRenderFns)
  const styles = card.parsed.styles.map(style => style.content).join('\n')
  const html = `<!doctype html><html lang="zh"><meta charset="utf-8"><title>T08维修卡片隔离预览</title>
<style>body{font:14px sans-serif;max-width:760px;margin:30px auto;padding:0 15px;background:#f4f6f8}button{margin:4px;padding:7px 12px}h1{font-size:20px}#app{background:white;padding:20px}${styles}</style>
<h1>T08 维修工具卡片隔离预览</h1><p>使用真实 DvRepairCard.vue 编译；仅本地 fixture，不连接 MES。</p>
<div id="app"><button v-for="item in fixtures" :key="item.name" @click="selected=item.value">{{item.name}}</button>
<repair-card :result="selected" :completed="completed" @confirm="confirm"></repair-card>
<p v-if="message">{{message}}</p></div><script>${vueSource}</script>
<script>const module={exports:{}}; const exports=module.exports; ${componentScript}
const card=module.exports.default; card.render=new Function(${render}); card.staticRenderFns=${staticFns}.map(s=>new Function(s));
const fixtures=[
 {name:'查询',value:{kind:'dv_repair_list',rows:[{repairId:8,repairCode:'R-8',repairName:'泵维修',machineryName:'泵',status:'PREPARE'}],total:1,page:1}},
 {name:'新增预览',value:{kind:'dv_repair_preview',action:'create',preview_id:'fixture-create',before:null,after:{repairCode:'R-9',repairName:'新单',machineryName:'泵',status:'PREPARE'},warnings:['隔离展示，不发出实际请求']}},
 {name:'主单删除预览',value:{kind:'dv_repair_preview',action:'delete',preview_id:'fixture-delete',before:{repair:{repairId:8,repairCode:'R-8',repairName:'泵维修'},lines:{count:1,samples:[{lineId:5,subjectName:'检查泵',malfunction:'异响'}]}},after:null,affected_counts:{line_count:1},warnings:['隔离展示，不发出实际请求']}},
 {name:'子表预览',value:{kind:'dv_repair_preview',action:'line_update',preview_id:'fixture-line',before:{repair:{repairId:8,repairCode:'R-8',repairName:'泵维修'},line:{lineId:5,repairId:8,subjectName:'检查泵',malfunction:'异响'}},after:{lineId:5,repairId:8,subjectName:'检查泵',malfunction:'已定位异响'},warnings:['隔离展示，不发出实际请求']}},
 {name:'结果未知',value:{kind:'dv_repair_result',action:'create',outcome:'unknown',verified:false,label:'写请求已发出，结果未知'}}];
Vue.component('repair-card',card);new Vue({el:'#app',data:{fixtures,selected:fixtures[0].value,completed:false,message:''},methods:{confirm(id){this.completed=true;this.message='仅本地演示点击：'+id+'；未连接MES，未发送确认。'}}});</script></html>`
  fs.writeFileSync(path.join(root, 'repair-card.html'), html)
  console.log('wrote', path.join(root, 'repair-card.html'))
}

main().catch(error => { console.error(error); process.exitCode = 1 })
