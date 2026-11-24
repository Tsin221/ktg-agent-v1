package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.mes.pro.domain.ProTask;
import com.ktg.mes.pro.service.impl.ProTaskServiceImpl;
import com.ktg.mes.qc.domain.QcIpqcLine;
import com.ktg.mes.qc.service.impl.QcIpqcLineServiceImpl;
import com.ktg.mes.wm.domain.WmSn;
import com.ktg.mes.wm.service.impl.WmSnServiceImpl;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabase;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Collections;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;

/** Isolated H2: real ownership joins, actual current subject, and legacy AOP boundary. */
class ProExecutionAccessTest {
    private EmbeddedDatabase db;
    private JdbcTemplate jdbc;
    private ExecutionAccessService access;
    private LegacyExecutionGuard guard;

    @BeforeEach void setup() {
        db = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2)
                .setName("mesb01-access-" + UUID.randomUUID()).build();
        jdbc = new JdbcTemplate(db);
        jdbc.execute("create table pro_workorder(workorder_id bigint primary key,create_by varchar(64))");
        jdbc.execute("create table pro_execution_order(workorder_id bigint primary key,release_user_id bigint)");
        jdbc.execute("create table pro_task(task_id bigint primary key,workorder_id bigint)");
        jdbc.execute("create table pro_execution_task(task_id bigint primary key,workorder_id bigint,assigned_user_id bigint,step_kind varchar(32),task_kind varchar(32))");
        jdbc.execute("create table pro_execution_unit(sn_id bigint primary key,workorder_id bigint,current_task_id bigint)");
        jdbc.execute("create table qc_ipqc(ipqc_id bigint primary key,workorder_id bigint,task_id bigint)");
        jdbc.execute("create table qc_ipqc_line(line_id bigint primary key,ipqc_id bigint)");
        jdbc.execute("create table pro_execution_inspection(inspection_id bigint primary key,sn_id bigint,ipqc_id bigint)");
        jdbc.update("insert into pro_workorder values (1,'creator'),(2,'other'),(3,'creator')");
        jdbc.update("insert into pro_execution_order values (1,10),(2,20)");
        jdbc.update("insert into pro_task values (11,1),(12,1),(21,2),(31,3)");
        jdbc.update("insert into pro_execution_task values (11,1,30,'ASSEMBLY','NORMAL'),(12,1,40,'INSPECTION','NORMAL'),(21,2,30,'ASSEMBLY','NORMAL')");
        jdbc.update("insert into pro_execution_unit values (100,1,11),(200,2,21)");
        jdbc.update("insert into qc_ipqc values (101,1,12),(301,3,31)");
        jdbc.update("insert into qc_ipqc_line values (1001,101),(3001,301)");
        jdbc.update("insert into pro_execution_inspection values (1,100,101)");
        ExecutionOwnershipService ownership = new ExecutionOwnershipService(db);
        access = new ExecutionAccessService(ownership, new PermissionService());
        guard = new LegacyExecutionGuard(ownership, new DataSourceTransactionManager(db));
    }

    @AfterEach void teardown() {
        SecurityContextHolder.clearContext();
        db.shutdown();
    }

    @Test void releaseUsesAuthenticatedDraftCreatorAndPermissionNotCallerData() {
        login(10, "creator", "release");
        assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.RELEASE, 3L, null, null));
        login(10, "wrong", "release");
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.RELEASE, 3L, null, null));
    }

    @Test void productOptionsRequireReleaseAndDraftCreationAndProductList() {
        login(10, "creator", "release");
        assertThrows(ExecutionException.class, () -> access.requireProductReleaseOptions());
        org.springframework.security.core.context.SecurityContextHolder.clearContext();
        com.ktg.common.core.domain.entity.SysUser user = new com.ktg.common.core.domain.entity.SysUser();
        user.setUserId(10L); user.setUserName("creator");
        LoginUser actor = new LoginUser(10L, 1L, user, new HashSet<>(Arrays.asList(
                "mes:pro:execution:release", "mes:pro:workorder:add", "mes:md:mditem:list")));
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(actor, null, Collections.emptyList()));
        assertDoesNotThrow(() -> access.requireProductReleaseOptions());
        actor.setPermissions(new HashSet<>(Arrays.asList("mes:pro:workorder:add", "mes:md:mditem:list")));
        assertThrows(ExecutionException.class, () -> access.requireProductReleaseOptions());
    }

    @Test void workerNeedsAssignedTaskAndMatchingWorkorderAndUnit() {
        login(30, "producer", "report");
        assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.REPORT, 1L, 11L, 100L));
        assertDoesNotThrow(() -> access.requireTaskUnits(ExecutionAction.REPORT, 1L, 11L, new java.util.TreeSet<>(Arrays.asList(100L))));
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.REPORT, 1L, 11L, 200L));
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.REPORT, 1L, 12L, 100L));
        assertThrows(ExecutionException.class,
                () -> access.requireTaskUnits(ExecutionAction.REPORT, 1L, 11L, Arrays.asList(100L, 200L)));
        assertThrows(ExecutionException.class,
                () -> access.requireTaskUnits(ExecutionAction.REPORT, 1L, 11L, Arrays.asList(100L, 100L)));
        login(40, "quality", "report", "inspect");
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.REPORT, 1L, 12L, 100L));
    }

    @Test void receiptDoesNotRequireOldUnitStateButDoesRequireCurrentPermissions() {
        jdbc.update("update pro_execution_unit set current_task_id=12 where sn_id=100");
        login(30, "producer", "query", "report");
        assertDoesNotThrow(() -> access.requireReceipt(ExecutionAction.REPORT, 1L, 11L, 100L));
        login(30, "producer", "report");
        assertThrows(ExecutionException.class,
                () -> access.requireReceipt(ExecutionAction.REPORT, 1L, 11L, 100L));
        jdbc.update("update pro_execution_task set assigned_user_id=40 where task_id=11");
        login(30, "producer", "query", "report");
        assertThrows(ExecutionException.class,
                () -> access.requireReceipt(ExecutionAction.REPORT, 1L, 11L, 100L));
    }

    @Test void coordinatorRequiresReleaseOwnershipOrExplicitGlobalScope() {
        login(10, "creator", "assign");
        assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.ASSIGN, 1L, 12L, null));
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.ASSIGN, 2L, 21L, null));
        login(10, "creator", "assign", "manageAll");
        assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.ASSIGN, 2L, 21L, null));
        login(10, "creator", "manageAll");
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.ASSIGN, 2L, 21L, null));
    }

    @Test void qualityRequiresInspectionPermissionAndCurrentUnit() {
        jdbc.update("update pro_execution_unit set current_task_id=12 where sn_id=100");
        login(40, "quality", "inspect");
        assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.INSPECT_ACCEPT, 1L, 12L, 100L));
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.INSPECT_ACCEPT, 1L, 12L, null));
        login(40, "quality", "start");
        assertThrows(ExecutionException.class,
                () -> access.requireOperation(ExecutionAction.START, 1L, 12L, null));
        login(40, "quality", "start", "inspect");
        assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.START, 1L, 12L, null));
    }

    @Test void queryAndTraceRequireObjectScope() {
        login(30, "producer", "query");
        assertDoesNotThrow(() -> access.requireOrderQuery(1L));
        assertDoesNotThrow(() -> access.requireTaskQuery(1L, 11L));
        assertThrows(ExecutionException.class, () -> access.requireTaskQuery(1L, 12L));
        assertThrows(ExecutionException.class, () -> access.requireTaskQuery(2L, 11L));
        assertThrows(ExecutionException.class, () -> access.requireTrace(1L, 100L));
        login(30, "producer", "query", "trace");
        assertDoesNotThrow(() -> access.requireTrace(1L, 100L));
        assertThrows(ExecutionException.class, () -> access.requireTrace(1L, 200L));
    }

    @Test void boundSnAndQcLineAreProtectedThroughDirectOldServices() {
        WmSnServiceImpl legacySn = proxy(new WmSnServiceImpl() {
            @Override public int updateWmSn(WmSn sn) { return 1; }
        });
        WmSn bound = new WmSn(); bound.setSnId(100L);
        ExecutionException error = assertThrows(ExecutionException.class, () -> legacySn.updateWmSn(bound));
        assertEquals("LEGACY_WRITE_PROTECTED", error.getErrorCode());
        WmSn old = new WmSn(); old.setSnId(900L);
        assertEquals(1, legacySn.updateWmSn(old));

        QcIpqcLineServiceImpl legacyQuality = proxy(new QcIpqcLineServiceImpl() {
            @Override public int updateQcIpqcLine(QcIpqcLine line) { return 1; }
            @Override public int deleteByIpqcId(Long id) { return 1; }
        });
        QcIpqcLine detach = new QcIpqcLine(); detach.setLineId(1001L); detach.setIpqcId(301L);
        assertThrows(ExecutionException.class, () -> legacyQuality.updateQcIpqcLine(detach));
        QcIpqcLine reparent = new QcIpqcLine(); reparent.setLineId(3001L); reparent.setIpqcId(101L);
        assertThrows(ExecutionException.class, () -> legacyQuality.updateQcIpqcLine(reparent));
        assertThrows(ExecutionException.class, () -> legacyQuality.deleteByIpqcId(101L));
        assertEquals(1, legacyQuality.deleteByIpqcId(301L));
    }

    @Test void oldServiceProtectsBothExistingAndProposedOwnerAndAllBatchItems() {
        // The real target will fail if reached without a mapper, so use a minimal test target.
        ProTaskServiceImpl target = new ProTaskServiceImpl() {
            @Override public int updateProTask(ProTask task) { return 1; }
            @Override public int deleteProTaskByTaskIds(Long[] ids) { return ids.length; }
        };
        ProTaskServiceImpl legacy = proxy(target);
        ProTask historic = new ProTask(); historic.setTaskId(31L); historic.setWorkorderId(3L);
        assertEquals(1, legacy.updateProTask(historic));
        ProTask reparent = new ProTask(); reparent.setTaskId(31L); reparent.setWorkorderId(1L);
        assertThrows(ExecutionException.class, () -> legacy.updateProTask(reparent));
        ProTask detach = new ProTask(); detach.setTaskId(11L); detach.setWorkorderId(3L);
        assertThrows(ExecutionException.class, () -> legacy.updateProTask(detach));
        assertThrows(ExecutionException.class, () -> legacy.deleteProTaskByTaskIds(new Long[]{31L,11L}));
        assertEquals(1, legacy.deleteProTaskByTaskIds(new Long[]{31L}));
    }

    @Test void oldServiceRetainsLegacyBehaviorWhenExecutionSchemaNotInstalled() {
        jdbc.execute("drop table pro_execution_order");
        ProTaskServiceImpl legacy = proxy(new ProTaskServiceImpl() {
            @Override public int updateProTask(ProTask task) { return 1; }
        });
        ProTask historic = new ProTask(); historic.setTaskId(31L); historic.setWorkorderId(3L);
        assertEquals(1, legacy.updateProTask(historic));
    }

    private <T> T proxy(T target) {
        AspectJProxyFactory factory = new AspectJProxyFactory(target);
        factory.setProxyTargetClass(true);
        factory.addAspect(guard);
        return factory.getProxy();
    }

    private void login(long id, String name, String... permissions) {
        SysUser sysUser = new SysUser(); sysUser.setUserName(name);
        HashSet<String> p = new HashSet<>();
        Arrays.stream(permissions).forEach(s -> p.add("mes:pro:execution:" + s));
        LoginUser login = new LoginUser(id, 1L, sysUser, p);
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(login, null, Collections.emptyList()));
    }
}
