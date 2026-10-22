-- MES-09. Apply only after pro_workorder.version migration and a backup in a controlled window.
-- Do not drop this table to roll back application code: receipts are durable proof of writes.
CREATE TABLE pro_workorder_operation (
    operation_id CHAR(36) NOT NULL PRIMARY KEY,
    action_type VARCHAR(20) NOT NULL,
    request_digest CHAR(71) NOT NULL,
    owner_id BIGINT NOT NULL,
    workorder_id BIGINT NULL,
    expected_version BIGINT NULL,
    outcome VARCHAR(16) NOT NULL,
    error_code VARCHAR(64) NULL,
    workorder_code VARCHAR(64) NULL,
    workorder_version BIGINT NULL,
    committed_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY idx_pro_workorder_operation_owner (owner_id, committed_at),
    KEY idx_pro_workorder_operation_workorder (workorder_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Committed draft command results';
