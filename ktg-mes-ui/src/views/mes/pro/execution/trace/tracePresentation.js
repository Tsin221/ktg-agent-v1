export const hasTracePermission = (permissions, permission) => permissions.includes('*:*:*') || permissions.includes(permission)

export function traceError(error, fallback) {
  const response = error && error.response && error.response.data
  const data = response || (error && error.data)
  if ((data && data.code === 404) || (error && error.response && error.response.status === 404)) return '未查询到可见记录，请核对输入及工单授权范围。'
  return (data && data.msg) || (error && error.message) || (typeof error === 'string' && error) || fallback
}
