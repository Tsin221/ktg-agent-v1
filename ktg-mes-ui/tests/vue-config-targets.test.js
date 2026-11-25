const assert = require('assert')

process.env.VUE_APP_BASE_API = '/dev-api'
process.env.KTG_API_TARGET = 'http://127.0.0.1:18080'
process.env.KTG_AGENT_TARGET = 'http://127.0.0.1:18001'

const config = require('../vue.config.js')
const fs = require('fs')
const path = require('path')
const dashboard = fs.readFileSync(path.join(__dirname, '../src/views/index.vue'), 'utf8')
// The production supervisor has no device list permission. Neither demo
// equipment reads nor placeholder equipment cards belong on that dashboard.
assert.match(dashboard, /<demo-equipment-summary v-if="canSeeEquipment && demoMode"/)
assert.match(dashboard, /<panel-group v-else-if="canSeeEquipment"/)
assert.match(dashboard, /canSeeEquipment\(\)[\s\S]*?checkPermi\(\['mes:dv:machinery:list'\]\)/)

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
