const assert = require('assert')
const path = require('path')
const { ReadableStream } = require('stream/web')
const { transformFileSync } = require('@babel/core')

const filename = path.resolve(__dirname, '../src/api/agentSession.js')
const transformed = transformFileSync(filename, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] })
const moduleRef = { exports: {} }
new Function('exports', 'module', 'require', transformed.code)(moduleRef.exports, moduleRef, require)
const { createSseParser, createAgentSessionClient } = moduleRef.exports

// The Vuex store may still be initializing when the layout imports this API module.
const apiFilename = path.resolve(__dirname, '../src/api/agent.js')
const apiCode = transformFileSync(apiFilename, { babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-modules-commonjs'] }).code
const apiModule = { exports: {} }
const apiRequire = name => {
  if (name === '@/utils/request') return () => {}
  if (name === '@/store') return undefined
  if (name === '@/utils/auth') return { getToken: () => null }
  if (name === './agentSession') return { createAgentSessionClient: () => ({}) }
  throw new Error('unexpected import: ' + name)
}
assert.doesNotThrow(() => new Function('exports', 'module', 'require', apiCode)(apiModule.exports, apiModule, apiRequire))
assert.ok(apiModule.exports.agentSession)
assert.ok(!Object.keys(apiModule.exports).some(name => /confirm|ExplorationView|InventoryOperation/.test(name)),
  'The assistant must not expose retired business confirmation or exploration APIs')

function stream(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    }
  })
}

const bytes = value => new TextEncoder().encode(value)
const frame = (seq, type = 'message_update') => {
  const envelope = { session_id: 's1', run_id: 'r1', seq, type, payload: { text: '你好' } }
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(envelope)}\n\n`
}

async function main() {
  const parsed = []
  const parser = createSseParser(item => parsed.push(item))
  for (const part of ['id: 1\r', '\nevent: message_update\r\ndata: {"a":\r\n', 'data: 1}\r\n\r\n: heartbeat\n\n', 'id: 2\ndata: two\n\n']) {
    parser.feed(part)
  }
  assert.deepStrictEqual(parsed, [
    { id: '1', event: 'message_update', data: '{"a":\n1}' },
    { id: '2', event: '', data: 'two' }
  ])

  const saved = new Map()
  const storage = {
    getItem: key => saved.get(key) || null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key)
  }
  let auth = { token: 'first', userId: 7 }
  let postCount = 0
  const controlCalls = []
  let runReads = 0
  const urls = []
  const received = []
  const snapshots = []
  const api = {
    getSession: async sid => ({ session_id: sid }),
    createRun: async (_sid, input) => {
      postCount++
      assert.strictEqual(input.requestId, 'request-1')
      return { run_id: 'r1', status: 'running' }
    },
    cancelRun: async (runId, requestId) => {
      controlCalls.push(['cancel', runId, requestId]); return { status: 'cancelling' }
    },
    addInput: async (runId, input) => {
      controlCalls.push(['input', runId, input]); return { input_status: 'queued' }
    },
    getRun: async () => ({ session_id: 's1', status: ++runReads === 1 ? 'running' : 'completed', last_seq: 3 }),
    listInputs: async () => ({ items: [], next_cursor: null }),
    listMessages: async (_sid, params) => params
      ? { items: [{ message_id: 2 }], next_cursor: null }
      : { items: [{ message_id: 1 }], next_cursor: 1 }
  }
  const utf8 = bytes(frame(1) + ': heartbeat\n\n' + frame(1) + frame(2))
  const unicodeAt = utf8.findIndex((value, index) => value > 127 && utf8[index + 1] > 127)
  const chunks = [utf8.slice(0, 4), utf8.slice(4, unicodeAt + 1), utf8.slice(unicodeAt + 1)]
  let connection = 0
  let attempts = 0
  const fetchStream = async (url, options) => {
    urls.push(url)
    assert.strictEqual(options.headers.Authorization, 'Bearer first')
    if (++attempts === 1) throw new Error('temporary network failure')
    connection++
    return { ok: true, status: 200,
      body: connection === 1 ? stream(chunks) : stream([bytes(frame(2) + frame(3, 'run_settled'))]) }
  }
  const client = createAgentSessionClient({ api, storage, getIdentity: () => auth, fetchStream, retryDelay: 0 })
  client.setSession('s1')
  await client.createRun({ requestId: 'request-1', message: '测试' })
  await client.createRun({ requestId: 'request-1', message: '测试' })
  await client.addInput({ requestId: 'input-1', mode: 'follow_up', message: '继续' })
  await client.cancelRun('cancel-1')
  assert.deepStrictEqual(controlCalls, [
    ['input', 'r1', { requestId: 'input-1', mode: 'follow_up', message: '继续' }],
    ['cancel', 'r1', 'cancel-1']
  ])
  assert.strictEqual(postCount, 2)
  // The server returns the same run for repeated request IDs; reconnect only calls GET events.
  const connectionState = []
  await client.subscribeRun({ onEvent: value => received.push(value.seq), onSnapshot: value => snapshots.push(value),
    onConnectionError: error => connectionState.push(error.message),
    onConnectionRestored: () => connectionState.push('restored') })
  assert.deepStrictEqual(received, [1, 2, 3])
  assert.deepStrictEqual(connectionState, ['temporary network failure', 'restored', 'restored'])
  assert.deepStrictEqual(urls, [
    '/agent-api/v1/runs/r1/events?after_seq=0',
    '/agent-api/v1/runs/r1/events?after_seq=0',
    '/agent-api/v1/runs/r1/events?after_seq=2'
  ])
  assert.strictEqual(postCount, 2)
  assert.strictEqual(snapshots[0].run.status, 'completed')
  assert.deepStrictEqual(snapshots[0].messages.items.map(item => item.message_id), [1, 2])
  assert.strictEqual(client.getState().seq, 3)
  client.setRun('r2')
  assert.strictEqual(client.getState().runId, 'r2')
  assert.strictEqual(client.getState().seq, 0)
  client.setRun('r1')
  assert.deepStrictEqual(Object.keys(JSON.parse(saved.values().next().value)).sort(), ['runId', 'seq', 'sessionId'])

  const restored = createAgentSessionClient({ api, storage, getIdentity: () => auth,
    fetchStream: async () => ({ ok: false, status: 410 }) })
  assert.strictEqual((await restored.restoreSession()).sessionId, 's1')
  await restored.subscribeRun({ onSnapshot: value => snapshots.push(value) })
  assert.strictEqual(snapshots.length, 2)

  // A reverse proxy may keep the response open after the server's terminal event.
  // Synchronize the persisted answer without waiting for EOF.
  let readerCancelled = false
  let timeout
  const terminalRun = { run_id: 'r1', session_id: 's1', status: 'completed', last_seq: 3 }
  const openAfterSettled = createAgentSessionClient({ api: { ...api, getRun: async () => terminalRun },
    getIdentity: () => auth, storage: null,
    fetchStream: async () => ({ ok: true, status: 200, body: new ReadableStream({
      start(controller) { controller.enqueue(bytes(frame(3, 'run_settled'))) },
      cancel() { readerCancelled = true }
    }) }) })
  openAfterSettled.setSession('s1')
  openAfterSettled.setRun('r1')
  let finalSnapshot
  try {
    await Promise.race([
      openAfterSettled.subscribeRun({ onSnapshot: value => { finalSnapshot = value } }),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('terminal SSE frame did not settle promptly')), 500) })
    ])
  } finally { clearTimeout(timeout); openAfterSettled.close() }
  assert.strictEqual(finalSnapshot.run.status, 'completed')
  assert.deepStrictEqual(finalSnapshot.messages.items.map(item => item.message_id), [1, 2])
  assert.strictEqual(readerCancelled, true)

  // REST may observe a terminal run before its final SSE frames arrive.
  let pendingSignal
  const restFirst = createAgentSessionClient({ api, storage: null, getIdentity: () => auth,
    fetchStream: async (_url, options) => {
      pendingSignal = options.signal
      return { ok: true, status: 200, body: new ReadableStream({ start(controller) {
        options.signal.addEventListener('abort', () => controller.close(), { once: true })
      } }) }
    } })
  restFirst.setSession('s1'); restFirst.setRun('r1')
  const pendingStream = restFirst.subscribeRun({})
  const restSnapshot = await restFirst.reconcileRun(terminalRun)
  assert.strictEqual(restSnapshot.run.status, 'completed')
  assert.deepStrictEqual(restSnapshot.messages.items.map(item => item.message_id), [1, 2])
  assert.strictEqual(pendingSignal.aborted, true)
  await pendingStream

  auth = { token: 'second', userId: 7 }
  restored.checkIdentity()
  assert.strictEqual(restored.getState(), null)
  assert.strictEqual(saved.size, 0)

  saved.set('ktg-agent-session-pointer', JSON.stringify({ sessionId: 'expired', runId: null, seq: 0 }))
  const expired = createAgentSessionClient({
    api: { ...api, getSession: async (_id, options) => {
      assert.strictEqual(options.restoring, true)
      throw Object.assign(new Error('Not found'), { response: { status: 404 } })
    } }, storage, getIdentity: () => auth, fetchStream
  })
  assert.strictEqual(await expired.restoreSession(), null)
  assert.strictEqual(saved.size, 0)

  saved.set('ktg-agent-session-pointer', JSON.stringify({ sessionId: 'old', runId: 'r1', seq: 3 }))
  const foreign = createAgentSessionClient({
    api: { ...api, getSession: async () => { throw new Error('SESSION_NOT_FOUND') } },
    storage, getIdentity: () => auth, fetchStream
  })
  await assert.rejects(foreign.restoreSession(), /SESSION_NOT_FOUND/)
  assert.strictEqual(saved.size, 0)

  saved.set('ktg-agent-session-pointer', JSON.stringify({ sessionId: 's1', runId: 'other', seq: 9 }))
  const mismatched = createAgentSessionClient({
    api: { ...api, getRun: async () => ({ session_id: 'different' }) },
    storage, getIdentity: () => auth, fetchStream
  })
  assert.deepStrictEqual(await mismatched.restoreSession(), { sessionId: 's1', runId: null, seq: 0 })
  mismatched.clear()

  let releaseStream
  let streamSignal
  const closing = createAgentSessionClient({ api, storage, getIdentity: () => auth,
    fetchStream: (_url, options) => new Promise(resolve => {
      streamSignal = options.signal
      releaseStream = () => resolve({ ok: true })
    }) })
  closing.setSession('s1')
  await closing.createRun({ requestId: 'request-1', message: '测试' })
  const pending = closing.subscribeRun({})
  auth = { token: '', userId: 7 } // Vuex can retain an old user ID after logout.
  closing.checkIdentity()
  assert.strictEqual(streamSignal.aborted, true)
  assert.strictEqual(closing.getState(), null)
  assert.strictEqual(saved.size, 0)
  releaseStream()
  await pending
  // Reload discards stored seq because it cannot restore transient text.
  auth = { token: 'first', userId: 7 }
  saved.set('ktg-agent-session-pointer', JSON.stringify({ sessionId: 's1', runId: 'r1', seq: 99 }))
  let gapReads = 0
  let gapConnections = 0
  let snapshotFailures = 0
  const gapUrls = []
  const gapSnapshots = []
  const gapClient = createAgentSessionClient({ storage, getIdentity: () => auth, retryDelay: 0,
    api: { ...api,
      getRun: async () => ({ run_id: 'r1', session_id: 's1',
        status: ++gapReads === 1 ? 'running' : gapReads === 2 ? 'running' : 'completed', last_seq: gapReads <= 2 ? 10 : 12 }),
      listMessages: async () => {
        if (gapReads >= 3 && snapshotFailures++ === 0) throw new Error('snapshot temporarily unavailable')
        return { items: [{ message_id: 99 }], next_cursor: null }
      }
    },
    fetchStream: async url => {
      gapUrls.push(url)
      if (++gapConnections === 1) return { status: 410, ok: false }
      return { status: 200, ok: true, body: stream([bytes(frame(11) + frame(12, 'run_settled'))]) }
    } })
  assert.strictEqual((await gapClient.restoreSession()).seq, 0)
  await gapClient.subscribeRun({ onSnapshot: s => gapSnapshots.push(s) })
  assert.deepStrictEqual(gapUrls, ['/agent-api/v1/runs/r1/events?after_seq=0', '/agent-api/v1/runs/r1/events?after_seq=10'])
  assert.strictEqual(gapSnapshots[0].historyIncomplete, true)
  assert.strictEqual(gapSnapshots[1].run.status, 'completed')
  assert.ok(snapshotFailures >= 2, 'terminal snapshot retries without reopening the stream')
  assert.strictEqual(gapConnections, 2)

  gapClient.setRun('r1', { replay: true })
  assert.strictEqual(gapClient.getState().seq, 0)

  let midstreamConnections = 0
  const midstreamEvents = []
  const midstreamSnapshots = []
  let midstreamRunReads = 0
  const midstream = createAgentSessionClient({ storage: null, getIdentity: () => auth, retryDelay: 0,
    api: { ...api, getRun: async () => ({ run_id: 'r1', session_id: 's1',
      status: ++midstreamRunReads === 1 ? 'running' : 'completed', last_seq: midstreamRunReads === 1 ? 5 : 6 }) },
    fetchStream: async url => {
      if (++midstreamConnections === 1) return { ok: true, status: 200, body: stream([bytes(frame(1) +
        'event: history_expired\ndata: {"run_id":"r1","session_id":"s1","type":"history_expired"}\n\n')]) }
      assert.ok(url.endsWith('after_seq=5'))
      return { ok: true, status: 200, body: stream([bytes(frame(6, 'run_settled'))]) }
    } })
  midstream.setSession('s1'); midstream.setRun('r1')
  await midstream.subscribeRun({ onEvent: e => midstreamEvents.push(e.seq), onSnapshot: s => midstreamSnapshots.push(s) })
  assert.deepStrictEqual(midstreamEvents, [1, 6])
  assert.strictEqual(midstreamSnapshots[0].historyIncomplete, true)
  assert.strictEqual(midstreamSnapshots[1].run.status, 'completed')

  // A late snapshot may not overwrite a newly selected session.
  let releaseMessages
  const staleClient = createAgentSessionClient({ storage: null, getIdentity: () => auth,
    api: { ...api, listMessages: () => new Promise(resolve => { releaseMessages = resolve }) }, fetchStream })
  staleClient.setSession('s1'); staleClient.setRun('r1')
  const oldSnapshot = staleClient.reconcileRun(terminalRun)
  staleClient.setSession('s2')
  releaseMessages({ items: [{ message_id: 'old' }], next_cursor: null })
  assert.strictEqual(await oldSnapshot, null)
  assert.strictEqual(staleClient.getState().sessionId, 's2')
  console.log('agent session tests passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
