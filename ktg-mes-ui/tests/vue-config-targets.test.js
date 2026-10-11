const assert = require('assert')

process.env.VUE_APP_BASE_API = '/dev-api'
process.env.KTG_API_TARGET = 'http://127.0.0.1:18080'
process.env.KTG_AGENT_TARGET = 'http://127.0.0.1:18001'

const config = require('../vue.config.js')

assert.strictEqual(
  config.devServer.proxy['/dev-api'].target,
  'http://127.0.0.1:18080'
)
assert.strictEqual(
  config.devServer.proxy['/agent-api'].target,
  'http://127.0.0.1:18001'
)
assert.strictEqual(
  config.devServer.proxy['/ureport'].target,
  'http://127.0.0.1:18080'
)

console.log('vue config target tests passed')
