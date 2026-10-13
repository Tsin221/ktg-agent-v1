-- Apply only after s3-02-mysql-fixture.sql and both S3-02/S3-03 migrations
-- in the dedicated ktg_s303_state_test database.
CREATE TABLE wm_item_recpt_line (
  line_id BIGINT PRIMARY KEY,
  recpt_id BIGINT NOT NULL,
  INDEX idx_recpt_id (recpt_id)
) ENGINE=InnoDB;
CREATE TABLE wm_issue_line (
  line_id BIGINT PRIMARY KEY,
  issue_id BIGINT NOT NULL,
  INDEX idx_issue_id (issue_id)
) ENGINE=InnoDB;
