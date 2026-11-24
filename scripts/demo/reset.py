"""Local administrator operation. Run stop.ps1 first; only ktg_demo is restored."""
import json
import socket
import subprocess
from pathlib import Path
from initialize import RUNTIME, sql, docker

for port in (8082,8002,4180):
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1',port))==0:
            raise SystemExit('Stop demo services before resetting data.')
marker=json.loads((RUNTIME/'initialized.json').read_text(encoding='utf-8'))
if marker.get('database')!='ktg_demo': raise SystemExit('Unexpected database; refusing reset.')
baseline=(RUNTIME/'baseline.sql').read_bytes()
if b'CREATE DATABASE' in baseline or b'\nUSE ' in baseline: raise SystemExit('Unexpected database directive in baseline.')
sql('DROP DATABASE ktg_demo; CREATE DATABASE ktg_demo CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;')
docker('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4 ktg_demo',baseline)
sql(Path(__file__).with_name('workorder-type-dictionary.sql').read_text(encoding='utf-8'))
subprocess.run(['python',str(Path(__file__).with_name('sync-permissions.py'))],check=True)
for p in RUNTIME.iterdir():
    if p.is_file() and ('.sqlite3' in p.name or p.name in ('visitors.json','token-secret.txt')):
        if p.resolve().parent!=RUNTIME.resolve(): raise SystemExit('Unexpected runtime path')
        p.unlink()
print('Restored ktg_demo. Old visitor sessions and pending confirmations have been invalidated.')
