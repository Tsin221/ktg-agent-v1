package com.ktg.mes.pro.execution;

import com.ktg.mes.pro.controller.ProTaskMobController;
import com.ktg.mes.pro.domain.*;
import com.ktg.mes.pro.mapper.*;
import com.ktg.mes.pro.service.impl.*;
import com.ktg.mes.wm.domain.WmSn;
import com.ktg.mes.wm.mapper.WmSnMapper;
import com.ktg.mes.wm.service.impl.WmSnServiceImpl;
import org.junit.jupiter.api.*;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.*;
import org.springframework.test.util.ReflectionTestUtils;
import java.util.UUID;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** B02 legacy entry points: actual services, durable ownership and no mapper mutation on refusal. */
class ProExecutionLegacyOrderGuardTest {
    EmbeddedDatabase db;
    JdbcTemplate jdbc;
    LegacyExecutionGuard guard;
    ProTaskMapper taskMapper;
    ProWorkorderMapper orderMapper;
    WmSnMapper snMapper;
    ProTaskServiceImpl tasks;
    ProWorkorderServiceImpl orders;
    WmSnServiceImpl sns;

    @BeforeEach void setup() {
        db = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2).setName("b02-legacy-"+UUID.randomUUID()).build();
        jdbc = new JdbcTemplate(db);
        jdbc.execute("create table pro_workorder(workorder_id bigint primary key,workorder_code varchar(64))");
        jdbc.execute("create table pro_execution_order(workorder_id bigint primary key)");
        jdbc.execute("create table pro_task(task_id bigint primary key,workorder_id bigint)");
        jdbc.execute("create table pro_execution_task(task_id bigint primary key,workorder_id bigint)");
        jdbc.execute("create table pro_execution_unit(sn_id bigint primary key,workorder_id bigint)");
        jdbc.update("insert into pro_workorder values(1,'BOUND'),(2,'OLD')");
        jdbc.update("insert into pro_execution_order values(1)");
        jdbc.update("insert into pro_task values(11,1),(22,2)");
        jdbc.update("insert into pro_execution_task values(11,1)");
        jdbc.update("insert into pro_execution_unit values(111,1)");
        guard = new LegacyExecutionGuard(new ExecutionOwnershipService(db), new DataSourceTransactionManager(db));
        taskMapper = mock(ProTaskMapper.class); orderMapper = mock(ProWorkorderMapper.class); snMapper = mock(WmSnMapper.class);
        ProTaskServiceImpl taskTarget = new ProTaskServiceImpl();
        ReflectionTestUtils.setField(taskTarget,"proTaskMapper",taskMapper); tasks=proxy(taskTarget);
        ProWorkorderServiceImpl orderTarget = new ProWorkorderServiceImpl();
        ReflectionTestUtils.setField(orderTarget,"proWorkorderMapper",orderMapper); orders=proxy(orderTarget);
        WmSnServiceImpl snTarget = new WmSnServiceImpl();
        ReflectionTestUtils.setField(snTarget,"wmSnMapper",snMapper); sns=proxy(snTarget);
    }
    @AfterEach void teardown() { db.shutdown(); }

    @Test void boundOrderCannotEditDraftDeleteOrResetStatus() {
        ProWorkorder d = new ProWorkorder(); d.setWorkorderId(1L); d.setStatus("PREPARE"); d.setVersion(0L);
        refused(() -> orders.updateProWorkorder(d));
        refused(() -> orders.updateDraftWorkorder(d));
        refused(() -> orders.deleteProWorkorderByWorkorderId(1L));
        refused(() -> orders.deleteProWorkorderByWorkorderIds(new Long[]{2L,1L}));
        verifyZeroInteractions(orderMapper);
    }
    @Test void tasksCannotBeCreatedInMovedIntoOrDetachedFromBoundOrder() {
        ProTask d = new ProTask(); d.setWorkorderId(1L);
        refused(() -> tasks.insertProTask(d));
        d.setTaskId(22L); refused(() -> tasks.updateProTask(d));
        d.setTaskId(11L); d.setWorkorderId(2L); refused(() -> tasks.updateProTask(d));
        d.setWorkorderId(null); d.setWorkorderCode("BOUND"); refused(() -> tasks.insertProTask(d));
        refused(() -> tasks.deleteProTaskByTaskId(11L));
        refused(() -> tasks.deleteProTaskByTaskIds(new Long[]{22L,11L}));
        verifyZeroInteractions(taskMapper);
    }
    @Test void boundSnIdentityAndWholeDeleteBatchAreProtected() {
        WmSn d = new WmSn(); d.setSnId(111L); d.setSnCode("NEW");
        refused(() -> sns.updateWmSn(d));
        refused(() -> sns.deleteWmSnBySnId(111L));
        refused(() -> sns.deleteWmSnBySnIds(new Long[]{222L,111L}));
        verifyZeroInteractions(snMapper);
    }
    @Test void mobileStatusAndFeedbackCannotMutateBoundTask() {
        ProTaskMobController mobile = new ProTaskMobController();
        ReflectionTestUtils.setField(mobile,"proTaskService",tasks);
        ProTask d = new ProTask(); d.setTaskId(11L); d.setStatus("FINISHED");
        refused(() -> mobile.changeStatus(d));
        ProTaskMobController protectedMobile = proxy(mobile);
        ProFeedback f = new ProFeedback(); f.setTaskId(11L);
        refused(() -> protectedMobile.feedBack(f));
        verifyZeroInteractions(taskMapper);
    }
    @Test void historicalWritesAndReadsStillReachOriginalMappers() {
        ProTask d = new ProTask(); d.setTaskId(22L); d.setWorkorderId(2L);
        when(taskMapper.updateProTask(d)).thenReturn(1);
        assertEquals(1,tasks.updateProTask(d));
        tasks.deleteProTaskByTaskIds(new Long[]{22L}); tasks.selectProTaskByTaskId(11L);
        WmSn sn = new WmSn(); sn.setSnId(222L);
        sns.updateWmSn(sn); sns.deleteWmSnBySnIds(new Long[]{222L}); sns.selectWmSnBySnId(111L);
        ProWorkorder order = new ProWorkorder(); order.setWorkorderId(2L);
        orders.updateProWorkorder(order); orders.selectProWorkorderByWorkorderId(1L);
        verify(taskMapper).updateProTask(d); verify(taskMapper).selectProTaskByTaskId(11L);
        verify(snMapper).updateWmSn(sn); verify(snMapper).selectWmSnBySnId(111L);
        verify(orderMapper).updateProWorkorder(order); verify(orderMapper).selectProWorkorderByWorkorderId(1L);
    }
    private void refused(Runnable action) {
        ExecutionException ex = assertThrows(ExecutionException.class, action::run);
        assertEquals(409,ex.getCode()); assertEquals("LEGACY_WRITE_PROTECTED",ex.getErrorCode());
    }
    private <T> T proxy(T target) {
        AspectJProxyFactory factory = new AspectJProxyFactory(target); factory.setProxyTargetClass(true); factory.addAspect(guard); return factory.getProxy();
    }
}
