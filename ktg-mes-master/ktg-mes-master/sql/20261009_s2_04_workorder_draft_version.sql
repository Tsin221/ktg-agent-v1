-- S2-04: 草稿乐观锁版本。可重复执行。
SET @version_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'pro_workorder'
      AND column_name = 'version'
);

SET @version_sql := IF(
    @version_exists = 0,
    'ALTER TABLE pro_workorder ADD COLUMN version bigint NOT NULL DEFAULT 0 COMMENT ''草稿乐观锁版本'' AFTER status',
    'SELECT ''pro_workorder.version already exists'''
);

PREPARE version_stmt FROM @version_sql;
EXECUTE version_stmt;
DEALLOCATE PREPARE version_stmt;
