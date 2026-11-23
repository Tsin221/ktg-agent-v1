package com.ktg.mes.pro.execution;

import com.ktg.mes.pro.controller.ProFeedbackController;
import com.ktg.mes.pro.controller.ProFeedBackMobController;
import com.ktg.mes.pro.controller.ProTaskMobController;
import com.ktg.mes.pro.domain.ProFeedback;
import com.ktg.mes.pro.domain.ProTask;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.mapper.ProFeedbackMapper;
import com.ktg.mes.pro.service.IProRouteProcessService;
import com.ktg.mes.pro.service.IProTaskService;
import com.ktg.mes.pro.service.IProWorkorderService;
import com.ktg.mes.pro.service.impl.ProFeedbackServiceImpl;
import com.ktg.mes.wm.domain.WmItemConsume;
import com.ktg.mes.wm.domain.WmItemConsumeLine;
import com.ktg.mes.wm.mapper.WmItemConsumeMapper;
import com.ktg.mes.wm.mapper.WmItemConsumeLineMapper;
import com.ktg.mes.wm.service.IWmItemConsumeService;
import com.ktg.mes.wm.service.impl.WmItemConsumeServiceImpl;
import com.ktg.mes.wm.service.impl.WmItemConsumeLineServiceImpl;
import org.junit.jupiter.api.*;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.*;
import org.springframework.test.util.ReflectionTestUtils;

import java.math.BigDecimal;
import java.util.UUID;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Verifies B03-generated feedback and all related consumption data cannot re-enter legacy writes. */
class ProExecutionLegacyReportGuardTest {
    EmbeddedDatabase source;
    LegacyExecutionGuard guard;
    ProFeedbackMapper feedbackMapper;
    WmItemConsumeMapper consumeMapper;
    WmItemConsumeLineMapper consumeLineMapper;
    ProFeedbackServiceImpl feedbacks;
    WmItemConsumeServiceImpl consumes;
    WmItemConsumeLineServiceImpl lines;

    @BeforeEach void setup() {
        source = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2).setName("b03-legacy-"+UUID.randomUUID()).build();
        JdbcTemplate db = new JdbcTemplate(source);
        db.execute("create table pro_workorder(workorder_id bigint primary key,workorder_code varchar(64))");
        db.execute("create table pro_execution_order(workorder_id bigint primary key)");
        db.execute("create table pro_task(task_id bigint primary key,workorder_id bigint)");
        db.execute("create table pro_execution_task(task_id bigint primary key,workorder_id bigint)");
        db.execute("create table pro_feedback(record_id bigint primary key,workorder_id bigint,task_id bigint)");
        db.execute("create table pro_execution_unit(sn_id bigint primary key,workorder_id bigint,assembly_feedback_id bigint)");
        db.execute("create table pro_execution_input(sn_id bigint,feedback_id bigint,consume_line_id bigint,material_stock_id bigint)");
        db.execute("create table wm_item_consume(record_id bigint primary key,workorder_id bigint,task_id bigint)");
        db.execute("create table wm_item_consume_line(line_id bigint primary key,record_id bigint,material_stock_id bigint)");
        db.execute("create table wm_material_stock(material_stock_id bigint primary key,workorder_id bigint,workorder_code varchar(64))");
        db.update("insert into pro_workorder values(1,'NEW'),(2,'OLD')");
        db.update("insert into pro_execution_order values(1)");
        db.update("insert into pro_task values(11,1),(22,2)");
        db.update("insert into pro_execution_task values(11,1)");
        db.update("insert into pro_feedback values(111,1,11),(222,2,22)");
        db.update("insert into pro_execution_unit values(1111,1,111)");
        db.update("insert into wm_material_stock values(1,1,'NEW'),(2,2,'OLD')");
        db.update("insert into wm_item_consume values(11111,1,11),(22222,2,22)");
        db.update("insert into wm_item_consume_line values(111111,11111,1),(222222,22222,2)");
        db.update("insert into pro_execution_input values(1111,111,111111,1)");
        guard = new LegacyExecutionGuard(new ExecutionOwnershipService(source), new DataSourceTransactionManager(source));
        feedbackMapper=mock(ProFeedbackMapper.class); consumeMapper=mock(WmItemConsumeMapper.class); consumeLineMapper=mock(WmItemConsumeLineMapper.class);
        ProFeedbackServiceImpl f=new ProFeedbackServiceImpl(); ReflectionTestUtils.setField(f,"proFeedbackMapper",feedbackMapper);feedbacks=proxy(f);
        WmItemConsumeServiceImpl c=new WmItemConsumeServiceImpl(); ReflectionTestUtils.setField(c,"wmItemConsumeMapper",consumeMapper);consumes=proxy(c);
        WmItemConsumeLineServiceImpl l=new WmItemConsumeLineServiceImpl(); ReflectionTestUtils.setField(l,"wmItemConsumeLineMapper",consumeLineMapper);lines=proxy(l);
    }
    @AfterEach void cleanup() { source.shutdown(); }

    @Test void directApprovalMobileEditAndMixedDeleteRejectBeforeAnyBusinessMutation() {
        ProFeedbackController approval=proxy(new ProFeedbackController());
        refused(()->approval.execute(111L)); // no dependencies: guard must intercept before old execute
        ProFeedBackMobController mobile=new ProFeedBackMobController(); ReflectionTestUtils.setField(mobile,"proFeedbackService",feedbacks);
        ProFeedback f=new ProFeedback();f.setRecordId(111L);f.setWorkorderId(2L);f.setTaskId(22L);
        refused(()->mobile.edit(f));
        refused(()->mobile.remove(new Long[]{222L,111L}));
        f.setRecordId(222L); f.setWorkorderId(1L);
        refused(()->feedbacks.updateProFeedback(f));
        ProFeedback newFeedback=new ProFeedback();newFeedback.setTaskId(11L);
        refused(()->feedbacks.insertProFeedback(newFeedback));
        ProTaskMobController legacyTask=proxy(new ProTaskMobController());
        refused(()->legacyTask.feedBack(newFeedback));
        verifyNoInteractions(feedbackMapper);
    }

    @Test void protectedConsumeHeaderAndLineCannotBeEditedDeletedOrRegenerated() {
        WmItemConsume c=new WmItemConsume();c.setRecordId(11111L);c.setWorkorderId(2L);c.setTaskId(22L);
        refused(()->consumes.updateWmItemConsume(c)); refused(()->consumes.deleteWmItemConsumeByRecordIds(new Long[]{22222L,11111L}));
        ProFeedback f=new ProFeedback(); f.setRecordId(111L);
        refused(()->consumes.generateItemConsume(f));
        WmItemConsumeLine line=new WmItemConsumeLine();line.setLineId(111111L);line.setRecordId(22222L);line.setMaterialStockId(2L);
        refused(()->lines.updateWmItemConsumeLine(line)); refused(()->lines.deleteWmItemConsumeLineByLineIds(new Long[]{222222L,111111L}));
        line.setLineId(222222L);line.setRecordId(11111L);refused(()->lines.updateWmItemConsumeLine(line));
        verifyNoInteractions(consumeMapper,consumeLineMapper);
    }

    @Test void oldWritesAndBoundReadsRemainAvailable() {
        ProFeedback f=new ProFeedback();f.setRecordId(222L);f.setTaskId(22L);f.setWorkorderId(2L);
        feedbacks.updateProFeedback(f);feedbacks.selectProFeedbackByRecordId(111L);
        consumes.deleteWmItemConsumeByRecordId(22222L);
        lines.deleteWmItemConsumeLineByLineId(222222L);
        verify(feedbackMapper).updateProFeedback(f);verify(feedbackMapper).selectProFeedbackByRecordId(111L);
        verify(consumeMapper).deleteWmItemConsumeByRecordId(22222L);verify(consumeLineMapper).deleteWmItemConsumeLineByLineId(222222L);
    }

    @Test void oldApprovalStillReachesOriginalControllerAndCompletesUnboundFeedback() {
        ProFeedback old = new ProFeedback();
        old.setRecordId(222L); old.setWorkorderId(2L); old.setTaskId(22L);
        old.setQuantityFeedback(BigDecimal.ONE);
        old.setQuantityQualified(BigDecimal.ONE);
        old.setQuantityUnquanlified(BigDecimal.ZERO);
        when(feedbackMapper.selectProFeedbackByRecordId(222L)).thenReturn(old);
        IProTaskService tasks = mock(IProTaskService.class);
        ProTask task = new ProTask(); task.setTaskId(22L); task.setWorkorderId(2L);
        when(tasks.selectProTaskByTaskId(22L)).thenReturn(task);
        IProWorkorderService orders = mock(IProWorkorderService.class);
        when(orders.selectProWorkorderByWorkorderId(2L)).thenReturn(new ProWorkorder());
        IProRouteProcessService route = mock(IProRouteProcessService.class);
        IWmItemConsumeService consumption = mock(IWmItemConsumeService.class);
        ProFeedbackController controller = new ProFeedbackController();
        ReflectionTestUtils.setField(controller,"proFeedbackService",feedbacks);
        ReflectionTestUtils.setField(controller,"proTaskService",tasks);
        ReflectionTestUtils.setField(controller,"proWorkorderService",orders);
        ReflectionTestUtils.setField(controller,"proRouteProcessService",route);
        ReflectionTestUtils.setField(controller,"wmItemConsumeService",consumption);
        assertEquals(200,proxy(controller).execute(222L).get("code"));
        assertEquals("FINISHED",old.getStatus());
        assertEquals(BigDecimal.ONE,task.getQuantityProduced());
        verify(tasks).updateProTask(task);
        verify(consumption).generateItemConsume(old);
        verify(feedbackMapper).updateProFeedback(old);
    }

    private void refused(Runnable action) {
        ExecutionException error=assertThrows(ExecutionException.class,action::run);
        assertEquals("LEGACY_WRITE_PROTECTED",error.getErrorCode());
    }
    private <T> T proxy(T target) {
        AspectJProxyFactory factory=new AspectJProxyFactory(target);factory.setProxyTargetClass(true);factory.addAspect(guard);return factory.getProxy();
    }
}
