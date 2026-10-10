drop table if exists pro_workorder_bom;
drop table if exists md_product_bom;
drop table if exists pro_workorder;
drop table if exists md_item;

create table md_item (
    item_id bigint primary key,
    item_code varchar(64) not null,
    item_name varchar(255) not null,
    specification varchar(255),
    unit_of_measure varchar(64)
);

create table md_product_bom (
    bom_id bigint auto_increment primary key,
    item_id bigint not null,
    bom_item_id bigint not null,
    bom_item_code varchar(64),
    bom_item_name varchar(255),
    bom_item_spec varchar(255),
    unit_of_measure varchar(64),
    item_or_product varchar(64),
    quantity decimal(20, 6) not null
);

create table pro_workorder (
    workorder_id bigint auto_increment primary key,
    workorder_code varchar(64) not null,
    workorder_name varchar(255),
    workorder_type varchar(64),
    order_source varchar(64),
    source_code varchar(64),
    product_id bigint not null,
    product_code varchar(64),
    product_name varchar(255),
    product_spc varchar(255),
    unit_of_measure varchar(64),
    batch_code varchar(64),
    quantity decimal(20, 6) not null,
    quantity_produced decimal(20, 6),
    quantity_changed decimal(20, 6),
    quantity_scheduled decimal(20, 6),
    client_id bigint,
    client_code varchar(64),
    client_name varchar(255),
    request_date timestamp,
    parent_id bigint,
    ancestors varchar(255),
    status varchar(64),
    version bigint not null default 0,
    remark varchar(500),
    attr1 varchar(64),
    attr2 varchar(255),
    attr3 integer,
    attr4 integer,
    create_by varchar(64),
    create_time timestamp,
    update_by varchar(64),
    update_time timestamp,
    constraint uk_pro_workorder_code unique (workorder_code)
);

create table pro_workorder_bom (
    line_id bigint auto_increment primary key,
    workorder_id bigint not null,
    item_id bigint not null,
    item_code varchar(64),
    item_name varchar(255),
    item_spc varchar(255),
    unit_of_measure varchar(64),
    item_or_product varchar(64),
    quantity decimal(20, 6) not null
);
