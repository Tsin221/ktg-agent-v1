-- MES-B01-02. Apply ONLY after successful execution preflight in an approved isolated schema.
-- MySQL 5.7: additive DDL, no historical data rewrite or destructive rollback.
SET @b01_kind_exists := (SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema=DATABASE() AND table_name='pro_route_process' AND column_name='execution_kind');
SET @b01_sql := IF(@b01_kind_exists=0,
    'ALTER TABLE pro_route_process ADD COLUMN execution_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL',
    'SELECT ''execution_kind already exists''');
PREPARE b01_stmt FROM @b01_sql;
EXECUTE b01_stmt;
DEALLOCATE PREPARE b01_stmt;

SET @b01_sn_index := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema=DATABASE() AND table_name='wm_sn' AND index_name='uk_wm_sn_code');
SET @b01_sql := IF(@b01_sn_index=0,
    'ALTER TABLE wm_sn ADD UNIQUE KEY uk_wm_sn_code (sn_code)',
    'SELECT ''uk_wm_sn_code already exists''');
PREPARE b01_stmt FROM @b01_sql;
EXECUTE b01_stmt;
DEALLOCATE PREPARE b01_stmt;

CREATE TABLE IF NOT EXISTS pro_execution_order (
    workorder_id BIGINT NOT NULL,
    route_id BIGINT NOT NULL,
    route_code VARCHAR(64) NOT NULL,
    route_name VARCHAR(255) NOT NULL,
    planned_units INT NOT NULL,
    release_user_id BIGINT NOT NULL,
    released_at DATETIME(3) NOT NULL,
    material_requirements_snapshot JSON NOT NULL,
    PRIMARY KEY (workorder_id),
    KEY ix_execution_order_releaser (release_user_id),
    CONSTRAINT fk_execution_order_workorder FOREIGN KEY (workorder_id)
        REFERENCES pro_workorder (workorder_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS pro_execution_task (
    task_id BIGINT NOT NULL,
    workorder_id BIGINT NOT NULL,
    normal_step_no INT NULL,
    route_process_id BIGINT NULL,
    process_id BIGINT NOT NULL,
    process_code VARCHAR(64) NOT NULL,
    process_name VARCHAR(255) NOT NULL,
    step_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    task_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_disposition_id BIGINT NULL,
    assigned_user_id BIGINT NULL,
    started_by BIGINT NULL,
    actual_started_at DATETIME(3) NULL,
    ended_by BIGINT NULL,
    actual_ended_at DATETIME(3) NULL,
    inspection_template_id BIGINT NULL,
    inspection_type VARCHAR(64) NULL,
    inspection_snapshot_json JSON NULL,
    PRIMARY KEY (task_id),
    UNIQUE KEY uk_execution_task_step (workorder_id, normal_step_no),
    UNIQUE KEY uk_execution_task_disposition (source_disposition_id),
    UNIQUE KEY uk_execution_task_order_task (workorder_id, task_id),
    KEY ix_execution_task_assignee (assigned_user_id),
    CONSTRAINT fk_execution_task_task FOREIGN KEY (task_id)
        REFERENCES pro_task (task_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_task_order FOREIGN KEY (workorder_id)
        REFERENCES pro_execution_order (workorder_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS pro_execution_unit (
    sn_id BIGINT NOT NULL,
    workorder_id BIGINT NOT NULL,
    current_task_id BIGINT NOT NULL,
    unit_state VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    assembly_feedback_id BIGINT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (sn_id),
    UNIQUE KEY uk_execution_unit_order_sn (workorder_id, sn_id),
    KEY ix_execution_unit_order_state (workorder_id, unit_state),
    KEY ix_execution_unit_task_state (current_task_id, unit_state),
    KEY ix_execution_unit_order_task (workorder_id, current_task_id),
    KEY ix_execution_unit_feedback (assembly_feedback_id),
    CONSTRAINT fk_execution_unit_sn FOREIGN KEY (sn_id)
        REFERENCES wm_sn (sn_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_unit_order FOREIGN KEY (workorder_id)
        REFERENCES pro_execution_order (workorder_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_unit_task FOREIGN KEY (workorder_id,current_task_id)
        REFERENCES pro_execution_task (workorder_id,task_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_unit_feedback FOREIGN KEY (assembly_feedback_id)
        REFERENCES pro_feedback (record_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
