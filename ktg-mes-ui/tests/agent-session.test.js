const assert = require('assert')
const path = require('path')
const { transformFileSync } = require('@babel/core')

const filename = path.resolve(__dirname, '../src/api/agentSession.js')
const transformed = transformFileSync(filename, { plugins: ['@babel/plugin-transform-modules-commonjs'] })
const moduleRef = { exports: {} }
new Function('exports', 'module', 'require', transformed.code)(moduleRef.exports, moduleRef, require)
const { createSseParser, createAgentSessionClient } = moduleRef.exports

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
    getRun: async () => ({ session_id: 's1', status: ++runReads === 1 ? 'running' : 'completed', last_seq: 3 }),
    listMessages: async (_sid, params) => params
      ? { items: [{ message_id: 2 }], next_cursor: null }
      : { items: [{ message_id: 1 }], next_cursor: 1 }
  }
  const utf8 = bytes(frame(1) + ': heartbeat\n\n' + frame(1) + frame(2))
  const unicodeAt = utf8.findIndex((value, index) => value > 127 && utf8[index + 1] > 127)
  const chunks = [utf8.slice(0, 4), utf8.slice(4, unicodeAt + 1), utf8.slice(unicodeAt + 1)]
  let connection = 0
  const fetchStream = async (url, options) => {
    urls.push(url)
    assert.strictEqual(options.headers.Authorization, 'Bearer first')
    connection++
    return { ok: true, status: 200,
      body: connection === 1 ? stream(chunks) : stream([bytes(frame(2) + frame(3, 'run_settled'))]) }
  }
  const client = createAgentSessionClient({ api, storage, getIdentity: () => auth, fetchStream, retryDelay: 0 })
  client.setSession('s1')
  await client.createRun({ requestId: 'request-1', message: '测试' })
  await client.createRun({ requestId: 'request-1', message: '测试' })
  assert.strictEqual(postCount, 2)
  // The server returns the same run for repeated request IDs; reconnect only calls GET events.
  await client.subscribeRun({ onEvent: value => received.push(value.seq), onSnapshot: value => snapshots.push(value) })
  assert.deepStrictEqual(received, [1, 2, 3])
  assert.deepStrictEqual(urls, [
    '/agent-api/v1/runs/r1/events?after_seq=0',
    '/agent-api/v1/runs/r1/events?after_seq=2'
  ])
  assert.strictEqual(postCount, 2)
  assert.strictEqual(snapshots[0].run.status, 'completed')
  assert.deepStrictEqual(snapshots[0].messages.items.map(item => item.message_id), [1, 2])
  assert.strictEqual(client.getState().seq, 3)
  assert.deepStrictEqual(Object.keys(JSON.parse(saved.values().next().value)).sort(), ['runId', 'seq', 'sessionId'])

  const restored = createAgentSessionClient({ api, storage, getIdentity: () => auth,
    fetchStream: async () => ({ ok: false, status: 410 }) })
  assert.strictEqual((await restored.restoreSession()).sessionId, 's1')
  await restored.subscribeRun({ onSnapshot: value => snapshots.push(value) })
  assert.strictEqual(snapshots.length, 2)
  auth = { token: 'second', userId: 7 }
  restored.checkIdentity()
  assert.strictEqual(restored.getState(), null)
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
  console.log('agent session tests passed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
