-- Add the permission used by the repair page's create button and controller.
-- Do not assign this permission to roles automatically; role grants remain an administrator decision.
INSERT INTO sys_menu (
    menu_name, parent_id, order_num, path, component, query,
    is_frame, is_cache, menu_type, visible, status, perms, icon,
    create_by, create_time, update_by, update_time, remark
)
SELECT
    '维修单新增', parent_menu.menu_id, 2, '', NULL, NULL,
    1, 0, 'F', '0', '0', 'mes:dv:repair:add', '#',
    'admin', NOW(), '', NULL, ''
FROM sys_menu parent_menu
WHERE parent_menu.perms = 'mes:dv:repair'
  AND NOT EXISTS (
      SELECT 1 FROM sys_menu existing_menu
      WHERE existing_menu.perms = 'mes:dv:repair:add'
  );
