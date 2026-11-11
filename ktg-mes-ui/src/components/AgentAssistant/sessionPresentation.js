const terminal = ['completed', 'needs_input', 'failed', 'cancelled', 'needs_reconciliation']

export const isTerminalRun = status => terminal.includes(status)

export function pageContext(route) {
  const context = { captured_at: new Date().toISOString() }
  if (route && typeof route.name === 'string') context.route_name = route.name.slice(0, 100)
  if (route && (route.path === '/tm/tooltype' || route.path === '/mes/tm/tooltype' || route.name === 'Tooltype')) {
    context.module = 'tm'
  }
  if (route && (route.path === '/tm/tool' || route.path === '/mes/tm/tool' || route.name === 'Tool')) {
    context.module = 'tm'
  }
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
      if (!entry || (item.role === 'assistant' && entry.text)) {
        entry = { id: `message-${item.message_id}`, runId, role: 'assistant', text: '', sourceText: questions.get(runId), results: [], resultIds: [] }
        turns.set(runId, entry)
        entries.push(entry)
      }
      if (item.role === 'assistant' && textOf(item)) entry.text = textOf(item)
      if (item.role === 'tool' && item.details && item.details.kind) {
        entry.results.push(item.details)
        entry.resultIds.push(`result-${item.message_id}`)
      }
    }
  }
  return entries.filter(entry => entry.role === 'user' || entry.text || (entry.results && entry.results.length))
}

// Compare only options and query scope, not timestamps or unrelated card kinds.
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((result, key) => {
      result[key] = canonical(value[key])
      return result
    }, {})
  }
  return value
}

export function optionCardPresentation(messages, expanded = {}) {
  const previous = new Map()
  return messages.map(message => ({ ...message,
    resultPresentation: (message.results || []).map((result, index) => {
      if (!['tm_tool_options', 'tm_tooltype_options'].includes(result.kind)) return null
      const id = message.resultIds && message.resultIds[index] || `${message.id}-result-${index}`
      const scope = JSON.stringify(canonical([result.kind, result.filters || null]))
      const data = JSON.stringify(canonical(result.options))
      const seen = previous.has(scope)
      const same = seen && previous.get(scope) === data
      previous.set(scope, data)
      const explicit = /重新查询|重新查|再查|最新|展开/.test(message.sourceText || '')
      const title = result.kind === 'tm_tool_options' ? '工装夹具基础选项' : '工装夹具类型选项'
      const count = result.options && result.options.tool_types
      return {
        id, title,
        open: Object.prototype.hasOwnProperty.call(expanded, id) ? expanded[id] : !same || explicit,
        summary: `${same ? '与上次查询内容相同' : seen ? '选项内容已更新' : '首次查询结果'}${count ? ` · 本次返回${count.length}个类型` : ''}`,
        queriedAt: result.queried_at || ''
      }
    })
  }))
}

export function progressFromEvent(progress, event) {
  const payload = event.payload || {}
  if (['message_start', 'message_update', 'message_end'].includes(event.type) && payload.role !== 'tool') {
    const id = payload.message_id
    if (!id) return progress
    const messages = [...(progress.messages || [])]
    const index = messages.findIndex(message => message.id === id)
    if (event.type === 'message_start') {
      if (index < 0) messages.push({ id, text: '', status: 'streaming' })
    } else if (index >= 0) {
      const previous = messages[index]
      if (event.type === 'message_update' && payload.delta_type === 'text' && previous.status === 'streaming') {
        messages.splice(index, 1, { ...previous, text: previous.text + (payload.text || '') })
      } else if (event.type === 'message_end') {
        messages.splice(index, 1, { ...previous,
          text: payload.message ? textOf(payload.message) : previous.text,
          status: payload.stop_reason === 'completed' || payload.stop_reason === 'tool_call' ? 'ended' : 'incomplete' })
      }
    }
    return { ...progress, messages }
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

export function usageText(usage) {
  if (!usage) return '用量：暂缺'
  if (usage.source === 'legacy_unknown') return '用量：无记录'
  const total = usage.known_total_tokens
  const known = Number.isSafeInteger(total) ? `已知用量 ${total} token` : '已知用量暂缺'
  return `${known} · ${usage.complete ? '统计完整' : '未完整统计（部分用量暂缺）'}`
}

export function contextPressureText(context) {
  if (!context || !context.configured) return ''
  const parts = [`窗口 ${context.window_tokens}`, `阈值 ${context.threshold_tokens}`]
  if (context.estimate_kind === 'estimated' && Number.isSafeInteger(context.estimated_tokens)) {
    parts.push(`估算 ${context.estimated_tokens} token（估算值，非已知用量）`)
  }
  return parts.join(' · ')
}

export function contextStatusText(context) {
  if (!context || !context.configured) return ''
  const summary = context.summary_id
    ? `摘要 是（${context.summary_id.slice(0, 8)}，覆盖 ${context.covers_start_message_id}-${context.covers_end_message_id}）`
    : '摘要 否'
  const spill = Number.isSafeInteger(context.spill_count) ? context.spill_count : '暂缺'
  return `${summary} · spill ${spill}`
}

export function runStatusText(run) {
  if (!run) return ''
  const summary = run.result_summary || {}
  const reason = summary.limit_reason || summary.error_code || run.stop_reason
  const reasons = {
    length: '单次模型输出达到长度上限',
    max_total_tokens: '已达到本轮总 token 停止阈值',
    max_run_seconds: '本轮运行时间已到',
    max_model_turns: '本轮模型调用次数已达上限',
    max_tool_calls: '本轮工具启动次数已达上限',
    MODEL_OUTPUT_LIMIT: '模型达到单次输出上限，未能生成答复',
    MODEL_USAGE_UNAVAILABLE: '模型用量暂缺，已停止后续执行',
    USAGE_PERSISTENCE_FAILED: '用量记录失败，已停止后续执行',
    cancelled: '本轮已由用户取消'
  }
  if (run.status === 'needs_reconciliation') {
    return `业务结果待核验；请查询对应操作回执。${reasons[reason] ? `聊天运行：${reasons[reason]}` : ''}`
  }
  if (isTerminalRun(run.status) && reasons[reason]) return reasons[reason]
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
  if (input.status === 'rejected') return '本轮拒绝输入，可主动作为新问题发送'
  if (input.status === 'conflict') return '请求 ID 与已提交内容冲突'
  return '回执未确认，请查询或用同一请求重试'
}
