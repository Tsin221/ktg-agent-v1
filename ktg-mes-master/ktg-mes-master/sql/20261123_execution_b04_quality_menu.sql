-- B04 permission metadata only. Execute only against an approved target.
-- This script never grants permissions to a user or role.
INSERT INTO sys_menu (menu_name,parent_id,order_num,path,component,query,is_frame,is_cache,menu_type,visible,status,perms,icon,create_by,create_time,update_by,update_time,remark)
SELECT actions.label,p.menu_id,actions.seq,'',NULL,NULL,1,0,'F','0','0',actions.permission,'#','admin',NOW(),'',NULL,'B04 独立质量动作权限'
FROM sys_menu p CROSS JOIN (
 SELECT '单件检验及检验任务完成' AS label,5 AS seq,'mes:pro:execution:inspect' AS permission
 UNION ALL SELECT '质量处置',6,'mes:pro:execution:disposition'
 UNION ALL SELECT '返工执行',7,'mes:pro:execution:rework'
) actions
WHERE p.component='mes/pro/execution/index' AND p.menu_type='C'
AND NOT EXISTS (SELECT 1 FROM sys_menu m WHERE m.perms=actions.permission);
SELECT menu_id,parent_id,menu_name,perms FROM sys_menu
WHERE perms IN ('mes:pro:execution:inspect','mes:pro:execution:disposition','mes:pro:execution:rework') ORDER BY menu_id;
