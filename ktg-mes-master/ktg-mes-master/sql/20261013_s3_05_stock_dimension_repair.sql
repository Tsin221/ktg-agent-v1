-- S3-04's original dynamic SQL collapsed quoted empty strings in the generated
-- expression. Replace the expression so batch and unit are separate key parts.
-- The existing unique index remains in place; this DDL fails if data is unsafe.
ALTER TABLE wm_material_stock MODIFY COLUMN stock_dimension_key CHAR(64)
    GENERATED ALWAYS AS (SHA2(CONCAT_WS(CHAR(31), item_id, warehouse_id,
        COALESCE(location_id,0), COALESCE(area_id,0), COALESCE(vendor_id,0),
        COALESCE(workorder_id,0), HEX(TRIM(COALESCE(unit_of_measure,''))),
        HEX(TRIM(COALESCE(batch_code,'')))), 256)) STORED;
