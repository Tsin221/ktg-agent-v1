const assert = require('assert')
const fs = require('fs')
const path = require('path')
const Vue = require('vue')
const babel = require('@babel/core')
const compiler = require('vue-template-compiler')

const apiCalls = []
let deferSubjectRequests = false
const pendingSubjects = []
const api = {
  listDvsubject: params => {
    apiCalls.push({ ...params })
    if (deferSubjectRequests) return new Promise(resolve => pendingSubjects.push({ params, resolve }))
    return Promise.resolve({ rows: [], total: 0 })
  },
  listCheckplan: () => Promise.resolve({ rows: [], total: 0 }),
  listChecksubject: params => {
    apiCalls.push({ planId: params.planId, subjectType: params.subjectType })
    return Promise.resolve({ rows: [], total: 0 })
  }
}

function loadComponent(relativePath, componentOverrides = {}) {
  const filename = path.resolve(__dirname, '../src', relativePath)
  const parsed = compiler.parseComponent(fs.readFileSync(filename, 'utf8'))
  const compiled = compiler.compileToFunctions(parsed.template.content)
  const code = babel.transformSync(parsed.script.content, { babelrc: false, configFile: false,
    plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
  const module = { exports: {} }
  const mockRequire = name => {
    if (['@/api/mes/dv/dvsubject', '@/api/mes/dv/checksubject', '@/api/mes/dv/checkplan'].includes(name)) return api
    if (name === '@/components/dvsubjectSelect/multi.vue') return componentOverrides.DvsubjectSelect
    if (name === './subject.vue') return componentOverrides.Checksubject
    if (name === './machinery.vue') return { name: 'Checkmachinery' }
    if (name === '@/api/system/autocode/rule') return {}
    throw new Error(`unexpected dependency: ${name}`)
  }
  new Function('exports', 'module', 'require', code)(module.exports, module, mockRequire)
  return { ...(module.exports.default || module.exports), ...compiled }
}

function nextTick() {
  return new Promise(resolve => Vue.nextTick(resolve))
}

function findComponent(vnode, name) {
  if (!vnode) return null
  const options = vnode.componentOptions
  if (options && options.Ctor.options.name === name) return options
  for (const child of [...(vnode.children || []), ...((options && options.children) || [])]) {
    const match = findComponent(child, name)
    if (match) return match
  }
  return null
}

async function main() {
  const selectorOptions = loadComponent('components/dvsubjectSelect/multi.vue')
  const Selector = Vue.extend(selectorOptions)
  const selector = new Selector({ propsData: { subjectType: 'MAINTEN' } })
  await nextTick()
  assert.strictEqual(apiCalls.at(-1).subjectType, 'MAINTEN', '保养项目查询应使用 MAINTEN')

  selector.subjectType = 'CHECK'
  await nextTick()
  assert.strictEqual(selector.queryParams.subjectType, 'CHECK', '类型切换应更新查询条件')
  assert.strictEqual(selector.ids.length, 0, '类型切换应清除上一类型的勾选')
  selector.queryParams.subjectName = '日常点检'
  selector.handleQuery()
  assert.deepStrictEqual(apiCalls.at(-1), { ...selector.queryParams, pageNum: 1, pageSize: 10 })
  selector.resetQuery()
  assert.strictEqual(apiCalls.at(-1).subjectType, 'CHECK', '重置仍应保留当前 CHECK 类型')

  selector.selectedRows = [{ subjectId: 9 }]
  selector.ids = [9]
  selector.queryParams.subjectName = '旧搜索'
  selector.showFlag = true
  await nextTick()
  assert.strictEqual(selector.queryParams.subjectType, 'CHECK')
  assert.strictEqual(selector.queryParams.subjectName, null, '重新打开应清除旧搜索条件')
  assert.strictEqual(selector.ids.length, 0, '重新打开应清除旧选择')
  assert.strictEqual(selector.selectedRows.length, 0)

  const subjectOptions = loadComponent('views/mes/dv/checkplan/subject.vue', {
    DvsubjectSelect: Selector
  })
  const Subject = Vue.extend(subjectOptions)
  const subject = new Subject({ propsData: { planId: 1, planType: 'MAINTEN', optType: 'edit' } })
  await nextTick()
  const nestedSelector = findComponent(subject._render(), 'DvsubjectSelect')
  assert(nestedSelector, '计划项目组件应渲染项目选择器')
  assert.strictEqual(nestedSelector.propsData.subjectType, 'MAINTEN', '保养计划应向选择器传递 MAINTEN')
  subject.planType = 'CHECK'
  await nextTick()
  assert.strictEqual(findComponent(subject._render(), 'DvsubjectSelect').propsData.subjectType, 'CHECK', '计划类型切换应同步到选择器')
  subject.planId = 2
  await nextTick()
  assert.strictEqual(subject.queryParams.planId, 2, '切换计划时应更新已绑定项目列表的计划 ID')
  assert.strictEqual(subject.ids.length, 0, '切换计划时应清除旧项目勾选')

  const callCount = apiCalls.length
  subject.planId = null
  await nextTick()
  assert.strictEqual(apiCalls.length, callCount, '清空计划时不得查询所有计划的项目')
  assert.strictEqual(subject.checksubjectList.length, 0)

  const pageOptions = loadComponent('views/mes/dv/checkplan/index.vue', { Checksubject: Subject })
  const page = new (Vue.extend(pageOptions))()
  page.dict = { type: { dv_plan_type: [], mes_order_status: [], mes_cycle_type: [] } }
  page.form = { planId: 1, planType: 'MAINTEN', status: 'PREPARE' }
  assert.strictEqual(findComponent(page._render(), 'Checksubject').propsData.planType, 'MAINTEN', '编辑弹窗应将实际计划类型传给项目列表')
  page.form.planType = 'CHECK'
  assert.strictEqual(findComponent(page._render(), 'Checksubject').propsData.planType, 'CHECK')

  deferSubjectRequests = true
  selector.getList()
  selector.subjectType = 'MAINTEN'
  await nextTick()
  assert.strictEqual(pendingSubjects.length, 2)
  pendingSubjects[1].resolve({ rows: [{ subjectId: 1, subjectType: 'MAINTEN' }], total: 1 })
  await nextTick()
  pendingSubjects[0].resolve({ rows: [{ subjectId: 2, subjectType: 'CHECK' }], total: 1 })
  await nextTick()
  assert.strictEqual(selector.dvsubjectList[0].subjectType, 'MAINTEN', '旧请求不得覆盖新类型的项目列表')
  assert.strictEqual(selector.loading, false)

  selector.$destroy()
  subject.$destroy()
  page.$destroy()
  console.log('Check-plan subject selection keeps plan type, search, and selection state in sync')
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
