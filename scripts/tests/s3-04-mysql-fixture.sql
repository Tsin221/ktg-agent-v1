-- Dedicated S3-04 test database only. First copy the production structure of
-- wm_material_stock and wm_transaction, then apply S3-02 and S3-04 migrations.
CREATE TABLE IF NOT EXISTS wm_item_recpt (
    recpt_id BIGINT PRIMARY KEY, recpt_code VARCHAR(64),
    status VARCHAR(64), version BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS wm_issue_header (
    issue_id BIGINT PRIMARY KEY, issue_code VARCHAR(64),
    status VARCHAR(64), version BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB;
