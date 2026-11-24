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

const queryLabels = { product_count: '产品总数', stock_summary: '分仓库存汇总', workorder_overview: '工单概览',
  workorder_materials: '工单用料' }
const queryTools = { ktg_count_products: 'product_count', ktg_query_stock_summary: 'stock_summary',
  ktg_query_workorder_overview: 'workorder_overview', ktg_query_workorder_materials: 'workorder_materials' }
const taskTools = ['create_task', 'update_task']
const progressTools = [...taskTools, 'search_tools', 'load_tools']
const toolKind = name => Object.prototype.hasOwnProperty.call(queryTools, name) ? queryTools[name] : undefined
const isQueryKind = kind => Object.prototype.hasOwnProperty.call(queryLabels, kind)
const isQueryPreface = calls => calls.length > 0 && calls.every(call => toolKind(call.name) || progressTools.includes(call.name))

export function presentMessages(items) {
  const entries = []
  const turns = new Map()
  const questions = new Map()
  const calls = new Map()
  for (const item of items) {
    const runId = item.run_id
    if (item.role === 'user') {
      const entry = { id: `message-${item.message_id}`, runId, role: 'user', text: textOf(item) }
      entries.push(entry)
      questions.set(runId, entry.text)
    } else if (item.role === 'assistant' || item.role === 'tool') {
      if (!calls.has(runId)) calls.set(runId, new Map())
      const runCalls = calls.get(runId)
      let entry = turns.get(runId)
      if (!entry || (item.role === 'assistant' && entry.text)) {
        entry = { id: `message-${item.message_id}`, runId, role: 'assistant', text: '', sourceText: questions.get(runId),
          results: [], resultIds: [], queries: [] }
        turns.set(runId, entry)
        entries.push(entry)
      }
      if (item.role === 'assistant') {
        const toolCalls = (item.content || []).filter(block => block.type === 'tool_call')
        toolCalls.forEach(call => runCalls.set(call.id, call))
        const text = textOf(item)
        if (text) {
          entry.text = text
          entry.queryPreface = isQueryPreface(toolCalls)
        }
      } else {
        const call = runCalls.get(item.tool_call_id)
        const result = item.details
        const id = `result-${item.message_id}`
        const kind = result && result.kind
        if (call && taskTools.includes(call.name) && result && typeof result.task_id === 'string' && !item.is_error) {
          entry.taskIds = [...new Set([...(entry.taskIds || []), result.task_id])]
        }
        if (kind) {
          entry.results.push(result)
          entry.resultIds.push(id)
        }
        const queryKind = isQueryKind(kind) ? kind : call && toolKind(call.name)
        if (queryKind && (isQueryKind(kind) || (item.is_error && !kind))) {
          entry.queries.push({ id, kind: queryKind, result, call,
            time: (result && result.queried_at) || item.created_at,
            error: item.is_error ? textOf(item) || '查询未成功' : '' })
        }
      }
    }
  }
  return entries.filter(entry => entry.role === 'user' || entry.text || entry.results.length || entry.queries.length || entry.taskIds)
}

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

export function compactResultPresentation(messages) {
  return messages.map(message => {
    if (message.role !== 'assistant') return message
    const text = message.text || ''
    const paragraphs = text.trim().split(/\r?\n\s*\r?\n/)
    const plain = paragraph => paragraph.replace(/^\s*#{1,6}\s+/, '').replace(/\*\*([^*]+)\*\*/g, '$1').trim()
    const explicitlyMarked = paragraphs.findIndex(paragraph => /^(?:核心结论|简要结论|总体结论|结论|综合判断|总结)\s*[:：]/.test(plain(paragraph)))
    const concludingParagraph = explicitlyMarked >= 0 ? explicitlyMarked : paragraphs.findIndex(paragraph => /^(?:也就是说|简单说|简单来说|总的来说|综上所述|总之)[，、：:]/.test(plain(paragraph)))
    // Never hide an unstructured answer just because its first paragraph is long or metadata.
    if (concludingParagraph < 0) return { ...message, textPreview: text, hasFullText: false }
    const preview = plain(paragraphs[concludingParagraph])
    const last = paragraphs.length - 1
    const tail = plain(paragraphs[last])
    const needsResponse = /[？?]\s*$/.test(tail) || /^(?:追问|请补充|请提供|请确认|需要您确认|需要你确认)[：:，、\s]?/.test(tail)
    const followUpIndex = last !== concludingParagraph && needsResponse ? last : -1
    const followUp = followUpIndex >= 0 ? paragraphs[followUpIndex] : ''
    const remaining = paragraphs.filter((_, index) => index !== concludingParagraph && index !== followUpIndex)
    const tables = (message.results || []).length ? [] : remaining.filter(paragraph => answerBlocks(paragraph)[0]?.type === 'table')
    const textTables = tables.join('\n\n')
    const textDetails = remaining.filter(paragraph => !tables.includes(paragraph)).join('\n\n')
    return { ...message, textPreview: preview, textDetails, textTables, followUp, hasFullText: Boolean(textDetails) }
  })
}

// Only recognize flat Markdown tables with a matching separator and column counts.
// Everything else remains visible as plain text, including untrusted HTML.
export function answerBlocks(text) {
  return (text || '').trim().split(/\r?\n\s*\r?\n/).filter(Boolean).map(paragraph => {
    const lines = paragraph.split(/\r?\n/)
    const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())
    if (lines.length < 3 || lines.some(line => !line.includes('|') || line.includes('\\|'))) return { type: 'text', text: paragraph }
    const [headers, separator, ...rows] = lines.map(cells)
    if (headers.length < 2 || separator.length !== headers.length || !separator.every(cell => /^:?-{3,}:?$/.test(cell)) ||
        rows.some(row => row.length !== headers.length)) return { type: 'text', text: paragraph }
    return { type: 'table', headers, rows }
  })
}

export function workorderStatusLabel(status) {
  return { PREPARE: '草稿', APPROVING: '审批中', APPROVED: '已审批', CONFIRMED: '已下达', FINISHED: '已完工' }[status] || status || '状态未获取'
}

export function queryScope(args, includePagination = true) {
  const labels = { item_code: '物料编码', warehouse_id: '仓库 ID',
    workorder_id: '工单 ID', workorder_code: '工单编号', workorder_codes: '工单编号', workorder_name: '工单名称',
    product_code: '产品编码', product_name: '产品名称', status: '工单状态', statuses: '工单状态', request_date: '需求日期',
    request_date_from: '需求日期起', request_date_to: '需求日期止',
    unfinished_only: '仅未完工', include_progress: '查询执行进度', include_batches: '包含批次' }
  if (includePagination) Object.assign(labels, { page: '页码', page_size: '每页' })
  return Object.keys(labels).filter(key => args[key] != null && args[key] !== '' &&
    (!Array.isArray(args[key]) || args[key].length)).map(key => {
    let value = args[key]
    if (key === 'status') value = workorderStatusLabel(value)
    if (key === 'statuses' && Array.isArray(value)) value = value.map(workorderStatusLabel)
    const displayed = Array.isArray(value) ? value.join('、') : typeof value === 'boolean' ? (value ? '是' : '否') : value
    return `${labels[key]} ${displayed}`
  }).join(' · ')
}

function queryGroups(queries) {
  const groups = new Map()
  for (const item of queries) {
    const { kind, result, call, error } = item
    const args = (call && call.arguments) || {}
    const product = kind === 'stock_summary' && (args.item_code || (result && result.filters && result.filters.item_code))
    const key = product ? `product:${product}` : kind === 'workorder_overview' ? 'workorders'
      : kind === 'workorder_materials' ? 'workorder_materials' : 'general'
    const title = product ? `物料 ${product}` : key === 'general' ? '通用查询' : queryLabels[kind]
    if (!groups.has(key)) groups.set(key, { id: key, title, queries: [], duplicates: new Map() })
    const group = groups.get(key)
    let signature
    if (!error && call && call.arguments && result) {
      const businessResult = { ...result }
      delete businessResult.queried_at
      delete businessResult.spill
      signature = JSON.stringify(canonical([call.name, call.arguments, businessResult]))
    }
    const prior = signature && group.duplicates.get(signature)
    const scopeArgs = kind === 'workorder_overview'
      ? { unfinished_only: false, include_progress: false, ...args, ...(result && result.filters || {}) }
      : kind === 'workorder_materials' ? { include_batches: false, ...args, ...(result && result.filters || {}) } : args
    const attempt = { ...item, scope: queryScope(scopeArgs) }
    if (prior) prior.attempts.push(attempt)
    else {
      const query = { id: item.id, title: queryLabels[kind], attempts: [attempt] }
      group.queries.push(query)
      if (signature) group.duplicates.set(signature, query)
    }
  }
  return [...groups.values()].map(({ id, title, queries }) => ({ id, title, queries }))
}

// Material facts remain visible under the conclusion even when earlier queries
// were needed to resolve the workorder. Preserve independent observations and
// keep only their query metadata in the expandable audit, avoiding duplicate cards.
function materialCards(groups) {
  const cards = []
  for (const group of groups) {
    for (const query of group.queries) {
      const successful = query.attempts.filter(attempt => !attempt.error &&
        attempt.result && attempt.result.kind === 'workorder_materials')
      if (!successful.length) continue
      const latest = successful[successful.length - 1]
      cards.push({ id: latest.id, result: latest.result })
      successful.forEach(attempt => {
        attempt.resultShown = true
        delete attempt.result
      })
    }
  }
  return cards
}

export function queryCardPresentation(messages) {
  const runs = new Map()
  for (const entry of messages) {
    if (entry.role !== 'assistant' || !entry.runId) continue
    if (!runs.has(entry.runId)) runs.set(entry.runId, { queries: [], taskIds: new Set() })
    const run = runs.get(entry.runId)
    run.queries.push(...(entry.queries || []))
    for (const taskId of entry.taskIds || []) run.taskIds.add(taskId)
    run.last = entry
    if (entry.text && !entry.queryPreface) run.answer = entry
  }
  return messages.map(entry => {
    const run = entry.role === 'assistant' && runs.get(entry.runId)
    if (!run || !run.queries.length) return entry
    const single = !run.taskIds.size && run.queries.length === 1 && run.queries[0]
    const singleMaterial = single && !single.error && single.result && single.result.kind === 'workorder_materials'
    if (single && !single.error && !singleMaterial) return entry
    const retained = (entry.results || []).map((result, index) => ({ result, id: entry.resultIds[index] }))
      .filter(item => !(entry.queries || []).some(query => query.id === item.id))
    const output = { ...entry, results: retained.map(item => item.result), resultIds: retained.map(item => item.id) }
    if (entry.queryPreface && run.answer) output.text = ''
    if (entry === run.last) {
      if (single) {
        if (singleMaterial) {
          output.results.push(single.result)
          output.resultIds.push(single.id)
        } else output.queryError = single.error
      } else {
        const groups = queryGroups(run.queries)
        for (const card of materialCards(groups)) {
          output.results.push(card.result)
          output.resultIds.push(card.id)
        }
        output.queryDetails = { count: run.queries.length, failed: run.queries.filter(query => query.error).length, groups }
      }
    }
    return output
  }).filter(entry => entry.role === 'user' || entry.text || (entry.results && entry.results.length) || entry.queryDetails || entry.queryError)
}

export function toolProgressText(tools) {
  const all = Object.values(tools)
  const running = all.filter(tool => tool.status === 'running')
  if (running.length) {
    const tool = running[0]
    const kind = toolKind(tool.name)
    const label = kind ? `正在查询${queryLabels[kind].replace(/查询$/, '')}`
      : taskTools.includes(tool.name) ? '正在更新任务清单'
        : progressTools.includes(tool.name) ? '正在准备查询' : '正在执行操作'
    return running.length === 1 ? label : `${label} · 另有 ${running.length - 1} 项正在执行`
  }
  return all.length ? `已执行 ${all.length} 项操作` : ''
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
          queryPreface: payload.stop_reason === 'tool_call' && isQueryPreface(
            ((payload.message && payload.message.content) || []).filter(block => block.type === 'tool_call')),
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
  if (input.status === 'queued') return input.mode === 'follow_up'
    ? '已排队，等待当前工作收尾'
    : '已排队，等待下一步'
  if (input.status === 'consumed') return '已进入本轮上下文'
  if (input.status === 'not_consumed') return input.reason === 'limit'
    ? '本轮执行额度已用完，可作为新问题发送'
    : '本轮未消费，可主动作为新问题发送'
  if (input.status === 'rejected') return '本轮拒绝输入，可主动作为新问题发送'
  if (input.status === 'conflict') return '请求 ID 与已提交内容冲突'
  return '回执未确认，请查询或用同一请求重试'
}
