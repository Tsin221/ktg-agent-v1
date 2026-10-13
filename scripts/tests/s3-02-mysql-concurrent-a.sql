-- Run on a fresh S3-02 fixture database, concurrently with the B script.
START TRANSACTION;
INSERT INTO wm_inventory_operation
 (operation_id, action_type, document_type, document_id, request_digest,
  executed_by_id, executed_by_name, operation_status, created_at)
 VALUES ('11111111-1111-4111-8111-111111111111', 'ITEM_RECEIPT_EXECUTE', 'IR', 1,
  REPEAT('a', 64), 7, 'fixture', 'executing', NOW(6));
INSERT INTO wm_inventory_executed_document VALUES
 ('ITEM_RECEIPT_EXECUTE', 'IR', 1, '11111111-1111-4111-8111-111111111111');
UPDATE wm_material_stock SET quantity_onhand = quantity_onhand + 2 WHERE material_stock_id = 1;
INSERT INTO wm_transaction
 (source_doc_type, source_doc_id, source_doc_line_id, transaction_type, transaction_quantity)
 VALUES ('IR', 1, 11, 'ITEM_RECPT', 2.00);
INSERT INTO wm_inventory_operation_transaction VALUES
 ('11111111-1111-4111-8111-111111111111', LAST_INSERT_ID(), 1);
UPDATE wm_item_recpt SET status = 'FINISHED' WHERE recpt_id = 1;
UPDATE wm_inventory_operation SET operation_status = 'succeeded',
 result_json = '{"operationStatus":"succeeded"}', completed_at = NOW(6)
 WHERE operation_id = '11111111-1111-4111-8111-111111111111';
SELECT 'A_HOLDS_UNCOMMITTED_DOCUMENT_KEY' AS checkpoint;
DO SLEEP(45);
COMMIT;
