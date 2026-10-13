-- Apply only to a newly created, dedicated S3-02 test database.
CREATE TABLE wm_transaction (
    transaction_id bigint NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source_doc_type varchar(64), source_doc_id bigint,
    source_doc_line_id bigint, transaction_type varchar(64),
    transaction_flag int, transaction_quantity decimal(12,2),
    unit_of_measure varchar(64), related_transaction_id bigint
) ENGINE=InnoDB;
CREATE TABLE wm_material_stock (
    material_stock_id bigint PRIMARY KEY,
    quantity_onhand decimal(12,2) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE wm_item_recpt (
    recpt_id bigint PRIMARY KEY, recpt_code varchar(64), status varchar(64)
) ENGINE=InnoDB;
CREATE TABLE wm_issue_header (
    issue_id bigint PRIMARY KEY, issue_code varchar(64), status varchar(64)
) ENGINE=InnoDB;
INSERT INTO wm_material_stock VALUES (1, 10.00);
INSERT INTO wm_item_recpt VALUES (1, 'S302-IR-1', 'APPROVED');
INSERT INTO wm_issue_header VALUES (2, 'S302-ISSUE-2', 'APPROVED');
