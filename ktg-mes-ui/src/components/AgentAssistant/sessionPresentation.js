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
    queued: '正在排队…', running: '正在处理…', cancelling: '正在停止后续执行，已发出的业务操作仍在核验结果…',
    completed: '本轮已完成', needs_input: '本轮需要补充信息',
    failed: '本轮处理失败，请重试。', cancelled: '本轮已取消',
    needs_reconciliation: '运行中断，请核对业务操作结果'
  }[run.status] || '正在获取状态…'
}

export function visibleInputs(inputs, messages, runId) {
  const counts = new Map()
  let originalQuestionSeen = false
  for (const entry of messages) {
    if (entry.role !== 'user' || entry.runId !== runId) continue
    if (!originalQuestionSeen) {
      originalQuestionSeen = true
      continue
    }
    counts.set(entry.text, (counts.get(entry.text) || 0) + 1)
  }
  return inputs.filter(input => {
    if (input.status !== 'consumed') return true
    const count = counts.get(input.message) || 0
    if (!count) return true
    counts.set(input.message, count - 1)
    return false
  })
}

export function inputStatusText(input) {
  if (input.status === 'queued') return '已排队，等待本轮处理'
  if (input.status === 'consumed') return '已进入本轮上下文'
  if (input.status === 'not_consumed') return '本轮未消费，可主动作为新问题发送'
  return '回执未确认，请查询或用同一请求重试'
}
