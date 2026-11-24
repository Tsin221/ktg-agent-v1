USE ktg_demo;
START TRANSACTION;

INSERT INTO sys_dict_type (dict_name, dict_type, status, create_by, create_time, remark)
SELECT '工单类型', 'mes_workorder_type', '0', 'demo_seed', NOW(), '生产工单类型'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_type WHERE dict_type = 'mes_workorder_type');

INSERT INTO sys_dict_data
    (dict_sort, dict_label, dict_value, dict_type, css_class, list_class, is_default, status, create_by, create_time)
SELECT expected.sort_order, expected.label, expected.code, 'mes_workorder_type', '', 'default', expected.is_default, '0', 'demo_seed', NOW()
FROM (
    SELECT 1 AS sort_order, '自制' AS label, 'SELF' AS code, 'Y' AS is_default
    UNION ALL SELECT 2, '委外', 'OUTSOURCE', 'N'
    UNION ALL SELECT 3, '外购', 'PURCHASE', 'N'
) AS expected
WHERE NOT EXISTS (
    SELECT 1 FROM sys_dict_data existing
    WHERE existing.dict_type = 'mes_workorder_type' AND existing.dict_value = expected.code
);

COMMIT;
