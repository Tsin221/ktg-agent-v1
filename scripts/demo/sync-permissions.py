"""Grant the current tool contract and required list/detail views in ktg_demo only."""
import json
import subprocess
from initialize import ROOT, RUNTIME, sql, quote, docker

def sync():
    exported=subprocess.run(['uv','run','--locked','python',str(ROOT/'scripts/demo/export-permissions.py')],cwd=ROOT/'ktg-agent',capture_output=True,text=True,encoding='utf-8',check=True)
    permissions=set(json.loads(exported.stdout))
    # The original MES lists and their line tables are also part of the embedded experience.
    permissions.update(['mes:wm:itemrecpt:list','mes:wm:itemrecptline:list','mes:wm:issueheader:list','mes:wm:issueline:list','mes:md:mditem:query'])
    sql('DELETE FROM ktg_demo.sys_role_menu WHERE role_id=9000;')
    for permission in sorted(permissions):
        existing=sql(f'SELECT menu_id FROM ktg_demo.sys_menu WHERE perms={quote(permission)};').strip()
        if not existing:
            prefix=permission.rsplit(':',1)[0]
            parents=sql(f"SELECT menu_id FROM ktg_demo.sys_menu WHERE menu_type='C' AND (perms={quote(prefix)} OR perms LIKE {quote(prefix+':%')}) ORDER BY menu_id LIMIT 1;").strip()
            parent=int(parents or 0)
            sql(f"INSERT INTO ktg_demo.sys_menu(menu_name,parent_id,order_num,path,is_frame,is_cache,menu_type,visible,status,perms,icon) VALUES('体验关联查询',{parent},99,'#',1,0,'F','1','0',{quote(permission)},'#');")
    literal=','.join(quote(p) for p in sorted(permissions))
    sql(f"INSERT IGNORE INTO ktg_demo.sys_role_menu SELECT 9000,menu_id FROM ktg_demo.sys_menu WHERE status='0' AND perms IN ({literal});")
    for _ in range(5):
        sql('INSERT IGNORE INTO ktg_demo.sys_role_menu SELECT DISTINCT 9000,m.parent_id FROM ktg_demo.sys_menu m JOIN (SELECT menu_id FROM ktg_demo.sys_role_menu WHERE role_id=9000) r ON m.menu_id=r.menu_id WHERE m.parent_id>0;')
    (RUNTIME/'permissions.json').write_text(json.dumps(sorted(permissions),ensure_ascii=False,indent=2),encoding='utf-8')
    print('Configured',len(permissions),'explicit business permissions.')

if __name__=='__main__':sync()
