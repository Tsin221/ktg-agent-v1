// Isolated live browser acceptance. Requires an existing approved instance and temporary ordinary account.
// No fixtures or roles are created here; never point this test at a shared or production server.
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const { chromium } = require(process.env.MES_B03_PLAYWRIGHT || 'C:/Users/Alice/AppData/Local/npm-cache/_npx/1e6cc5b018c8a4f1/node_modules/playwright')
const base = process.env.MES_B03_UI_URL
const accountFile = process.env.MES_B03_ACCOUNT_FILE
const evidence = process.env.MES_B03_EVIDENCE_DIR
if (!base || !accountFile || !evidence || !/^http:\/\/(localhost|127\.0\.0\.1):\d+$/.test(base)) throw Error('Specify isolated localhost URL, account file and evidence directory.')
const expectedOrderId = process.env.MES_B03_ORDER_ID || '991000058'
const expectedTaskId = process.env.MES_B03_TASK_ID || '991000107'
const orderCode = process.env.MES_B03_ORDER_CODE || 'B02-LIVE-UI-10'
const batches = (process.env.MES_B03_BATCH_CODES || 'B06,B07,B08').split(',')
assert.strictEqual(batches.length,3)
const account = JSON.parse(fs.readFileSync(accountFile,'utf8').replace(/^\uFEFF/,''))
const save = (name, value) => fs.writeFileSync(path.join(evidence,name), JSON.stringify(value,null,2))
const waitPost = (page,suffix) => page.waitForResponse(r=>r.url().endsWith(suffix) && r.request().method()==='POST')
;(async()=>{
  const browser=await chromium.launch({executablePath:process.env.MES_B03_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1080}})
    const errors=[];page.on('pageerror',error=>errors.push(error.message))
    page.on('request',r=>{if(r.method()!=='POST')return;const match=new URL(r.url()).pathname.match(/\/execution\/tasks\/\d+\/(starts|reports|completions)$/);if(match)save('mes-b03-live-'+({starts:'start',reports:'report',completions:'completion'}[match[1]])+'-request.json',r.postDataJSON())})
    const diagnostic=process.env.MES_B03_READONLY_DIAG==='1'
    const executionNetwork=[], sessionResponses=[]
    page.on('response',async r=>{if(r.url().includes('/prod-api/') || r.url().includes('/execution/')){try{const body=await r.json();const endpoint=new URL(r.url());if(endpoint.pathname.includes('/execution/'))executionNetwork.push({path:endpoint.pathname,query:endpoint.search,status:r.status(),code:body.code,rows:diagnostic&&body.rows&&body.rows.map(row=>({taskId:row.taskId,status:row.status,availableUnits:row.availableUnits})),message:body.msg});if(body.code===401||r.status()===401)sessionResponses.push({path:endpoint.pathname,status:r.status(),code:body.code})}catch(_){if(r.status()===401)sessionResponses.push({path:new URL(r.url()).pathname,status:r.status()})}}})
    const checkBrowser = () => {
      // Existing layout auto-queries the global print client on login. An ordinary
      // execution account can lack that unrelated permission. Account for exactly
      // the observed print 401 errors; never ignore execution or other API errors.
      const print401=sessionResponses.filter(r=>r.path==='/prod-api/print/client/getAll' && r.code===401).length
      let remaining=print401
      const unexpected=errors.filter(message=>{if(remaining && message==='无效的会话，或者会话已过期，请重新登录。'){remaining--;return false}return true})
      assert.deepStrictEqual(sessionResponses.filter(r=>r.path!=='/prod-api/print/client/getAll'),[])
      assert.deepStrictEqual(executionNetwork.filter(r=>r.code!==200),[])
      assert.deepStrictEqual(unexpected,[])
      return { print401, errors, executionNetwork, sessionResponses, unexpected }
    }
    await page.goto(base+'/login')
    await page.getByPlaceholder('账号',{exact:true}).fill(account.username)
    await page.getByPlaceholder('密码',{exact:true}).fill(account.password)
    if(await page.getByRole('button',{name:'取消',exact:true}).isVisible()) await page.getByRole('button',{name:'取消',exact:true}).click()
    await page.getByRole('button',{name:'登 录'}).click()
    await page.getByText('生产管理',{exact:true}).waitFor()
    if(!(await page.getByText('车间工作台',{exact:true}).first().isVisible())) await page.getByText('生产管理',{exact:true}).click()
    await page.getByText('车间工作台',{exact:true}).first().click()
    await page.getByRole('heading',{name:'车间工作台'}).waitFor()
    const list=page.locator('section[aria-label="生产任务列表"]')
    const detail=page.locator('section[aria-label="任务详情"]')
    if(diagnostic) {
      await list.getByPlaceholder('工单 ID').fill(expectedOrderId)
      await page.screenshot({path:path.join(evidence,'mes-b03-live-before-query-diagnostic.png')})
      console.log('list before query:',await list.innerText(),'buttons:',await list.getByRole('button').allInnerTexts())
      const queried=page.waitForResponse(r=>r.url().includes('/execution/tasks?')&&r.url().includes('workorderId='+expectedOrderId))
      await list.getByRole('button').filter({hasText:'查询'}).click()
      const queryResponse=await (await queried).json()
      assert.strictEqual(queryResponse.code,200)
      const row=queryResponse.rows.find(r=>String(r.taskId)===expectedTaskId)
      assert(row && row.workorderCode===orderCode)
      assert.strictEqual(row.status,'FINISHED');assert.strictEqual(row.availableUnits,0)
      const detailed=page.waitForResponse(r=>r.url().endsWith('/execution/tasks/'+expectedTaskId))
      await list.locator('.el-table__body tr').filter({hasText:row.taskCode}).getByRole('button',{name:'查看',exact:true}).click()
      const detailResponse=await (await detailed).json()
      assert.strictEqual(detailResponse.code,200);assert.strictEqual(detailResponse.data.status,'FINISHED');assert.strictEqual(detailResponse.data.availableUnits,0)
      assert(detailResponse.data.actualStartedAt && detailResponse.data.actualEndedAt)
      await detail.getByText('已完成',{exact:true}).waitFor()
      await page.waitForTimeout(300)
      await page.screenshot({path:path.join(evidence,'mes-b03-live-query-diagnostic.png')})
      save('mes-b03-live-query-diagnostic.json',{executionNetwork,sessionResponses,errors,input:await list.getByPlaceholder('工单 ID').inputValue(),visibleList:await list.innerText(),finalTask:{taskId:detailResponse.data.taskId,status:detailResponse.data.status,availableUnits:detailResponse.data.availableUnits,quantityProduced:detailResponse.data.quantityProduced,actualStartedAt:detailResponse.data.actualStartedAt,actualEndedAt:detailResponse.data.actualEndedAt}})
      console.log(JSON.stringify({executionNetwork,sessionResponses,errors,input:await list.getByPlaceholder('工单 ID').inputValue(),visibleList:await list.innerText()}))
      checkBrowser()
      return
    }
    async function selectAssembly(state) {
      await list.getByPlaceholder('工单 ID').fill(expectedOrderId)
      const queried=page.waitForResponse(r=>r.url().includes('/execution/tasks?')&&r.url().includes('workorderId='+expectedOrderId))
      await list.getByRole('button').filter({hasText:'查询'}).click()
      const rows=(await (await queried).json()).rows
      const row=rows.find(r=>String(r.taskId)===expectedTaskId)
      assert(row && row.workorderCode===orderCode, 'expected ordinary-account assembly task must be visible')
      assert.strictEqual(row.status,state)
      await list.locator('.el-table__body tr').filter({hasText:row.taskCode}).getByRole('button',{name:'查看',exact:true}).click()
      await detail.getByText(row.taskCode+' · '+row.processName,{exact:true}).waitFor()
    }
    await selectAssembly('READY')
    await detail.getByRole('button',{name:'开工',exact:true}).click()
    const started=waitPost(page,`/tasks/${expectedTaskId}/starts`)
    await page.getByRole('button',{name:/确定|确 定/}).last().click()
    const startResponse=await (await started).json();assert.strictEqual(startResponse.code,200);save('mes-b03-live-start-response.json',startResponse)
    await detail.getByRole('button',{name:'批量报工',exact:true}).waitFor()
    await page.screenshot({path:path.join(evidence,'mes-b03-live-started.png')})
    await detail.getByRole('button',{name:'批量报工',exact:true}).click()
    const dialog=page.getByRole('dialog',{name:'批量装配报工'})
    await dialog.getByRole('button',{name:'选择本页',exact:true}).waitFor()
    await dialog.getByRole('button',{name:'选择本页',exact:true}).click()
    await dialog.getByText('已选择 10 件',{exact:true}).waitFor()
    const snCodes=(await dialog.locator('.selection .el-tag').allInnerTexts()).map(v=>v.trim()).sort()
    assert.strictEqual(snCodes.length,10)
    const materials=dialog.locator('section[aria-label="实际投入分组"]')
    const partitions=[snCodes.slice(0,2),snCodes.slice(2,5),snCodes.slice(5)]
    for(let i=0;i<3;i++) {
      const batch=batches[i]
      await materials.getByPlaceholder('原料批次').fill(batch)
      const listed=page.waitForResponse(r=>r.url().includes('/available-materials?') && r.url().includes('batchCode='+encodeURIComponent(batch)))
      await materials.getByRole('button',{name:'查询库存',exact:true}).click()
      const stockRows=(await (await listed).json()).rows
      assert(stockRows.some(r=>r.sourceBatchCode===batch),`missing actual batch ${batch}`)
      await materials.locator('.el-table__body tr').filter({hasText:batch}).first().getByRole('button',{name:'新增分组',exact:true}).click()
      const group=materials.locator('.material-group').nth(i)
      await group.locator('.el-select__input').click()
      for (const code of partitions[i]) await page.locator(`.execution-material-group-${i+1} .el-select-dropdown__item`).filter({hasText:code}).click()
      await group.locator('.group-heading strong').click()
      await group.getByPlaceholder('每件实际数量').fill('1')
    }
    await page.locator('.execution-material-group-3').waitFor({state:'hidden'})
    await dialog.getByText('3. 核对提交',{exact:true}).scrollIntoViewIfNeeded()
    await page.screenshot({path:path.join(evidence,'mes-b03-live-report-groups.png'),fullPage:true})
    const reported=waitPost(page,`/tasks/${expectedTaskId}/reports`)
    await dialog.getByRole('button',{name:'确认报工 10 件',exact:true}).click()
    const report=await reported;save('mes-b03-live-report-request.json',report.request().postDataJSON())
    const reportResponse=await report.json();save('mes-b03-live-report-response.json',reportResponse);assert.strictEqual(reportResponse.code,200)
    const reportRef=typeof reportResponse.data.resultRef==='string'?JSON.parse(reportResponse.data.resultRef):reportResponse.data.resultRef
    assert.strictEqual(reportRef.snCount,10);assert.strictEqual(reportRef.assembled,10);assert.strictEqual(reportRef.qualified,0)
    await page.getByText(/报工单 ID：/).waitFor()
    await detail.getByRole('button',{name:'完成装配任务',exact:true}).waitFor()
    await page.screenshot({path:path.join(evidence,'mes-b03-live-report-receipt.png')})
    await page.reload()
    await page.getByRole('heading',{name:'车间工作台'}).waitFor()
    await selectAssembly('STARTED')
    assert.strictEqual(await detail.getByRole('button',{name:'批量报工',exact:true}).count(),0)
    await detail.getByRole('button',{name:'完成装配任务',exact:true}).click()
    const completed=waitPost(page,`/tasks/${expectedTaskId}/completions`)
    await page.getByRole('button',{name:/确定|确 定/}).last().click()
    const completionResponse=await (await completed).json();save('mes-b03-live-completion-response.json',completionResponse);assert.strictEqual(completionResponse.code,200)
    await detail.getByText('已完成',{exact:true}).waitFor()
    await page.locator('.el-message-box__wrapper').waitFor({state:'hidden'})
    await page.screenshot({path:path.join(evidence,'mes-b03-live-finished.png')})
    save('mes-b03-live-browser-health.json',checkBrowser())
    console.log('B03 isolated ordinary-account UI: READY → STARTED, 10 SN grouped across real B06/B07/B08, receipt, refresh, FINISHED passed.')
  } finally { await browser.close() }
})().catch(error=>{console.error(error.message);process.exitCode=1})
