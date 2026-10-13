-- A reserved operation survives, while an unsuccessful ledger transaction rolls back.
INSERT INTO wm_inventory_operation
 (operation_id, action_type, document_type, document_id, request_digest,
  executed_by_id, executed_by_name, operation_status, created_at)
 VALUES ('33333333-3333-4333-8333-333333333333', 'PRODUCTION_ISSUE_EXECUTE', 'ISSUE', 2,
  REPEAT('c', 64), 7, 'fixture', 'executing', NOW(6));
START TRANSACTION;
INSERT INTO wm_inventory_executed_document VALUES
 ('PRODUCTION_ISSUE_EXECUTE', 'ISSUE', 2, '33333333-3333-4333-8333-333333333333');
UPDATE wm_material_stock SET quantity_onhand = quantity_onhand - 2 WHERE material_stock_id = 1;
INSERT INTO wm_transaction
 (source_doc_type, source_doc_id, source_doc_line_id, transaction_type, transaction_quantity)
 VALUES ('ISSUE', 2, 21, 'ITEM_ISSUE_OUT', -2.00);
-- Simulates the second ledger write failing before the transaction can commit.
ROLLBACK;
UPDATE wm_inventory_operation SET operation_status = 'failed',
 error_code = 'EXECUTION_FAILED', completed_at = NOW(6)
 WHERE operation_id = '33333333-3333-4333-8333-333333333333';
