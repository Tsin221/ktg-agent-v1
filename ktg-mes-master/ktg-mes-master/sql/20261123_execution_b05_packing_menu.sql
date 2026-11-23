-- B05 permission metadata only; no role or user grants. Approved isolated target only.
INSERT INTO sys_menu (menu_name,parent_id,order_num,path,component,query,is_frame,is_cache,menu_type,visible,status,perms,icon,create_by,create_time,remark)
SELECT a.label,p.menu_id,a.seq,'',NULL,NULL,1,0,'F','0','0',a.permission,'#','admin',NOW(),'B05 包装与工单完结独立权限'
FROM sys_menu p CROSS JOIN (
 SELECT '单件包装及包装任务完成' label,8 seq,'mes:pro:execution:pack' permission
 UNION ALL SELECT '生产工单完结',9,'mes:pro:execution:complete'
) a WHERE p.component='mes/pro/execution/index' AND p.menu_type='C'
AND NOT EXISTS(SELECT 1 FROM sys_menu m WHERE m.perms=a.permission);
