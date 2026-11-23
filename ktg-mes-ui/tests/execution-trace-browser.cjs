// Render production Vue SFC templates in real Chrome against explicit mock read APIs.
// This is component verification; independent live application acceptance is separate.
const assert = require('assert')
const fs = require('fs')
const compiler = require('vue-template-compiler')
const { chromium } = require(process.env.MES_PLAYWRIGHT || 'C:/Users/Alice/AppData/Local/npm-cache/_npx/1e6cc5b018c8a4f1/node_modules/playwright')
const dir = 'src/views/mes/pro/execution/trace/'
const names = ['TraceDocumentDialog', 'UnitFacts', 'UnitTracePanel', 'MaterialTracePanel', 'index']
const scripts = []
const styles = []
for (const name of names) {
  const sfc = compiler.parseComponent(fs.readFileSync(dir + name + '.vue', 'utf8'))
  scripts.push(`const ${name}=(()=>{${sfc.script.content.replace(/^import .*$/mg, '').replace('export default', 'const component=')};component.template=${JSON.stringify(sfc.template.content)};return component})()`)
  styles.push(...sfc.styles.map(style => style.content))
}
const pagination = compiler.parseComponent(fs.readFileSync('src/components/Pagination/index.vue', 'utf8'))
const paginationScript = pagination.script.content.replace(/^import .*$/mg, '').replace('export default', 'const component=')
const helpers = ['src/views/mes/pro/execution/taskPresentation.js', dir + 'tracePresentation.js'].map(path => fs.readFileSync(path, 'utf8').replace(/export (function|const)/g, '$1')).join('\n')
const fixture = {
  traceStatus: 'TRACKED', snId: '9007199254740993', snCode: 'L03', itemId: '40', itemCode: 'P01', itemName: '成品灯具', unitState: 'PACKED',
  workorderId: 7, workorderCode: 'MO-TRACE-R', workorderName: '追溯案例 R', orderStatus: 'FINISHED', routeId: 6, routeCode: 'ROUTE-1', routeName: '三道工序', releasedAt: '2026-11-23 09:00:00', releaseUserId: 1, currentTaskId: 13,
  tasks: [{ taskId: 11, taskCode: 'ASSEMBLY', processName: '装配', taskKind: 'NORMAL', status: 'FINISHED', normalStepNo: 1 }, { taskId: 12, taskCode: 'INSPECTION', processName: '质检', taskKind: 'NORMAL', status: 'FINISHED', normalStepNo: 2 }, { taskId: 13, taskCode: 'PACKING', processName: '包装', taskKind: 'NORMAL', status: 'FINISHED', normalStepNo: 3 }, { taskId: 14, taskCode: 'REWORK', processName: '返修', taskKind: 'REWORK', status: 'FINISHED', sourceDispositionId: 9 }],
  assemblyFeedback: { recordId: 40, feedbackTime: '2026-11-23 10:00:00', quantityFeedback: 10, nickName: '生产员' },
  inputs: [{ inputId: 3, taskId: 11, itemId: 77, itemCode: 'M77', itemName: '电池', sourceBatchCode: 'B07', quantity: 1, unitOfMeasure: '个', usedBy: 2, usedAt: '2026-11-23 10:00:00', transactions: [{ transactionId: 101, transactionType: 'CONSUME', transactionQuantity: 3 }] }],
  inspections: [{ inspectionId: '3', roundNo: 1, ipqcId: '20', ipqcCode: 'QC-001', taskId: '12', checkResult: 'REJECT', lines: [{ lineId: '1', indexName: '电压', remark: '{"value":2.9}' }], defects: [{ recordId: '1', defectName: '电压异常', defectQuantity: 1 }] }, { inspectionId: '11', roundNo: 2, ipqcId: '22', ipqcCode: 'QC-011', taskId: '12', checkResult: 'ACCEPT', produceLineId: '11', produceRecordId: '100', producedBatchCode: 'P-07', quantityProduce: 1, produceTransactions: [{ transactionId: '102', transactionQuantity: 1, transactionFlag: 1, transactionDate: '2026-11-23 10:45:00' }], lines: [{ lineId: '2', indexName: '电压', remark: '{"value":3.5}' }], defects: [] }],
  dispositions: [{ dispositionId: 9, inspectionId: 3, decision: 'REWORK', reason: '修复电路后复检', reworkTaskId: 14, decidedBy: 1, decidedAt: '2026-11-23 10:20:00' }],
  packing: { taskId: 13, packedBy: 3, packedAt: '2026-11-23 11:00:00' }
}
const html = `<html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/node_modules/element-ui/lib/theme-chalk/index.css"><style>body{margin:24px;font:14px sans-serif} ${styles.join('\n')}</style><body><div id="app"><execution-trace /></div><script src="/node_modules/vue/dist/vue.js"></script><script src="/node_modules/element-ui/lib/index.js"></script><script>
Vue.use(ELEMENT);Vue.prototype.$store={getters:{permissions:['mes:pro:execution:query','mes:pro:execution:trace']}};
const scrollTo=()=>{};Vue.component('pagination',(()=>{${paginationScript};component.template=${JSON.stringify(pagination.template.content)};return component})());
const ItemSelect={template:'<div></div>'};const getIpqc=()=>Promise.reject(Error('未使用'));const getFeedback=getIpqc;
${helpers}
const fixture=${JSON.stringify(fixture)};
const traceExecutionUnit=async ({snCode})=>{if(snCode==='LEGACY')return {data:{snId:99,snCode,traceStatus:'LEGACY_NO_RECORD'}};if(snCode!=='L03')throw Error('未查询到可见记录');return {data:fixture}};
const traceExecutionMaterials=async ({itemId,batchCode,pageNum})=>{if(itemId!=='77'||batchCode!=='B07')return {rows:[],total:0};return {rows:(pageNum===1?['L03','L04','L05','L11','L12','L13','L14','L15','L16','L17']:['L18']).map((snCode,i)=>({snId:String(i+1),snCode,workorderCode:'MO-TRACE-R',itemCode:'P01',itemName:'成品灯具',unitState:'PACKED',inputCount:1,firstUsedAt:'2026-11-23 10:00:00',lastUsedAt:'2026-11-23 10:00:00'})),total:11}};
${scripts.join('\n')};new Vue({el:'#app',components:{ExecutionTrace:index}});</script></body></html>`

;(async()=>{
  const browser = await chromium.launch({ executablePath: process.env.MES_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error' || (message.type() === 'warning' && message.text().includes('[Vue warn]'))) errors.push(message.text()) })
    await page.route('http://localhost/**', route => new URL(route.request().url()).pathname.startsWith('/node_modules/') ? route.fulfill({ path: '.' + new URL(route.request().url()).pathname }) : route.fulfill({ contentType: 'text/html', body: html }))
    await page.goto('http://localhost/mes-b06-mock')
    await page.getByPlaceholder('输入唯一成品 SN').fill('L03')
    await page.getByPlaceholder('输入唯一成品 SN').press('Enter')
    await page.getByText('第 2 轮检验', { exact: false }).waitFor()
    assert(await page.getByText('修复电路后复检', { exact: true }).isVisible())
    assert(await page.getByText('无箱号关联', { exact: true }).isVisible())
    assert(await page.getByText('不合格', { exact: true }).isVisible())
    assert(await page.getByText('合格', { exact: true }).isVisible())
    assert(await page.getByText('9007199254740993', { exact: true }).isVisible())
    await page.screenshot({ path: '../output/mes-b06-mock-unit.png', fullPage: true })
    await page.getByRole('button', { name: 'B07', exact: true }).click()
    await page.getByRole('button', { name: 'L05', exact: true }).waitFor()
    assert(await page.getByText('已提交条件：物料 ID 77 · 批次 B07 · 共 11 件可见成品', { exact: true }).isVisible())
    await page.screenshot({ path: '../output/mes-b06-mock-material.png', fullPage: true })
    await page.locator('.el-pagination .btn-next').click()
    await page.getByRole('button', { name: 'L18', exact: true }).waitFor()
    assert.strictEqual(await page.getByRole('button', { name: 'L05', exact: true }).count(), 0)
    await page.locator('.el-pagination .btn-prev').click()
    await page.getByRole('button', { name: 'L03', exact: true }).click()
    await page.getByText('第 2 轮检验', { exact: false }).waitFor()
    await page.getByRole('tab', { name: '原料批次追溯' }).click()
    await page.getByPlaceholder('输入完整原料批次号').fill(' B07 ')
    await page.getByPlaceholder('输入完整原料批次号').press('Enter')
    await page.getByText('无可见实际投入记录；不能据此推断没有发生历史业务。', { exact: true }).waitFor()
    await page.getByRole('tab', { name: '成品 SN 追溯' }).click()
    await page.getByPlaceholder('输入唯一成品 SN').fill('LEGACY')
    await page.getByPlaceholder('输入唯一成品 SN').press('Enter')
    await page.getByText('该历史 SN 尚无新生产执行闭环采集记录。不能据此判断历史业务是否发生。', { exact: true }).waitFor()
    assert.strictEqual(await page.getByText('第 2 轮检验', { exact: false }).count(), 0)
    await page.getByPlaceholder('输入唯一成品 SN').fill('UNKNOWN')
    await page.getByPlaceholder('输入唯一成品 SN').press('Enter')
    await page.getByText('未查询到可见记录', { exact: true }).waitFor()
    assert.strictEqual(await page.getByText('成品身份', { exact: true }).count(), 0)
    for (const width of [320, 768, 1024]) {
      await page.setViewportSize({ width, height: 900 })
      assert(await page.getByPlaceholder('输入唯一成品 SN').isVisible())
      const submit = page.getByRole('region', { name: '成品 SN 追溯查询' }).locator('button[type=submit]')
      await submit.waitFor({ state: 'visible' })
      assert(/查询/.test(await submit.innerText()), `visible query button at ${width}px`)
    }
    assert.deepStrictEqual(errors, [])
    console.log('Chrome mock trace: rework history, packing, both direction drilldown, pagination, legacy, and failed query passed; no Vue/runtime errors.')
  } finally { await browser.close() }
})().catch(error=>{ console.error(error); process.exitCode=1 })
