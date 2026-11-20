"""Private demo adapter: Feishu p2p text -> existing Agent query -> safe card."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from cards import render_result as result_card

LOG = logging.getLogger('ktg.feishu')


def parse_message(payload: dict, app_id: str) -> dict | None:
    try:
        header, event = payload['header'], payload['event']
        sender, msg = event['sender'], event['message']
        if header['app_id'] != app_id or sender['sender_type'] != 'user':
            return None
        if msg['chat_type'] != 'p2p' or msg['message_type'] != 'text':
            return None
        mid, uid, chat, tenant = msg['message_id'], sender['sender_id']['open_id'], msg['chat_id'], header['tenant_key']
        if not all(isinstance(v, str) and 0 < len(v) <= 200 for v in (mid, uid, chat, tenant)):
            return None
        if not re.fullmatch(r'om_[A-Za-z0-9_-]+', mid):
            return None
        age = time.time() - int(msg['create_time']) / 1000
        if not -300 <= age <= 86400:
            return None
        raw = msg['content']
        if not isinstance(raw, str) or len(raw) > 20000:
            return None
        text = json.loads(raw).get('text')
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 2000:
            return None
        owner = hashlib.sha256(json.dumps([app_id, tenant, uid, chat]).encode()).hexdigest()
        return {'mid': mid, 'owner': owner, 'text': text.strip()}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    mid TEXT PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL,
                    status TEXT NOT NULL, card TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    owner TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
                UPDATE jobs SET status='interrupted' WHERE status='processing';
            ''')
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, job: dict) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('done','failed')").fetchone()[0] >= 100:
                return False
            if db.execute('SELECT COUNT(*) FROM jobs WHERE owner=? AND created>?', (job['owner'], time.time()-60)).fetchone()[0] >= 20:
                return False
            return db.execute("INSERT OR IGNORE INTO jobs(mid,owner,text,status,created) VALUES (?,?,?,'queued',?)",
                              (job['mid'], job['owner'], job['text'], time.time())).rowcount == 1

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status IN ('queued','outbox','interrupted') ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='processing' WHERE mid=?", (row['mid'],))
            return dict(row)

    def card(self, mid: str, card: dict):
        with self.connect() as db:
            db.execute('UPDATE jobs SET card=? WHERE mid=?', (json.dumps(card, ensure_ascii=False), mid))

    def finish(self, mid: str, ok: bool):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN ? THEN 'done' WHEN attempts>=4 THEN 'failed' ELSE 'outbox' END, attempts=attempts+1, text='' WHERE mid=?", (ok, mid))

    def account(self, owner: str) -> str | None:
        with self.connect() as db:
            row = db.execute('SELECT token FROM accounts WHERE owner=? AND expires>?', (owner, time.time())).fetchone()
            return row['token'] if row else None

    def save_account(self, owner: str, token: str):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?)', (owner, token, time.time()+7*3600))


def process_one(store: Store, query, reply, mes_url: str) -> bool:
    job = store.claim()
    if job is None:
        return False
    if job['card']:
        card = json.loads(job['card'])
    else:
        try:
            if job['status'] == 'interrupted':
                result = {'answer': '服务重启中断了上一次查询，未自动重复执行。请重新发送问题。'}
            else:
                result = query(job['owner'], job['text'])
            card = result_card(result, mes_url)
        except Exception as exc:
            LOG.warning('query_failed type=%s', type(exc).__name__)
            card = result_card({}, mes_url)
        store.card(job['mid'], card)
    try:
        reply(job['mid'], card)
    except Exception as exc:
        LOG.warning('reply_failed type=%s', type(exc).__name__)
        store.finish(job['mid'], False)
    else:
        store.finish(job['mid'], True)
        LOG.info('query_reply_completed')
    return True


class ApiError(Exception):
    pass


def http_json(url: str, body=None, token=None, timeout=20) -> dict:
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read(2*1024*1024))
    except HTTPError as exc:
        raise ApiError('HTTP_' + str(exc.code)) from None
    if not isinstance(data, dict):
        raise ApiError('INVALID_RESPONSE')
    return data


class Adapter:
    def __init__(self, store: Store):
        self.store = store
        self.app_id = os.environ['FEISHU_APP_ID']
        self.app_secret = os.environ['FEISHU_APP_SECRET']
        self.backend = os.environ.get('KTG_BACKEND_URL', 'http://backend:8080')
        self.agent = os.environ.get('KTG_AGENT_URL', 'http://agent:8001')
        self.credentials = json.loads(Path(os.environ.get('KTG_DEMO_ACCOUNT', '/run/demo-account.json')).read_text())
        if self.credentials.get('username') != 'demo':
            raise ValueError('The Feishu demo adapter requires the demo account')
        self.feishu_token = ''
        self.feishu_expiry = 0.0

    def tenant_token(self) -> str:
        if time.time() >= self.feishu_expiry:
            data = http_json('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                             {'app_id': self.app_id, 'app_secret': self.app_secret})
            if data.get('code') != 0 or not data.get('tenant_access_token'):
                raise ApiError('FEISHU_AUTH_' + str(data.get('code')))
            self.feishu_token = data['tenant_access_token']
            self.feishu_expiry = time.time()+max(30, int(data.get('expire',7200))-120)
        return self.feishu_token

    def query(self, owner: str, message: str) -> dict:
        token = self.store.account(owner)
        if token:
            try:
                if http_json(self.backend+'/getInfo', token=token).get('code') != 200:
                    token = None
            except ApiError:
                token = None
        if not token:
            data = http_json(self.backend+'/login', self.credentials)
            if data.get('code') != 200 or not isinstance(data.get('token'), str):
                raise ApiError('DEMO_LOGIN_FAILED')
            token = data['token']
            self.store.save_account(owner, token)
        # No business confirm or execution endpoint is reachable from this adapter.
        return http_json(self.agent+'/agent-api/v1/query', {'message': message}, token=token, timeout=240)

    def reply(self, mid: str, card: dict):
        data = http_json('https://open.feishu.cn/open-apis/im/v1/messages/'+mid+'/reply',
                         {'msg_type': 'interactive', 'content': json.dumps(card,ensure_ascii=False),
                          'uuid': hashlib.sha256(mid.encode()).hexdigest()[:32]}, self.tenant_token())
        if data.get('code') != 0:
            raise ApiError('FEISHU_REPLY_' + str(data.get('code')))


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    import lark_oapi as lark
    store = Store(Path(os.environ.get('BRIDGE_STATE_DB', '/data/bridge.sqlite3')))
    adapter = Adapter(store)
    try:
        adapter.tenant_token()  # Verify credentials without sending a message.
    except ApiError as exc:
        LOG.error('startup_auth_rejected code=%s', str(exc))
        raise SystemExit(2) from None
    LOG.info('credentials_validated')

    def on_message(data):
        job = parse_message(json.loads(lark.JSON.marshal(data)), adapter.app_id)
        if job:
            store.enqueue(job)

    handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(on_message).build()
    client = lark.ws.Client(adapter.app_id, adapter.app_secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
    # The vendor logger can contain temporary WebSocket URLs; keep it out of logs.
    from lark_oapi.core.log import logger as vendor_logger
    vendor_logger.disabled = True
    mes_url = os.environ.get('MES_PUBLIC_URL', 'http://122.51.96.163/experience')
    def work():
        while True:
            try:
                worked = process_one(store, adapter.query, adapter.reply, mes_url)
            except Exception as exc:
                LOG.warning('worker_failed type=%s', type(exc).__name__)
                worked = True
            time.sleep(3 if worked else 0.5)
    def health():
        was_connected = False
        while True:
            # SDK pinned to 1.8.0; _conn is cleared by its disconnect handler.
            connected = client._conn is not None
            p = store.path.parent/'health.json'
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps({'connected':connected,'checked_at':time.time()}))
            tmp.replace(p)
            if connected != was_connected:
                LOG.info('feishu_connection_%s', 'established' if connected else 'disconnected')
                was_connected = connected
            time.sleep(3)
    threading.Thread(target=work, daemon=True).start()
    threading.Thread(target=health, daemon=True).start()
    try:
        client.start()
    except Exception as exc:
        LOG.error('connection_stopped type=%s', type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
