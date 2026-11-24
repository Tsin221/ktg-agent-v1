"""Create ONLY ktg_demo from schema/configuration and new fictional records."""
import json
import re
import secrets
import subprocess
from pathlib import Path
import bcrypt
from seed_data import records

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / '.runtime/demo'
SOURCE = 'ktg_s208_acceptance'
TARGET = 'ktg_demo'


def docker(command, data=None):
    result = subprocess.run(['docker','exec','-i','ktg-mes-mysql','sh','-c',command], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # SQL input can contain newly generated credentials: never echo statements.
        raise RuntimeError('Database command failed; verify Docker and schema. Error code: ' + str(result.returncode))
    return result.stdout


def sql(statement):
    return docker('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4 --batch --skip-column-names', statement.encode()).decode('utf-8')


def quote(value):
    if value is None: return 'NULL'
    if isinstance(value,(int,float)): return str(value)
    return "'" + str(value).replace('\\','\\\\').replace("'","''") + "'"


def initialize():
    if sql("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='ktg_demo'").strip():
        raise RuntimeError('ktg_demo already exists. Use the documented stop/reset workflow; initialization never overwrites it.')
    RUNTIME.mkdir(parents=True,exist_ok=True)
    schema = docker(f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --no-data --skip-comments --set-gtid-purged=OFF {SOURCE}')
    sql('CREATE DATABASE ktg_demo CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;')
    docker('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4 ktg_demo',schema)
    config_tables=['sys_menu','sys_dict_type','sys_dict_data','sys_config','sys_auto_code_rule','sys_auto_code_part']
    for table in config_tables:
        sql(f'INSERT INTO {TARGET}.{table} SELECT * FROM {SOURCE}.{table};')
    sql(Path(__file__).with_name('workorder-type-dictionary.sql').read_text(encoding='utf-8'))
    sql("UPDATE ktg_demo.sys_config SET config_value='false' WHERE config_key='sys.account.captchaEnabled';")
    password=secrets.token_urlsafe(32)
    db_password=secrets.token_urlsafe(32)
    hashed=bcrypt.hashpw(password.encode(),bcrypt.gensalt()).decode()
    sql(f"CREATE USER IF NOT EXISTS 'ktg_demo_app'@'%' IDENTIFIED BY {quote(db_password)}; ALTER USER 'ktg_demo_app'@'%' IDENTIFIED BY {quote(db_password)}; GRANT SELECT,INSERT,UPDATE,DELETE ON ktg_demo.* TO 'ktg_demo_app'@'%';")
    sql(f"INSERT INTO ktg_demo.sys_user(user_id,user_name,nick_name,password,status,del_flag) VALUES(9000,'demo','体验员工',{quote(hashed)},'0','0'); INSERT INTO ktg_demo.sys_role(role_id,role_name,role_key,role_sort,status,data_scope) VALUES(9000,'助手业务体验','demo_agent',1,'0','1'); INSERT INTO ktg_demo.sys_user_role VALUES(9000,9000);")
    subprocess.run(['python',str(ROOT/'scripts/demo/sync-permissions.py')],check=True)
    for table,rows in records().items():
        for row in rows:
            columns=','.join(f'`{key}`' for key in row)
            sql(f"INSERT INTO ktg_demo.`{table}` ({columns}) VALUES ({','.join(quote(v) for v in row.values())});")
    (RUNTIME/'account.json').write_text(json.dumps(dict(username='demo',password=password),ensure_ascii=False),encoding='utf-8')
    (RUNTIME/'database.json').write_text(json.dumps(dict(username='ktg_demo_app',password=db_password,database=TARGET)),encoding='utf-8')
    (RUNTIME/'initialized.json').write_text(json.dumps(dict(database=TARGET,source_schema=SOURCE,version=1)),encoding='utf-8')
    baseline=docker('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --set-gtid-purged=OFF ktg_demo')
    (RUNTIME/'baseline.sql').write_bytes(baseline)
    print('Created ktg_demo with fictional records and an assistant-scoped account.')
    print(json.dumps({table:len(rows) for table,rows in records().items()},ensure_ascii=False))


if __name__=='__main__': initialize()
