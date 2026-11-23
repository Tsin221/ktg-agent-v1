-- B03 menu metadata only. Review and execute exclusively on an approved target.
-- No user or role grants; role authorization is a separate administrator decision.
-- Locate the production parent via the existing workorder menu, not a fixed ID.
INSERT INTO sys_menu (menu_name,parent_id,order_num,path,component,query,is_frame,is_cache,menu_type,visible,status,perms,icon,create_by,create_time,update_by,update_time,remark)
SELECT '车间工作台', p.menu_id, 9, 'execution', 'mes/pro/execution/index', NULL, 1, 1, 'C', '0', '0', 'mes:pro:execution:query', 'log', 'admin', NOW(), '', NULL, 'B03 生产执行工作台'
FROM sys_menu p
WHERE p.menu_type = 'M' AND p.menu_id = (
    SELECT MIN(w.parent_id) FROM sys_menu w WHERE w.component = 'mes/pro/workorder/index' AND w.menu_type = 'C'
) AND NOT EXISTS (SELECT 1 FROM sys_menu m WHERE m.component = 'mes/pro/execution/index');

INSERT INTO sys_menu (menu_name,parent_id,order_num,path,component,query,is_frame,is_cache,menu_type,visible,status,perms,icon,create_by,create_time,update_by,update_time,remark)
SELECT actions.label,p.menu_id,actions.seq,'',NULL,NULL,1,0,'F','0','0',actions.permission,'#','admin',NOW(),'',NULL,'B03 独立动作权限'
FROM sys_menu p CROSS JOIN (
    SELECT '分派任务' AS label, 1 AS seq, 'mes:pro:execution:assign' AS permission
    UNION ALL SELECT '任务开工', 2, 'mes:pro:execution:start'
    UNION ALL SELECT '生产报工及装配任务完成', 3, 'mes:pro:execution:report'
    UNION ALL SELECT '跨工单协调（独立授权）', 4, 'mes:pro:execution:manageAll'
) actions
WHERE p.component = 'mes/pro/execution/index' AND p.menu_type = 'C'
  AND NOT EXISTS (SELECT 1 FROM sys_menu m WHERE m.perms = actions.permission);

-- Verify before granting the C menu and only the needed action F permissions.
SELECT menu_id,parent_id,menu_name,menu_type,component,perms
FROM sys_menu WHERE component='mes/pro/execution/index'
 OR perms IN ('mes:pro:execution:assign','mes:pro:execution:start','mes:pro:execution:report','mes:pro:execution:manageAll')
ORDER BY menu_id;
