-- Register repair detail permissions. Safe to run repeatedly.
-- Optional explicit grant: SET @repairline_role_id = <approved role id>;
-- Only that role receives detail permissions matching its existing header grants.
-- Leave @repairline_role_id unset to register permissions without granting them.
START TRANSACTION;

INSERT INTO sys_menu (
    menu_name, parent_id, order_num, path, component, query,
    is_frame, is_cache, menu_type, visible, status, perms, icon,
    create_by, create_time, update_by, update_time, remark
)
SELECT p.menu_name, parent.menu_id, p.order_num, '', NULL, NULL,
       1, 0, 'F', '0', '0', p.perms, '#',
       'admin', NOW(), '', NULL, 'Repair detail API permission'
FROM sys_menu parent
JOIN (
    SELECT '维修明细列表查询' AS menu_name, 10 AS order_num, 'mes:dv:repairline:list' AS perms
    UNION ALL SELECT '维修明细查看', 11, 'mes:dv:repairline:query'
    UNION ALL SELECT '维修明细新增', 12, 'mes:dv:repairline:add'
    UNION ALL SELECT '维修明细修改', 13, 'mes:dv:repairline:edit'
    UNION ALL SELECT '维修明细删除', 14, 'mes:dv:repairline:remove'
) p ON 1 = 1
WHERE parent.perms = 'mes:dv:repair'
AND NOT EXISTS (SELECT 1 FROM sys_menu existing WHERE existing.perms = p.perms);

INSERT INTO sys_role_menu (role_id, menu_id)
SELECT DISTINCT role.role_id, detail.menu_id
FROM sys_role role
JOIN sys_role_menu header_grant ON header_grant.role_id = role.role_id
JOIN sys_menu header ON header.menu_id = header_grant.menu_id
JOIN sys_menu detail ON detail.perms = CONCAT('mes:dv:repairline:', SUBSTRING_INDEX(header.perms, ':', -1))
WHERE role.role_id = @repairline_role_id
  AND role.status = '0' AND role.del_flag = '0'
  AND header.status = '0'
  AND header.perms IN ('mes:dv:repair:list', 'mes:dv:repair:query',
                       'mes:dv:repair:add', 'mes:dv:repair:edit', 'mes:dv:repair:remove')
  AND NOT EXISTS (SELECT 1 FROM sys_role_menu existing
                  WHERE existing.role_id = role.role_id AND existing.menu_id = detail.menu_id);

COMMIT;
