-- Define the permissions used by the check-plan device and subject detail lists.
-- Role grants are intentionally managed separately from this shared migration.
INSERT INTO sys_menu (
    menu_name, parent_id, order_num, path, component, query,
    is_frame, is_cache, menu_type, visible, status, perms, icon,
    create_by, create_time, update_by, update_time, remark
)
SELECT
    permission.menu_name, parent_menu.menu_id, permission.order_num, '', NULL, NULL,
    1, 0, 'F', '0', '0', permission.perms, '#',
    'admin', NOW(), '', NULL, ''
FROM sys_menu parent_menu
CROSS JOIN (
    SELECT '点检设备列表查询' AS menu_name, 6 AS order_num, 'mes:dv:checkmachinery:list' AS perms
    UNION ALL SELECT '点检设备新增', 7, 'mes:dv:checkmachinery:add'
    UNION ALL SELECT '点检设备删除', 8, 'mes:dv:checkmachinery:remove'
    UNION ALL SELECT '点检项目列表查询', 9, 'mes:dv:checksubject:list'
    UNION ALL SELECT '点检项目新增', 10, 'mes:dv:checksubject:add'
    UNION ALL SELECT '点检项目删除', 11, 'mes:dv:checksubject:remove'
) permission
WHERE parent_menu.perms = 'mes:dv:checkplan'
  AND NOT EXISTS (
      SELECT 1 FROM sys_menu existing_menu
      WHERE existing_menu.perms = permission.perms
  );
