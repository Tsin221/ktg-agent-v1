// Actual Chrome rendering and interaction of production Vue components, using explicit API fixtures only.
const assert = require('assert'), fs = require('fs'), path = require('path'), compiler = require('vue-template-compiler')
const { chromium } = require(process.env.MES_PLAYWRIGHT || 'C:/Users/Alice/AppData/Local/npm-cache/_npx/1e6cc5b018c8a4f1/node_modules/playwright')
const dir = 'src/views/mes/pro/execution/'
const output = process.env.MES_BROWSER_OUTPUT || '../.runtime/mes-b07-p102-105-test-20261124/browser-components'
fs.mkdirSync(output, { recursive: true })
const helpers = fs.readFileSync(dir + 'reportContract.js', 'utf8').replace(/export (function|class|const)/g, '$1')
const presentation = fs.readFileSync(dir + 'taskPresentation.js', 'utf8').replace(/export (function|const)/g, '$1')
const components = [
  ['PackingPanel', dir + 'PackingPanel.vue'],
  ['ExecutionOrderProgress', dir + 'ExecutionOrderProgress.vue'],
  ['ExecutionReleaseDialog', 'src/views/mes/pro/workorder/ExecutionReleaseDialog.vue']
].map(([name, file]) => {
  const s = compiler.parseComponent(fs.readFileSync(file, 'utf8'))
  return `const ${name}=(()=>{${s.script.content.replace(/^import .*$/mg, '').replace('export default', 'const component=')};component.template=${JSON.stringify(s.template.content)};return component})()`
}).join('\n')

// Deliberately conflicting old compatibility fields ensure the UI renders only the authoritative scalar and scoped task rows.
const detail = {
  order: { workorderId: 1, version: 3, status: 'CONFIRMED', workorderCode: 'B07-MOCK', routeName: '示例路线', plannedUnits: 2 },
  summary: { allocated: 1, assembled: 1, qualified: 0, packed: 0, scrapped: 0 },
  tasks: [
    { taskId: 2, taskCode: 'VISIBLE-QC', stepKind: 'INSPECTION', processName: '过程检验', workstationName: '质检台', status: 'STARTED', quantity: 2, quantityProduced: 2, actualStartedAt: '2026-11-23 10:00:00' },
    { taskId: 3, taskCode: 'VISIBLE-PACK', stepKind: 'PACKING', processName: '单件包装', workstationName: '包装台', status: 'STARTED', quantity: 2, quantityProduced: 0, actualStartedAt: '2026-11-23 11:00:00' }
  ],
  units: [{ snId: 1, snCode: 'L01', unitState: 'WAIT_PACKING' }],
  progress: { planned: 2, allocated: 2, assembled: 2, waitingAssembly: 0, waitingInspection: 0, pendingDisposition: 1, reworkInProgress: 0, waitingReinspection: 0, qualified: 1, packable: 1, packed: 0, scrapped: 0, firstInspectionRejected: 1, reinspectionCount: 0, completionAllowed: false, completionReasons: ['仍有待处置单件'], tasks: [{ taskId: 999, taskCode: 'PEER-HIDDEN', stepKind: 'ASSEMBLY', status: 'STARTED' }] }
}
const html = `<html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/node_modules/element-ui/lib/theme-chalk/index.css"><body><div id="app"><packing-panel ref="pack" @submit="packed"></packing-panel><execution-release-dialog ref="release" @released="changed"></execution-release-dialog><p id="result">{{result}}</p></div><script src="/node_modules/vue/dist/vue.js"></script><script src="/node_modules/element-ui/lib/index.js"></script><script>
Vue.use(ELEMENT);Vue.directive('hasPermi',{});Vue.component('pagination',{template:'<div></div>'});Vue.prototype.$store={state:{user:{id:9}}};${presentation};
const listExecutionTaskUnits=async()=>({rows:[{snId:1,snCode:'L01',unitState:'WAIT_PACKING'},{snId:2,snCode:'L03',unitState:'WAIT_DISPOSITION'}],total:2});
${helpers};const executionOperation=async()=>({});
const completeExecutionOrder=async(id,p)=>{window.completionPayload={id,p};app.detail.order.status='FINISHED';app.detail.progress.completionAllowed=false;return {code:200,data:{operationId:p.operationId,committedAt:'2026-11-23 12:00:00'}}};
const executionOrder=async()=>({data:app.detail});
${components};
window.app=new Vue({el:'#app',components:{PackingPanel,ExecutionReleaseDialog},data:{result:'',detail:${JSON.stringify(detail)}},mounted(){this.$refs.pack.open({taskId:3,canPack:true})},methods:{packed(id,p){this.result=JSON.stringify(p);this.$refs.pack.visible=false;this.detail.progress.packable=0;this.detail.progress.packed=1;this.detail.tasks[1].quantityProduced=1;this.detail.units[0].unitState='PACKED';this.$refs.release.open(this.detail.order)},changed(){this.result='completed'}}});
</script></body></html>`

;(async () => {
  const browser = await chromium.launch({ executablePath: process.env.MES_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1200 } }), errors = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('console', m => { if (m.type() === 'error' || (m.type() === 'warning' && m.text().includes('[Vue warn]'))) errors.push(m.text()) })
    // Explicitly route all navigation/API traffic. No live service or external site is involved.
    await page.route('**/*', r => {
      const url = new URL(r.request().url())
      if (url.origin !== 'http://localhost') return r.abort()
      if (url.pathname.startsWith('/node_modules/')) return r.fulfill({ path: '.' + url.pathname })
      if (url.pathname === '/b07-mock') return r.fulfill({ contentType: 'text/html', body: html })
      return r.abort()
    })
    await page.goto('http://localhost/b07-mock')
    const packing = page.getByRole('dialog', { name: '登记单件包装' })
    await packing.getByText('L01', { exact: true }).waitFor()
    assert.strictEqual(await packing.getByText('L03', { exact: true }).count(), 0)
    assert.strictEqual(await packing.getByText(/装箱行|箱号/).count(), 0)
    assert.strictEqual(await packing.locator('input:not([type="checkbox"])').count(), 0)
    await packing.locator('.el-table__body .el-checkbox').click()
    await page.screenshot({ path: path.join(output, 'b07-packing-component.png'), animations: 'disabled' })
    await packing.getByRole('button', { name: '确认包装 1 件' }).click()
    await page.waitForFunction(() => app.result.includes('snIds'))
    await packing.waitFor({ state: 'hidden' })
    assert.deepStrictEqual(await page.evaluate(() => JSON.parse(app.result)), { snIds: [1] })

    const release = page.getByRole('dialog', { name: '生产执行' })
    const progress = release.getByRole('region', { name: '工单真实生产进度' })
    await progress.getByRole('heading', { name: '整单生产进度' }).waitFor()
    assert.strictEqual(await release.getByRole('heading', { name: '整单生产进度' }).count(), 1)
    assert.strictEqual(await release.getByText('已分配', { exact: true }).count(), 1)
    assert.strictEqual(await release.getByText(/已分配 SN 总数/).count(), 0)
    const allocated = progress.getByText('已分配', { exact: true }).locator('xpath=following-sibling::*[1]')
    assert.strictEqual((await allocated.textContent()).trim(), '2')
    for (const code of ['VISIBLE-QC', 'VISIBLE-PACK']) {
      assert.strictEqual(await release.getByText(code, { exact: true }).count(), 1)
    }
    assert.strictEqual(await release.getByText('PEER-HIDDEN', { exact: true }).count(), 0)
    assert.strictEqual(await progress.getByText('质检台', { exact: true }).count(), 1)
    assert.strictEqual(await progress.getByText('包装台', { exact: true }).count(), 1)
    assert.strictEqual(await progress.getByText('仍有待处置单件', { exact: true }).count(), 1)
    const complete = progress.getByRole('button', { name: '完结工单', exact: true })
    assert(await complete.isDisabled())
    await page.screenshot({ path: path.join(output, 'b07-order-progress-component.png'), animations: 'disabled' })

    await page.evaluate(() => {
      Object.assign(app.detail.progress, { pendingDisposition: 0, packable: 0, packed: 1, scrapped: 1, completionAllowed: true, completionReasons: [] })
      app.detail.tasks.forEach(task => { task.status = 'FINISHED'; task.actualEndedAt = '2026-11-23 11:30:00' })
      app.detail.units[0].unitState = 'PACKED'
    })
    await complete.click()
    const confirmation = page.getByRole('button', { name: /确定|确 定/ }).last()
    await confirmation.click()
    await page.waitForFunction(() => app.result === 'completed')
    await confirmation.waitFor({ state: 'hidden' })
    const completion = await page.evaluate(() => window.completionPayload)
    assert.strictEqual(completion.id, 1)
    assert.strictEqual(completion.p.version, 3)
    assert(completion.p.operationId)
    assert.strictEqual(await progress.getByText(/工单完结已提交/).count(), 1)
    await page.screenshot({ path: path.join(output, 'b07-order-complete-component.png'), animations: 'disabled' })
    assert.deepStrictEqual(errors, [])
    console.log('Chrome mock component interaction passed: qualified SN only/no carton field, one global total, scoped task rows, blocked premature completion, original confirmed completion + receipt.')
    console.log('Screenshots:', path.resolve(output))
  } finally { await browser.close() }
})().catch(e => { console.error(e); process.exitCode = 1 })
