const STORAGE_KEY = 'ktg-agent-session-pointer'
const terminalStatuses = ['completed', 'needs_input', 'failed', 'cancelled', 'needs_reconciliation']

export function createSseParser(onFrame) {
  let line = ''
  let data = []
  let event = ''
  let id = ''
  let afterCR = false

  const consumeLine = () => {
    if (!line) {
      if (data.length) onFrame({ event, id, data: data.join('\n') })
      data = []
      event = ''
      id = ''
    } else if (line[0] !== ':') {
      const colon = line.indexOf(':')
      const field = colon < 0 ? line : line.slice(0, colon)
      let value = colon < 0 ? '' : line.slice(colon + 1)
      if (value[0] === ' ') value = value.slice(1)
      if (field === 'data') data.push(value)
      if (field === 'event') event = value
      if (field === 'id' && !value.includes('\u0000')) id = value
    }
    line = ''
  }

  return {
    feed(chunk) {
      for (const char of chunk) {
        if (afterCR) {
          afterCR = false
          if (char === '\n') continue
        }
        if (char === '\r' || char === '\n') {
          consumeLine()
          afterCR = char === '\r'
        } else {
          line += char
        }
      }
    }
  }
}

export function createAgentSessionClient({ api, getIdentity, storage, fetchStream, retryDelay = 1000 }) {
  let identity
  let pointer = null
  let controller = null
  let generation = 0

  const save = () => {
    if (storage) {
      if (pointer) storage.setItem(STORAGE_KEY, JSON.stringify(pointer))
      else storage.removeItem(STORAGE_KEY)
    }
  }
  const close = () => {
    generation += 1
    if (controller) controller.abort()
    controller = null
  }
  const clear = () => {
    close()
    pointer = null
    save()
  }
  const checkIdentity = () => {
    const current = getIdentity()
    const next = current.token && current.userId ? String(current.userId) + ':' + current.token : null
    if (identity !== undefined && identity !== next) {
      clear()
    }
    identity = next
    return Boolean(next)
  }
  const requireIdentity = () => {
    if (!checkIdentity()) throw new Error('Agent 需要当前登录用户。')
  }
  const getState = () => pointer ? { ...pointer } : null

  async function restoreSession() {
    requireIdentity()
    const current = generation
    if (!pointer && storage) {
      try {
        const saved = JSON.parse(storage.getItem(STORAGE_KEY))
        if (saved && typeof saved.sessionId === 'string') {
          pointer = { sessionId: saved.sessionId, runId: typeof saved.runId === 'string' ? saved.runId : null,
            seq: 0 } // Reload has no transient text.
        }
      } catch (_) {
        save()
      }
    }
    if (!pointer) return null
    try {
      await api.getSession(pointer.sessionId, { restoring: true }) // Always recheck ownership before using a saved ID.
      if (!checkIdentity() || current !== generation) return null
      if (pointer.runId) {
        const run = await api.getRun(pointer.runId, { restoring: true })
        if (!checkIdentity() || current !== generation) return null
        if (run.session_id !== pointer.sessionId) {
          pointer.runId = null
          pointer.seq = 0
          save()
        }
      }
      return getState()
    } catch (error) {
      clear()
      // A remembered ID can disappear after demo login renewal or an administrator reset.
      if (error.response && error.response.status === 404) return null
      throw error
    }
  }

  function setSession(sessionId) {
    requireIdentity()
    close()
    pointer = { sessionId, runId: null, seq: 0 }
    save()
  }

  function setRun(runId, { replay = false } = {}) {
    requireIdentity()
    if (!pointer) throw new Error('请先选择会话。')
    if (pointer.runId === runId && !replay) return
    close()
    pointer.runId = runId
    pointer.seq = 0
    save()
  }

  async function createRun({ requestId, message, pageContext, taskId }) {
    requireIdentity()
    if (!pointer || !requestId || !message) throw new Error('缺少会话、请求 ID 或消息。')
    const current = generation
    const result = await api.createRun(pointer.sessionId, { requestId, message, pageContext, ...(taskId ? { taskId } : {}) })
    if (!checkIdentity() || current !== generation || !pointer) return null
    if (pointer.runId !== result.run_id) {
      pointer.runId = result.run_id
      pointer.seq = 0
      save()
    }
    return result
  }

  async function cancelRun(requestId) {
    requireIdentity()
    if (!pointer || !pointer.runId) throw new Error('没有可停止的 run。')
    const current = generation
    const result = await api.cancelRun(pointer.runId, requestId)
    return checkIdentity() && current === generation ? result : null
  }

  async function addInput(input) {
    requireIdentity()
    if (!pointer || !pointer.runId) throw new Error('没有可追加输入的 run。')
    const current = generation
    const result = await api.addInput(pointer.runId, input)
    return checkIdentity() && current === generation ? result : null
  }

  async function listInputs(runId = pointer && pointer.runId) {
    requireIdentity()
    if (!runId) return []
    const current = generation
    const items = []
    let cursor
    do {
      const page = await api.listInputs(runId, cursor ? { cursor } : undefined)
      if (!checkIdentity() || current !== generation || !pointer || pointer.runId !== runId) return null
      items.push(...page.items)
      cursor = page.next_cursor
    } while (cursor)
    return items
  }

  async function readSnapshot(runId, sessionId, active, knownRun) {
    const run = knownRun || await api.getRun(runId)
    if (!active()) return null
    const messages = { items: [], next_cursor: null }
    let cursor
    do {
      const page = await api.listMessages(sessionId, cursor ? { cursor } : undefined)
      messages.items.push(...page.items)
      cursor = page.next_cursor
    } while (cursor && active())
    if (!active()) return null
    const inputs = await listInputs(runId)
    if (!active() || !inputs) return null
    if (terminalStatuses.includes(run.status) && Number.isSafeInteger(run.last_seq) && run.last_seq > pointer.seq) {
      pointer.seq = run.last_seq
      save()
    }
    return { run, messages, inputs }
  }

  // A terminal REST read is authoritative even if its final SSE frames are
  // delayed. Obtain the persisted answer before aborting the old stream.
  async function reconcileRun(run) {
    requireIdentity()
    if (!pointer || !run || run.run_id !== pointer.runId || !terminalStatuses.includes(run.status)) return null
    const current = generation
    const runId = pointer.runId
    const sessionId = pointer.sessionId
    const active = () => generation === current && checkIdentity() && pointer &&
      pointer.runId === runId && pointer.sessionId === sessionId
    if (run.session_id && run.session_id !== sessionId) return null
    const result = await readSnapshot(runId, sessionId, active, run)
    if (!result || !active()) return null
    close()
    return result
  }

  async function subscribeRun({ onEvent, onSnapshot, onConnectionError, onConnectionRestored }) {
    requireIdentity()
    if (!pointer || !pointer.runId) throw new Error('没有可订阅的 run。')
    close()
    const current = generation
    const runId = pointer.runId
    const sessionId = pointer.sessionId
    controller = new AbortController()
    const signal = controller.signal
    const active = () => !signal.aborted && generation === current && checkIdentity() &&
      pointer && pointer.runId === runId && pointer.sessionId === sessionId
    const snapshot = async(knownRun) => {
      const result = await readSnapshot(runId, sessionId, active, knownRun)
      if (!result) return null
      if (onSnapshot) onSnapshot(result)
      return result.run
    }

    let pendingTerminal = false
    const recoverGap = async () => {
      const run = await api.getRun(runId)
      if (!active()) return null
      const resume = run.last_seq // Boundary captured BEFORE snapshot reads.
      const result = await readSnapshot(runId, sessionId, active, run)
      if (!result || !active()) return null
      result.historyIncomplete = true
      if (onSnapshot) onSnapshot(result)
      if (!terminalStatuses.includes(run.status)) {
        if (!Number.isSafeInteger(resume) || resume < 0) throw new Error('Invalid recovery cursor')
        pointer.seq = resume
        save()
      }
      return run
    }
    while (active()) {
      try {
        if (pendingTerminal) {
          await snapshot()
          return
        }
        const url = '/agent-api/v1/runs/' + encodeURIComponent(runId) +
          '/events?after_seq=' + pointer.seq
        const response = await fetchStream(url, {
          headers: { Authorization: 'Bearer ' + getIdentity().token, Accept: 'text/event-stream' },
          cache: 'no-store', signal
        })
        if (!active()) return
        if (response.status === 410) {
          if (onConnectionRestored) onConnectionRestored()
          const run = await recoverGap()
          if (!run || terminalStatuses.includes(run.status)) return
          continue
        }
        if (response.status === 401) {
          clear()
          const error = new Error('Agent 登录状态已失效。')
          error.authFailure = true
          throw error
        }
        if (!response.ok) {
          const error = new Error('事件订阅失败：' + response.status)
          error.permanent = response.status < 500 && response.status !== 429
          throw error
        }
        if (!response.body) throw new Error('浏览器不支持流式事件读取。')
        if (onConnectionRestored) onConnectionRestored()
        const reader = response.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let settled = false
        let gap = false
        const parser = createSseParser(frame => {
          if (!active()) return
          const envelope = JSON.parse(frame.data)
          if (frame.event === 'history_expired' && envelope.run_id === runId && envelope.session_id === sessionId) { gap = true; return }
          const seq = envelope.seq
          if (envelope.run_id !== runId || envelope.session_id !== sessionId ||
              !Number.isSafeInteger(seq) || seq <= pointer.seq || String(seq) !== frame.id ||
              envelope.type !== frame.event) return
          if (onEvent) onEvent(envelope)
          pointer.seq = seq
          save()
          if (envelope.type === 'run_settled') settled = true
        })
        try {
          while (active() && !settled && !gap) {
            const { value, done } = await reader.read()
            if (done) break
            parser.feed(decoder.decode(value, { stream: true }))
          }
          parser.feed(decoder.decode())
        } finally {
          reader.cancel().catch(() => {})
        }
        if (!active()) return
        if (gap) {
          const run = await recoverGap()
          if (!run || terminalStatuses.includes(run.status)) return
          continue
        }
        if (settled) {
          pendingTerminal = true
          await snapshot()
          return
        }
        const run = await api.getRun(runId)
        if (!active()) return
        if (terminalStatuses.includes(run.status)) {
          pendingTerminal = true
          await snapshot(run)
          return
        }
      } catch (error) {
        if (error.authFailure || error.permanent || error.message === '浏览器不支持流式事件读取。') throw error
        if (!active()) return
        if (onConnectionError) onConnectionError(error)
      }
      await new Promise(resolve => setTimeout(resolve, retryDelay))
    }
  }

  return { checkIdentity, getState, restoreSession, setSession, setRun, createRun,
    cancelRun, addInput, listInputs, subscribeRun, reconcileRun, close, clear }
}
