package com.ktg.mes.dv.service.impl;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.domain.DvRepair;
import com.ktg.mes.dv.domain.DvRepairLine;
import com.ktg.mes.dv.mapper.DvRepairLineMapper;
import com.ktg.mes.dv.mapper.DvRepairMapper;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabase;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

class DvRepairToolWriteIntegrationTest {
    private AnnotationConfigApplicationContext context;
    private EmbeddedDatabase db;
    private JdbcTemplate jdbc;
    private DvRepairServiceImpl repairs;
    private DvRepairLineServiceImpl lines;
    private PlatformTransactionManager txManager;

    @BeforeEach
    void setUp() {
        db = new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
        context = new AnnotationConfigApplicationContext();
        context.registerBean("database", EmbeddedDatabase.class, () -> db);
        context.registerBean("transactionManager", PlatformTransactionManager.class, () -> new DataSourceTransactionManager(db));
        context.registerBean("sqlSessionFactory", SqlSessionFactory.class, () -> {
            try {
                SqlSessionFactoryBean bean = new SqlSessionFactoryBean();
                bean.setDataSource(db);
                bean.setTypeAliasesPackage("com.ktg.mes.dv.domain");
                bean.setMapperLocations(new PathMatchingResourcePatternResolver().getResources("classpath:mapper/dv/DvRepair{,Line}Mapper.xml"));
                return bean.getObject();
            } catch (Exception e) { throw new IllegalStateException(e); }
        });
        context.registerBean("sqlSessionTemplate", SqlSessionTemplate.class, () -> new SqlSessionTemplate(context.getBean(SqlSessionFactory.class)));
        context.registerBean(DvRepairMapper.class, () -> context.getBean(SqlSessionTemplate.class).getMapper(DvRepairMapper.class));
        context.registerBean(DvRepairLineMapper.class, () -> context.getBean(SqlSessionTemplate.class).getMapper(DvRepairLineMapper.class));
        context.registerBean("repairService", DvRepairServiceImpl.class, () -> {
            DvRepairServiceImpl service = new DvRepairServiceImpl();
            ReflectionTestUtils.setField(service, "dvRepairMapper", context.getBean(DvRepairMapper.class));
            ReflectionTestUtils.setField(service, "dvRepairLineMapper", context.getBean(DvRepairLineMapper.class));
            return service;
        });
        context.registerBean("lineService", DvRepairLineServiceImpl.class, () -> {
            DvRepairLineServiceImpl service = new DvRepairLineServiceImpl();
            ReflectionTestUtils.setField(service, "dvRepairMapper", context.getBean(DvRepairMapper.class));
            ReflectionTestUtils.setField(service, "dvRepairLineMapper", context.getBean(DvRepairLineMapper.class));
            return service;
        });
        context.registerBean("configuration", TestTransactions.class);
        context.refresh();
        jdbc = new JdbcTemplate(db);
        createTables();
        repairs = context.getBean("repairService", DvRepairServiceImpl.class);
        lines = context.getBean("lineService", DvRepairLineServiceImpl.class);
        txManager = context.getBean(PlatformTransactionManager.class);
    }

    @AfterEach
    void tearDown() {
        if (context != null) context.close();
        if (db != null) db.shutdown();
    }

    @Test
    void newRepairMustBeDraftAndIgnoresCallerId() {
        DvRepair request = new DvRepair();
        request.setRepairId(999L);
        request.setRepairCode("NEW");
        request.setStatus("FINISHED");
        assertThrows(ServiceException.class, () -> repairs.insertDvRepair(request));
        assertEquals(0, count("dv_repair"));

        request.setStatus(null);
        assertEquals(1, repairs.insertDvRepair(request));
        assertNotEquals(Long.valueOf(999L), request.getRepairId());
        assertEquals("PREPARE", jdbc.queryForObject("select status from dv_repair where repair_code='NEW'", String.class));
    }

    @Test
    void finishedRepairRejectsEditAndDeleteWithoutChangingRows() {
        repair(1, "FINISHED");
        line(11, 1);
        DvRepair edit = new DvRepair();
        edit.setRepairId(1L);
        edit.setRepairName("changed");
        assertThrows(ServiceException.class, () -> repairs.updateDvRepair(edit));
        assertThrows(ServiceException.class, () -> repairs.deleteDvRepairByRepairId(1L));
        assertEquals("repair 1", jdbc.queryForObject("select repair_name from dv_repair where repair_id=1", String.class));
        assertEquals(1, count("dv_repair_line"));
    }

    @Test
    void lineWritesRequireExistingDraftParentAndCannotReassign() {
        repair(1, "FINISHED");
        repair(2, "PREPARE");
        line(11, 1);
        line(12, 2);

        DvRepairLine add = new DvRepairLine();
        add.setRepairId(1L);
        add.setSubjectId(100L);
        assertThrows(ServiceException.class, () -> lines.insertDvRepairLine(add));
        add.setRepairId(999L);
        assertThrows(ServiceException.class, () -> lines.insertDvRepairLine(add));
        add.setRepairId(2L);
        add.setLineId(999L);
        assertEquals(1, lines.insertDvRepairLine(add));
        assertNotEquals(Long.valueOf(999L), add.getLineId());

        DvRepairLine edit = new DvRepairLine();
        edit.setLineId(11L);
        edit.setMalfunction("changed");
        assertThrows(ServiceException.class, () -> lines.updateDvRepairLine(edit));
        assertThrows(ServiceException.class, () -> lines.deleteDvRepairLineByLineId(11L));

        edit.setLineId(12L);
        edit.setRepairId(1L);
        assertThrows(ServiceException.class, () -> lines.updateDvRepairLine(edit));
        assertEquals(Long.valueOf(2L), jdbc.queryForObject("select repair_id from dv_repair_line where line_id=12", Long.class));
        assertEquals("original", jdbc.queryForObject("select malfunction from dv_repair_line where line_id=12", String.class));
    }

    @Test
    void directDeleteByRepairIdAlsoRequiresDraftParent() {
        repair(1, "FINISHED");
        repair(2, "PREPARE");
        line(11, 1);
        line(12, 2);
        assertThrows(ServiceException.class, () -> lines.deleteByRepairId(1L));
        assertThrows(ServiceException.class, () -> lines.deleteByRepairId(999L));
        assertEquals(2, count("dv_repair_line"));
        assertEquals(1, lines.deleteByRepairId(2L));
        assertEquals(Long.valueOf(1L), jdbc.queryForObject("select repair_id from dv_repair_line", Long.class));
    }

    @Test
    void parentDeleteCascadesLinesAndDeduplicatesIds() {
        repair(1, "PREPARE");
        repair(2, "PREPARE");
        line(11, 1);
        line(12, 1);
        line(21, 2);
        assertEquals(1, repairs.deleteDvRepairByRepairIds(new Long[] {1L, 1L}));
        assertEquals(1, count("dv_repair"));
        assertEquals(1, count("dv_repair_line"));
        assertEquals(Long.valueOf(2L), jdbc.queryForObject("select repair_id from dv_repair_line", Long.class));
    }

    @Test
    void parentDeleteRollsBackLineDeletionWhenHeaderDeleteFails() {
        repair(1, "PREPARE");
        line(11, 1);
        jdbc.update("insert into dv_repair_delete_guard(repair_id) values (1)");
        assertThrows(RuntimeException.class, () -> repairs.deleteDvRepairByRepairId(1L));
        assertEquals(1, count("dv_repair"));
        assertEquals(1, count("dv_repair_line"));
    }

    @Test
    void mixedStatusAndMissingIdsFailBeforeBatchDeletion() {
        repair(1, "PREPARE");
        repair(2, "FINISHED");
        line(11, 1);
        line(21, 2);
        assertThrows(ServiceException.class, () -> repairs.deleteDvRepairByRepairIds(new Long[] {1L, 2L}));
        assertThrows(ServiceException.class, () -> repairs.deleteDvRepairByRepairIds(new Long[] {1L, 999L}));
        assertThrows(ServiceException.class, () -> lines.deleteDvRepairLineByLineIds(new Long[] {11L, 21L}));
        assertThrows(ServiceException.class, () -> lines.deleteDvRepairLineByLineIds(new Long[] {11L, 999L}));
        assertEquals(2, count("dv_repair"));
        assertEquals(2, count("dv_repair_line"));
    }

    @Test
    void lineInsertWaitsForParentLockAndRejectsNewStatus() throws Exception {
        repair(1, "PREPARE");
        CountDownLatch statusChanged = new CountDownLatch(1);
        CountDownLatch releaseStatus = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<?> change = pool.submit(() -> new TransactionTemplate(txManager).execute(status -> {
                jdbc.update("update dv_repair set status='FINISHED' where repair_id=1");
                statusChanged.countDown();
                await(releaseStatus);
                return null;
            }));
            assertTrue(statusChanged.await(3, TimeUnit.SECONDS));
            Future<Boolean> insert = pool.submit(() -> {
                DvRepairLine request = new DvRepairLine();
                request.setRepairId(1L);
                request.setSubjectId(100L);
                try { lines.insertDvRepairLine(request); return true; }
                catch (ServiceException expected) { return false; }
            });
            Thread.sleep(200);
            assertFalse(insert.isDone(), "line insert should wait for the parent row lock");
            releaseStatus.countDown();
            change.get(3, TimeUnit.SECONDS);
            assertFalse(insert.get(3, TimeUnit.SECONDS));
            assertEquals("FINISHED", jdbc.queryForObject("select status from dv_repair where repair_id=1", String.class));
            assertEquals(0, count("dv_repair_line"));
        } finally {
            releaseStatus.countDown();
            pool.shutdownNow();
        }
    }

    private void repair(long id, String status) {
        jdbc.update("insert into dv_repair(repair_id,repair_code,repair_name,status) values (?,?,?,?)",
                id, "REPAIR-" + id, "repair " + id, status);
    }

    private void line(long id, long repairId) {
        jdbc.update("insert into dv_repair_line(line_id,repair_id,subject_id,malfunction) values (?,?,?,?)",
                id, repairId, 100, "original");
    }

    private int count(String table) {
        return jdbc.queryForObject("select count(*) from " + table, Integer.class);
    }

    private void createTables() {
        jdbc.execute("create table dv_repair(repair_id bigint generated by default as identity primary key,repair_code varchar(64),repair_name varchar(100),machinery_id bigint,machinery_code varchar(64),machinery_name varchar(100),machinery_brand varchar(100),machinery_spec varchar(100),machinery_type_id bigint,require_date date,finish_date date,confirm_date date,repair_result varchar(20),accepted_by varchar(64),confirm_by varchar(64),status varchar(20),remark varchar(255),attr1 varchar(255),attr2 varchar(255),attr3 bigint,attr4 bigint,create_by varchar(64),create_time timestamp,update_by varchar(64),update_time timestamp)");
        jdbc.execute("create table dv_repair_line(line_id bigint generated by default as identity primary key,repair_id bigint references dv_repair(repair_id),subject_id bigint,subject_code varchar(64),subject_name varchar(100),subject_type varchar(20),subject_content varchar(255),subject_standard varchar(255),malfunction varchar(255),malfunction_url varchar(255),repair_des varchar(255),remark varchar(255),attr1 varchar(255),attr2 varchar(255),attr3 bigint,attr4 bigint,create_by varchar(64),create_time timestamp,update_by varchar(64),update_time timestamp)");
        jdbc.execute("create table dv_repair_delete_guard(repair_id bigint references dv_repair(repair_id))");
    }

    private static void await(CountDownLatch latch) {
        try { if (!latch.await(3, TimeUnit.SECONDS)) throw new AssertionError("timed out waiting to release transaction"); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IllegalStateException(e); }
    }

    @Configuration
    @EnableTransactionManagement(proxyTargetClass = true)
    static class TestTransactions { }
}
