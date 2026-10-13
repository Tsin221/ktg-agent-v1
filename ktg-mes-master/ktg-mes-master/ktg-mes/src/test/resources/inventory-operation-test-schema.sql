CREATE TABLE wm_inventory_operation (
 operation_id VARCHAR(36) PRIMARY KEY, action_type VARCHAR(64) NOT NULL,
 document_type VARCHAR(64) NOT NULL, document_id BIGINT NOT NULL,
 request_digest CHAR(64) NOT NULL, executed_by_id BIGINT NOT NULL,
 executed_by_name VARCHAR(64) NOT NULL, operation_status VARCHAR(16) NOT NULL,
 result_json CLOB, error_code VARCHAR(64), created_at TIMESTAMP NOT NULL,
 completed_at TIMESTAMP);
CREATE TABLE wm_inventory_executed_document (
 action_type VARCHAR(64) NOT NULL, document_type VARCHAR(64) NOT NULL,
 document_id BIGINT NOT NULL, operation_id VARCHAR(36) NOT NULL UNIQUE,
 PRIMARY KEY(action_type, document_type, document_id),
 FOREIGN KEY(operation_id) REFERENCES wm_inventory_operation(operation_id));
CREATE TABLE wm_inventory_operation_transaction (
 operation_id VARCHAR(36) NOT NULL, transaction_id BIGINT NOT NULL UNIQUE,
 sequence_no INT NOT NULL, PRIMARY KEY(operation_id, transaction_id),
 UNIQUE(operation_id, sequence_no),
 FOREIGN KEY(operation_id) REFERENCES wm_inventory_operation(operation_id));
CREATE TABLE wm_item_recpt (recpt_id BIGINT PRIMARY KEY, recpt_code VARCHAR(64), status VARCHAR(64), version BIGINT NOT NULL DEFAULT 0);
CREATE TABLE wm_issue_header (issue_id BIGINT PRIMARY KEY, issue_code VARCHAR(64), status VARCHAR(64), version BIGINT NOT NULL DEFAULT 0);
CREATE TABLE wm_item_recpt_line (line_id BIGINT PRIMARY KEY, recpt_id BIGINT NOT NULL);
CREATE TABLE wm_issue_line (line_id BIGINT PRIMARY KEY, issue_id BIGINT NOT NULL);
CREATE TABLE wm_material_stock (material_stock_id BIGINT PRIMARY KEY, quantity_onhand DECIMAL(12,2));
CREATE TABLE wm_warehouse (warehouse_id BIGINT PRIMARY KEY, warehouse_code VARCHAR(64), warehouse_name VARCHAR(64));
CREATE TABLE wm_storage_location (location_id BIGINT PRIMARY KEY, warehouse_id BIGINT, location_code VARCHAR(64), location_name VARCHAR(64));
CREATE TABLE wm_storage_area (area_id BIGINT PRIMARY KEY, location_id BIGINT, area_code VARCHAR(64), area_name VARCHAR(64));
CREATE TABLE wm_transaction (
 transaction_id BIGINT AUTO_INCREMENT PRIMARY KEY, transaction_type VARCHAR(64),
 transaction_flag INT, transaction_quantity DECIMAL(12,2), source_doc_type VARCHAR(64),
 source_doc_id BIGINT, source_doc_line_id BIGINT, unit_of_measure VARCHAR(64),
 related_transaction_id BIGINT, material_stock_id BIGINT, item_id BIGINT,
 batch_code VARCHAR(64), warehouse_id BIGINT, location_id BIGINT, area_id BIGINT);
