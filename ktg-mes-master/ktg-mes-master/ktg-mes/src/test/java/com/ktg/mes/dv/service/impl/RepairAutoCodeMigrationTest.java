package com.ktg.mes.dv.service.impl;

import org.h2.jdbcx.JdbcDataSource;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.FileSystemResource;
import org.springframework.jdbc.datasource.init.DatabasePopulatorUtils;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;

import javax.sql.DataSource;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;

class RepairAutoCodeMigrationTest {
    private static final String MIGRATION_NAME = "20261026_dv_repair_autocode.sql";

    @Test
    void insertsMissingRuleAndPartsAndIsSafeToRepeat() {
        DataSource db = newDatabase();
        createSchema(db);

        runMigration(db);

        assertEquals(1, count(db, "sys_auto_code_rule"));
        assertEquals(3, count(db, "sys_auto_code_part"));
        long ruleId = queryLong(db, "select rule_id from sys_auto_code_rule where rule_code='REPAIR_CODE'");
        assertEquals("维修工单编号规则", queryString(db, "select rule_name from sys_auto_code_rule where rule_id=" + ruleId));
        assertEquals(10, queryInt(db, "select max_length from sys_auto_code_rule where rule_id=" + ruleId));
        assertEquals("REP", queryString(db, "select fix_character from sys_auto_code_part where rule_id=" + ruleId + " and part_index=1"));
        assertEquals("yyyy", queryString(db, "select date_format from sys_auto_code_part where rule_id=" + ruleId + " and part_index=2"));
        assertEquals("YEAR", queryString(db, "select cycle_method from sys_auto_code_part where rule_id=" + ruleId + " and part_index=3"));

        execute(db, "update sys_auto_code_rule set rule_name='CUSTOM RULE', max_length=23 where rule_id=" + ruleId);
        execute(db, "update sys_auto_code_part set part_code='CUSTOM_PREFIX', fix_character='XYZ' where rule_id=" + ruleId + " and part_index=1");
        execute(db, "insert into sys_auto_code_result(rule_id, gen_date, gen_index, last_result, last_serial_no) values (" + ruleId + ", '2026', 17, 'KEEP-17', 17)");

        runMigration(db);

        assertEquals(1, count(db, "sys_auto_code_rule"), "reapplying migration must not add a rule");
        assertEquals(3, count(db, "sys_auto_code_part"), "reapplying migration must not add parts");
        assertEquals("CUSTOM RULE", queryString(db, "select rule_name from sys_auto_code_rule where rule_id=" + ruleId));
        assertEquals(23, queryInt(db, "select max_length from sys_auto_code_rule where rule_id=" + ruleId));
        assertEquals("CUSTOM_PREFIX", queryString(db, "select part_code from sys_auto_code_part where rule_id=" + ruleId + " and part_index=1"));
        assertEquals("XYZ", queryString(db, "select fix_character from sys_auto_code_part where rule_id=" + ruleId + " and part_index=1"));
        assertEquals(1, count(db, "sys_auto_code_result"), "generated sequence history must remain untouched");
        assertEquals("KEEP-17", queryString(db, "select last_result from sys_auto_code_result where rule_id=" + ruleId));
        assertEquals(17, queryInt(db, "select last_serial_no from sys_auto_code_result where rule_id=" + ruleId));
    }

    @Test
    void addsOnlyTheMissingPartForAnExistingCustomizedRule() {
        DataSource db = newDatabase();
        createSchema(db);
        execute(db, "insert into sys_auto_code_rule(rule_code, rule_name, max_length, is_padded, padded_method, enable_flag)"
                + " values ('REPAIR_CODE', 'CUSTOM RULE', 23, 'N', 'L', 'Y')");
        long ruleId = queryLong(db, "select rule_id from sys_auto_code_rule where rule_code='REPAIR_CODE'");
        execute(db, "insert into sys_auto_code_part(rule_id, part_index, part_type, part_code, part_name, part_length, fix_character)"
                + " values (" + ruleId + ", 1, 'FIXCHAR', 'CUSTOM_PREFIX', 'custom', 3, 'XYZ')");
        execute(db, "insert into sys_auto_code_part(rule_id, part_index, part_type, part_code, part_name, part_length, seria_start_no, seria_step, seria_now_no, cycle_flag, cycle_method)"
                + " values (" + ruleId + ", 3, 'SERIALNO', 'CUSTOM_SERIAL', 'custom', 3, 7, 2, 87, 'N', 'OTHER')");

        runMigration(db);

        assertEquals(1, count(db, "sys_auto_code_rule"), "an existing rule must not be replaced");
        assertEquals(3, count(db, "sys_auto_code_part"), "only the missing date part should be added");
        assertEquals("CUSTOM RULE", queryString(db, "select rule_name from sys_auto_code_rule where rule_id=" + ruleId));
        assertEquals(23, queryInt(db, "select max_length from sys_auto_code_rule where rule_id=" + ruleId));
        assertEquals("CUSTOM_PREFIX", queryString(db, "select part_code from sys_auto_code_part where rule_id=" + ruleId + " and part_index=1"));
        assertEquals("XYZ", queryString(db, "select fix_character from sys_auto_code_part where rule_id=" + ruleId + " and part_index=1"));
        assertEquals("CUSTOM_SERIAL", queryString(db, "select part_code from sys_auto_code_part where rule_id=" + ruleId + " and part_index=3"));
        assertEquals(87, queryInt(db, "select seria_now_no from sys_auto_code_part where rule_id=" + ruleId + " and part_index=3"));
        assertEquals("yyyy", queryString(db, "select date_format from sys_auto_code_part where rule_id=" + ruleId + " and part_index=2"));
    }

    private DataSource newDatabase() {
        JdbcDataSource db = new JdbcDataSource();
        db.setURL("jdbc:h2:mem:repair-autocode-" + UUID.randomUUID() + ";MODE=MySQL;DB_CLOSE_DELAY=-1");
        return db;
    }

    private void createSchema(DataSource db) {
        execute(db, "create table sys_auto_code_rule (rule_id bigint generated by default as identity primary key,"
                + " rule_code varchar(64) not null, rule_name varchar(255), rule_desc varchar(500), max_length int,"
                + " is_padded char(1), padded_char varchar(20), padded_method char(1), enable_flag char(1),"
                + " create_by varchar(64), create_time timestamp, update_by varchar(64), update_time timestamp)");
        execute(db, "create table sys_auto_code_part (part_id bigint generated by default as identity primary key,"
                + " rule_id bigint not null, part_index int not null, part_type varchar(20), part_code varchar(64),"
                + " part_name varchar(255), part_length int, date_format varchar(20), fix_character varchar(64),"
                + " seria_start_no int, seria_step int, seria_now_no int, cycle_flag char(1), cycle_method varchar(20),"
                + " create_by varchar(64), create_time timestamp, update_by varchar(64), update_time timestamp)");
        execute(db, "create table sys_auto_code_result (code_id bigint generated by default as identity primary key,"
                + " rule_id bigint not null, gen_date varchar(20), gen_index int, last_result varchar(64), last_serial_no int)");
    }

    private void runMigration(DataSource db) {
        DatabasePopulatorUtils.execute(
                new ResourceDatabasePopulator(new FileSystemResource(migrationPath().toFile())), db);
    }

    private Path migrationPath() {
        Path current = Paths.get("").toAbsolutePath();
        while (current != null) {
            Path direct = current.resolve("sql").resolve(MIGRATION_NAME);
            Path nested = current.resolve("ktg-mes-master").resolve("ktg-mes-master").resolve("sql").resolve(MIGRATION_NAME);
            if (Files.isRegularFile(direct)) return direct;
            if (Files.isRegularFile(nested)) return nested;
            current = current.getParent();
        }
        throw new IllegalStateException("Could not locate SQL migration " + MIGRATION_NAME);
    }

    private void execute(DataSource db, String sql) {
        new org.springframework.jdbc.core.JdbcTemplate(db).execute(sql);
    }

    private int count(DataSource db, String table) {
        return new org.springframework.jdbc.core.JdbcTemplate(db).queryForObject("select count(*) from " + table, Integer.class);
    }

    private long queryLong(DataSource db, String sql) {
        return new org.springframework.jdbc.core.JdbcTemplate(db).queryForObject(sql, Long.class);
    }

    private int queryInt(DataSource db, String sql) {
        return new org.springframework.jdbc.core.JdbcTemplate(db).queryForObject(sql, Integer.class);
    }

    private String queryString(DataSource db, String sql) {
        return new org.springframework.jdbc.core.JdbcTemplate(db).queryForObject(sql, String.class);
    }
}
