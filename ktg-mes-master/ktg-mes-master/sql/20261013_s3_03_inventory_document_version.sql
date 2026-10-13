-- Both header versions cover header and line mutations. Apply after S3-02.
SET @ir_exists := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'wm_item_recpt' AND column_name = 'version');
SET @ir_sql := IF(@ir_exists = 0,
  'ALTER TABLE wm_item_recpt ADD COLUMN version BIGINT NOT NULL DEFAULT 0 AFTER status',
  'SELECT ''wm_item_recpt.version already exists''');
PREPARE ir_stmt FROM @ir_sql; EXECUTE ir_stmt; DEALLOCATE PREPARE ir_stmt;

SET @issue_exists := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'wm_issue_header' AND column_name = 'version');
SET @issue_sql := IF(@issue_exists = 0,
  'ALTER TABLE wm_issue_header ADD COLUMN version BIGINT NOT NULL DEFAULT 0 AFTER status',
  'SELECT ''wm_issue_header.version already exists''');
PREPARE issue_stmt FROM @issue_sql; EXECUTE issue_stmt; DEALLOCATE PREPARE issue_stmt;
