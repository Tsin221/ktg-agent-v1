-- S2-01 生产工单字段契约迁移（MySQL 5.7，可重复执行）
-- workorder_type 的既有数据统一回填为 SELF；工单编号由数据库唯一索引兜底。

SET @has_workorder_type := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'pro_workorder'
      AND column_name = 'workorder_type'
);
SET @ddl := IF(
    @has_workorder_type = 0,
    'ALTER TABLE pro_workorder ADD COLUMN workorder_type varchar(64) NOT NULL DEFAULT ''SELF'' COMMENT ''工单类型'' AFTER workorder_name',
    'SELECT ''workorder_type already exists'''
);
PREPARE statement FROM @ddl;
EXECUTE statement;
DEALLOCATE PREPARE statement;

UPDATE pro_workorder
SET workorder_type = 'SELF'
WHERE workorder_type IS NULL OR workorder_type = '';

SET @has_workorder_code_index := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'pro_workorder'
      AND index_name = 'uk_pro_workorder_code'
);
SET @ddl := IF(
    @has_workorder_code_index = 0,
    'ALTER TABLE pro_workorder ADD UNIQUE INDEX uk_pro_workorder_code (workorder_code)',
    'SELECT ''uk_pro_workorder_code already exists'''
);
PREPARE statement FROM @ddl;
EXECUTE statement;
DEALLOCATE PREPARE statement;
