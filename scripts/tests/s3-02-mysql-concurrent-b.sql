-- The second claim must wait for A, then fail with duplicate key 1062.
START TRANSACTION;
INSERT INTO wm_inventory_operation
 (operation_id, action_type, document_type, document_id, request_digest,
  executed_by_id, executed_by_name, operation_status, created_at)
 VALUES ('22222222-2222-4222-8222-222222222222', 'ITEM_RECEIPT_EXECUTE', 'IR', 1,
  REPEAT('b', 64), 8, 'fixture', 'executing', NOW(6));
INSERT INTO wm_inventory_executed_document VALUES
 ('ITEM_RECEIPT_EXECUTE', 'IR', 1, '22222222-2222-4222-8222-222222222222');
-- These must never run after the duplicate-key failure.
UPDATE wm_material_stock SET quantity_onhand = quantity_onhand + 2 WHERE material_stock_id = 1;
INSERT INTO wm_transaction
 (source_doc_type, source_doc_id, source_doc_line_id, transaction_type, transaction_quantity)
 VALUES ('IR', 1, 11, 'ITEM_RECPT', 2.00);
COMMIT;
