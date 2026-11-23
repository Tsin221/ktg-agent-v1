export const taskLabel = state => ({ WAITING: '等待前序', READY: '可开工', STARTED: '已开工', FINISHED: '已完成', WAIT_ASSEMBLY: '待装配', WAIT_INSPECTION: '待检', WAIT_DISPOSITION: '待处置', REWORK_IN_PROGRESS: '返工中', WAIT_REINSPECTION: '待复检', WAIT_PACKING: '待包装', PACKED: '已包装', SCRAPPED: '已报废' }[state] || state || '—')
export const stepLabel = step => ({ ASSEMBLY: '装配', INSPECTION: '质检', PACKING: '包装' }[step] || step || '—')
const millis = value => typeof value === 'number' ? value : (value ? Date.parse(String(value).replace(' ', 'T')) : NaN)

export function elapsedSeconds(task, sinceRead = 0) {
  const started = millis(task.actualStartedAt)
  const ended = task.actualEndedAt ? millis(task.actualEndedAt) : millis(task.serverTime) + Math.max(0, sinceRead)
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return null
  return Math.max(0, Math.floor((ended - started) / 1000))
}
export function formatDuration(seconds) {
  if (seconds == null) return '—'
  return [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(v => String(v).padStart(2, '0')).join(':')
}
export function availableActions(task) {
  if (!task) return []
  const actions = []
  if (task.canAssign) actions.push('assign')
  if (!['ASSEMBLY', 'INSPECTION'].includes(task.stepKind) && task.taskKind !== 'REWORK') return actions
  if (task.status === 'READY' && task.canStart && task.availableUnits > 0) actions.push('start')
  if (task.taskKind !== 'REWORK' && task.stepKind === 'ASSEMBLY' && task.status === 'STARTED' && task.availableUnits > 0 && task.canReport) actions.push('report')
  if (task.status === 'STARTED' && task.canComplete && (task.taskKind === 'REWORK' || task.availableUnits === 0)) actions.push('complete')
  return actions
}
export const safeDocumentUrl = value => typeof value === 'string' && /^(https?:\/\/|\/(?!\/))[^\s\\]+$/i.test(value) ? value : ''
