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

const queryLabels = {
  product_count: '产品总数', items: '产品查询', workorders: '工单查询',
  workorder: '工单详情', protask: '生产任务', feedback: '报工记录',
  stock: '库存查询', stock_summary: '分仓库存汇总', workorder_progress: '工单进度试验视图'
}
const queryTools = {
  ktg_count_products: 'product_count', ktg_search_items: 'items', ktg_list_workorders: 'workorders',
  ktg_pro_workorder_get: 'workorder', ktg_pro_task_list: 'protask', ktg_pro_task_get: 'protask',
  ktg_pro_feedback_list: 'feedback', ktg_pro_feedback_get: 'feedback', ktg_query_stock: 'stock',
  ktg_query_stock_summary: 'stock_summary', ktg_pro_workorder_progress: 'workorder_progress'
}
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
        if (kind && kind !== 'exploration_catalog') {
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

// These are read-only reference cards. Previews, confirmations and outcomes
// remain in the main result area and are never hidden by this projection.
const referenceKinds = new Set([
  'dv_machinerytype_tree', 'dv_machinerytype_detail', 'dv_machinery_list', 'dv_machinery_detail',
  'md_machinery_workshop_options', 'dv_subject_list', 'dv_subject_detail',
  'dv_checkplan_list', 'dv_checkplan_detail', 'dv_checkplan_options', 'dv_checkplan_links',
  'dv_checkplan_machinery_candidates', 'dv_checkplan_subject_candidates',
  'dv_repair_list', 'dv_repair_detail', 'dv_repair_lines', 'dv_repair_options',
  'dv_repair_machinery_candidates', 'dv_repair_subject_candidates',
  'tm_tooltype_list', 'tm_tooltype_detail', 'tm_tooltype_options',
  'tm_tool_list', 'tm_tool_detail', 'tm_tool_options'
])

export function compactResultPresentation(messages) {
  return messages.map(message => {
    if (message.role !== 'assistant') return message
    const results = (message.results || []).map((result, index) => ({ result,
      id: message.resultIds && message.resultIds[index] || `${message.id}-${index}`,
      presentation: message.resultPresentation && message.resultPresentation[index] || null,
      reference: referenceKinds.has(result.kind) && !result.error
    }))
    const main = results.filter(item => !item.reference)
    const references = results.filter(item => item.reference)
    const ordered = [...main, ...references]
    const text = message.text || ''
    const long = text.length > 160 || /\n\s*\|/.test(text)
    const firstParagraph = (text.trim().split(/\n\s*\n/)[0] || '').replace(/^\s*\|.*$/gm, '').trim() || '查询结果见下方。'
    const preview = long ? firstParagraph.slice(0, 120) + (firstParagraph.length > 120 ? '…' : '') : text
    return { ...message, results: ordered.map(item => item.result), resultIds: ordered.map(item => item.id),
      resultPresentation: ordered.map(item => item.presentation), evidenceStart: main.length,
      evidenceCount: references.length, textPreview: preview, hasFullText: long && preview !== text }
  })
}

function queryScope(args) {
  const labels = { workorder_id: '工单 ID', workorder_code: '工单编号', workorder_name: '工单名称',
    workorder_type: '工单类型', item_code: '产品编码', item_name: '产品名称', warehouse_id: '仓库 ID',
    task_id: '任务 ID', record_id: '报工 ID', status: '状态', from_time: '开始时间', to_time: '结束时间',
    page: '页码', page_size: '每页条数' }
  return Object.keys(labels).filter(key => args[key] != null).map(key => `${labels[key]} ${args[key]}`).join(' · ')
}

const queryRows = result => result ? result.rows || [result.workorder || result.data].filter(Boolean) : []

function workorderGroups(queries, priorQueries) {
  const orders = new Map()
  const codes = new Map()
  const products = new Map()
  const tasks = new Map()
  const orderId = value => value == null ? '' : String(value)
  for (const { result } of [...priorQueries, ...queries]) {
    for (const row of queryRows(result)) {
      const id = orderId(row.workorderId)
      if (!id) continue
      if (row.workorderCode) {
        orders.set(id, row.workorderCode)
        codes.set(row.workorderCode, id)
      }
      if (row.productCode) {
        const matches = products.get(row.productCode) || new Set()
        matches.add(id)
        products.set(row.productCode, matches)
      }
      if (row.taskId != null) tasks.set(orderId(row.taskId), id)
    }
  }

  const groups = new Map()
  for (const item of queries) {
    const { kind, result, call, error } = item
    const args = (call && call.arguments) || {}
    const rows = queryRows(result)
    const rowIds = new Set(rows.map(row => orderId(row.workorderId)))
    let id = orderId(args.workorder_id) || (rowIds.size === 1 ? [...rowIds][0] : '')
    if (!id && (kind === 'protask' || kind === 'feedback')) id = tasks.get(orderId(args.task_id))
    if (!id && kind === 'workorders' && !rows.length) id = codes.get(args.workorder_code)

    const stock = kind === 'stock' || kind === 'stock_summary'
    const rowProducts = new Set(rows.map(row => row.itemCode))
    const product = stock && (args.item_code || (result && result.filters && result.filters.item_code) ||
      (rowProducts.size === 1 ? [...rowProducts][0] : ''))
    const matches = products.get(product)
    if (!id && matches && matches.size === 1) id = [...matches][0]

    const key = id ? `order:${id}` : product ? `product:${product}` : 'general'
    if (!groups.has(key)) {
      groups.set(key, { id: key, title: id ? orders.get(id) || `工单 ID ${id}` : product ? `产品 ${product}` : '通用查询',
        queries: [], duplicates: new Map() })
    }
    const group = groups.get(key)
    let signature
    if (!error && call && call.arguments && result) {
      const businessResult = { ...result }
      delete businessResult.queried_at
      delete businessResult.spill
      signature = JSON.stringify(canonical([call.name, call.arguments, businessResult]))
    }
    const prior = signature && group.duplicates.get(signature)
    const attempt = { ...item, scope: queryScope(args) }
    if (prior) prior.attempts.push(attempt)
    else {
      const query = { id: item.id, title: queryLabels[kind], attempts: [attempt] }
      group.queries.push(query)
      if (signature) group.duplicates.set(signature, query)
    }
  }
  return [...groups.values()].map(({ id, title, queries }) => ({ id, title, queries }))
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
  const taskQueries = new Map()
  for (const run of runs.values()) {
    run.priorQueries = []
    if (run.taskIds.size !== 1) continue
    const taskId = [...run.taskIds][0]
    run.priorQueries = taskQueries.get(taskId) || []
    taskQueries.set(taskId, [...run.priorQueries, ...run.queries])
  }
  return messages.map(entry => {
    const run = entry.role === 'assistant' && runs.get(entry.runId)
    if (!run || !run.queries.length) return entry
    const single = !run.taskIds.size && run.queries.length === 1 && run.queries[0]
    if (single && !single.error) return entry
    const retained = (entry.results || []).map((result, index) => ({ result, id: entry.resultIds[index] }))
      .filter(item => !(entry.queries || []).some(query => query.id === item.id))
    const output = { ...entry, results: retained.map(item => item.result), resultIds: retained.map(item => item.id) }
    if (entry.queryPreface && run.answer) output.text = ''
    if (entry === run.last) {
      if (single) {
        output.queryError = single.error
      } else {
        output.queryDetails = { count: run.queries.length, failed: run.queries.filter(query => query.error).length,
          groups: workorderGroups(run.queries, run.priorQueries) }
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
