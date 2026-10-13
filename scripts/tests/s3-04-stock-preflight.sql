-- Read-only. Run on each target database before the S3-04 migration.
SELECT COUNT(*) AS null_quantity_count FROM wm_material_stock WHERE quantity_onhand IS NULL;
SELECT COUNT(*) AS invalid_dimension_count FROM wm_material_stock
WHERE item_id IS NULL OR item_id <= 0 OR warehouse_id IS NULL OR warehouse_id <= 0
   OR unit_of_measure IS NULL OR TRIM(unit_of_measure) = '';
SELECT item_id, warehouse_id, COALESCE(location_id,0) AS location_key,
       COALESCE(area_id,0) AS area_key, COALESCE(vendor_id,0) AS vendor_key,
       COALESCE(workorder_id,0) AS workorder_key,
       HEX(TRIM(COALESCE(unit_of_measure,''))) AS unit_key,
       HEX(TRIM(COALESCE(batch_code,''))) AS batch_key,
       COUNT(*) AS rows_in_dimension,
       GROUP_CONCAT(material_stock_id ORDER BY material_stock_id) AS stock_ids
FROM wm_material_stock
GROUP BY item_id, warehouse_id, location_key, area_key, vendor_key,
         workorder_key, unit_key, batch_key
HAVING COUNT(*) > 1;
