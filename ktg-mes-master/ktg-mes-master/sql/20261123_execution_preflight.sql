-- MES-B01 read-only report. The migration runner requires every check to be ok (1).
-- GROUP BY uses the original SN collation; case and trailing spaces are not distinct.
SELECT 'mysql57', IF(VERSION() LIKE '5.7.%',1,0), VERSION()
UNION ALL SELECT 'isolated_target', IF(DATABASE() REGEXP '^ktg_mes_b01_[a-z0-9_]+$',1,0), DATABASE()
UNION ALL SELECT 'legacy_innodb_tables', IF(COUNT(*)=12,1,0), COUNT(*)
    FROM information_schema.tables WHERE table_schema=DATABASE() AND engine='InnoDB'
    AND table_name IN ('pro_workorder','pro_task','pro_route_process','pro_feedback','wm_sn','qc_ipqc',
        'wm_item_consume_line','wm_product_produce_line','wm_package_line','wm_material_stock','wm_transaction','pro_route')
UNION ALL SELECT 'legacy_pk_types', IF(COUNT(*)=8,1,0), COUNT(*)
    FROM information_schema.columns WHERE table_schema=DATABASE() AND data_type='bigint'
    AND column_type NOT LIKE '%unsigned%' AND is_nullable='NO' AND column_key='PRI'
    AND ((table_name='pro_workorder' AND column_name='workorder_id')
      OR (table_name='pro_task' AND column_name='task_id')
      OR (table_name='pro_feedback' AND column_name='record_id')
      OR (table_name='wm_sn' AND column_name='sn_id')
      OR (table_name='qc_ipqc' AND column_name='ipqc_id')
      OR (table_name='wm_item_consume_line' AND column_name='line_id')
      OR (table_name='wm_product_produce_line' AND column_name='line_id')
      OR (table_name='wm_package_line' AND column_name='line_id'))
UNION ALL SELECT 'legacy_workorder_version', IF(COUNT(*)=1,1,0), COUNT(*)
    FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='pro_workorder'
    AND column_name='version' AND data_type='bigint' AND is_nullable='NO'
UNION ALL SELECT 'legacy_task_workstation_not_null', IF(COUNT(*)=3,1,0), COUNT(*)
    FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='pro_task'
    AND column_name IN ('workstation_id','workstation_code','workstation_name') AND is_nullable='NO'
UNION ALL SELECT 'legacy_stock_precision', IF(COUNT(*)=1,1,0), COUNT(*)
    FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='wm_material_stock'
    AND column_name='quantity_onhand' AND numeric_precision=18 AND numeric_scale=2 AND is_nullable='NO'
UNION ALL SELECT 'legacy_stock_unique_dimension', IF(COUNT(*)=1,1,0), COUNT(*)
    FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='wm_material_stock'
    AND index_name='uk_wm_stock_dimension' AND non_unique=0 AND column_name='stock_dimension_key' AND sub_part IS NULL
UNION ALL SELECT 'sn_code_type_and_collation', IF(COUNT(*)=1,1,0), COUNT(*)
    FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='wm_sn' AND column_name='sn_code'
    AND data_type='varchar' AND character_maximum_length=64 AND is_nullable='NO' AND collation_name='utf8mb4_general_ci'
UNION ALL SELECT 'sn_empty_codes', IF(COUNT(*)=0,1,0), COUNT(*) FROM wm_sn WHERE sn_code IS NULL OR sn_code REGEXP '^[[:space:]]*$'
UNION ALL SELECT 'sn_duplicate_groups', IF(COUNT(*)=0,1,0), COUNT(*) FROM
    (SELECT sn_code FROM wm_sn GROUP BY sn_code HAVING COUNT(*)>1) duplicate_sn
UNION ALL SELECT 'existing_sn_index_compatible', IF(COUNT(*)=0 OR (COUNT(*)=1 AND SUM(non_unique=0 AND column_name='sn_code' AND sub_part IS NULL AND index_type='BTREE')=1),1,0), COUNT(*)
    FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='wm_sn' AND index_name='uk_wm_sn_code'
UNION ALL SELECT 'existing_execution_kind_compatible', IF(COUNT(*)=0 OR (COUNT(*)=1 AND SUM(data_type='varchar' AND character_maximum_length=32 AND is_nullable='YES' AND collation_name='ascii_bin' AND column_default IS NULL AND extra='')=1),1,0), COUNT(*)
    FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='pro_route_process' AND column_name='execution_kind';
