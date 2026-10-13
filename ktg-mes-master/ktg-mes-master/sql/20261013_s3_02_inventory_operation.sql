-- S3-02. Run the preflight queries in the accompanying runbook before applying.
-- InnoDB unique keys, rather than a JVM lock, arbitrate concurrent executors.
CREATE TABLE IF NOT EXISTS wm_inventory_operation (
    operation_id varchar(36) NOT NULL,
    action_type varchar(64) NOT NULL,
    document_type varchar(64) NOT NULL,
    document_id bigint NOT NULL,
    request_digest char(64) NOT NULL,
    executed_by_id bigint NOT NULL,
    executed_by_name varchar(64) NOT NULL,
    operation_status varchar(16) NOT NULL,
    result_json longtext NULL,
    error_code varchar(64) NULL,
    created_at datetime(6) NOT NULL,
    completed_at datetime(6) NULL,
    PRIMARY KEY (operation_id),
    KEY idx_inventory_operation_document (action_type, document_type, document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Durable inventory execution receipt';

-- A failed attempt may be retried with a new operation ID. Only a committed
-- execution owns the one-time document key. This row commits with inventory.
CREATE TABLE IF NOT EXISTS wm_inventory_executed_document (
    action_type varchar(64) NOT NULL,
    document_type varchar(64) NOT NULL,
    document_id bigint NOT NULL,
    operation_id varchar(36) NOT NULL,
    PRIMARY KEY (action_type, document_type, document_id),
    UNIQUE KEY uk_inventory_execution_operation (operation_id),
    CONSTRAINT fk_inventory_execution_operation FOREIGN KEY (operation_id)
        REFERENCES wm_inventory_operation (operation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='One-time document execution key';

CREATE TABLE IF NOT EXISTS wm_inventory_operation_transaction (
    operation_id varchar(36) NOT NULL,
    transaction_id bigint NOT NULL,
    sequence_no int NOT NULL,
    PRIMARY KEY (operation_id, transaction_id),
    UNIQUE KEY uk_inventory_operation_sequence (operation_id, sequence_no),
    UNIQUE KEY uk_inventory_operation_transaction (transaction_id),
    CONSTRAINT fk_inventory_operation_transaction FOREIGN KEY (operation_id)
        REFERENCES wm_inventory_operation (operation_id),
    CONSTRAINT fk_inventory_operation_ledger FOREIGN KEY (transaction_id)
        REFERENCES wm_transaction (transaction_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Receipt to inventory ledger links';
