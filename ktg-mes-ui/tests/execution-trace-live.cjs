// Read-only browser acceptance against the approved isolated B06 closure backend.
// The companion fixture scripts create temporary accounts and produce real R/S evidence.
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const http = require('http')
const { chromium } = require(process.env.MES_PLAYWRIGHT || 'C:/Users/Alice/AppData/Local/npm-cache/_npx/1e6cc5b018c8a4f1/node_modules/playwright')

const run = path.resolve('../.runtime/mes-b06-closure')
const root = path.resolve('../.runtime/mes-b06-20261123/ui-trace-release')
const backendPort = 18088
const source = path.join(run, 'credentials.json')
const types = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ico': 'image/x-icon' }
function summary(file, expectedCase) {
  const resolved = path.resolve(run, file || '')
  assert.strictEqual(path.dirname(resolved), run, 'use isolated closure evidence only')
  const evidence = fs.readFileSync(resolved, 'utf8').trim().split(/\r?\n/).map(line => JSON.parse(line))
  const latest = evidence.findLast(record => record.passed === true && record.case === expectedCase)
  assert(latest && latest.sn.length === 10 && latest.batches.length === 3, 'closure result not verified')
  return latest
}

function serve() {
  assert(fs.existsSync(path.join(root, 'index.html')), 'isolated production build missing')
  const server = http.createServer((req, res) => {
    if (req.url.startsWith('/prod-api/')) {
      const proxy = http.request({ host: '127.0.0.1', port: backendPort, path: req.url.replace(/^\/prod-api/, ''), method: req.method, headers: req.headers }, upstream => { res.writeHead(upstream.statusCode, upstream.headers); upstream.pipe(res) })
      proxy.on('error', () => { res.writeHead(502); res.end('Isolated backend unavailable') })
      req.pipe(proxy)
      return
    }
    let target
    try { target = path.resolve(root, '.' + decodeURIComponent(req.url.split('?')[0])) } catch (_) { res.writeHead(400); res.end(); return }
    if (target !== root && !target.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return }
    if (!fs.existsSync(target) || !fs.statSync(target).isFile()) target = path.join(root, 'index.html')
    res.writeHead(200, { 'Content-Type': types[path.extname(target)] || 'application/octet-stream' })
    fs.createReadStream(target).pipe(res)
  })
  return new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', () => resolve(server)) })
}

async function requestByInput(page, input, endpoint, value) {
  const response = page.waitForResponse(res => res.url().includes(endpoint) && res.request().method() === 'GET')
  await input.fill(value); await input.press('Enter')
  return (await response).json()
}

async function verifyCase(browser, url, account, branch, other) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } })
  const runtimeErrors = [], startupErrors = [], networkErrors = [], traces = [], printStartup = []
  let phase = 'login'
  page.on('pageerror', error => {
    if (phase === 'login' && error.message === '无效的会话，或者会话已过期，请重新登录。') startupErrors.push(error.message)
    else runtimeErrors.push(error.message)
  })
  page.on('console', message => { if (message.type() === 'warning' && message.text().includes('[Vue warn]')) runtimeErrors.push(message.text()) })
  page.on('response', async response => {
    const pathname = new URL(response.url()).pathname
    if (!pathname.startsWith('/prod-api/')) return
    if (response.status() >= 400) networkErrors.push({ pathname, status: response.status() })
    if (/\/execution\/(units|materials)\/trace$/.test(pathname)) traces.push({ pathname, query: new URL(response.url()).search, status: response.status() })
    if (pathname === '/prod-api/print/client/getAll' && phase === 'login') {
      try { printStartup.push({ pathname, status: response.status(), code: (await response.json()).code }) } catch (_) { printStartup.push({ pathname, status: response.status(), code: null }) }
    }
  })
  try {
    await page.goto(url + '/login')
    await page.getByPlaceholder('账号', { exact: true }).fill(account.username)
    await page.getByPlaceholder('密码', { exact: true }).fill(account.password)
    if (await page.getByRole('button', { name: '取消', exact: true }).isVisible()) await page.getByRole('button', { name: '取消', exact: true }).click()
    await page.getByRole('button', { name: '登 录' }).click()
    await page.waitForURL(value => !value.pathname.endsWith('/login'))
    phase = 'trace'
    await page.goto(url + '/mes/pro/execution-trace')
    await page.getByRole('heading', { name: '双向追溯', exact: true }).waitFor()
    const snInput = page.getByPlaceholder('输入唯一成品 SN')
    const bad = branch.sn[2].snCode
    const branchName = branch.case
    const response = await requestByInput(page, snInput, '/execution/units/trace?', bad)
    assert.strictEqual(response.code, 200)
    const unit = response.data
    assert.strictEqual(unit.traceStatus, 'TRACKED')
    assert.strictEqual(unit.snCode, bad)
    assert.strictEqual(String(unit.workorderId), String(branch.workorderId))
    assert.strictEqual(unit.orderStatus, 'FINISHED')
    assert(unit.inputs.some(input => input.sourceBatchCode === branch.batches[1]), 'bad SN has actual B07 input')
    assert.strictEqual(unit.inspections[0].checkResult, 'REJECT')
    assert.strictEqual(unit.dispositions.length, 1)
    if (branchName === 'R') {
      assert.strictEqual(unit.unitState, 'PACKED')
      assert.strictEqual(unit.inspections.length, 2)
      assert.strictEqual(unit.inspections[1].checkResult, 'ACCEPT')
      assert.strictEqual(unit.dispositions[0].decision, 'REWORK')
      assert(unit.packing)
      await page.getByText('第 2 轮检验', { exact: false }).waitFor()
    } else {
      assert.strictEqual(unit.unitState, 'SCRAPPED')
      assert.strictEqual(unit.inspections.length, 1)
      assert.strictEqual(unit.dispositions[0].decision, 'SCRAP')
      assert(!unit.packing)
      await page.getByText('暂无本件包装事实记录。', { exact: true }).waitFor()
    }
    await page.screenshot({ path: path.join(run, `ui-trace-${branchName}-unit.png`), fullPage: true })
    const batchResponse = page.waitForResponse(res => res.url().includes('/execution/materials/trace?'))
    await page.getByRole('button', { name: branch.batches[1], exact: true }).click()
    const material = await (await batchResponse).json()
    assert.strictEqual(material.code, 200)
    assert.strictEqual(material.total, 3)
    const expected = branch.sn.slice(2, 5).map(sn => sn.snCode).sort()
    assert.deepStrictEqual(material.rows.map(row => row.snCode).sort(), expected)
    assert(material.rows.every(row => String(row.workorderId) === String(branch.workorderId)))
    for (const sn of expected) await page.getByRole('button', { name: sn, exact: true }).waitFor()
    await page.screenshot({ path: path.join(run, `ui-trace-${branchName}-material.png`), fullPage: true })
    await page.getByRole('button', { name: bad, exact: true }).click()
    await page.getByRole('heading', { name: '历次质量检验', exact: true }).waitFor()
    const denied = await requestByInput(page, snInput, '/execution/units/trace?', other.sn[2].snCode)
    assert.strictEqual(denied.code, 404, 'case leader cannot read unrelated case SN')
    await page.getByText('未查询到可见记录，请核对输入及工单授权范围。', { exact: true }).waitFor()
    assert.strictEqual(await page.getByText('成品身份', { exact: true }).count(), 0)
    await page.screenshot({ path: path.join(run, `ui-trace-${branchName}-denied.png`), fullPage: true })
    await page.goto(url + '/mes/pro/execution')
    await page.getByRole('heading', { name: '车间工作台', exact: true }).waitFor()
    await page.getByPlaceholder('工单 ID', { exact: true }).fill(String(branch.workorderId))
    await page.getByRole('region', { name: '生产任务列表' }).getByRole('button', { name: /查询/ }).click()
    const taskRow = page.locator('.el-table__body tr').filter({ hasText: unit.workorderCode }).first()
    await taskRow.getByRole('button', { name: '查看', exact: true }).waitFor()
    const orderResponse = page.waitForResponse(res => res.url().endsWith(`/execution/workorders/${branch.workorderId}`) && res.request().method() === 'GET')
    await taskRow.getByRole('button', { name: '查看', exact: true }).click()
    const order = (await (await orderResponse).json()).data
    assert.strictEqual(order.order.status, 'FINISHED')
    assert.strictEqual(order.progress.packed, branchName === 'R' ? 10 : 9)
    assert.strictEqual(order.progress.scrapped, branchName === 'R' ? 0 : 1)
    await page.getByRole('heading', { name: '生产进度', exact: true }).waitFor()
    await page.screenshot({ path: path.join(run, `ui-trace-${branchName}-workbench.png`), fullPage: true })
    assert.deepStrictEqual(runtimeErrors, [])
    // The existing print initializer may run before login. Only acknowledge that exact 401
    // response and the matching pre-login session message; trace-page errors still fail.
    assert(startupErrors.length <= printStartup.filter(response => response.status === 200 && response.code === 401).length)
    return { case: branchName, snCode: bad, workorderId: unit.workorderId, unitState: unit.unitState, inputBatches: unit.inputs.map(input => input.sourceBatchCode), inspectionResults: unit.inspections.map(record => record.checkResult), disposition: unit.dispositions[0].decision, packed: !!unit.packing, actualBatch: expected, crossScopeCode: denied.code, workbenchFinished: order.order.status, workbenchPacked: order.progress.packed, workbenchScrapped: order.progress.scrapped, runtimeErrors, startupErrors, printStartup, networkErrors, traces }
  } finally { await page.close() }
}

;(async () => {
  assert(process.argv.includes('--approve-isolated-b06'), 'isolated B06 approval required')
  const r = summary(process.argv[2], 'R'), s = summary(process.argv[3], 'S')
  const accounts = JSON.parse(fs.readFileSync(source, 'utf8')).accounts
  const server = await serve()
  const url = `http://127.0.0.1:${server.address().port}`
  let browser
  try {
    browser = await chromium.launch({ executablePath: process.env.MES_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
    const results = [await verifyCase(browser, url, accounts.leader_r, r, s), await verifyCase(browser, url, accounts.leader_s, s, r)]
    fs.writeFileSync(path.join(run, 'ui-trace-live-results.json'), JSON.stringify(results, null, 2))
    console.log('REAL B06 browser trace passed: R and S; precise inverse batch, history/packing and scope denial.')
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)) }
})().catch(error => { console.error(error); process.exitCode = 1 })
