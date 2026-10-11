const assert = require('assert')
const path = require('path')
const { transformFileSync } = require('@babel/core')

function loadEsModule(relativePath) {
  const filename = path.resolve(__dirname, '..', relativePath)
  const transformed = transformFileSync(filename, {
    plugins: ['@babel/plugin-transform-modules-commonjs']
  })
  const module = { exports: {}}
  const evaluate = new Function('exports', 'module', 'require', transformed.code)
  evaluate(module.exports, module, require)
  return module.exports
}

const {
  buildWorkorderDraftPayload,
  extractWorkorderConflict
} = loadEsModule('src/api/mes/pro/workorderContract.js')

const form = {
  workorderId: 42,
  version: 3,
  workorderCode: 'WO-42',
  workorderName: '测试工单',
  workorderType: 'SELF',
  orderSource: 'MANUAL',
  sourceCode: null,
  productId: 9,
  productCode: 'P-09',
  productName: '服务端产品快照',
  productSpc: 'S-1',
  unitOfMeasure: 'EA',
  quantity: 12,
  quantityProduced: 7,
  requestDate: '2026-10-31',
  parentId: 0,
  status: 'FINISHED',
  remark: '保留备注',
  createBy: 'attacker',
  vendorId: 5
}

assert.deepStrictEqual(buildWorkorderDraftPayload(form, false), {
  workorderCode: 'WO-42',
  workorderName: '测试工单',
  workorderType: 'SELF',
  orderSource: 'MANUAL',
  sourceCode: null,
  productId: 9,
  quantity: 12,
  requestDate: '2026-10-31',
  parentId: 0,
  remark: '保留备注'
})

assert.deepStrictEqual(buildWorkorderDraftPayload(form, true), {
  workorderId: 42,
  version: 3,
  workorderCode: 'WO-42',
  workorderName: '测试工单',
  workorderType: 'SELF',
  orderSource: 'MANUAL',
  sourceCode: null,
  productId: 9,
  quantity: 12,
  requestDate: '2026-10-31',
  parentId: 0,
  remark: '保留备注'
})

assert.deepStrictEqual(extractWorkorderConflict({
  code: 409,
  data: {
    msg: '生产工单已被其他客户端修改，请重新加载！当前状态：CONFIRMED，当前版本：4'
  }
}), {
  message: '生产工单已被其他客户端修改，请重新加载！当前状态：CONFIRMED，当前版本：4',
  status: 'CONFIRMED',
  version: 4
})

assert.strictEqual(extractWorkorderConflict(new Error('network error')), null)
assert.strictEqual(extractWorkorderConflict({
  code: 409,
  data: { msg: '生产工单编号已存在！' }
}), null)

console.log('workorder contract tests passed')
