const assert = require('assert')
const path = require('path')
const { transformFileSync } = require('@babel/core')
const notifications = []
let rejectResponse
const service = { interceptors: {
  request: { use() {} },
  response: { use(_success, failure) { rejectResponse = failure } }
} }
function load(file, requireMock) {
  const code = transformFileSync(path.resolve(__dirname, file), { babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const mod = { exports: {} }
  new Function('exports', 'module', 'require', code)(mod.exports, mod, requireMock)
  return mod.exports
}
load('../src/utils/request.js', name => {
  if (name === 'axios') return { defaults: { headers: {} }, create: () => service }
  if (name === 'element-ui') return { Message: notice => notifications.push(notice) }
  return {}
})
const requests = []
const api = load('../src/api/agent.js', name => {
  if (name === '@/utils/request') return config => requests.push(config)
  if (name === './agentSession') return { createAgentSessionClient: () => ({}) }
  return {}
})
async function main() {
  api.getAgentSession('expired', { restoring: true })
  api.getAgentRun('expired-run', { restoring: true })
  api.getAgentSession('explicit-selection')
  api.getAgentRun('active-run')
  for (let i = 0; i < requests.length; i++) {
    const error = Object.assign(new Error('Request failed with status code 404'), {
      config: requests[i], response: { status: 404 }
    })
    await assert.rejects(rejectResponse(error), value => value === error)
    assert.strictEqual(notifications.length, Math.max(0, i - 1))
  }
  const serverError = Object.assign(new Error('Request failed with status code 500'), {
    config: requests[0], response: { status: 500 }
  })
  await assert.rejects(rejectResponse(serverError), value => value === serverError)
  assert.strictEqual(notifications.length, 3, 'Recovery must not hide a real service failure')
  console.log('stale session recovery is quiet; normal 404 and server failures remain visible')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
