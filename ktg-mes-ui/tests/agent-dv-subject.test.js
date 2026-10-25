const assert = require('assert')
const fs = require('fs')
const path = require('path')
const compiler = require('vue-template-compiler')
const { transformSync } = require('@babel/core')

function component(relative, dependencies = {}) {
  const filename = path.resolve(__dirname, '..', relative)
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  assert.deepStrictEqual(compiler.compile(parsed.template.content).errors, [])
  const code = transformSync(parsed.script.content, { filename: filename + '.js', babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const loaded = { exports: {}}
  new Function('exports', 'module', 'require', code)(loaded.exports, loaded, name => dependencies[name] || {})
  return { options: loaded.exports.default, parsed }
}

async function main() {
  let calls = 0
  let outcome = 'succeeded'
  const api = { confirmDvSubject: async() => {
    calls++
    if (outcome === 'transport-error') throw new Error('lost reply')
    return { result: { kind: 'dv_subject_update_result', outcome, label: 'result' }}
  } }
  const { options, parsed } = component('src/components/AgentAssistant/index.vue', { '@/api/agent': api })
  assert.ok(options.components.AgentDvSubjectCard)
  assert.ok(parsed.template.content.includes('@confirm="confirmDvSubjectPreview"'))
  for (const kind of ['dv_subject_list', 'dv_subject_detail', 'dv_subject_preview', 'dv_subject_delete_result']) {
    assert.ok(options.methods.isDvSubjectResult({ kind }))
  }
  assert.strictEqual(options.methods.isDvSubjectResult({ kind: 'md_machinery_workshop_options' }), false)
  const events = []
  global.CustomEvent = function(type, options) { this.type = type; this.detail = options.detail }
  global.window = { dispatchEvent: event => events.push(event) }
  const view = { ...options.data(), saveLocalState() {}, scrollToBottom() {} }
  view.confirm = options.methods.confirmDvSubjectPreview.bind(view)
  await view.confirm('preview-1')
  await view.confirm('preview-1')
  assert.strictEqual(calls, 1)
  assert.strictEqual(events[0].type, 'ktg:dv-subject-changed')
  assert.strictEqual(view.businessMessages[0].results[0].outcome, 'succeeded')
  outcome = 'unknown'
  await view.confirm('preview-2')
  assert.strictEqual(events.length, 1)
  outcome = 'transport-error'
  await view.confirm('preview-3')
  await view.confirm('preview-3')
  assert.strictEqual(calls, 3)
  assert.ok(view.completedDvSubjectPreviewIds.includes('preview-3'))
  assert.strictEqual(view.confirmingSubjectPreviewId, '')
  assert.ok(parsed.script.content.includes('completedDvSubjectPreviewIds: this.completedDvSubjectPreviewIds'))
  assert.ok(parsed.script.content.includes('this.completedDvSubjectPreviewIds = state.completedDvSubjectPreviewIds || []'))

  const card = component('src/components/AgentAssistant/DvSubjectCard.vue').options
  assert.deepStrictEqual(card.dicts, ['mes_dvsubject_type', 'sys_yes_no'])
  const preview = { result: { kind: 'dv_subject_preview', action: 'delete', after: { subjectId: 10, subjectCode: 'S10', subjectName: '' }}}
  assert.ok(card.computed.preview.call(preview))
  assert.strictEqual(card.methods.display(''), '—')
  const dict = { type: { sys_yes_no: [{ value: 'N', label: '否' }] }}
  assert.strictEqual(card.methods.display.call({ dict }, 'N', 'enableFlag'), '否')
  console.log('subject assistant/card confirmation tests passed')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
