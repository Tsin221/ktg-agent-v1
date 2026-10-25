package com.ktg.mes.dv.service.impl;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.controller.DvCheckPlanController;
import com.ktg.mes.dv.domain.DvCheckMachinery;
import com.ktg.mes.dv.domain.DvCheckPlan;
import com.ktg.mes.dv.domain.DvCheckSubject;
import com.ktg.mes.dv.mapper.DvCheckMachineryMapper;
import com.ktg.mes.dv.mapper.DvCheckPlanMapper;
import com.ktg.mes.dv.mapper.DvCheckSubjectMapper;
import com.ktg.mes.dv.service.impl.DvCheckMachineryServiceImpl;
import com.ktg.mes.dv.service.impl.DvCheckPlanServiceImpl;
import com.ktg.mes.dv.service.impl.DvCheckSubjectServiceImpl;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.TransactionAwareDataSourceProxy;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabase;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.TransactionTemplate;

import javax.sql.DataSource;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

class DvCheckPlanTransactionIntegrationTest {
    private AnnotationConfigApplicationContext context;
    private EmbeddedDatabase db;
    private JdbcTemplate jdbc;
    private DvCheckPlanServiceImpl plans;
    private DvCheckMachineryServiceImpl machinery;
    private DvCheckSubjectServiceImpl subjects;
    private PlatformTransactionManager txManager;

    @BeforeEach
    void setUp() throws Exception {
        db = new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
        context = new AnnotationConfigApplicationContext();
        context.registerBean("database", EmbeddedDatabase.class, () -> db);
        context.registerBean("transactionManager", PlatformTransactionManager.class, () -> new DataSourceTransactionManager(db));
        context.registerBean("sqlSessionFactory", SqlSessionFactory.class, () -> {
            try {
                SqlSessionFactoryBean bean = new SqlSessionFactoryBean();
                bean.setDataSource(db);
                bean.setTypeAliasesPackage("com.ktg.mes.dv.domain");
                bean.setMapperLocations(new PathMatchingResourcePatternResolver().getResources("classpath:mapper/dv/DvCheck{Plan,Machinery,Subject}Mapper.xml"));
                return bean.getObject();
            } catch (Exception e) { throw new IllegalStateException(e); }
        });
        context.registerBean("sqlSessionTemplate", SqlSessionTemplate.class, () -> new SqlSessionTemplate(context.getBean(SqlSessionFactory.class)));
        context.registerBean(DvCheckPlanMapper.class, () -> context.getBean(SqlSessionTemplate.class).getMapper(DvCheckPlanMapper.class));
        context.registerBean(DvCheckMachineryMapper.class, () -> context.getBean(SqlSessionTemplate.class).getMapper(DvCheckMachineryMapper.class));
        context.registerBean(DvCheckSubjectMapper.class, () -> context.getBean(SqlSessionTemplate.class).getMapper(DvCheckSubjectMapper.class));
        context.registerBean("planService", DvCheckPlanServiceImpl.class, () -> {
            DvCheckPlanServiceImpl service = new DvCheckPlanServiceImpl();
            ReflectionTestUtils.setField(service, "dvCheckPlanMapper", context.getBean(DvCheckPlanMapper.class));
            ReflectionTestUtils.setField(service, "dvCheckMachineryMapper", context.getBean(DvCheckMachineryMapper.class));
            ReflectionTestUtils.setField(service, "dvCheckSubjectMapper", context.getBean(DvCheckSubjectMapper.class));
            return service;
        });
        context.registerBean("machineryService", DvCheckMachineryServiceImpl.class, () -> {
            DvCheckMachineryServiceImpl service = new DvCheckMachineryServiceImpl();
            ReflectionTestUtils.setField(service, "dvCheckMachineryMapper", context.getBean(DvCheckMachineryMapper.class));
            ReflectionTestUtils.setField(service, "dvCheckPlanMapper", context.getBean(DvCheckPlanMapper.class));
            return service;
        });
        context.registerBean("subjectService", DvCheckSubjectServiceImpl.class, () -> {
            DvCheckSubjectServiceImpl service = new DvCheckSubjectServiceImpl();
            ReflectionTestUtils.setField(service, "dvCheckSubjectMapper", context.getBean(DvCheckSubjectMapper.class));
            ReflectionTestUtils.setField(service, "dvCheckPlanMapper", context.getBean(DvCheckPlanMapper.class));
            return service;
        });
        context.registerBean("configuration", TestTransactions.class);
        context.refresh();
        jdbc = new JdbcTemplate(db);
        createTables();
        plans = context.getBean("planService", DvCheckPlanServiceImpl.class);
        machinery = context.getBean("machineryService", DvCheckMachineryServiceImpl.class);
        subjects = context.getBean("subjectService", DvCheckSubjectServiceImpl.class);
        txManager = context.getBean(PlatformTransactionManager.class);
    }

    @AfterEach
    void tearDown() {
        if (context != null) context.close();
        if (db != null) db.shutdown();
    }

    @Test
    void mixedPlanDeleteValidatesAllHeadersBeforeDeletingEitherKindOfLink() {
        plan(1, "PREPARE"); plan(2, "PREPARE"); plan(3, "FINISHED");
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (11,1,100),(13,3,300)");
        jdbc.update("insert into dv_check_subject(record_id,plan_id,subject_id) values (21,1,200),(23,3,300)");
        DvCheckPlanController controller = new DvCheckPlanController();
        ReflectionTestUtils.setField(controller, "dvCheckPlanService", plans);

        assertThrows(ServiceException.class, () -> controller.remove(new Long[] { 1L, 3L }));

        assertEquals(3, jdbc.queryForObject("select count(*) from dv_check_plan", Integer.class));
        assertEquals(2, jdbc.queryForObject("select count(*) from dv_check_machinery", Integer.class));
        assertEquals(2, jdbc.queryForObject("select count(*) from dv_check_subject", Integer.class));
    }

    @Test
    void planDeleteRollsBackBothLinkKindsWhenHeaderDeleteFailsAfterward() {
        plan(1, "PREPARE");
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (11,1,100)");
        jdbc.update("insert into dv_check_subject(record_id,plan_id,subject_id) values (21,1,200)");
        jdbc.update("insert into dv_plan_delete_guard(plan_id) values (1)");

        assertThrows(RuntimeException.class, () -> plans.deleteDvCheckPlanByPlanIds(new Long[] { 1L }));

        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_plan where plan_id=1", Integer.class));
        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_machinery where plan_id=1", Integer.class));
        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_subject where plan_id=1", Integer.class));
    }

    @Test
    void activationCannotRacePastAnAssociationRemoval() throws Exception {
        plan(1, "PREPARE");
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (11,1,100)");
        jdbc.update("insert into dv_check_subject(record_id,plan_id,subject_id) values (21,1,200)");
        CountDownLatch removalHasDeleted = new CountDownLatch(1);
        CountDownLatch commitRemoval = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<?> remove = pool.submit(() -> new TransactionTemplate(txManager).execute(status -> {
                subjects.deleteDvCheckSubjectByRecordId(21L);
                removalHasDeleted.countDown();
                await(commitRemoval);
                return null;
            }));
            assertTrue(removalHasDeleted.await(3, TimeUnit.SECONDS));
            Future<Boolean> activate = pool.submit(() -> {
                DvCheckPlan request = new DvCheckPlan(); request.setPlanId(1L); request.setStatus("FINISHED");
                try { plans.updateDvCheckPlan(request); return true; }
                catch (ServiceException expected) { return false; }
            });
            Thread.sleep(200);
            assertFalse(activate.isDone(), "activation should wait for the plan row lock held by link removal");
            commitRemoval.countDown();
            remove.get(3, TimeUnit.SECONDS);
            assertFalse(activate.get(3, TimeUnit.SECONDS));
            assertEquals("PREPARE", jdbc.queryForObject("select status from dv_check_plan where plan_id=1", String.class));
            assertEquals(0, jdbc.queryForObject("select count(*) from dv_check_subject where plan_id=1", Integer.class));
        } finally {
            commitRemoval.countDown();
            pool.shutdownNow();
        }
    }

    @Test
    void associationWritesRejectFinishedPlansAndReassignment() {
        plan(1, "FINISHED"); plan(2, "PREPARE");
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (11,1,100)");
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (12,2,101)");
        jdbc.update("insert into dv_check_subject(record_id,plan_id,subject_id,subject_name) values (21,1,200,'saved subject')");
        DvCheckMachinery add = new DvCheckMachinery(); add.setPlanId(1L); add.setMachineryId(101L);
        assertThrows(ServiceException.class, () -> machinery.insertDvCheckMachinery(add));
        DvCheckMachinery reassignment = new DvCheckMachinery(); reassignment.setRecordId(11L); reassignment.setPlanId(2L);
        assertThrows(ServiceException.class, () -> machinery.updateDvCheckMachinery(reassignment));
        DvCheckMachinery editFinishedMachinery = new DvCheckMachinery(); editFinishedMachinery.setRecordId(11L); editFinishedMachinery.setMachineryName("edited");
        assertThrows(ServiceException.class, () -> machinery.updateDvCheckMachinery(editFinishedMachinery));
        assertThrows(ServiceException.class, () -> machinery.deleteDvCheckMachineryByRecordId(11L));

        DvCheckSubject addSubject = new DvCheckSubject(); addSubject.setPlanId(1L); addSubject.setSubjectId(201L);
        assertThrows(ServiceException.class, () -> subjects.insertDvCheckSubject(addSubject));
        DvCheckSubject editFinishedSubject = new DvCheckSubject(); editFinishedSubject.setRecordId(21L); editFinishedSubject.setSubjectName("edited");
        assertThrows(ServiceException.class, () -> subjects.updateDvCheckSubject(editFinishedSubject));
        assertThrows(ServiceException.class, () -> subjects.deleteDvCheckSubjectByRecordId(21L));

        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_machinery where plan_id=1", Integer.class));
        assertEquals("saved subject", jdbc.queryForObject("select subject_name from dv_check_subject where plan_id=1", String.class));
    }

    @Test
    void missingIdsFailWithoutChangesAndDuplicatePlanIdsCollapseToOneDelete() {
        plan(1, "PREPARE");
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (11,1,100)");
        jdbc.update("insert into dv_check_subject(record_id,plan_id,subject_id) values (21,1,200)");
        assertThrows(ServiceException.class, () -> plans.deleteDvCheckPlanByPlanIds(new Long[] { 1L, 999L }));
        assertThrows(ServiceException.class, () -> machinery.deleteDvCheckMachineryByRecordIds(new Long[] { 11L, 999L }));
        assertThrows(ServiceException.class, () -> subjects.deleteDvCheckSubjectByRecordIds(new Long[] { 21L, 999L }));
        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_plan", Integer.class));
        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_machinery", Integer.class));
        assertEquals(1, jdbc.queryForObject("select count(*) from dv_check_subject", Integer.class));

        assertEquals(1, plans.deleteDvCheckPlanByPlanIds(new Long[] { 1L, 1L }));
        assertEquals(0, jdbc.queryForObject("select count(*) from dv_check_plan", Integer.class));
    }

    @Test
    void newPlanCannotStartFinishedOrUseCallerSuppliedId() {
        DvCheckPlan invalid = new DvCheckPlan(); invalid.setPlanId(999L); invalid.setPlanCode("NEW");
        invalid.setPlanType("CHECK"); invalid.setStatus("FINISHED");
        assertThrows(ServiceException.class, () -> plans.insertDvCheckPlan(invalid));
        assertEquals(0, jdbc.queryForObject("select count(*) from dv_check_plan", Integer.class));

        invalid.setStatus("PREPARE");
        assertEquals(1, plans.insertDvCheckPlan(invalid));
        assertNotEquals(Long.valueOf(999L), jdbc.queryForObject("select plan_id from dv_check_plan where plan_code='NEW'", Long.class));
        assertEquals("PREPARE", jdbc.queryForObject("select status from dv_check_plan where plan_code='NEW'", String.class));
    }

    @Test
    void finishedPlanAcceptsOnlyStatusOnlyDeactivationIncludingFullUnchangedFormPayload() {
        plan(1, "FINISHED");
        DvCheckPlan stop = new DvCheckPlan(); stop.setPlanId(1L); stop.setPlanCode("PLAN-1");
        stop.setPlanName("plan 1"); stop.setPlanType("CHECK"); stop.setCycleType("DAY");
        stop.setCycleCount(1L); stop.setStatus("PREPARE"); stop.setRemark("");
        assertEquals(1, plans.updateDvCheckPlan(stop));
        assertEquals("PREPARE", jdbc.queryForObject("select status from dv_check_plan where plan_id=1", String.class));

        jdbc.update("update dv_check_plan set status='FINISHED' where plan_id=1");
        stop.setPlanName("changed name");
        assertThrows(ServiceException.class, () -> plans.updateDvCheckPlan(stop));
        assertEquals("FINISHED", jdbc.queryForObject("select status from dv_check_plan where plan_id=1", String.class));
    }

    @Test
    void activationRequiresBothLinkKindsAndEmptyDraftCanStillChangeType() {
        plan(1, "PREPARE");
        DvCheckPlan typeChange = new DvCheckPlan(); typeChange.setPlanId(1L); typeChange.setPlanType("MAINTEN");
        assertEquals(1, plans.updateDvCheckPlan(typeChange));
        jdbc.update("update dv_check_plan set plan_type='CHECK' where plan_id=1");
        DvCheckPlan activate = new DvCheckPlan(); activate.setPlanId(1L); activate.setStatus("FINISHED");
        assertThrows(ServiceException.class, () -> plans.updateDvCheckPlan(activate));
        jdbc.update("insert into dv_check_machinery(record_id,plan_id,machinery_id) values (11,1,100)");
        assertThrows(ServiceException.class, () -> plans.updateDvCheckPlan(activate));
        jdbc.update("insert into dv_check_subject(record_id,plan_id,subject_id) values (21,1,200)");
        assertEquals(1, plans.updateDvCheckPlan(activate));
        assertEquals("FINISHED", jdbc.queryForObject("select status from dv_check_plan where plan_id=1", String.class));
    }

    private void plan(long id, String status) {
        jdbc.update("insert into dv_check_plan(plan_id,plan_code,plan_name,plan_type,cycle_type,cycle_count,status) values (?,?,?,?,?,?,?)",
                id, "PLAN-" + id, "plan " + id, "CHECK", "DAY", 1, status);
    }

    private void createTables() {
        jdbc.execute("create table dv_check_plan(plan_id bigint generated by default as identity primary key,plan_code varchar(64),plan_name varchar(100),plan_type varchar(20),start_date date,end_date date,cycle_type varchar(20),cycle_count bigint,status varchar(20),remark varchar(255),attr1 varchar(255),attr2 varchar(255),attr3 bigint,attr4 bigint,create_by varchar(64),create_time timestamp,update_by varchar(64),update_time timestamp)");
        jdbc.execute("create table dv_check_machinery(record_id bigint generated by default as identity primary key,plan_id bigint,machinery_id bigint,machinery_code varchar(64),machinery_name varchar(100),machinery_brand varchar(100),machinery_spec varchar(100),remark varchar(255),attr1 varchar(255),attr2 varchar(255),attr3 bigint,attr4 bigint,create_by varchar(64),create_time timestamp,update_by varchar(64),update_time timestamp)");
        jdbc.execute("create table dv_check_subject(record_id bigint generated by default as identity primary key,plan_id bigint,subject_id bigint,subject_code varchar(64),subject_name varchar(100),subject_type varchar(20),subject_content varchar(255),subject_standard varchar(255),remark varchar(255),attr1 varchar(255),attr2 varchar(255),attr3 bigint,attr4 bigint,create_by varchar(64),create_time timestamp,update_by varchar(64),update_time timestamp)");
        jdbc.execute("create table dv_plan_delete_guard(plan_id bigint references dv_check_plan(plan_id))");
        jdbc.execute("create table dv_subject(subject_id bigint primary key,subject_code varchar(64),subject_name varchar(100),subject_type varchar(20),subject_content varchar(255),subject_standard varchar(255),enable_flag char(1))");
    }

    private static void await(CountDownLatch latch) {
        try { if (!latch.await(3, TimeUnit.SECONDS)) throw new AssertionError("timed out waiting to release transaction"); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IllegalStateException(e); }
    }

    @Configuration
    @EnableTransactionManagement(proxyTargetClass = true)
    static class TestTransactions { }
}
