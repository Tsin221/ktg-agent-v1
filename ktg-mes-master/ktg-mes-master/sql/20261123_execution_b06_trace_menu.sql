-- B06 metadata only; review and apply exclusively to an approved target.
-- Explicitly no role grants. API also requires mes:pro:execution:query.
INSERT INTO sys_menu (menu_name,parent_id,order_num,path,component,query,is_frame,is_cache,menu_type,visible,status,perms,icon,create_by,create_time,remark)
SELECT '生产追溯',p.menu_id,10,'execution-trace','mes/pro/execution/trace/index',NULL,1,1,'C','0','0',
       'mes:pro:execution:trace','search','admin',NOW(),'B06 真实单件与批次双向追溯'
FROM sys_menu p
WHERE p.menu_type='M' AND p.menu_id=(
    SELECT MIN(w.parent_id) FROM sys_menu w WHERE w.component='mes/pro/workorder/index' AND w.menu_type='C'
)
AND NOT EXISTS(SELECT 1 FROM sys_menu m WHERE m.component='mes/pro/execution/trace/index' OR m.perms='mes:pro:execution:trace');

SELECT menu_id,parent_id,menu_name,menu_type,component,perms
FROM sys_menu WHERE component='mes/pro/execution/trace/index' OR perms='mes:pro:execution:trace';
