-- Run the duplicate-dimension preflight in the S3-04 record first. Do not
-- discard or silently merge historical balances when the unique key fails.
-- Stock quantity is decimal because FLOAT/DOUBLE cannot preserve exact cents.
ALTER TABLE wm_material_stock MODIFY quantity_onhand DECIMAL(18,2) NOT NULL;

SET @s304_has_key := (SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'wm_material_stock'
      AND column_name = 'stock_dimension_key');
SET @s304_sql := IF(@s304_has_key = 0,
    'ALTER TABLE wm_material_stock ADD COLUMN stock_dimension_key CHAR(64) GENERATED ALWAYS AS (SHA2(CONCAT_WS(CHAR(31), item_id, warehouse_id, COALESCE(location_id,0), COALESCE(area_id,0), COALESCE(vendor_id,0), COALESCE(workorder_id,0), HEX(TRIM(COALESCE(unit_of_measure,''))), HEX(TRIM(COALESCE(batch_code,'')))), 256)) STORED',
    'SELECT 1');
PREPARE s304_stmt FROM @s304_sql;
EXECUTE s304_stmt;
DEALLOCATE PREPARE s304_stmt;

SET @s304_has_index := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'wm_material_stock'
      AND index_name = 'uk_wm_stock_dimension');
SET @s304_sql := IF(@s304_has_index = 0,
    'ALTER TABLE wm_material_stock ADD UNIQUE KEY uk_wm_stock_dimension (stock_dimension_key)',
    'SELECT 1');
PREPARE s304_stmt FROM @s304_sql;
EXECUTE s304_stmt;
DEALLOCATE PREPARE s304_stmt;
