'use strict'

// Dedicated same-origin gateway for the formal MES demo only.
const fs = require('fs')
const http = require('http')
const path = require('path')

const root = path.resolve(process.argv[2] || '')
const isolatedD1 = process.argv[3] === '--d1-validation'
const runtime = path.resolve(__dirname, '../.runtime/mes-demo/agent')
const backendPort = isolatedD1 ? 18092 : 18090
const agentPort = isolatedD1 ? 18093 : 18091
const uiPort = isolatedD1 ? 18855 : 18854
const approvedRoot = isolatedD1 ? root === path.resolve(__dirname, '../.runtime/mes-d1-ui-dist')
  : root.startsWith(runtime + path.sep) && path.basename(root).startsWith('ui-build-')
if (!process.argv[2] || !approvedRoot ||
    !fs.existsSync(path.join(root, 'index.html'))) {
  throw new Error('Provide a verified independent MES demo UI build directory')
}

const types = {
  '.html': 'text/html; charset=utf-8', '.js': 'application/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
  '.map': 'application/json', '.pdf': 'application/pdf'
}

const hopHeaders = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade'
])
function forwardedHeaders(headers) {
  const blocked = new Set(hopHeaders)
  for (const name of String(headers.connection || '').split(',')) {
    if (name.trim()) blocked.add(name.trim().toLowerCase())
  }
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !blocked.has(name.toLowerCase())))
}

function proxy(req, res, port, upstreamPath) {
  const headers = forwardedHeaders(req.headers)
  headers.host = '127.0.0.1:' + port
  const upstream = http.request({
    host: '127.0.0.1', port, method: req.method, path: upstreamPath, headers
  }, upstreamResponse => {
    const outgoing = forwardedHeaders(upstreamResponse.headers)
    // SSE must not be buffered, compressed or cached by the gateway.
    if (String(outgoing['content-type'] || '').includes('text/event-stream')) {
      outgoing['cache-control'] = 'no-cache, no-store, no-transform'
      outgoing['x-accel-buffering'] = 'no'
    }
    res.writeHead(upstreamResponse.statusCode || 502, outgoing)
    res.flushHeaders()
    upstreamResponse.pipe(res)
    upstreamResponse.on('error', () => res.destroy())
    res.once('close', () => {
      if (!res.writableFinished) upstreamResponse.destroy()
    })
  })
  upstream.on('error', () => {
    if (res.headersSent) return res.destroy()
    res.writeHead(502, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'})
    res.end(JSON.stringify({error: {code: 'UPSTREAM_UNAVAILABLE', message: 'MES demo service unavailable'}}))
  })
  req.on('aborted', () => upstream.destroy())
  res.once('close', () => { if (!res.writableFinished) upstream.destroy() })
  req.pipe(upstream)
}

const server = http.createServer((req, res) => {
  const requestPath = req.url || '/'
  const pathname = requestPath.split('?')[0]
  if (pathname === '/prod-api' || pathname.startsWith('/prod-api/')) {
    return proxy(req, res, backendPort, requestPath.replace(/^\/prod-api/, '') || '/')
  }
  if (pathname === '/agent-api' || pathname.startsWith('/agent-api/')) {
    return proxy(req, res, agentPort, requestPath)
  }
  if (pathname === '/ureport' || pathname.startsWith('/ureport/')) {
    return proxy(req, res, backendPort, requestPath)
  }
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405); return res.end()
  }
  let target
  try {
    target = path.resolve(root, '.' + decodeURIComponent(pathname))
  } catch {
    res.writeHead(400); return res.end()
  }
  if (target !== root && !target.startsWith(root + path.sep)) {
    res.writeHead(403); return res.end()
  }
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
    target = path.join(root, 'index.html')
  }
  res.writeHead(200, {
    'Content-Type': types[path.extname(target)] || 'application/octet-stream',
    'Content-Length': fs.statSync(target).size,
    'Cache-Control': target.endsWith('index.html') ? 'no-cache' : 'public, max-age=3600'
  })
  if (req.method === 'HEAD') return res.end()
  fs.createReadStream(target).on('error', () => res.destroy()).pipe(res)
})
server.timeout = 0
server.listen(uiPort, '127.0.0.1', () => {
  console.log(`MES ${isolatedD1 ? 'D1 validation' : 'formal demo'} UI ${uiPort} -> MES ${backendPort} / Agent ${agentPort}`)
})
