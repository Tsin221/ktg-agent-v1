const WORKORDER_KINDS = [
  'workorder_draft_preview',
  'workorder_draft_result',
  'workorder_product_candidates'
]

const FIELD_LABELS = {
  workorderCode: '工单编号',
  workorderName: '工单名称',
  workorderType: '工单类型',
  orderSource: '来源类型',
  sourceCode: '来源单号',
  clientId: '客户',
  clientCode: '客户编码',
  clientName: '客户名称',
  batchCode: '批次号',
  productCode: '产品编号',
  productName: '产品名称',
  productSpc: '规格型号',
  unitOfMeasure: '单位',
  productId: '产品',
  quantity: '数量',
  requestDate: '需求日期',
  parentId: '父工单',
  remark: '备注',
  status: '状态',
  version: '版本'
}

export function isWorkorderAssistantResult(result) {
  return Boolean(result && WORKORDER_KINDS.includes(result.kind))
}

export function workorderFieldLabel(field) {
  return FIELD_LABELS[field] || field
}

export function formatWorkorderValue(value) {
  if (value === null || value === undefined) return '未设置'
  if (value === '') return '空'
  return String(value)
}

export function formatWorkorderType(value) {
  return ({ SELF: '自制', OUTSOURCE: '委外', PURCHASE: '外购' })[value] || value
}

export function formatProductCandidatesHint(result) {
  const candidates = Array.isArray(result && result.candidates) ? result.candidates : []
  const total = Number.isInteger(result && result.total) ? result.total : candidates.length
  const visible = candidates.length
  const count = `共 ${total} 项，当前显示 ${visible} 项。`
  return result && result.truncated
    ? `${count} 候选过多，请提供更精确的产品编码。`
    : `${count} 请按产品编码选择后重新生成预览。`
}
