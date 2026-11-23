package com.ktg.mes.pro.execution;

import com.ktg.mes.wm.domain.WmTransaction;
import com.ktg.mes.wm.service.IWmTransactionService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.Collections;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL", matches=".+")
class ProExecutionReportMySqlTest extends ExecutionConsumptionFixture {
    @Test void partialAndFinalReportConsumesRealBatchesOnceAndKeepsYieldZero() { assertPartialThenFinalReport(); }

    @Test void reportRequiresStartedTaskAndAssignedSn() {
        db.update("update pro_task set status='READY' where task_id=?",task);
        db.update("update pro_execution_task set started_by=null,actual_started_at=null where task_id=?",task);
        assertEquals(409,reports.report(task,request(0,2,group(b06,0,2))).get("code"));
        db.update("update pro_task set status='STARTED' where task_id=?",task);
        db.update("update pro_execution_task set started_by=77,actual_started_at=current_timestamp(3) where task_id=?",task);
        ExecutionInputGroup invalid=group(b06,0,1);
        invalid.snIds=Collections.singletonList(Long.MAX_VALUE);
        ExecutionReportRequest request=request(0,1,invalid);
        request.snIds=invalid.snIds;
        assertEquals(404,reports.report(task,request).get("code"));
        assertUnchanged();
    }

    @Test void failureAfterFirstRealStockDeductionRollsBackBothLedgerAndFeedback() {
        AtomicBoolean firstDebited=new AtomicBoolean();
        IWmTransactionService faulty=mock(IWmTransactionService.class);
        when(faulty.processTransaction(any(WmTransaction.class))).thenAnswer(call->{
            WmTransaction transaction=call.getArgument(0);
            if (Long.valueOf(b07).equals(transaction.getMaterialStockId())) {
                assertTrue(firstDebited.get());
                throw new IllegalStateException("injected failure after first physical ledger write");
            }
            WmTransaction committedInTransaction=ledger.processTransaction(transaction);
            firstDebited.set(true);
            return committedInTransaction;
        });
        assertEquals(202,newReportService(faulty).report(task,request(0,5,group(b06,0,2),group(b07,2,5))).get("code"));
        assertTrue(firstDebited.get());
        assertUnchanged();
    }

    private void assertUnchanged() {
        assertBalance(b06,"2.00"); assertBalance(b07,"5.00"); assertEquals(10,countUnits("WAIT_ASSEMBLY"));
        assertEquals(0,db.queryForObject("select count(*) from pro_feedback where task_id=?",Integer.class,task));
        assertEquals(0,db.queryForObject("select count(*) from wm_item_consume where workorder_id=?",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_input where task_id=?",Integer.class,task));
        assertEquals(0,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='ITEM_CONSUME'",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_operation where workorder_id=? and action_type='REPORT'",Integer.class,released.order));
    }
}
