// Real isolated MES browser acceptance. The parent prepares ordinary accounts and task assignment.
const assert=require('assert'),fs=require('fs'),path=require('path')
const {chromium}=require('C:/Users/Alice/AppData/Local/npm-cache/_npx/1e6cc5b018c8a4f1/node_modules/playwright')
const base=process.env.MES_B04_UI_URL||'http://127.0.0.1:18841'
if(!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base))throw Error('Isolated localhost only')
const evidence=process.env.MES_B04_EVIDENCE_DIR||'../.runtime/mes-b04-20261123'
const account=JSON.parse(fs.readFileSync(process.env.MES_B04_ACCOUNT_FILE||path.join(evidence,'inspector-account.json'),'utf8').replace(/^\uFEFF/,''))
let activePage,activeNetwork=[],activeErrors=[];const sessions=[];
const checkErrors=errors=>{const allowed=sessions.filter(s=>s.path==='/prod-api/print/client/getAll'&&s.code===401).length;let remaining=allowed;const unexpected=errors.filter(e=>{const message=typeof e==='string'?e:e.message;if(remaining&&message==='无效的会话，或者会话已过期，请重新登录。'){remaining--;return false}return true});assert.deepStrictEqual(sessions.filter(s=>s.path!=='/prod-api/print/client/getAll'),[]);assert.deepStrictEqual(unexpected,[])};
const pendingFile=path.join(evidence,'inspection-browser-pending.json');
const save=(name,value)=>fs.writeFileSync(path.join(evidence,name),JSON.stringify(value,null,2))
;(async()=>{const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});try{
 const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[],network=[]
 activePage=page;activeNetwork=network;activeErrors=errors;page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(r.url().includes('/prod-api/')||r.url().includes('/execution/'))try{const b=await r.json();if(b.code===401||r.status()===401)sessions.push({path:new URL(r.url()).pathname,code:b.code});if(!r.url().includes('/execution/'))return;network.push({path:new URL(r.url()).pathname,code:b.code,msg:b.msg})}catch(_){}})
 if(fs.existsSync(pendingFile)){const pending=JSON.parse(fs.readFileSync(pendingFile,'utf8'));await page.addInitScript(p=>{for(const [key,value] of p)localStorage.setItem(key,value)},pending)}
 await page.goto(base+'/login');await page.getByPlaceholder('账号',{exact:true}).fill(account.username);await page.getByPlaceholder('密码',{exact:true}).fill(account.password)
 await page.waitForTimeout(1500);if(await page.getByRole('button',{name:'取消',exact:true}).isVisible())await page.getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'登 录'}).click();await page.getByText('生产管理',{exact:true}).waitFor();if(!(await page.getByText('车间工作台',{exact:true}).first().isVisible()))await page.getByText('生产管理',{exact:true}).click();await page.getByText('车间工作台',{exact:true}).first().click()
 await page.getByRole('heading',{name:'车间工作台'}).waitFor();await page.waitForTimeout(300);if(await page.getByRole('button',{name:'查询原操作结果'}).isVisible()){await page.getByRole('button',{name:'查询原操作结果'}).click();const retry=page.getByRole('button',{name:'重试原操作'});await retry.waitFor();const recovery=page.waitForResponse(r=>r.url().endsWith('/inspections')&&r.request().method()==='POST');await retry.click();const recovered=await(await recovery).json();save('live-inspection-recovery.json',recovered);assert.strictEqual(recovered.code,200);fs.unlinkSync(pendingFile)}
 const list=page.locator('section[aria-label="生产任务列表"]'),detail=page.locator('section[aria-label="任务详情"]')
 const response=page.waitForResponse(r=>r.url().includes('/execution/tasks?')&&r.url().includes('workorderId=991000058'))
 await list.getByPlaceholder('工单 ID').fill('991000058');await list.getByRole('button').filter({hasText:'查询'}).click()
 const row=(await(await response).json()).rows.find(x=>x.taskId===991000108);assert(row,'inspector must see task')
 await list.locator('.el-table__body tr').filter({hasText:row.taskCode}).getByRole('button',{name:'查看',exact:true}).click()
 await detail.locator('.detail-heading h2').filter({hasText:row.taskCode}).waitFor();await detail.getByRole('button',{name:'刷新详情'}).waitFor();await page.waitForTimeout(300);if(await detail.getByRole('button',{name:'开工',exact:true}).isVisible()){await detail.getByRole('button',{name:'开工',exact:true}).click();const started=page.waitForResponse(r=>r.url().endsWith('/starts')&&r.request().method()==='POST');await page.getByRole('button',{name:/确定|确 定/}).last().click();assert.strictEqual((await(await started).json()).code,200);await page.waitForTimeout(300)}
 for(let n=1;n<=10;n++){
  const code='B02LIVE-'+String(n).padStart(8,'0')
  const unitRow=detail.locator('.el-table__body tr').filter({hasText:code});const stateText=await unitRow.innerText();if(stateText.includes('待包装')||stateText.includes('待处置'))continue;await unitRow.getByRole('button',{name:'检验 / 历史 / 处置'}).click()
  const dialog=page.getByRole('dialog',{name:'单件质量记录 · '+code})
  await dialog.getByPlaceholder('输入实际数值').fill(n===3?'12':'10')
  if(n===3){await dialog.getByPlaceholder('选择真实缺陷').click();await page.locator('.el-select-dropdown:visible .el-select-dropdown__item').first().click();await dialog.locator('textarea').fill('实测12超出冻结上限11')}
  await page.screenshot({path:path.join(evidence,'live-inspection-'+n+'.png')})
  const posted=page.waitForResponse(r=>r.url().endsWith('/inspections')&&r.request().method()==='POST')
  await dialog.getByRole('button',{name:'提交本件检验'}).click();const result=await(await posted).json();save('live-inspection-'+n+'.json',result);assert.strictEqual(result.code,200)
  await dialog.waitFor({state:'hidden'});await page.waitForTimeout(300)
 }
 await page.screenshot({path:path.join(evidence,'live-inspection-final.png')});save('live-inspection-health.json',{errors,network,sessions});assert.deepStrictEqual(network.filter(x=>x.code!==200 && !(x.code===404 && x.path==='/prod-api/mes/pro/execution/operations/6f206a80-369a-4f5e-9bb8-9433b68c791d')),[]);checkErrors(errors);console.log('Real ordinary inspector: explicit start, 9 accept and L03 reject submitted via UI')
}catch(error){if(activePage){const pending=await activePage.evaluate(()=>Object.entries(localStorage).filter(([k])=>k.startsWith('mes-execution-pending-')));if(pending.length)fs.writeFileSync(pendingFile,JSON.stringify(pending));await activePage.screenshot({path:path.join(evidence,'execution-inspection-live.cjs-failure.png')});save('execution-inspection-live.cjs-failure.json',{error:String(error),network:activeNetwork,errors:activeErrors,sessions,text:await activePage.locator('body').innerText()})}throw error}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1})
