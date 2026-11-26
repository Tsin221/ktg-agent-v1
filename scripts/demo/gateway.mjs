import http from 'node:http';
import { readFile, writeFile, rename, stat, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

const project = fileURLToPath(new URL('../../', import.meta.url));
const formalGuide = process.argv.includes('--formal-guide');
const runtime = path.join(project, formalGuide ? '.runtime/mes-demo/agent/production-guide-gateway' : '.runtime/demo');
const root = path.join(project, formalGuide ? '.runtime/mes-demo/agent/ui-build-production-guide-preview' : 'ktg-mes-ui/dist-demo');
const showcaseRoot = path.resolve(project, '../ktg-showcase');
let credentials;
if (formalGuide) {
  const account = JSON.parse((await readFile(path.join(project, '.runtime/mes-demo/accounts.json'), 'utf8')).replace(/^\uFEFF/, '')).leader;
  if (account?.username !== 'lamp_leader' || typeof account.password !== 'string' || !account.password) throw new Error('Unexpected formal demo identity');
  credentials = { username: account.username, password: account.password };
  await mkdir(runtime, { recursive: true });
} else credentials = JSON.parse(await readFile(path.join(runtime, 'account.json'), 'utf8'));
const port = Number(process.env.DEMO_PORT || (formalGuide ? 4175 : 4180));
const backend = formalGuide ? 'http://127.0.0.1:18090' : 'http://127.0.0.1:8082';
const agent = formalGuide ? 'http://127.0.0.1:18091' : 'http://127.0.0.1:8002';
const visitorsFile = path.join(runtime, 'visitors.json');
let visitors = {};
let saveQueue = Promise.resolve();
try { visitors = JSON.parse(await readFile(visitorsFile, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript', '.mjs': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.ico': 'image/x-icon', '.json': 'application/json', '.md': 'text/plain; charset=utf-8', '.pdf': 'application/pdf' };
const showcaseFiles = new Set(['/', '/index.html', '/favicon.svg', '/share.jpg', '/site.css', '/experience.css', '/experience.mjs', '/app.mjs', '/content.mjs', '/interactions.mjs', '/stock-guide.mjs', '/engineering.css', '/resume-guide.css', '/workorder-guide.css']);
function json(res, status, value) { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)); }
function cookie(req) { return /(?:^|;\s*)ktg_demo_visitor=([a-f0-9]{64})(?:;|$)/.exec(req.headers.cookie || '')?.[1]; }
function allowedOrigin(req) { return req.headers.origin === `http://${req.headers.host}` && ['127.0.0.1', 'localhost'].includes(req.headers.host?.split(':')[0]); }
function saveVisitors() {
  const pending = saveQueue.then(async () => {
    await writeFile(visitorsFile + '.tmp', JSON.stringify(visitors), { mode: 0o600 });
    await rename(visitorsFile + '.tmp', visitorsFile);
  });
  saveQueue = pending.catch(() => {});
  return pending;
}
async function api(url, token, body) {
  const response = await fetch(url, { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(12000) });
  return response.json();
}
const server = http.createServer(async (req, res) => {
  res.setHeader('Content-Security-Policy', formalGuide ? "frame-ancestors 'self'" : "frame-ancestors 'self' http://127.0.0.1:4173 http://localhost:4173");
  res.setHeader('X-Content-Type-Options', 'nosniff');
  try {
    const pathname = new URL(req.url, 'http://local').pathname;
    if (pathname === '/demo-health') {
      if (!formalGuide && ['http://127.0.0.1:4173', 'http://localhost:4173'].includes(req.headers.origin)) {
        res.setHeader('Access-Control-Allow-Origin', req.headers.origin);
        res.setHeader('Vary', 'Origin');
      }
      const [mesStatus, agentStatus] = await Promise.all([
        fetch(`${backend}/captchaImage`, { signal: AbortSignal.timeout(4000) }),
        fetch(`${agent}/health`, { signal: AbortSignal.timeout(4000) })
      ]);
      const ready = mesStatus.ok && agentStatus.ok;
      return json(res, ready ? 200 : 503, { ready });
    }
    if (pathname === '/demo-session') {
      if (req.method !== 'POST' || !allowedOrigin(req)) return json(res, 403, { message: '禁止访问' });
      let id = cookie(req), visitor = visitors[id];
      if (visitor && (await api(`${backend}/getInfo`, visitor.token)).code !== 200) visitor = null;
      if (!visitor) {
        const result = await api(`${backend}/login`, null, credentials);
        if (result.code !== 200 || !result.token) throw new Error('Demo login unavailable');
        id = randomBytes(32).toString('hex');
        visitor = visitors[id] = { token: result.token, created: Date.now() };
        for (const [key, item] of Object.entries(visitors)) if (Date.now() - item.created > 86400000) delete visitors[key];
        await saveVisitors();
      }
      res.setHeader('Set-Cookie', `ktg_demo_visitor=${id}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400`);
      return json(res, 200, { token: visitor.token });
    }
    if (pathname.startsWith('/demo-api/') || pathname.startsWith('/agent-api/')) {
      const visitor = visitors[cookie(req)];
      if (!visitor) return json(res, 401, { code: 401, msg: '请重新进入体验' });
      if (!['GET', 'HEAD'].includes(req.method) && !allowedOrigin(req)) return json(res, 403, { code: 403 });
      const isAgent = pathname.startsWith('/agent-api/');
      const upstreamPath = isAgent ? req.url : req.url.slice('/demo-api'.length);
      // The existing feedback page reads the current employee's profile.
      const ownProfile = req.method === 'GET' && /^\/system\/user\/profile(?:\?|$)/.test(upstreamPath);
      if (!isAgent && !ownProfile && !/^\/(?:getInfo(?:\?|$)|getRouters(?:\?|$)|logout(?:\?|$)|mes\/|system\/dict\/data\/type\/|system\/autocode\/get\/)/.test(upstreamPath)) return json(res, 403, { code: 403, msg: '体验账号未开放此功能' });
      const upstream = http.request(new URL(upstreamPath, isAgent ? agent : backend), {
        method: req.method, headers: { 'Content-Type': req.headers['content-type'] || 'application/json', Authorization: `Bearer ${visitor.token}`, ...(req.headers['content-length'] ? { 'Content-Length': req.headers['content-length'] } : {}), ...(req.headers['last-event-id'] ? { 'Last-Event-ID': req.headers['last-event-id'] } : {}) }
      }, incoming => { res.writeHead(incoming.statusCode, { 'Content-Type': incoming.headers['content-type'] || 'application/json', 'Cache-Control': 'no-store' }); incoming.pipe(res); });
      upstream.on('error', () => { if (!res.headersSent) json(res, 502, { code: 502, msg: '体验服务暂时不可用' }); else res.end(); });
      res.on('close', () => upstream.destroy());
      req.pipe(upstream);
      return;
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') return json(res, 405, {});
    const decoded = decodeURIComponent(pathname);
    const useShowcase = formalGuide && (showcaseFiles.has(decoded) || decoded === '/demo' || ['/demo/', '/assets/', '/media/', '/vendor/', '/engineering/', '/evidence/'].some(prefix => decoded.startsWith(prefix)));
    const staticRoot = useShowcase ? showcaseRoot : root;
    const file = path.resolve(staticRoot, '.' + decoded);
    const relative = path.relative(staticRoot, file);
    if (relative.startsWith('..') || path.isAbsolute(relative) || relative.split(path.sep).some(part => part.startsWith('.'))) return json(res, 403, {});
    let target = file;
    try { if (!(await stat(target)).isFile()) target = useShowcase ? path.join(target, 'index.html') : path.join(root, 'index.html'); }
    catch { if (useShowcase || path.extname(file)) return json(res, 404, {}); target = path.join(root, 'index.html'); }
    res.setHeader('Content-Type', mime[path.extname(target)] || 'application/octet-stream');
    res.end(await readFile(target));
  } catch { if (!res.headersSent) json(res, 503, { message: '体验服务暂时不可用' }); else res.end(); }
});
server.listen(port, '127.0.0.1', () => console.log(`MES demo: http://127.0.0.1:${port}/experience`));
