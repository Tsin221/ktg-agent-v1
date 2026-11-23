"""Opt-in B06 fixture; copies an old *isolated* database without changing it.

Only the new target receives temporary identity/role/menu metadata. No production
execution facts are written here. Opening stock and all business facts are
created by the separate application acceptance script.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess

CONTAINER = 'ktg-mes-b03-disposable-mysql'
SOURCE = 'ktg_mes_b05_live'
TARGET = 'ktg_mes_b06_live'
ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / '.runtime' / 'mes-b06-closure'
APP_USER = 'mes_b06_closure_app'
RECEIPT_PERMS = ['mes:wm:itemrecpt:add', 'mes:wm:itemrecpt:list', 'mes:wm:itemrecpt:query',
                 'mes:wm:itemrecpt:edit', 'mes:wm:itemrecptline:add', 'mes:md:sop:add', 'mes:md:sop:list']
LEGACY_PERMS = ['mes:wm:sn:add','mes:wm:sn:list','mes:wm:sn:query']


def docker(*args, input_text=None):
    result = subprocess.run(['docker', *args], input=input_text, text=True,
                            encoding='utf-8', capture_output=True)
    if result.returncode:
        raise RuntimeError('Docker command failed; stop and inspect isolated fixture')
    return result.stdout.strip()


def sql(db, statement):
    if db not in (SOURCE, TARGET, 'information_schema'):
        raise ValueError('database not permitted')
    return docker('exec', '-i', CONTAINER, 'sh', '-c',
                  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 '
                  '-N -B ' + db + ' 2>/dev/null', input_text=statement)


def prepare():
    import bcrypt
    RUN.mkdir(parents=True, exist_ok=True)
    if (RUN / 'credentials.json').exists():
        raise RuntimeError('existing run; do not overwrite credentials')
    ports = docker('port', CONTAINER, '3306')
    if ports != '127.0.0.1:33308':
        raise RuntimeError('unexpected source container port')
    if sql('information_schema', f"SELECT COUNT(*) FROM SCHEMATA WHERE SCHEMA_NAME='{TARGET}';") != '0':
        raise RuntimeError('target exists; stop and inspect (never reset)')
    baseline = sql(SOURCE, "SELECT CONCAT(COUNT(*),':',MAX(workorder_id)) FROM pro_execution_order;")
    if not baseline or sql(SOURCE, "SELECT VERSION();") != '5.7.44':
        raise RuntimeError('unexpected isolated reference')
    docker('exec', CONTAINER, 'sh', '-c',
           'set -e; mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE '
           + TARGET + ' CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci" 2>/dev/null; '
           'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --skip-lock-tables '
           + SOURCE + ' 2>/dev/null > /tmp/b06-closure-consistent-copy.sql; '
           'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ' + TARGET + ' 2>/dev/null '
           '< /tmp/b06-closure-consistent-copy.sql; rm /tmp/b06-closure-consistent-copy.sql')
    if baseline != sql(TARGET, "SELECT CONCAT(COUNT(*),':',MAX(workorder_id)) FROM pro_execution_order;"):
        raise RuntimeError('copied baseline mismatch')

    permissions = {
        'leader_r': ['release', 'assign', 'disposition', 'complete', 'trace'],
        'leader_s': ['release', 'assign', 'disposition', 'complete', 'trace'],
        'assembler': ['start', 'report', 'trace'],
        'inspector': ['start', 'inspect', 'trace'],
        'reworker': ['start', 'rework'],
        'packer': ['start', 'pack'],
        'observer': ['trace'],
    }
    credentials = {'mysqlUser': APP_USER, 'mysqlPassword': secrets.token_hex(24),
                   'redisPassword': secrets.token_hex(24), 'tokenSecret': secrets.token_hex(32),
                   'accounts': {role: {'username': 'b06_closure_' + role,
                                       'password': secrets.token_hex(16) + 'A1!'}
                                for role in permissions}}
    # Test-only permissions missing from old menu metadata; production menu is untouched.
    missing = ['mes:wm:issueline:add', *RECEIPT_PERMS, *LEGACY_PERMS]
    statements = [f"CREATE USER '{APP_USER}'@'%' IDENTIFIED BY '{credentials['mysqlPassword']}';",
                  f"GRANT SELECT,INSERT,UPDATE,DELETE ON {TARGET}.* TO '{APP_USER}'@'%';",
                  (ROOT/'ktg-mes-master/ktg-mes-master/sql/20261123_execution_b06_trace_menu.sql').read_text(encoding='utf-8'),
                  'START TRANSACTION;',
                  "SET @workbench=(SELECT MIN(menu_id) FROM sys_menu WHERE component='mes/pro/execution/index');",
                  'SET @parent=(SELECT parent_id FROM sys_menu WHERE menu_id=@workbench);']
    for perm in missing:
        statements.append(f"INSERT INTO sys_menu(menu_name,parent_id,order_num,path,menu_type,visible,status,perms,icon,create_by,create_time) SELECT 'B06 fixture permission',@workbench,99,'','F','0','0','{perm}','#','B06-closure',NOW() FROM DUAL WHERE NOT EXISTS(SELECT 1 FROM sys_menu WHERE perms='{perm}');")
    for role, actions in permissions.items():
        account = credentials['accounts'][role]
        password_hash = bcrypt.hashpw(account['password'].encode(), bcrypt.gensalt()).decode()
        username = account['username']
        statements.extend([
            f"INSERT INTO sys_user(dept_id,user_name,nick_name,password,status,del_flag,create_by,create_time) SELECT MIN(dept_id),'{username}','B06 {role}','{password_hash}','0','0','B06-closure',NOW() FROM sys_dept WHERE status='0' AND del_flag='0'; SET @u=LAST_INSERT_ID();",
            f"INSERT INTO sys_role(role_name,role_key,role_sort,data_scope,status,del_flag,create_by,create_time) VALUES('B06 {role}','{username}',30,'1','0','0','B06-closure',NOW()); SET @r=LAST_INSERT_ID(); INSERT INTO sys_user_role VALUES(@u,@r);",
        ])
        perms = ['mes:pro:execution:' + p for p in ['query', *actions]]
        if role.startswith('leader'):
            perms += ['mes:pro:workorder:list', 'mes:pro:workorder:add', 'mes:pro:workorder:query',
                      'mes:wm:issueheader:add', 'mes:wm:issueheader:list', 'mes:wm:issueheader:query',
                      'mes:wm:issueheader:edit', 'mes:wm:issueline:add', 'mes:wm:wmstock:list', *RECEIPT_PERMS]
        if role=='leader_r': perms+=LEGACY_PERMS
        quoted = ','.join("'" + p + "'" for p in perms)
        statements.append(f"INSERT INTO sys_role_menu SELECT DISTINCT @r,menu_id FROM sys_menu WHERE menu_id IN (@workbench,@parent) OR perms IN ({quoted});")
    statements.append('COMMIT;')
    # Preserve access to the new copy for safe cleanup if later setup fails.
    (RUN / 'credentials.json').write_text(json.dumps(credentials), encoding='utf-8')
    sql(TARGET, '\n'.join(statements))
    print('Prepared new isolated database and seven temporary non-admin role accounts; source read-only.')


def cleanup():
    if sql(TARGET, 'SELECT DATABASE();') != TARGET:
        raise RuntimeError('wrong cleanup database')
    sql(TARGET, "START TRANSACTION; UPDATE sys_user SET status='1' WHERE create_by='B06-closure'; UPDATE sys_role SET status='1' WHERE create_by='B06-closure'; DELETE rm FROM sys_role_menu rm JOIN sys_role r ON r.role_id=rm.role_id WHERE r.create_by='B06-closure'; DELETE ur FROM sys_user_role ur JOIN sys_user u ON u.user_id=ur.user_id WHERE u.create_by='B06-closure'; DELETE FROM sys_menu WHERE create_by='B06-closure' AND menu_type='F' AND NOT EXISTS(SELECT 1 FROM sys_role_menu rm WHERE rm.menu_id=sys_menu.menu_id); COMMIT; DROP USER IF EXISTS 'mes_b06_closure_app'@'%';")
    counts = sql(TARGET, "SELECT COUNT(*) FROM sys_user WHERE create_by='B06-closure' AND status<>'1'; SELECT COUNT(*) FROM sys_role WHERE create_by='B06-closure' AND status<>'1'; SELECT COUNT(*) FROM sys_role_menu rm JOIN sys_role r ON r.role_id=rm.role_id WHERE r.create_by='B06-closure'; SELECT COUNT(*) FROM sys_user_role ur JOIN sys_user u ON u.user_id=ur.user_id WHERE u.create_by='B06-closure'; SELECT COUNT(*) FROM mysql.user WHERE user='mes_b06_closure_app';")
    assert counts.splitlines() == ['0'] * 5, counts
    print('Temporary B06 users/roles disabled and associations removed. Facts retained.')
    (RUN / 'credentials.json').unlink(missing_ok=True)
    out=ROOT.parent/'docs'/'ktg完善文档'/'证据'/'MES-B06'
    out.mkdir(parents=True,exist_ok=True)
    (out/'closure-cleanup.json').write_text(json.dumps(dict(zip(['activeTestUsers','activeTestRoles','testRoleMenus',
          'testUserRoles','dbAppUser'],[int(n) for n in counts.splitlines()]))|{'credentialFileRemoved':not (RUN/'credentials.json').exists(),
          'databaseRetained':TARGET},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'cleanup'])
    parser.add_argument('--approve-isolated-b06', action='store_true', required=True)
    args = parser.parse_args()
    (prepare if args.action == 'prepare' else cleanup)()
