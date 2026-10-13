-- Run only against the isolated S3 acceptance database.
-- Baseline fixtures only: this script never executes receipt/issue operations.
USE ktg_s208_acceptance;
START TRANSACTION;

INSERT INTO wm_warehouse (warehouse_id, warehouse_code, warehouse_name, enable_flag, remark, create_by, create_time)
SELECT 930001, 'S3FIX-WH', 'S3 fixture warehouse', 'Y', 'S3 isolated acceptance fixture; do not use in production', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_warehouse WHERE warehouse_code='S3FIX-WH');
INSERT INTO wm_storage_location (location_id, location_code, location_name, warehouse_id, area_flag, remark, create_by, create_time)
SELECT 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'Y', 'S3 isolated acceptance fixture', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_storage_location WHERE location_code='S3FIX-LOC');
INSERT INTO wm_storage_area (area_id, area_code, area_name, location_id, enable_flag, remark, create_by, create_time)
SELECT 930001, 'S3FIX-AREA', 'S3 fixture area', 930001, 'Y', 'S3 isolated acceptance fixture', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_storage_area WHERE area_code='S3FIX-AREA');

INSERT INTO wm_material_stock (material_stock_id, item_type_id, item_id, item_code, item_name, specification, unit_of_measure, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, quantity_onhand, create_by, create_time)
SELECT 930001, i.item_type_id, i.item_id, i.item_code, i.item_name, i.specification, i.unit_of_measure, 'S3FIX-B01', 930001, 'S3FIX-WH', 'S3 fixture warehouse', 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'S3FIX-AREA', 'S3 fixture area', 10.00, 's3_fixture', NOW()
FROM md_item i WHERE i.item_id=3 AND i.item_code='A-01'
AND NOT EXISTS (SELECT 1 FROM wm_material_stock WHERE material_stock_id=930001);
INSERT INTO wm_material_stock (material_stock_id, item_type_id, item_id, item_code, item_name, specification, unit_of_measure, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, quantity_onhand, create_by, create_time)
SELECT 930002, i.item_type_id, i.item_id, i.item_code, i.item_name, i.specification, i.unit_of_measure, 'S3FIX-B02', 930001, 'S3FIX-WH', 'S3 fixture warehouse', 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'S3FIX-AREA', 'S3 fixture area', 3.00, 's3_fixture', NOW()
FROM md_item i WHERE i.item_id=3 AND i.item_code='A-01'
AND NOT EXISTS (SELECT 1 FROM wm_material_stock WHERE material_stock_id=930002);

INSERT INTO wm_item_recpt (recpt_id, recpt_code, recpt_name, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, recpt_date, status, remark, create_by, create_time)
SELECT 930001, 'S3FIX-IR-OK', 'S3 valid two-line receipt', 930001, 'S3FIX-WH', 'S3 fixture warehouse', 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'S3FIX-AREA', 'S3 fixture area', NOW(), 'APPROVED', 'Isolated acceptance only; do not execute before S3 safety implementation', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_item_recpt WHERE recpt_code='S3FIX-IR-OK');
INSERT INTO wm_item_recpt_line (line_id, recpt_id, item_id, item_code, item_name, specification, unit_of_measure, quantity_recived, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, remark, create_by, create_time)
SELECT 930001, 930001, i.item_id, i.item_code, i.item_name, i.specification, i.unit_of_measure, 2.00, 'S3FIX-R01', 930001, 'S3FIX-WH', 'S3 fixture warehouse', 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'S3FIX-AREA', 'S3 fixture area', 'S3 valid receipt line 1', 's3_fixture', NOW()
FROM md_item i WHERE i.item_id=3 AND i.item_code='A-01' AND NOT EXISTS (SELECT 1 FROM wm_item_recpt_line WHERE line_id=930001);
INSERT INTO wm_item_recpt_line (line_id, recpt_id, item_id, item_code, item_name, specification, unit_of_measure, quantity_recived, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, remark, create_by, create_time)
SELECT 930002, 930001, i.item_id, i.item_code, i.item_name, i.specification, i.unit_of_measure, 1.50, 'S3FIX-R02', 930001, 'S3FIX-WH', 'S3 fixture warehouse', 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'S3FIX-AREA', 'S3 fixture area', 'S3 valid receipt line 2', 's3_fixture', NOW()
FROM md_item i WHERE i.item_id=3 AND i.item_code='A-01' AND NOT EXISTS (SELECT 1 FROM wm_item_recpt_line WHERE line_id=930002);
INSERT INTO wm_item_recpt (recpt_id, recpt_code, recpt_name, status, remark, create_by, create_time)
SELECT 930002, 'S3FIX-IR-EMPTY', 'S3 empty receipt', 'PREPARE', 'Negative sample: no lines', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_item_recpt WHERE recpt_code='S3FIX-IR-EMPTY');

INSERT INTO wm_issue_header (issue_id, issue_code, issue_name, workorder_id, workorder_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, issue_date, status, remark, create_by, create_time)
SELECT 930001, 'S3FIX-ISSUE-OK', 'S3 valid two-line issue', 1, 'WO-DEMO-A01-001', 930001, 'S3FIX-WH', 'S3 fixture warehouse', 930001, 'S3FIX-LOC', 'S3 fixture location', 930001, 'S3FIX-AREA', 'S3 fixture area', NOW(), 'APPROVED', 'Isolated acceptance only; do not execute before S3 safety implementation', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_issue_header WHERE issue_code='S3FIX-ISSUE-OK');
INSERT INTO wm_issue_line (line_id, issue_id, material_stock_id, item_id, item_code, item_name, specification, unit_of_measure, quantity_issued, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, remark, create_by, create_time)
SELECT 930001, 930001, s.material_stock_id, s.item_id, s.item_code, s.item_name, s.specification, s.unit_of_measure, 2.00, s.batch_code, s.warehouse_id, s.warehouse_code, s.warehouse_name, s.location_id, s.location_code, s.location_name, s.area_id, s.area_code, s.area_name, 'S3 valid issue line 1', 's3_fixture', NOW()
FROM wm_material_stock s WHERE s.material_stock_id=930001 AND NOT EXISTS (SELECT 1 FROM wm_issue_line WHERE line_id=930001);
INSERT INTO wm_issue_line (line_id, issue_id, material_stock_id, item_id, item_code, item_name, specification, unit_of_measure, quantity_issued, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, remark, create_by, create_time)
SELECT 930002, 930001, s.material_stock_id, s.item_id, s.item_code, s.item_name, s.specification, s.unit_of_measure, 1.00, s.batch_code, s.warehouse_id, s.warehouse_code, s.warehouse_name, s.location_id, s.location_code, s.location_name, s.area_id, s.area_code, s.area_name, 'S3 valid issue line 2', 's3_fixture', NOW()
FROM wm_material_stock s WHERE s.material_stock_id=930002 AND NOT EXISTS (SELECT 1 FROM wm_issue_line WHERE line_id=930002);
INSERT INTO wm_issue_header (issue_id, issue_code, issue_name, workorder_id, workorder_code, status, remark, create_by, create_time)
SELECT 930002, 'S3FIX-ISSUE-LOW', 'S3 insufficient stock issue', 1, 'WO-DEMO-A01-001', 'PREPARE', 'Negative sample: requested quantity exceeds stock', 's3_fixture', NOW()
WHERE NOT EXISTS (SELECT 1 FROM wm_issue_header WHERE issue_code='S3FIX-ISSUE-LOW');
INSERT INTO wm_issue_line (line_id, issue_id, material_stock_id, item_id, item_code, item_name, specification, unit_of_measure, quantity_issued, batch_code, warehouse_id, warehouse_code, warehouse_name, location_id, location_code, location_name, area_id, area_code, area_name, remark, create_by, create_time)
SELECT 930003, 930002, s.material_stock_id, s.item_id, s.item_code, s.item_name, s.specification, s.unit_of_measure, 11.00, s.batch_code, s.warehouse_id, s.warehouse_code, s.warehouse_name, s.location_id, s.location_code, s.location_name, s.area_id, s.area_code, s.area_name, 'S3 insufficient-stock line', 's3_fixture', NOW()
FROM wm_material_stock s WHERE s.material_stock_id=930001 AND NOT EXISTS (SELECT 1 FROM wm_issue_line WHERE line_id=930003);

COMMIT;
