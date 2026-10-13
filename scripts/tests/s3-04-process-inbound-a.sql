-- Run only in ktg_s304_atomicity_test, concurrently with process-inbound-b.
START TRANSACTION;
INSERT INTO wm_material_stock (item_id, warehouse_id, location_id, area_id,
    unit_of_measure, batch_code, quantity_onhand)
VALUES (9904, 8804, 7704, 6604, 'pcs', 'S304-PROCESS', 2.00)
ON DUPLICATE KEY UPDATE quantity_onhand = quantity_onhand + VALUES(quantity_onhand),
    material_stock_id = LAST_INSERT_ID(material_stock_id);
SELECT SLEEP(15);
COMMIT;
