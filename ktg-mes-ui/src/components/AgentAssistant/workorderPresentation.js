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
