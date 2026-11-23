package com.ktg.mes.pro.execution;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

import static org.junit.jupiter.api.Assertions.*;

/** Tests the actual migrated MySQL 5.7 schema; all fixture data rolls back. */
@EnabledIfEnvironmentVariable(named = "MES_B01_MYSQL_URL", matches = ".+")
class ProExecutionSchemaMySqlTest {
    private Connection connect() throws SQLException {
        Connection connection = DriverManager.getConnection(System.getenv("MES_B01_MYSQL_URL"),
                System.getenv("MES_B01_MYSQL_USER"), System.getenv("MES_B01_MYSQL_PASSWORD"));
        try (Statement s = connection.createStatement()) {
            String database = scalar(s, "SELECT DATABASE()");
            assertTrue(database.matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"), "Requires an explicitly isolated B01 database");
            assertTrue(scalar(s, "SELECT VERSION()").startsWith("5.7."), "Requires real MySQL 5.7");
        } catch (Throwable t) {
            connection.close();
            throw t;
        }
        return connection;
    }

    @Test
    void expectedIndexesAndHistoricalConstraintsRemainPresent() throws SQLException {
        try (Connection c = connect(); Statement s = c.createStatement()) {
            assertEquals("8", scalar(s, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                    + "AND table_name LIKE 'pro\\_execution\\_%' AND ENGINE='InnoDB'"));
            assertEquals("3", scalar(s, "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() "
                    + "AND table_name='pro_task' AND column_name IN ('workstation_id','workstation_code','workstation_name') AND is_nullable='NO'"));
            assertEquals("YES", scalar(s, "SELECT is_nullable FROM information_schema.columns WHERE table_schema=DATABASE() "
                    + "AND table_name='pro_route_process' AND column_name='execution_kind'"));
            index(s, "wm_sn", "uk_wm_sn_code", "sn_code", true);
            index(s, "pro_execution_task", "uk_execution_task_step", "workorder_id,normal_step_no", true);
            index(s, "pro_execution_task", "uk_execution_task_disposition", "source_disposition_id", true);
            index(s, "pro_execution_unit", "ix_execution_unit_order_state", "workorder_id,unit_state", false);
            index(s, "pro_execution_unit", "ix_execution_unit_task_state", "current_task_id,unit_state", false);
            index(s, "pro_execution_input", "uk_execution_input_consume", "sn_id,consume_line_id", true);
            index(s, "pro_execution_input", "ix_execution_input_batch", "item_id,source_batch_code", false);
            index(s, "pro_execution_inspection", "uk_execution_inspection_round", "sn_id,round_no", true);
            index(s, "pro_execution_inspection", "uk_execution_inspection_ipqc", "ipqc_id", true);
            index(s, "pro_execution_inspection", "uk_execution_inspection_disposition", "source_disposition_id", true);
            index(s, "pro_execution_inspection", "uk_execution_inspection_produce", "produce_line_id", true);
            index(s, "pro_execution_inspection", "uk_execution_inspection_release", "released_sn_id", true);
            index(s, "pro_execution_disposition", "uk_execution_disposition_inspection", "inspection_id", true);
            index(s, "pro_execution_pack", "PRIMARY", "sn_id", true);
            index(s, "pro_execution_operation", "PRIMARY", "operation_id", true);
            assertEquals("18,2", scalar(s, "SELECT CONCAT(numeric_precision,',',numeric_scale) FROM information_schema.columns "
                    + "WHERE table_schema=DATABASE() AND table_name='pro_execution_input' AND column_name='quantity'"));
            assertEquals("0", scalar(s, "SELECT COUNT(*) FROM information_schema.referential_constraints WHERE constraint_schema=DATABASE() "
                    + "AND table_name LIKE 'pro\\_execution\\_%' AND (delete_rule NOT IN ('RESTRICT','NO ACTION') OR update_rule NOT IN ('RESTRICT','NO ACTION'))"));
            assertEquals("2", scalar(s, "SELECT COUNT(*) FROM information_schema.referential_constraints WHERE constraint_schema=DATABASE() "
                    + "AND constraint_name IN ('fk_execution_task_disposition','fk_execution_inspection_disposition')"));
        }
    }

    @Test
    void singleUnitAndFactConstraintsPermitMultipleBatchesAndRoundsButRejectDuplicates() throws SQLException {
        try (Connection c = connect(); Statement s = c.createStatement()) {
            c.setAutoCommit(false);
            try {
                // Isolated transaction, no committed fabricated history.
                for (long n : new long[]{-991000001L, -991000002L}) {
                    s.executeUpdate("INSERT INTO pro_workorder(workorder_id,workorder_code,workorder_name,order_source,product_id,product_code,product_name,unit_of_measure,request_date,ancestors) VALUES ("+n+",'B01-SCHEMA-"+n+"','schema-test','MANUAL',1,'P','P','EA',NOW(),'0')");
                    s.executeUpdate("INSERT INTO pro_execution_order VALUES ("+n+",1,'R','R',2,1,NOW(3),'{}')");
                }
                task(s, -991000011L, -991000001L, 1);
                task(s, -991000012L, -991000001L, 2);
                task(s, -991000013L, -991000002L, 1);
                legacyTask(s, -991000014L, -991000001L);
                reject(s, "INSERT INTO pro_execution_task(task_id,workorder_id,normal_step_no,process_id,process_code,process_name,step_kind,task_kind) VALUES (-991000014,-991000001,1,1,'P','P','ASSEMBLY','NORMAL')");
                for (long n : new long[]{-991000021L, -991000022L, -991000023L}) {
                    s.executeUpdate("INSERT INTO wm_sn(sn_id,sn_code,item_id) VALUES ("+n+",'B01-SN-"+n+"',1)");
                }
                reject(s, "INSERT INTO wm_sn(sn_id,sn_code,item_id) VALUES (-991000024,'b01-sn--991000021 ',1)");
                unit(s, -991000021L, -991000001L, -991000011L);
                unit(s, -991000022L, -991000001L, -991000011L);
                reject(s, "INSERT INTO pro_execution_unit(sn_id,workorder_id,current_task_id,unit_state,created_at,updated_at) VALUES (-991000023,-991000001,-991000013,'WAIT_ASSEMBLY',NOW(3),NOW(3))");
                reject(s, "DELETE FROM wm_sn WHERE sn_id=-991000021");
                s.executeUpdate("INSERT INTO pro_feedback(record_id,feedback_type,workstation_id,workorder_id,process_id,task_id,item_id,item_code,item_name) VALUES (-991000031,'TEST',1,-991000001,1,-991000011,1,'P','P')");
                s.executeUpdate("INSERT INTO wm_item_consume_line(line_id,item_id,quantity_consume,batch_code) VALUES (-991000041,1,2,'BATCH-A'),(-991000042,1,1,'BATCH-B')");
                input(s, -991000021L, -991000041L, "BATCH-A");
                input(s, -991000021L, -991000042L, "BATCH-B");
                input(s, -991000022L, -991000041L, "BATCH-A");
                reject(s, "INSERT INTO pro_execution_input(sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code,consume_line_id,quantity,unit_of_measure,used_by,used_at) VALUES (-991000021,-991000011,-991000031,1,1,'BATCH-A',-991000041,1,'EA',1,NOW(3))");
                for (long n = -991000051L; n >= -991000055L; n--) {
                    s.executeUpdate("INSERT INTO qc_ipqc(ipqc_id,ipqc_code,ipqc_type,template_id,workorder_id,workstation_id,item_id) VALUES ("+n+",'QC-"+n+"','TEST',1,-991000001,1,1)");
                }
                s.executeUpdate("INSERT INTO wm_product_produce_line(line_id,item_id,quantity_produce) VALUES (-991000061,1,1),(-991000062,1,1)");
                s.executeUpdate("INSERT INTO pro_execution_inspection(inspection_id,sn_id,task_id,ipqc_id,round_no) VALUES (-991000071,-991000021,-991000012,-991000051,1)");
                reject(s, "INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no) VALUES (-991000021,-991000012,-991000052,1)");
                reject(s, "INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no) VALUES (-991000022,-991000012,-991000051,1)");
                s.executeUpdate("INSERT INTO pro_execution_disposition(disposition_id,inspection_id,sn_id,decision,reason,decided_by,decided_at) VALUES (-991000081,-991000071,-991000021,'REWORK','test',1,NOW(3))");
                reject(s, "INSERT INTO pro_execution_disposition(inspection_id,sn_id,decision,reason,decided_by,decided_at) VALUES (-991000071,-991000021,'SCRAP','test',1,NOW(3))");
                legacyTask(s, -991000015L, -991000001L);
                legacyTask(s, -991000016L, -991000001L);
                String reworkTask = "INSERT INTO pro_execution_task(task_id,workorder_id,process_id,process_code,process_name,step_kind,task_kind,source_disposition_id) VALUES (";
                s.executeUpdate(reworkTask + "-991000015,-991000001,1,'R','R','ASSEMBLY','REWORK',-991000081)");
                reject(s, reworkTask + "-991000016,-991000001,1,'R','R','ASSEMBLY','REWORK',-991000081)");
                s.executeUpdate("INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no,source_disposition_id,produce_line_id) VALUES (-991000021,-991000012,-991000052,2,-991000081,-991000061)");
                reject(s, "INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no,source_disposition_id) VALUES (-991000021,-991000012,-991000053,3,-991000081)");
                reject(s, "INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no,produce_line_id) VALUES (-991000021,-991000012,-991000054,3,-991000062)");
                reject(s, "INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no,produce_line_id) VALUES (-991000022,-991000012,-991000055,1,-991000061)");
                s.executeUpdate("INSERT INTO pro_execution_pack(sn_id,task_id,packed_by,packed_at) VALUES (-991000021,-991000012,1,NOW(3))");
                reject(s, "INSERT INTO pro_execution_pack(sn_id,task_id,packed_by,packed_at) VALUES (-991000021,-991000012,1,NOW(3))");
                reject(s, "DELETE FROM qc_ipqc WHERE ipqc_id=-991000051");
                reject(s, "DELETE FROM wm_item_consume_line WHERE line_id=-991000041");
                reject(s, "DELETE FROM wm_product_produce_line WHERE line_id=-991000061");
                assertEquals("2", scalar(s, "SELECT COUNT(*) FROM pro_execution_input WHERE sn_id=-991000021"));
                assertEquals("2", scalar(s, "SELECT COUNT(*) FROM pro_execution_inspection WHERE sn_id=-991000021"));
            } finally {
                c.rollback();
            }
        }
    }

    private static void task(Statement s, long id, long order, int step) throws SQLException {
        legacyTask(s, id, order);
        s.executeUpdate("INSERT INTO pro_execution_task(task_id,workorder_id,normal_step_no,process_id,process_code,process_name,step_kind,task_kind) VALUES ("+id+","+order+","+step+",1,'P','P','ASSEMBLY','NORMAL')");
    }

    private static void legacyTask(Statement s, long id, long order) throws SQLException {
        s.executeUpdate("INSERT INTO pro_task(task_id,task_code,task_name,workorder_id,workorder_code,workorder_name,workstation_id,workstation_code,workstation_name,process_id,item_id,item_code,item_name,unit_of_measure) VALUES ("+id+",'T-"+id+"','T',"+order+",'WO','WO',1,'WS','WS',1,1,'P','P','EA')");
    }

    private static void unit(Statement s, long sn, long order, long task) throws SQLException {
        s.executeUpdate("INSERT INTO pro_execution_unit(sn_id,workorder_id,current_task_id,unit_state,created_at,updated_at) VALUES ("+sn+","+order+","+task+",'WAIT_ASSEMBLY',NOW(3),NOW(3))");
    }

    private static void input(Statement s, long sn, long line, String batch) throws SQLException {
        s.executeUpdate("INSERT INTO pro_execution_input(sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code,consume_line_id,quantity,unit_of_measure,used_by,used_at) VALUES ("+sn+",-991000011,-991000031,1,1,'"+batch+"',"+line+",1,'EA',1,NOW(3))");
    }

    private static void index(Statement s, String table, String name, String columns, boolean unique) throws SQLException {
        assertEquals(columns, scalar(s, "SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='"+table+"' AND index_name='"+name+"' AND non_unique="+(unique?0:1)));
    }

    private static String scalar(Statement s, String sql) throws SQLException {
        try (ResultSet r = s.executeQuery(sql)) { assertTrue(r.next()); return r.getString(1); }
    }

    private static void reject(Statement s, String sql) {
        SQLException e = assertThrows(SQLException.class, () -> s.executeUpdate(sql));
        assertEquals("23000", e.getSQLState(), e.getMessage());
    }
}
