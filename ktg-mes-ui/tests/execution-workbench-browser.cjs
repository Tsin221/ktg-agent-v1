// Browser-level component integration against an in-memory API fixture (not a live MES).
const assert=require('assert'),fs=require('fs'),compiler=require('vue-template-compiler')
const { chromium }=require(process.env.MES_B03_PLAYWRIGHT || 'C:/Users/Alice/AppData/Local/npm-cache/_npx/1e6cc5b018c8a4f1/node_modules/playwright')
const dir='src/views/mes/pro/execution/'
const helpers=['reportContract.js','taskPresentation.js'].map(file=>fs.readFileSync(dir+file,'utf8').replace(/export (function|class|const)/g,'$1')).join('\n')
const components=['MaterialGroups','ExecutionReportDialog','ReworkPanel','DispositionPanel','InspectionPanel','PackingPanel','ExecutionOrderProgress','TaskDetail','TaskList','index'].map(file=>{
 const sfc=compiler.parseComponent(fs.readFileSync(dir+file+'.vue','utf8'))
 return {script:`const ${file==='index'?'Workbench':file}=(()=>{${sfc.script.content.replace(/^import .*$/mg,'').replace('export default','const component=')};component.template=${JSON.stringify(sfc.template.content)};return component})()`,css:sfc.styles.map(s=>s.content).join('\n')}
})
const fixture=`
let task={taskId:101,taskCode:'T-001',workorderId:1,workorderCode:'WO-001',itemName:'台灯',workstationName:'装配台',processName:'电池装配',stepKind:'ASSEMBLY',taskKind:'NORMAL',status:'READY',quantity:10,quantityProduced:0,availableUnits:10,assignedUserName:'测试人员',canAssign:false,canStart:true,canReport:false,canComplete:false,serverTime:'2026-11-23 10:00:03',processContents:[{contentId:1,orderNum:1,contentText:'安装实际批次电池'}],productSops:[]};
const units=Array.from({length:10},(_,i)=>({snId:i+1,snCode:'L'+String(i+1).padStart(2,'0'),unitState:'WAIT_ASSEMBLY'}));
const stock=[['B06',2],['B07',3],['B08',5]].map((v,i)=>({materialStockId:51+i,itemCode:'BAT',itemName:'电池',sourceBatchCode:v[0],quantityOnhand:v[1],unitOfMeasure:'个'}));
const copy=v=>JSON.parse(JSON.stringify(v));
const listExecutionTasks=async()=>({rows:[copy(task)],total:1});
const getExecutionTask=async()=>({data:copy(task)});
const listExecutionTaskUnits=async()=>({rows:task.availableUnits?units:[],total:task.availableUnits});
const listExecutionMaterials=async(_,q)=>({rows:stock.filter(s=>s.sourceBatchCode.includes(q.batchCode||'')),total:stock.filter(s=>s.sourceBatchCode.includes(q.batchCode||'')).length});
const executionAssignmentOptions=async()=>({data:[]});
const executionOrder=async()=>({data:{order:{workorderId:1},summary:{},progress:{},tasks:[],units:[]}});const completeExecutionOrder=async()=>({});const packExecutionTask=async()=>({});const assigned=async()=>({});const assignExecutionTask=assigned;const inspectExecutionUnit=assigned,disposeExecutionUnit=assigned,getIpqc=assigned,inspectionRequirements=assigned,inspectionHistory=assigned;
const receipt=(payload,actionType,resultRef)=>({code:200,data:{operationId:payload.operationId,actionType,committedAt:task.serverTime,resultRef}});
const startExecutionTask=async(id,payload)=>{Object.assign(task,{status:'STARTED',canStart:false,canReport:true,actualStartedAt:'2026-11-23 10:00:00'});return receipt(payload,'START',{})};
let reportCalls=0; let reportPayload;
const reportExecutionTask=async(id,payload)=>{reportCalls++;if(reportCalls===1){reportPayload=JSON.stringify(payload);throw Error('模拟网络中断')}if(reportPayload!==JSON.stringify(payload))throw Error('operation changed');Object.assign(task,{availableUnits:0,quantityProduced:10,canReport:false,canComplete:true});return receipt(payload,'REPORT',{feedbackId:501,consumeId:601,snCount:10,assembled:10})};
const executionOperation=async()=>{throw Object.assign(Error('not found'),{data:{errorCode:'NOT_FOUND'}})};
const completeExecutionTask=async(id,payload)=>{Object.assign(task,{status:'FINISHED',canComplete:false,actualEndedAt:'2026-11-23 10:01:01'});return receipt(payload,'TASK_COMPLETE',{state:'FINISHED'})};
Vue.directive('hasPermi',{});Vue.prototype.$store={state:{user:{id:19}},getters:{permissions:['mes:pro:execution:query','mes:pro:execution:start','mes:pro:execution:report']}};
Vue.component('pagination',{props:['total','page','limit'],template:'<div role="status">共 {{total}} 条</div>'});
`
const html=`<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><link rel="stylesheet" href="/node_modules/element-ui/lib/theme-chalk/index.css"><style>body{font-family:Arial,sans-serif;margin:24px;} ${components.map(c=>c.css).join('\n')}</style></head><body><div id="app"><workbench/></div><script src="/node_modules/vue/dist/vue.js"></script><script src="/node_modules/element-ui/lib/index.js"></script><script>Vue.use(ELEMENT);${helpers}\n${fixture}\n${components.map(c=>c.script).join('\n')}\nVue.component('workbench',Workbench);new Vue({el:'#app'});</script></body></html>`
;(async()=>{
 const browser=await chromium.launch({executablePath:process.env.MES_B03_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
 try {
  const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error' || (m.type()==='warning' && m.text().includes('[Vue warn]')))errors.push(m.text())})
  await page.route('http://localhost/**',route=>{const url=new URL(route.request().url());return url.pathname.startsWith('/node_modules/')?route.fulfill({path:'.'+url.pathname}):route.fulfill({contentType:'text/html; charset=utf-8',body:html})})
  await page.goto('http://localhost/b03-mock');await page.getByRole('button',{name:'查看',exact:true}).click();await page.getByText('安装实际批次电池').waitFor()
  await page.getByRole('button',{name:'开工',exact:true}).click();await page.getByRole('button',{name:/确定|确 定/}).last().click();await page.getByRole('button',{name:'批量报工'}).click()
  const dialog=page.getByRole('dialog',{name:'批量装配报工'});await dialog.getByRole('button',{name:'选择本页',exact:true}).click();await dialog.getByText('已选择 10 件',{exact:true}).waitFor()
  for(const [index,selection] of [['B06',['L01','L02']],['B07',['L03','L04','L05']],['B08',['L06','L07','L08','L09','L10']]].entries()){
   const [batch,codes]=selection;const material=dialog.locator('section[aria-label="实际投入分组"]');await material.locator('.el-table__body tr').filter({hasText:batch}).getByRole('button',{name:'新增分组'}).click();const group=material.locator('.material-group').nth(index);await group.locator('.el-select__input').click();for(const code of codes)await page.locator(`.execution-material-group-${index+1} .el-select-dropdown__item`).filter({hasText:code}).click();await group.locator('.group-heading strong').click();await group.getByPlaceholder('每件实际数量').fill('1')
  }
  const evidence=process.env.MES_B03_MOCK_EVIDENCE||'../output';await page.locator('.execution-material-group-3').waitFor({state:'hidden'});await dialog.getByText('3. 核对提交',{exact:true}).scrollIntoViewIfNeeded();await dialog.getByRole('button',{name:'确认报工 10 件'}).waitFor();await page.screenshot({path:evidence+'/mes-b03-mock-groups.png'});await dialog.getByRole('button',{name:'确认报工 10 件'}).click();await page.getByRole('button',{name:'查询原操作结果'}).waitFor();await dialog.waitFor({state:'hidden'});assert(await page.getByRole('button',{name:'批量报工'}).isDisabled());await page.screenshot({path:evidence+'/mes-b03-mock-unknown.png'});await page.getByRole('button',{name:'查询原操作结果'}).click();await page.getByRole('button',{name:'重试原操作'}).click();await page.getByText(/报工单 ID：501/).waitFor();await page.getByRole('button',{name:'完成装配任务'}).click();await page.getByRole('button',{name:/确定|确 定/}).last().click();await page.locator('.task-detail').getByText('已完成',{exact:true}).waitFor();await page.locator('.el-message-box__wrapper').waitFor({state:'hidden'});await page.screenshot({path:evidence+'/mes-b03-mock-complete.png'});assert.deepStrictEqual(errors,[]);console.log('Chrome mock workbench: start, 10 grouped SN, unknown recovery, same payload retry, receipt and finish passed')
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1})
