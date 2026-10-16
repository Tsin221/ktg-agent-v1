const terminal = ['completed', 'needs_input', 'failed', 'cancelled', 'needs_reconciliation']

export const isTerminalRun = status => terminal.includes(status)

export function pageContext(route) {
  const context = { captured_at: new Date().toISOString() }
  if (route && typeof route.name === 'string') context.route_name = route.name.slice(0, 100)
  const query = route && route.query || {}
  const filters = {}
  for (const key of ['itemCode', 'warehouseId', 'workorderCode']) {
    const value = query[key]
    if ((typeof value === 'string' || typeof value === 'number') && String(value).length <= 100) filters[key] = value
  }
  if (Object.keys(filters).length) context.filters = filters
  const selected = [['workorderId', 'workorder'], ['itemId', 'item'], ['recptId', 'receipt'], ['issueId', 'issue']]
    .find(([key]) => typeof query[key] === 'string' && query[key].length <= 100)
  if (selected) context.selected_entity = { type: selected[1], id: query[selected[0]] }
  return context
}

const textOf = message => (message.content || []).filter(block => block.type === 'text' && block.model_visible !== false)
  .map(block => block.text).join('')

export function presentMessages(items) {
  const entries = []
  const turns = new Map()
  const questions = new Map()
  for (const item of items) {
    const runId = item.run_id
    if (item.role === 'user') {
      const entry = { id: `message-${item.message_id}`, runId, role: 'user', text: textOf(item) }
      entries.push(entry)
      questions.set(runId, entry.text)
    } else if (item.role === 'assistant' || item.role === 'tool') {
      let entry = turns.get(runId)
      if (!entry) {
        entry = { id: `run-${runId}`, runId, role: 'assistant', text: '', sourceText: questions.get(runId), results: [] }
        turns.set(runId, entry)
        entries.push(entry)
      }
      if (item.role === 'assistant' && textOf(item)) entry.text = textOf(item)
      if (item.role === 'tool' && item.details && item.details.kind) entry.results.push(item.details)
    }
  }
  return entries.filter(entry => entry.role === 'user' || entry.text || (entry.results && entry.results.length))
}

export function progressFromEvent(progress, event) {
  const payload = event.payload || {}
  if (event.type === 'message_start') return { ...progress, text: '' }
  if (event.type === 'message_update' && payload.delta_type === 'text') {
    return { ...progress, text: (progress.text || '') + (payload.text || '') }
  }
  if (event.type === 'message_end' && payload.message) {
    return { ...progress, text: textOf(payload.message) || progress.text }
  }
  if (event.type.startsWith('tool_execution_')) {
    const id = payload.tool_call_id
    if (!id) return progress
    const tools = { ...progress.tools }
    tools[id] = { name: payload.tool_name || (tools[id] && tools[id].name) || '工具',
      status: event.type === 'tool_execution_end' ? (payload.status || 'completed') : 'running',
      message: payload.message || (tools[id] && tools[id].message) || '' }
    return { ...progress, tools }
  }
  return progress
}

export function runStatusText(run) {
  if (!run) return ''
  return {
    queued: '正在排队…', running: '正在处理…', cancelling: '正在处理…',
    completed: '本轮已完成', needs_input: '本轮需要补充信息',
    failed: '本轮未完成', cancelled: '本轮已取消',
    needs_reconciliation: '运行中断，请核对业务操作结果'
  }[run.status] || '正在获取状态…'
}
