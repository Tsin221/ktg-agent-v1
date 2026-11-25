"""One-time setup of a named disposable D1 test clone. Source demo is read only."""
from pathlib import Path
import json
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / '.runtime' / 'mes-d1-validation'
CONTAINER = 'ktg-mes-b03-disposable-mysql'
SOURCE = 'ktg_mes_demo'
TARGET = 'ktg_mes_d1_validation_20261124'
APP = 'mes_d1_validation_app'


def docker(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(['docker', *args], input=input_text, text=True, encoding='utf-8', capture_output=True)
    if result.returncode:
        raise RuntimeError('Docker command failed; check the isolated target without printing secrets.')
    return result.stdout.strip()


def sql(statement: str, db: str = TARGET) -> str:
    assert db in (TARGET, SOURCE, 'information_schema')
    return docker('exec', '-i', CONTAINER, 'sh', '-c',
                  f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --default-character-set=utf8mb4 -N -B {db}',
                  input_text=statement)


def main() -> None:
    assert docker('port', CONTAINER, '3306') == '127.0.0.1:33308'
    assert TARGET != SOURCE and TARGET.startswith('ktg_mes_d1_validation_')
    assert sql(f"SELECT COUNT(*) FROM schemata WHERE schema_name='{TARGET}'", 'information_schema') == '0'
    assert not (RUN / 'runtime-secrets.json').exists()
    RUN.mkdir(parents=True, exist_ok=True)
    sql(f'CREATE DATABASE {TARGET} CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci', 'information_schema')
    docker('exec', CONTAINER, 'sh', '-c',
           f'set -e; export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
           f'mysqldump -uroot --single-transaction --skip-lock-tables --skip-add-drop-table --set-gtid-purged=OFF {SOURCE} > /tmp/d1-validation-20261124.sql; '
           f'mysql -uroot {TARGET} < /tmp/d1-validation-20261124.sql; rm /tmp/d1-validation-20261124.sql')
    secret = {'mysqlDatabase': TARGET, 'mysqlUser': APP, 'mysqlPassword': secrets.token_hex(24),
              'redisPassword': secrets.token_hex(24), 'tokenSecret': secrets.token_hex(32)}
    sql(f"CREATE USER '{APP}'@'%' IDENTIFIED BY '{secret['mysqlPassword']}'; GRANT SELECT,INSERT,UPDATE,DELETE ON {TARGET}.* TO '{APP}'@'%';")
    (RUN / 'runtime-secrets.json').write_text(json.dumps(secret), encoding='utf-8')
    before = {table: int(sql(f'SELECT COUNT(*) FROM {table}')) for table in
              ('pro_workorder', 'pro_task', 'pro_execution_unit', 'wm_transaction')}
    (RUN / 'initial-counts.json').write_text(json.dumps(before), encoding='utf-8')
    print(f'D1 isolated clone ready: {TARGET}; baseline counts: {before}')


if __name__ == '__main__':
    main()
