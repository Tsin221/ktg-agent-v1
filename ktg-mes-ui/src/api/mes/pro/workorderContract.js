const DRAFT_FIELDS = [
  'workorderCode',
  'workorderName',
  'workorderType',
  'orderSource',
  'sourceCode',
  'productId',
  'batchCode',
  'quantity',
  'clientId',
  'clientCode',
  'clientName',
  'requestDate',
  'parentId',
  'remark'
]

export function buildWorkorderDraftPayload(form, editing) {
  const fields = editing
    ? ['workorderId', 'version', ...DRAFT_FIELDS]
    : DRAFT_FIELDS

  return fields.reduce((payload, field) => {
    if (form[field] !== undefined) {
      payload[field] = form[field]
    }
    return payload
  }, {})
}

export function extractWorkorderConflict(error) {
  if (!error || error.code !== 409) {
    return null
  }

  const message = (error.data && error.data.msg) || error.message || '工单已发生变更，请重新读取'
  const statusMatch = message.match(/当前状态：([^，,]+)/)
  const versionMatch = message.match(/当前版本：(\d+)/)
  if (!statusMatch && !versionMatch) {
    return null
  }

  return {
    message,
    status: statusMatch ? statusMatch[1] : null,
    version: versionMatch ? Number(versionMatch[1]) : null
  }
}
