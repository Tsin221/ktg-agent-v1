-- MES-B01-03. These tables link to original facts; there are no cascading deletes.
CREATE TABLE IF NOT EXISTS pro_execution_input (
    input_id BIGINT NOT NULL AUTO_INCREMENT,
    sn_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    feedback_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    material_stock_id BIGINT NOT NULL,
    source_batch_code VARCHAR(255) NOT NULL,
    consume_line_id BIGINT NOT NULL,
    quantity DECIMAL(18,2) NOT NULL,
    unit_of_measure VARCHAR(64) NOT NULL,
    used_by BIGINT NOT NULL,
    used_at DATETIME(3) NOT NULL,
    PRIMARY KEY (input_id),
    UNIQUE KEY uk_execution_input_consume (sn_id,consume_line_id),
    KEY ix_execution_input_batch (item_id,source_batch_code),
    KEY ix_execution_input_task (task_id),
    KEY ix_execution_input_feedback (feedback_id),
    KEY ix_execution_input_consume_line (consume_line_id),
    CONSTRAINT fk_execution_input_unit FOREIGN KEY (sn_id)
        REFERENCES pro_execution_unit (sn_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_input_task FOREIGN KEY (task_id)
        REFERENCES pro_execution_task (task_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_input_feedback FOREIGN KEY (feedback_id)
        REFERENCES pro_feedback (record_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_input_consume_line FOREIGN KEY (consume_line_id)
        REFERENCES wm_item_consume_line (line_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS pro_execution_inspection (
    inspection_id BIGINT NOT NULL AUTO_INCREMENT,
    sn_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    ipqc_id BIGINT NOT NULL,
    round_no INT NOT NULL,
    source_disposition_id BIGINT NULL,
    produce_line_id BIGINT NULL,
    released_sn_id BIGINT GENERATED ALWAYS AS (IF(produce_line_id IS NULL,NULL,sn_id)) STORED,
    PRIMARY KEY (inspection_id),
    UNIQUE KEY uk_execution_inspection_round (sn_id,round_no),
    UNIQUE KEY uk_execution_inspection_ipqc (ipqc_id),
    UNIQUE KEY uk_execution_inspection_disposition (source_disposition_id),
    UNIQUE KEY uk_execution_inspection_produce (produce_line_id),
    UNIQUE KEY uk_execution_inspection_release (released_sn_id),
    KEY ix_execution_inspection_task (task_id),
    CONSTRAINT fk_execution_inspection_unit FOREIGN KEY (sn_id)
        REFERENCES pro_execution_unit (sn_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_inspection_task FOREIGN KEY (task_id)
        REFERENCES pro_execution_task (task_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_inspection_ipqc FOREIGN KEY (ipqc_id)
        REFERENCES qc_ipqc (ipqc_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_inspection_produce FOREIGN KEY (produce_line_id)
        REFERENCES wm_product_produce_line (line_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS pro_execution_disposition (
    disposition_id BIGINT NOT NULL AUTO_INCREMENT,
    inspection_id BIGINT NOT NULL,
    sn_id BIGINT NOT NULL,
    decision VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    reason VARCHAR(500) NOT NULL,
    decided_by BIGINT NOT NULL,
    decided_at DATETIME(3) NOT NULL,
    PRIMARY KEY (disposition_id),
    UNIQUE KEY uk_execution_disposition_inspection (inspection_id),
    KEY ix_execution_disposition_sn (sn_id),
    CONSTRAINT fk_execution_disposition_inspection FOREIGN KEY (inspection_id)
        REFERENCES pro_execution_inspection (inspection_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_disposition_unit FOREIGN KEY (sn_id)
        REFERENCES pro_execution_unit (sn_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS pro_execution_pack (
    sn_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    packed_by BIGINT NOT NULL,
    packed_at DATETIME(3) NOT NULL,
    package_line_id BIGINT NULL,
    PRIMARY KEY (sn_id),
    KEY ix_execution_pack_task (task_id),
    KEY ix_execution_pack_package (package_line_id),
    CONSTRAINT fk_execution_pack_unit FOREIGN KEY (sn_id)
        REFERENCES pro_execution_unit (sn_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_pack_task FOREIGN KEY (task_id)
        REFERENCES pro_execution_task (task_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_pack_line FOREIGN KEY (package_line_id)
        REFERENCES wm_package_line (line_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- These two links close dependency cycles only after all tables exist.
SET @b01_fk := (SELECT COUNT(*) FROM information_schema.referential_constraints
    WHERE constraint_schema=DATABASE() AND table_name='pro_execution_task' AND constraint_name='fk_execution_task_disposition');
SET @b01_sql := IF(@b01_fk=0,
    'ALTER TABLE pro_execution_task ADD CONSTRAINT fk_execution_task_disposition FOREIGN KEY (source_disposition_id) REFERENCES pro_execution_disposition (disposition_id) ON DELETE RESTRICT ON UPDATE RESTRICT',
    'SELECT ''fk_execution_task_disposition already exists''');
PREPARE b01_stmt FROM @b01_sql;
EXECUTE b01_stmt;
DEALLOCATE PREPARE b01_stmt;

SET @b01_fk := (SELECT COUNT(*) FROM information_schema.referential_constraints
    WHERE constraint_schema=DATABASE() AND table_name='pro_execution_inspection' AND constraint_name='fk_execution_inspection_disposition');
SET @b01_sql := IF(@b01_fk=0,
    'ALTER TABLE pro_execution_inspection ADD CONSTRAINT fk_execution_inspection_disposition FOREIGN KEY (source_disposition_id) REFERENCES pro_execution_disposition (disposition_id) ON DELETE RESTRICT ON UPDATE RESTRICT',
    'SELECT ''fk_execution_inspection_disposition already exists''');
PREPARE b01_stmt FROM @b01_sql;
EXECUTE b01_stmt;
DEALLOCATE PREPARE b01_stmt;
