-- MES-B01-04. Claim + finalize + business writes must share the same transaction.
CREATE TABLE IF NOT EXISTS pro_execution_operation (
    operation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    action_type VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    workorder_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,
    request_digest CHAR(71) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    result_ref VARCHAR(4096) NULL,
    committed_at DATETIME(3) NULL,
    PRIMARY KEY (operation_id),
    KEY ix_execution_operation_order (workorder_id,committed_at),
    KEY ix_execution_operation_actor (actor_id,committed_at),
    CONSTRAINT fk_execution_operation_workorder FOREIGN KEY (workorder_id)
        REFERENCES pro_workorder (workorder_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
